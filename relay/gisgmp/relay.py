#!/usr/bin/env python3
# relay/gisgmp/relay.py
#
# Мост ГИС ГМП → ЖКХ. Демон на ВМ PODS2 (видит И корп-сеть с реестром, И интернет).
# Управление из ЖКХ (pull): релей раз в ~2 мин опрашивает конфиг; по should_run
# логинится в реестр и ИНКРЕМЕНТАЛЬНО собирает начисления.
#
# ИНКРЕМЕНТ (сервер ГИС медленный — не тянем всё каждый раз):
#   • сортируем реестр по дате актуализации DESC (новое/изменённое сверху);
#   • идём по страницам и ОСТАНАВЛИВАЕМСЯ, как только дата актуализации стала
#     старше курсора `since` (это уже в кэше ЖКХ) — шлём только новое/изменённое;
#   • первый прогон (since пустой) — полный проход в пределах окна months_back;
#   • ретраи страниц при 5xx/таймаутах, чтобы один сбой не рушил весь сбор.
# Долг копится на стороне ЖКХ (кэш по УИН), здесь только сбор+отправка.

import html
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests

REGISTRY = os.environ.get("REGISTRY_URL", "https://gisgmp.cgu.mchs.ru").rstrip("/")
PASSPORT = os.environ.get("PASSPORT_URL", "https://passport.cgu.mchs.ru").rstrip("/")
JKH_URL = os.environ.get("JKH_URL", "https://asy-tk.ru").rstrip("/")
JKH_TOKEN = os.environ.get("GISGMP_SYNC_TOKEN", "")
USERNAME = os.environ.get("PASSPORT_USERNAME", "")
PASSWORD = os.environ.get("PASSPORT_PASSWORD", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "500"))      # потолок (первый полный проход)
PAGE_SLEEP = float(os.environ.get("PAGE_SLEEP", "0.4"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
PAGE_RETRIES = int(os.environ.get("PAGE_RETRIES", "3"))  # ретраи страницы при сбое
UA = "Mozilla/5.0 (gisgmp-relay)"

# Версия релея — отправляется при опросе конфига, ЖКХ показывает её в статусе и
# сравнивает с актуальной (из задеплоенного relay.py) → видно «обновлён или нет».
# БАМПАТЬ при изменении relay.py (формат YYYY-MM-DD[.N]).
RELAY_VERSION = "2026-06-05.1"

# Пауза между запросами актуализации (бережём тормозной сервер реестра).
ACTUALIZE_SLEEP = float(os.environ.get("ACTUALIZE_SLEEP", "1.2"))


def log(*a):
    print(*a, flush=True)


def _auth():
    return {"Authorization": f"Bearer {JKH_TOKEN}"}


def parse_reg_dt(s):
    s = (s or "").strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _txt(c):
    d = re.search(r'<div class="no-print">(.*?)</div>', c, re.S)
    raw = d.group(1) if d else c
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw)).strip())


def parse_page(page_html):
    m = re.search(r"<tbody>(.*?)</tbody>", page_html, re.S)
    body = m.group(1) if m else page_html
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 15:
            continue
        uin = _txt(cells[0])
        if not re.match(r"^\d{15,25}$", uin):
            continue
        u = re.search(r"/charge/([0-9a-fA-F-]{36})", row)
        out.append({
            "uin": uin,
            "amount": re.sub(r"[\s ]", "", _txt(cells[1])).replace(",", "."),
            "bill_date": _txt(cells[2]), "actualize_date": _txt(cells[3]),
            "account": _txt(cells[8]), "payer_name": _txt(cells[9]),
            "purpose": _txt(cells[10]), "ack_status": _txt(cells[11]),
            "change_status": _txt(cells[12]), "source": _txt(cells[13]),
            "charge_uuid": u.group(1) if u else None,
        })
    return out


def login(s):
    r = s.get(f"{REGISTRY}/charge/", headers={"User-Agent": UA}, timeout=30)
    if 'href="/logout"' in r.text:
        return
    ch = re.search(r"challenge=([a-f0-9]+)", r.text)
    csrf = re.search(r'name="_csrf_token"\s+value="([^"]+)"', r.text)
    if not (ch and csrf):
        raise RuntimeError("не нашёл форму входа (challenge/_csrf_token)")
    s.post(f"{PASSPORT}/oauth/login?challenge={ch.group(1)}",
           data={"username": USERNAME, "password": PASSWORD, "_csrf_token": csrf.group(1)},
           headers={"User-Agent": UA}, timeout=30)
    if 'href="/logout"' not in s.get(f"{REGISTRY}/charge/", headers={"User-Agent": UA}, timeout=30).text:
        raise RuntimeError("вход не удался — проверь логин/пароль")


def _fetch_page(s, params):
    """GET страницы с ретраями (сервер ГИС флапает 5xx/таймауты)."""
    last = ""
    for attempt in range(PAGE_RETRIES):
        try:
            r = s.get(f"{REGISTRY}/charge/", params=params,
                      headers={"User-Agent": UA}, timeout=60)
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            return r
        except Exception as e:
            last = str(e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"страница не отдалась после {PAGE_RETRIES} попыток: {last}")


def scrape(s, months_back, since):
    """Инкремент: сорт по дате актуализации DESC, стоп когда дошли до курсора."""
    since_dt = parse_reg_dt(since) if since else None
    d_from = (date.today() - timedelta(days=months_back * 31)).strftime("%d.%m.%Y")
    charges = []
    for page in range(1, MAX_PAGES + 1):
        params = {"page": page, "filtration[billDate_from]": d_from,
                  "sort": "c.actualizeDate", "direction": "desc"}
        r = _fetch_page(s, params)
        if 'href="/logout"' not in r.text:
            raise RuntimeError("сессия отвалилась во время чтения")
        rows = parse_page(r.text)
        if not rows:
            break
        hit_old = False
        for ch in rows:
            if since_dt is not None:
                adt = parse_reg_dt(ch.get("actualize_date"))
                if adt is not None and adt < since_dt:
                    hit_old = True
                    break
            charges.append(ch)
        if hit_old:
            break          # дошли до уже известного (по дате актуализации) — стоп
        time.sleep(PAGE_SLEEP)
    return charges


def push(charges):
    # asy-tk.ru флапает (502) — ретраим большой POST, чтобы не потерять сбор.
    last = ""
    for attempt in range(4):
        try:
            r = requests.post(f"{JKH_URL}/api/financier/gisgmp/sync",
                              headers=_auth(), json={"charges": charges}, timeout=300)
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"отправка в ЖКХ не удалась после ретраев: {last}")


def get_config():
    # v/poll — релей сообщает свою версию и интервал опроса (для индикатора в UI).
    r = requests.get(f"{JKH_URL}/api/financier/gisgmp/relay-config",
                     headers=_auth(),
                     params={"v": RELAY_VERSION, "poll": POLL_SECONDS},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def report(ok, count=0, updated=0, created=0, not_found=0, message=""):
    try:
        requests.post(f"{JKH_URL}/api/financier/gisgmp/relay-report",
                      headers=_auth(),
                      json={"ok": ok, "count": count, "updated": updated,
                            "created": created, "not_found": not_found,
                            "message": (message or "")[:500]},
                      timeout=30)
    except Exception as e:
        log("[relay] отчёт не ушёл:", e)


def run_once(months_back, since):
    s = requests.Session()
    log("[relay] логинимся…")
    login(s)
    log(f"[relay] читаем (окно {months_back} мес, since={since or 'нет — полный проход'})…")
    charges = scrape(s, months_back, since)
    log(f"[relay] собрано новых/изменённых: {len(charges)}")
    if not charges:
        report(True, 0, message="нет изменений (инкремент пуст)")
        return
    res = push(charges)
    log(f"[relay] ЖКХ: {res}")
    report(True, len(charges),
           updated=res.get("matched", 0), not_found=res.get("not_found", 0),
           message=f"получено {res.get('received', len(charges))}, "
                   f"в кэше {res.get('cache_total', '?')}, жильцов {res.get('residents', '?')}")


def do_recheck(surnames, deep_months):
    """Точечный добор: по каждой фамилии тянем её начисления за deep_months мес
    (фильтр payerName) и доливаем в кэш. Сошедшихся не трогаем — экономим сервер."""
    s = requests.Session()
    login(s)
    d_from = (date.today() - timedelta(days=deep_months * 31)).strftime("%d.%m.%Y")
    collected = []
    for sn in surnames:
        for page in range(1, 31):
            params = {"page": page, "filtration[payerName]": sn,
                      "filtration[billDate_from]": d_from}
            r = _fetch_page(s, params)
            if 'href="/logout"' not in r.text:
                raise RuntimeError("сессия отвалилась при дотягивании")
            rows = parse_page(r.text)
            if not rows:
                break
            collected.extend(rows)
            time.sleep(PAGE_SLEEP)
    log(f"[relay] дотянуто {len(collected)} строк по {len(surnames)} фамилиям")
    if collected:
        res = push(collected)
        log(f"[relay] ЖКХ: {res}")
        report(True, len(collected),
               message=f"дотягивание: {len(surnames)} фам., {len(collected)} строк, кэш {res.get('cache_total', '?')}")
    else:
        report(True, 0, message="дотягивание: ничего не найдено")


def actualize_progress(done, ok, fail, finished=False, message=""):
    """Прогресс массовой актуализации → ЖКХ (индикатор в UI)."""
    try:
        requests.post(f"{JKH_URL}/api/financier/gisgmp/actualize-progress",
                      headers=_auth(),
                      json={"done": done, "ok": ok, "fail": fail,
                            "finished": finished, "message": (message or "")[:300]},
                      timeout=30)
    except Exception as e:
        log("[relay] прогресс актуализации не ушёл:", e)


def do_actualize(uuids):
    """Массовая актуализация: по каждому UUID дёргаем actualize-request в реестре
    (как кнопка «Актуализировать из ГИС ГМП»), ждём отклик, шлём прогресс каждые
    10 счетов. Долго (сервер тормозной) — фоновая задача."""
    s = requests.Session()
    login(s)
    total, ok, fail = len(uuids), 0, 0
    hdr = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"}
    log(f"[relay] актуализация {total} счетов…")
    for i, u in enumerate(uuids, 1):
        try:
            r = s.get(f"{REGISTRY}/api/charge/{u}/actualize-request",
                      headers=hdr, timeout=60)
            # «ok» ТОЛЬКО если реестр реально принял запрос: тело вида
            # {"result":"Запрос на актуализацию начисления отправлен"}. Голый
            # HTTP 200 бывает страницей/ошибкой при <400 — это НЕ успех.
            accepted = False
            if r.status_code < 400:
                try:
                    accepted = "отправл" in str((r.json() or {}).get("result", "")).lower()
                except Exception:
                    accepted = '"result"' in (r.text or "")
            if accepted:
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
        time.sleep(ACTUALIZE_SLEEP)
        if i % 10 == 0 or i == total:
            actualize_progress(i, ok, fail, finished=(i == total),
                               message=f"актуализировано {i}/{total} (ok {ok}, ошибок {fail})")
    log(f"[relay] актуализация завершена: ok {ok}, ошибок {fail} из {total}")


def revoke_progress(done, ok, fail, finished=False, message=""):
    """Прогресс массового аннулирования → ЖКХ (индикатор в UI)."""
    try:
        requests.post(f"{JKH_URL}/api/financier/gisgmp/annul-progress",
                      headers=_auth(),
                      json={"done": done, "ok": ok, "fail": fail,
                            "finished": finished, "message": (message or "")[:300]},
                      timeout=30)
    except Exception as e:
        log("[relay] прогресс аннулирования не ушёл:", e)


def do_revoke(uuids):
    """Массовое аннулирование: по каждому UUID дёргаем revoke-request в реестре
    (как кнопка «Аннулировать начисление»). ОБРАТИМО — un-revoke-request возвращает.
    «ok» только если реестр принял ({"result":"…отправлен"})."""
    s = requests.Session()
    login(s)
    total, ok, fail = len(uuids), 0, 0
    hdr = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"}
    log(f"[relay] аннулирование {total} счетов…")
    for i, u in enumerate(uuids, 1):
        try:
            r = s.get(f"{REGISTRY}/api/charge/{u}/revoke-request",
                      headers=hdr, timeout=60)
            accepted = False
            if r.status_code < 400:
                try:
                    accepted = "отправл" in str((r.json() or {}).get("result", "")).lower()
                except Exception:
                    accepted = '"result"' in (r.text or "")
            if accepted:
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
        time.sleep(ACTUALIZE_SLEEP)
        if i % 10 == 0 or i == total:
            revoke_progress(i, ok, fail, finished=(i == total),
                            message=f"аннулировано {i}/{total} (ok {ok}, ошибок {fail})")
    log(f"[relay] аннулирование завершено: ok {ok}, ошибок {fail} из {total}")


def _reexec():
    """Перезапуск процесса на месте (execv) — мгновенно, без 30с паузы systemd.
    Подхватывает свежий relay.py и обновлённое окружение (PASSPORT_*)."""
    log("[relay] перезапуск (применяю изменения)…")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _set_env_line(lines, key, val):
    pref = key + "="
    for i, ln in enumerate(lines):
        if ln.startswith(pref):
            lines[i] = pref + val
            return lines
    lines.append(pref + val)
    return lines


def _write_env_creds(username, password):
    """Обновляет PASSPORT_USERNAME/PASSWORD в relay.env (остальные строки целы)
    и в текущем окружении — чтобы после execv новые значения подхватились."""
    path = os.environ.get("RELAY_ENV_FILE", "/opt/gisgmp-relay/relay.env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    lines = _set_env_line(lines, "PASSPORT_USERNAME", username)
    lines = _set_env_line(lines, "PASSPORT_PASSWORD", password)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.environ["PASSPORT_USERNAME"] = username
    os.environ["PASSPORT_PASSWORD"] = password


def _install_new_relay():
    """Качает свежий relay.py из ЖКХ, ВАЛИДИРУЕТ синтаксис, ставит на место.
    True — если установлен. Битый код не ставим (остаёмся на рабочем)."""
    r = requests.get(f"{JKH_URL}/api/financier/gisgmp/relay.py", headers=_auth(), timeout=60)
    r.raise_for_status()
    src = r.text
    compile(src, "relay.py", "exec")  # SyntaxError → исключение, файл не трогаем
    path = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else os.path.abspath(__file__)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return True


def handle_control(ctrl):
    """Команды из ЖКХ: смена учётки passport / самообновление / перезапуск.
    Применяет накопленное и перезапускается одним execv (если было что делать)."""
    need = False
    creds = ctrl.get("credentials")
    if creds and creds.get("username") and creds.get("password"):
        try:
            _write_env_creds(creds["username"], creds["password"])
            log("[relay] учётка passport обновлена из ЖКХ")
            report(True, message="учётка passport обновлена")
            need = True
        except Exception as e:
            log("[relay] не смог записать учётку:", e)
            report(False, message="смена учётки: " + str(e)[:200])
    if ctrl.get("self_update"):
        try:
            if _install_new_relay():
                log("[relay] установлен свежий relay.py")
                report(True, message="релей обновлён из ЖКХ")
                need = True
        except Exception as e:
            log("[relay] самообновление не удалось:", e)
            report(False, message="самообновление: " + str(e)[:200])
    if ctrl.get("restart"):
        need = True
    if need:
        _reexec()


def main():
    miss = [k for k in ("GISGMP_SYNC_TOKEN", "PASSPORT_USERNAME", "PASSPORT_PASSWORD")
            if not os.environ.get(k)]
    if miss:
        log(f"[relay] не заданы переменные: {', '.join(miss)} (см. relay.env)")
        return 2

    log(f"[relay] демон запущен, опрос каждые {POLL_SECONDS}с")
    while True:
        try:
            cfg = get_config()
            annul = cfg.get("annul")
            actualize = cfg.get("actualize")
            recheck = cfg.get("recheck")
            if annul and annul.get("uuids"):
                try:
                    do_revoke(annul["uuids"])
                except Exception as e:
                    log("[relay] ошибка аннулирования:", e)
                    revoke_progress(0, 0, 0, finished=True, message="ошибка: " + str(e)[:200])
            elif actualize and actualize.get("uuids"):
                try:
                    do_actualize(actualize["uuids"])
                except Exception as e:
                    log("[relay] ошибка актуализации:", e)
                    actualize_progress(0, 0, 0, finished=True, message="ошибка: " + str(e)[:200])
            elif recheck and recheck.get("surnames"):
                log(f"[relay] дотягивание {len(recheck['surnames'])} фамилий…")
                try:
                    do_recheck(recheck["surnames"], int(recheck.get("deep_months", 36)))
                except Exception as e:
                    log("[relay] ошибка дотягивания:", e)
                    report(False, message="дотягивание: " + str(e)[:300])
            elif cfg.get("should_run"):
                mb = int(cfg.get("months_back", 36))
                since = cfg.get("since")
                log(f"[relay] запуск (reason={cfg.get('reason')})")
                try:
                    run_once(mb, since)
                except Exception as e:
                    log("[relay] ошибка прогона:", e)
                    report(False, message=str(e)[:400])
            # Команды управления из ЖКХ (обновление/рестарт/смена учётки).
            # ПОСЛЕ прогона — чтобы плановый запуск этого цикла не потерялся.
            ctrl = cfg.get("control")
            if ctrl:
                handle_control(ctrl)   # применит и перезапустится (execv, не вернётся)
        except Exception as e:
            log("[relay] ошибка опроса конфига:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main() or 0)
