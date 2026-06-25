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
RELAY_VERSION = "2026-06-25.10"

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


def _onec_click(pg, text, timeout=12000, exact=True):
    """Клик по видимому элементу с текстом (ждём появления). True/False."""
    try:
        loc = pg.get_by_text(text, exact=exact).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.click()
        return True
    except Exception:
        return False


def _onec_wait_desktop(pg, timeout=120000):
    """Ждём готовности рабочего стола 1С (плитки разделов #themesCell_theme_N),
    а не фикс. паузу — за сплэшем веб-клиент догружается 10-40с."""
    pg.locator("[id^='themesCell_theme_']").first.wait_for(state="visible", timeout=timeout)


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


def _onec_open_report(pg, report_name, shot=None):
    """Навигация: раздел «Учет и отчетность» → «Стандартные отчеты» → отчёт.
    Разделы рисуются плитками .themeBox (#themesCell_theme_N). Скрин после
    каждого шага (shot) — чтобы видеть, где застряло."""
    def _s(n):
        if shot:
            shot(n)
    # 1. Раздел «Учет и отчетность» (плитка раздела)
    sect = pg.locator(".themeBox", has_text="Учет и отчетность").first
    if not sect.count():
        sect = pg.get_by_text("Учет и отчетность", exact=True).first
    sect.wait_for(state="visible", timeout=30000)
    sect.click()
    pg.wait_for_timeout(3000)
    _s("nav1_section")
    # 2. «Стандартные отчеты» (навигация раздела)
    if not _onec_click(pg, "Стандартные отчеты"):
        _onec_click(pg, "Стандартные отчёты")
    pg.wait_for_timeout(3000)
    _s("nav2_stdreports")
    # 3. Сам отчёт «Оборотно-сальдовая ведомость по счету»
    if not _onec_click(pg, "Оборотно-сальдовая ведомость по счету"):
        _onec_click(pg, report_name, exact=False)
    pg.wait_for_timeout(3500)
    _s("nav3_report")


def _onec_date_digits(d):
    """'01.01.2026' (ДД.ММ.ГГГГ от ЖКХ) → 'MMDDYYYY' цифрами — маска поля дат
    этого 1С (показывает 'M/D/YYYY'). Печатаем цифры, маска сама расставит '/'."""
    try:
        dd, mm, yy = d.split(".")
        return f"{mm}{dd}{yy}"
    except Exception:
        return d


def _onec_set(pg, suffix, value):
    """Заполнить поле 1С. suffix — окончание id без префикса формы (напр. '_Счет',
    '_НачалоПериода'). 1С прячет настоящий <input class=editInput> до активации
    ячейки → кликаем по ячейке (.field-предок), затем печатаем с клавиатуры."""
    if not value:
        return
    inp = pg.locator(f"[id$='{suffix}_i0']").first
    cell = inp.locator("xpath=ancestor-or-self::*[contains(@class,'field')][1]")
    target = cell if cell.count() else inp
    target.scroll_into_view_if_needed(timeout=8000)
    target.click(timeout=15000)          # активировать ячейку → input станет видим
    pg.wait_for_timeout(450)
    # ВСЕ действия привязаны к КОНКРЕТНОМУ input (а не глоб. фокус — иначе печать
    # утекает в предыдущее поле). press_sequentially уважает маску даты.
    inp.click(timeout=8000)
    inp.press("Control+a")
    inp.press("Delete")
    inp.press_sequentially(str(value), delay=30)
    inp.press("Tab")
    pg.wait_for_timeout(900)


def _onec_set_account(pg, code):
    """Счёт — это КОМБОБОКС: клик по ячейке открывает выпадающий список (он
    перекрывает input, поэтому inp.click не годится). Активируем ячейку, печатаем
    код в фокусированный combobox-input, выбираем точное совпадение из списка."""
    if not code:
        return
    inp = pg.locator("[id$='_Счет_i0']").first
    cell = inp.locator("xpath=ancestor-or-self::*[contains(@class,'field')][1]")
    (cell if cell.count() else inp).click(timeout=15000)
    pg.wait_for_timeout(600)
    pg.keyboard.press("Control+a")
    pg.keyboard.press("Delete")
    pg.keyboard.type(str(code), delay=45)   # combobox в фокусе (Счёт ставим последним)
    pg.wait_for_timeout(1300)               # дать выпасть списку
    opt = pg.get_by_text(str(code), exact=True).last
    try:
        opt.click(timeout=5000)
    except Exception:
        pg.keyboard.press("Enter")
    pg.wait_for_timeout(1000)


def _onec_save_xlsx(pg, at, shot=None):
    """Экспорт результата ОСВ в xlsx. Кнопка «Сохранить» табличного документа
    (VW_pageN…_cmd_SaveButton) открывает диалог: Имя файла + Тип файла (по умолч.
    *.mxl) + кнопка OK (Ctrl+Enter). Меняем Тип на Excel и жмём OK → download."""
    def _s(n):
        if shot:
            shot(n)
    save_btn = pg.locator("[id^='VW_'][id$='_cmd_SaveButton']:visible").first
    if not save_btn.count():
        save_btn = pg.locator("[id$='_cmd_SaveButton']:visible").first
    save_btn.click(timeout=15000)
    pg.wait_for_timeout(2000)
    _s(f"save_dialog_{at}")
    try:
        # Сменить Тип файла на Excel: открыть выпадающий список (DLB) и выбрать.
        dlb = pg.locator("[id$='_FileType_DLB']:visible").first
        if dlb.count():
            dlb.click(timeout=8000)
            pg.wait_for_timeout(1000)
            _onec_dump(pg, f"filetype_{at}")     # список форматов (для проверки)
            # Берём ИМЕННО xlsx (Excel 2007+), а не плейн «Лист Excel» (это .xls/OLE2,
            # который openpyxl не читает). Якорь — подстрока «xlsx».
            (_onec_click(pg, "xlsx", exact=False)
             or _onec_click(pg, "2007", exact=False)
             or _onec_click(pg, "Лист Excel 2007", exact=False))
            pg.wait_for_timeout(800)
        _s(f"save_fmt_{at}")
        # OK диалога = Ctrl+Enter (так в подписи кнопки). Ловим download.
        with pg.expect_download(timeout=60000) as dl:
            pg.keyboard.press("Control+Enter")
            pg.wait_for_timeout(1500)
            ok = pg.locator("[id$='_popup_OK']:visible").first
            try:
                if ok.count():
                    ok.click(timeout=3000)
            except Exception:
                pass
        path = os.path.join(ONEC_PROBE_DIR, f"osv_{at}.xlsx")
        dl.value.save_as(path)
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        log(f"[onec] экспорт xlsx ({at}) не удался:", e)
        _s(f"save_fail_{at}")
        _onec_dump(pg, f"save_{at}")
        return None


def _onec_collect_account(pg, code, period, at, shot=None, dry=False, set_dates=True):
    """Один счёт в УЖЕ ОТКРЫТОМ отчёте ОСВ: задать период (один раз) + счёт,
    Сформировать, сохранить в xlsx → bytes. Отчёт НЕ переоткрываем (для 2-го счёта
    1С просто фокусировал бы существующую вкладку → поля недоступны)."""
    def _s(n):
        if shot:
            shot(n)
    _s(f"preform_{at}")
    # Даты ПЕРВЫМИ (один раз), Счёт — комбобокс, ставим ПОСЛЕДНИМ.
    if set_dates:
        _onec_set(pg, "_НачалоПериода", _onec_date_digits(period.get("from")))
        _s(f"d1_{at}")
        _onec_set(pg, "_КонецПериода", _onec_date_digits(period.get("to")))
        _s(f"d2_{at}")
    _onec_set_account(pg, code)
    _s(f"form_{at}")
    pg.locator("[id$='_СформироватьОтчет']:visible").first.click(timeout=15000)
    pg.wait_for_timeout(7000)
    _s(f"formed_{at}")
    data = _onec_save_xlsx(pg, at, shot=shot)
    return None if dry else data


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

        def shot(n):
            _onec_shot(pg, n, notes)

        try:
            log("[onec] открываю", url)
            pg.goto(url, wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)
            _onec_shot(pg, "01_open", notes)
            try:
                _onec_login(pg, login, password)
                _onec_wait_desktop(pg)          # ждём рабочий стол (а не фикс. паузу)
                pg.wait_for_timeout(1500)
                _onec_shot(pg, "02_after_login", notes)
            except Exception as e:
                _onec_shot(pg, "02_login_fail", notes)
                _onec_dump(pg, "login_fail")
                onec_report(False, status="error", message="логин/загрузка 1С не удалась: " + str(e)[:300])
                return

            # Отчёт открываем ОДИН раз, дальше переключаем счёт в нём же.
            _onec_open_report(pg, report_name, shot=shot)

            if probe:
                _onec_dump(pg, "desktop")
                try:
                    pa = accounts[0] if accounts else {"code": "205.31", "account_type": "205"}
                    _onec_collect_account(pg, pa.get("code"), period,
                                          pa.get("account_type"), shot=shot, dry=True)
                    _onec_shot(pg, "03_report_form", notes)
                    _onec_dump(pg, "report_form")
                except Exception as e:
                    log("[onec] разведка отчёта:", e)
                    _onec_shot(pg, "03_nav_fail", notes)
                    _onec_dump(pg, "nav_fail")
                onec_report(True, status="probe",
                            message="разведка ок, артефакты в " + ONEC_PROBE_DIR
                                    + ": " + ", ".join(notes))
                return

            files = {}
            counts = {"205": 0, "209": 0}
            for i, acc in enumerate(accounts):
                code, at = acc.get("code"), acc.get("account_type")
                try:
                    # Даты ставим только на первом счёте (потом сохраняются в форме).
                    data = _onec_collect_account(pg, code, period, at, shot=shot,
                                                 set_dates=(i == 0))
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
