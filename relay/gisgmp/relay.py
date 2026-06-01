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
    r = requests.get(f"{JKH_URL}/api/financier/gisgmp/relay-config",
                     headers=_auth(), timeout=30)
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
            if cfg.get("should_run"):
                mb = int(cfg.get("months_back", 36))
                since = cfg.get("since")
                log(f"[relay] запуск (reason={cfg.get('reason')})")
                try:
                    run_once(mb, since)
                except Exception as e:
                    log("[relay] ошибка прогона:", e)
                    report(False, message=str(e)[:400])
        except Exception as e:
            log("[relay] ошибка опроса конфига:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main() or 0)
