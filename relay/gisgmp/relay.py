#!/usr/bin/env python3
# relay/gisgmp/relay.py
#
# Мост ГИС ГМП → ЖКХ. Демон на ВМ PODS2 (единственная машина, которая видит
# И корп-сеть с реестром gisgmp.cgu.mchs.ru, И интернет с asy-tk.ru).
#
# Управление — из ЖКХ (pull-модель): релей за NAT, ЖКХ к нему внутрь не
# достучится, поэтому релей сам раз в ~2 мин опрашивает ЖКХ
# (GET /api/financier/gisgmp/relay-config). Если ЖКХ говорит should_run
# (нажали «Запустить сейчас» или истёк интервал) — релей:
#   1) логинится в реестр (OAuth2/passport: логин/пароль + _csrf_token);
#   2) читает «Начисления» за скользящее окно (последние months_back месяцев,
#      фильтр по дате начисления) постранично, парсит (15 ячеек/строка);
#   3) шлёт строки в ЖКХ POST /gisgmp/sync;
#   4) отчитывается POST /gisgmp/relay-report (статус/счётчики).
# Классификацию (наем→205 / комуслуги→209, «Не сквитировано»=долг,
# «аннулирование»→мимо) и матч ФИО→жилец делает бэкенд ЖКХ.
#
# Креды/токен — из EnvironmentFile (relay.env). Окно/интервал/вкл-выкл —
# из ЖКХ (меняются в панели, релей подхватывает на следующем опросе).

import html
import os
import re
import sys
import time
from datetime import date, timedelta

import requests

REGISTRY = os.environ.get("REGISTRY_URL", "https://gisgmp.cgu.mchs.ru").rstrip("/")
PASSPORT = os.environ.get("PASSPORT_URL", "https://passport.cgu.mchs.ru").rstrip("/")
JKH_URL = os.environ.get("JKH_URL", "https://asy-tk.ru").rstrip("/")
JKH_TOKEN = os.environ.get("GISGMP_SYNC_TOKEN", "")
USERNAME = os.environ.get("PASSPORT_USERNAME", "")
PASSWORD = os.environ.get("PASSPORT_PASSWORD", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))
PAGE_SLEEP = float(os.environ.get("PAGE_SLEEP", "0.4"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
ONLY_UNPAID = os.environ.get("ONLY_UNPAID", "0") == "1"
UNPAID_STATUS_ID = "13911fbd-daac-4fb5-b996-f04c726cd030"  # «Не сквитировано»
UA = "Mozilla/5.0 (gisgmp-relay)"


def log(*a):
    print(*a, flush=True)


def _auth():
    return {"Authorization": f"Bearer {JKH_TOKEN}"}


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
        raise RuntimeError("не нашёл форму входа (challenge/_csrf_token) — реестр изменился?")
    s.post(f"{PASSPORT}/oauth/login?challenge={ch.group(1)}",
           data={"username": USERNAME, "password": PASSWORD, "_csrf_token": csrf.group(1)},
           headers={"User-Agent": UA}, timeout=30)
    if 'href="/logout"' not in s.get(f"{REGISTRY}/charge/", headers={"User-Agent": UA}, timeout=30).text:
        raise RuntimeError("вход не удался — проверь PASSPORT_USERNAME/PASSPORT_PASSWORD")


def scrape(s, months_back):
    # Скользящее окно: только начисления с датой начисления за последние
    # months_back месяцев (фильтр режет «огромное кол-во записей» на порядок).
    d_from = (date.today() - timedelta(days=months_back * 31)).strftime("%d.%m.%Y")
    charges, hit_cap = [], True
    for page in range(1, MAX_PAGES + 1):
        params = {"page": page, "filtration[billDate_from]": d_from}
        if ONLY_UNPAID:
            params["filtration[acknowledgmentStatus]"] = UNPAID_STATUS_ID
        r = s.get(f"{REGISTRY}/charge/", params=params, headers={"User-Agent": UA}, timeout=30)
        if 'href="/logout"' not in r.text:
            raise RuntimeError("сессия отвалилась во время чтения")
        rows = parse_page(r.text)
        if not rows:
            hit_cap = False
            break
        charges.extend(rows)
        time.sleep(PAGE_SLEEP)
    if hit_cap:
        log(f"[relay] ВНИМАНИЕ: предел {MAX_PAGES} страниц — увеличь MAX_PAGES.")
    return charges


def push(charges):
    r = requests.post(f"{JKH_URL}/api/financier/gisgmp/sync",
                      headers=_auth(), json={"charges": charges}, timeout=120)
    r.raise_for_status()
    return r.json()


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
        log("[relay] не смог отправить отчёт:", e)


def run_once(months_back):
    s = requests.Session()
    log("[relay] логинимся в реестр…")
    login(s)
    log(f"[relay] читаем начисления (окно {months_back} мес)…")
    charges = scrape(s, months_back)
    log(f"[relay] собрано начислений: {len(charges)}")
    if not charges:
        report(True, 0, message="пусто (нет начислений за окно)")
        return
    res = push(charges)
    log(f"[relay] ЖКХ ответил: {res}")
    report(True, len(charges),
           updated=res.get("updated", 0), created=res.get("created", 0),
           not_found=(res.get("not_found_209", 0) + res.get("not_found_205", 0)),
           message="ok")


def main():
    miss = [k for k in ("GISGMP_SYNC_TOKEN", "PASSPORT_USERNAME", "PASSPORT_PASSWORD")
            if not os.environ.get(k)]
    if miss:
        log(f"[relay] не заданы переменные: {', '.join(miss)} (см. relay.env)")
        return 2

    log(f"[relay] демон запущен, опрос ЖКХ каждые {POLL_SECONDS}с")
    while True:
        try:
            cfg = get_config()
            if cfg.get("should_run"):
                mb = int(cfg.get("months_back", 2))
                log(f"[relay] запуск (reason={cfg.get('reason')}), окно {mb} мес")
                try:
                    run_once(mb)
                except Exception as e:
                    log("[relay] ошибка прогона:", e)
                    report(False, message=str(e)[:400])
        except Exception as e:
            log("[relay] ошибка опроса конфига:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main() or 0)
