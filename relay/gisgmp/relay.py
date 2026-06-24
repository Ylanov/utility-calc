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
# Ключ проверки подписи само-обновления (RCE-защита). ОТДЕЛЬНЫЙ от JKH_TOKEN
# (тот летит в каждом запросе → виден MITM). По сети не передаётся.
RELAY_UPDATE_SECRET = os.environ.get("RELAY_UPDATE_SECRET", "").strip()
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
RELAY_VERSION = "2026-06-24.1"

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
    # RCE-защита (#19): проверяем HMAC-подпись кода ПЕРЕД исполнением. Ключ
    # RELAY_UPDATE_SECRET по сети не ходит → MITM/подмену кода отвергаем.
    # Если ключ задан — подпись ОБЯЗАТЕЛЬНА; нет/неверна → не ставим, остаёмся
    # на рабочем коде. Если ключ не задан — старое поведение (предупреждаем).
    if RELAY_UPDATE_SECRET:
        import hmac
        import hashlib
        expected = hmac.new(
            RELAY_UPDATE_SECRET.encode(), src.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        got = (r.headers.get("X-Relay-Signature") or "").strip()
        if not got or not hmac.compare_digest(got, expected):
            log("[relay] ОТКАЗ обновления: подпись relay.py не совпала "
                "(возможна подмена) — остаёмся на текущем коде")
            return False
    else:
        log("[relay] ВНИМАНИЕ: RELAY_UPDATE_SECRET не задан — "
            "обновление БЕЗ проверки подписи")
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


# =========================================================================
# 1С (БГУ) — БРАУЗЕР-АВТОМАТИЗАЦИЯ: ОСВ по счёту → Excel → ЖКХ
# =========================================================================
# Тот же демон headless-браузером (Playwright) заходит в веб-клиент 1С:БГУ,
# формирует «Оборотно-сальдовую ведомость по счёту» за период (с начала года →
# сегодня), сохраняет в xlsx и шлёт в ЖКХ /api/financier/onec/sync — там тот же
# парсер, что и для ручной загрузки. Креды 1С приходят из ЖКХ (onec/relay-config)
# и хранятся в onec.env. Селекторы веб-клиента 1С динамические → первый прогон
# делаем в режиме probe (логин + скрины/DOM) и по ним дорабатываем селекторы.
# playwright импортируется ЛЕНИВО — если он не установлен, ГИС-ГМП-ветка цела.

ONEC_ENV_FILE = os.environ.get("ONEC_ENV_FILE", "/opt/gisgmp-relay/onec.env")
ONEC_PROBE_DIR = os.environ.get("ONEC_PROBE_DIR", "/opt/gisgmp-relay/onec_probe")


def _onec_load_creds():
    u = os.environ.get("ONEC_LOGIN", "")
    p = os.environ.get("ONEC_PASSWORD", "")
    if u and p:
        return u, p
    try:
        with open(ONEC_ENV_FILE, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                if ln.startswith("ONEC_LOGIN="):
                    u = ln.split("=", 1)[1]
                elif ln.startswith("ONEC_PASSWORD="):
                    p = ln.split("=", 1)[1]
    except FileNotFoundError:
        pass
    return u, p


def _onec_save_creds(login, password):
    # \n/\r сломали бы построчный onec.env (молчаливое обрезание пароля).
    login = (login or "").replace("\r", "").replace("\n", "")
    password = (password or "").replace("\r", "").replace("\n", "")
    try:
        lines = []
        if os.path.exists(ONEC_ENV_FILE):
            with open(ONEC_ENV_FILE, encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines = _set_env_line(lines, "ONEC_LOGIN", login)
        lines = _set_env_line(lines, "ONEC_PASSWORD", password)
        with open(ONEC_ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log("[onec] не смог записать onec.env:", e)
    os.environ["ONEC_LOGIN"] = login
    os.environ["ONEC_PASSWORD"] = password


def get_onec_config(creds_ack=None):
    params = {"v": RELAY_VERSION}
    if creds_ack is not None:
        params["creds_ack"] = creds_ack
    r = requests.get(f"{JKH_URL}/api/financier/onec/relay-config",
                     headers=_auth(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def onec_report(ok, status=None, message="", count_205=0, count_209=0):
    try:
        requests.post(f"{JKH_URL}/api/financier/onec/relay-report",
                      headers=_auth(),
                      json={"ok": ok, "status": status, "message": (message or "")[:1500],
                            "count_205": count_205, "count_209": count_209},
                      timeout=30)
    except Exception as e:
        log("[onec] отчёт не ушёл:", e)


def onec_upload(files):
    """files: dict {'file_205': (name, bytes), 'file_209': (name, bytes)}."""
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    data = {k: (v[0], v[1], xlsx) for k, v in files.items()}
    r = requests.post(f"{JKH_URL}/api/financier/onec/sync",
                      headers=_auth(), files=data, timeout=300)
    r.raise_for_status()
    return r.json()


def _onec_shot(pg, name, notes):
    try:
        pg.screenshot(path=os.path.join(ONEC_PROBE_DIR, name + ".png"), full_page=True)
        notes.append(name)
    except Exception as e:
        log("[onec] скрин не вышел:", name, e)


def _onec_dump(pg, name):
    try:
        with open(os.path.join(ONEC_PROBE_DIR, name + ".html"), "w", encoding="utf-8") as f:
            f.write(pg.content())
    except Exception as e:
        log("[onec] dump:", name, e)


def _onec_click_text(pg, texts):
    for t in texts:
        try:
            loc = pg.get_by_text(t, exact=False).first
            if loc.count() and loc.is_visible():
                loc.click()
                return True
        except Exception:
            continue
    return False


def _onec_fill_labeled(pg, labels, value):
    if not value:
        return False
    for lb in labels:
        try:
            field = pg.locator(
                f"xpath=//*[normalize-space(text())='{lb}']/following::input[1]"
            ).first
            if field.count():
                field.click()
                field.fill(str(value))
                field.press("Tab")
                return True
        except Exception:
            continue
    return False


def _onec_login(pg, login, password):
    """Форма входа веб-клиента 1С. Якорь — поле пароля (самое надёжное)."""
    pw = pg.locator("input[type=password]").first
    pw.wait_for(state="visible", timeout=60000)
    try:
        user = pg.locator(
            "input[type=text]:visible, input:not([type]):visible"
        ).first
        if user.count():
            user.fill(login)
    except Exception as e:
        log("[onec] поле логина не найдено:", e)
    pw.fill(password)
    if not _onec_click_text(pg, ["Войти", "Вход", "ОК", "OK"]):
        pw.press("Enter")


def _onec_open_report(pg, report_name):
    """Навигация: раздел → Стандартные отчёты → нужный отчёт. Названия раздела —
    кандидаты (на ВМ уточним по probe-скринам)."""
    _onec_click_text(pg, ["Учет и отчетность", "Учёт и отчётность",
                          "Бухгалтерский учет", "Бухгалтерский учёт"])
    pg.wait_for_timeout(1500)
    _onec_click_text(pg, ["Стандартные отчеты", "Стандартные отчёты"])
    pg.wait_for_timeout(1500)
    _onec_click_text(pg, [report_name, "Оборотно-сальдовая ведомость по счету",
                          "Оборотно-сальдовая ведомость по счёту"])
    pg.wait_for_timeout(2500)


def _onec_collect_account(pg, report_name, code, period, notes, at):
    """ОСВ по счёту: задать счёт+период, Сформировать, сохранить в xlsx → bytes."""
    _onec_open_report(pg, report_name)
    _onec_fill_labeled(pg, ["Счет", "Счёт"], code)
    _onec_fill_labeled(pg, ["с", "Период с", "Начало периода"], period.get("from"))
    _onec_fill_labeled(pg, ["по", "Период по", "Конец периода"], period.get("to"))
    _onec_shot(pg, f"form_{at}", notes)
    _onec_click_text(pg, ["Сформировать"])
    pg.wait_for_timeout(5000)
    _onec_shot(pg, f"formed_{at}", notes)
    # Сохранить табличный документ в xlsx (ловим браузерный download).
    try:
        with pg.expect_download(timeout=60000) as dl:
            if not _onec_click_text(pg, ["Сохранить как", "Сохранить"]):
                _onec_click_text(pg, ["Ещё", "Еще"])
                pg.wait_for_timeout(800)
                _onec_click_text(pg, ["Сохранить как…", "Сохранить как", "Сохранить"])
            pg.wait_for_timeout(1200)
            _onec_click_text(pg, ["Лист Excel 2007", "Лист Excel", "Excel (xlsx)", "xlsx"])
            _onec_click_text(pg, ["Сохранить", "ОК", "OK"])
        path = os.path.join(ONEC_PROBE_DIR, f"osv_{at}.xlsx")
        dl.value.save_as(path)
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        log(f"[onec] сохранение xlsx ({at}) не удалось:", e)
        _onec_dump(pg, f"save_{at}")
        return None


def run_onec(oc):
    """Прогон 1С. oc — ответ get_onec_config(). probe=True → только разведка."""
    from playwright.sync_api import sync_playwright  # ленивый импорт

    login, password = _onec_load_creds()
    if not login or not password:
        onec_report(False, status="error", message="нет логина/пароля 1С (не отдан из ЖКХ?)")
        return
    base = (oc.get("base_url") or "").rstrip("/")
    ib = (oc.get("infobase_path") or "").strip().strip("/")
    url = base + (("/" + ib) if ib else "")
    probe = bool(oc.get("probe"))
    headless = bool(oc.get("headless", True))
    period = oc.get("period") or {}
    accounts = oc.get("accounts") or []
    report_name = oc.get("report_name") or "Оборотно-сальдовая ведомость по счёту"
    os.makedirs(ONEC_PROBE_DIR, exist_ok=True)
    notes = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--no-sandbox"])
        ctx = browser.new_context(accept_downloads=True, ignore_https_errors=True,
                                  viewport={"width": 1600, "height": 900})
        pg = ctx.new_page()
        pg.set_default_timeout(60000)
        try:
            log("[onec] открываю", url)
            pg.goto(url, wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)
            _onec_shot(pg, "01_open", notes)
            try:
                _onec_login(pg, login, password)
                pg.wait_for_timeout(5000)
                _onec_shot(pg, "02_after_login", notes)
            except Exception as e:
                _onec_shot(pg, "02_login_fail", notes)
                _onec_dump(pg, "login_fail")
                onec_report(False, status="error", message="логин 1С не удался: " + str(e)[:300])
                return

            if probe:
                _onec_dump(pg, "desktop")
                try:
                    _onec_open_report(pg, report_name)
                    _onec_shot(pg, "03_report_form", notes)
                    _onec_dump(pg, "report_form")
                except Exception as e:
                    log("[onec] разведка отчёта:", e)
                onec_report(True, status="probe",
                            message="разведка ок, артефакты в " + ONEC_PROBE_DIR
                                    + ": " + ", ".join(notes))
                return

            files = {}
            counts = {"205": 0, "209": 0}
            for acc in accounts:
                code, at = acc.get("code"), acc.get("account_type")
                try:
                    data = _onec_collect_account(pg, report_name, code, period, notes, at)
                    if data:
                        files[f"file_{at}"] = (f"osv_{at}.xlsx", data)
                        counts[at] = 1
                except Exception as e:
                    _onec_shot(pg, f"err_{at}", notes)
                    _onec_dump(pg, f"collect_{at}")
                    log(f"[onec] счёт {code}/{at} не собрался:", e)
        finally:
            browser.close()

    if not files:
        onec_report(False, status="error", message="ни один счёт не выгрузился в Excel")
        return
    res = onec_upload(files)
    onec_report(True, status="ok",
                message=f"ОСВ выгружены {list(files.keys())}; staged batch={res.get('batch_id')}",
                count_205=counts["205"], count_209=counts["209"])


_ONEC_HAVE_VERSION = None  # версия учётки, которую релей уже сохранил (для ack)


def onec_tick():
    """Один цикл 1С: опрос конфига, приём учётки (ack-доставка), запуск по should_run."""
    global _ONEC_HAVE_VERSION
    oc = get_onec_config(creds_ack=_ONEC_HAVE_VERSION)
    creds = oc.get("credentials")
    if creds and creds.get("login") and creds.get("password"):
        _onec_save_creds(creds["login"], creds["password"])
        # Запоминаем версию — на следующем опросе подтвердим (ack), ЖКХ погасит pending.
        _ONEC_HAVE_VERSION = creds.get("version", oc.get("creds_version"))
        log("[onec] учётка 1С получена из ЖКХ (v=%s)" % _ONEC_HAVE_VERSION)
    if oc.get("should_run"):
        log(f"[onec] запуск 1С (reason={oc.get('reason')}, probe={oc.get('probe')})")
        try:
            run_onec(oc)
        except Exception as e:
            log("[onec] ошибка прогона 1С:", e)
            onec_report(False, status="error", message=str(e)[:400])


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
            # 1С (БГУ) — отдельный конфиг/ветка (та же ВМ, headless-браузер).
            try:
                onec_tick()
            except Exception as e:
                log("[onec] ошибка опроса конфига 1С:", e)
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
