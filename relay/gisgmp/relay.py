#!/usr/bin/env python3
# relay/gisgmp/relay.py
#
# Мост ГИС ГМП → ЖКХ (серверный релей, запускается на ВМ PODS2 по таймеру).
#
# ВМ PODS2 — единственная машина, которая видит И корп-сеть (реестр
# gisgmp.cgu.mchs.ru), И интернет (asy-tk.ru). Браузера на ней нет, поэтому
# логинимся в реестр сами: реестр за OAuth2 (ORY Hydra), вход — обычная
# форма passport.cgu.mchs.ru (логин/пароль + _csrf_token), без ЭЦП.
#
# Что делает:
#   1) логинится в реестр (GET /charge/ → цепочка hydra → passport → POST формы);
#   2) постранично читает «Начисления», парсит строки (15 ячеек/строка);
#   3) шлёт все строки в ЖКХ POST /api/financier/gisgmp/sync (Bearer-токен).
# Классификацию (наем→205 / комуслуги→209, «Не сквитировано»=долг,
# «аннулирование»→мимо) и матч ФИО→жилец делает бэкенд ЖКХ.
#
# Конфиг — из EnvironmentFile (relay.env): креды passport, токен ЖКХ, лимиты.
# Сеть: ВМ должна иметь маршрут к корп-сети (10.0.0.0/8 via 10.23.0.1) и
# /etc/hosts с IP gisgmp/passport/hydra (systemd-юнит ставит маршрут сам).

import html
import os
import re
import sys
import time

import requests

REGISTRY = os.environ.get("REGISTRY_URL", "https://gisgmp.cgu.mchs.ru").rstrip("/")
PASSPORT = os.environ.get("PASSPORT_URL", "https://passport.cgu.mchs.ru").rstrip("/")
JKH_URL = os.environ.get("JKH_URL", "https://asy-tk.ru").rstrip("/")
JKH_TOKEN = os.environ.get("GISGMP_SYNC_TOKEN", "")
USERNAME = os.environ.get("PASSPORT_USERNAME", "")
PASSWORD = os.environ.get("PASSPORT_PASSWORD", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))
PAGE_SLEEP = float(os.environ.get("PAGE_SLEEP", "0.4"))
# ONLY_UNPAID=1 — фильтровать реестр по «Не сквитировано» (быстрее, только
# долги). По умолчанию 0 — читаем все, бэкенд обнулит оплативших.
ONLY_UNPAID = os.environ.get("ONLY_UNPAID", "0") == "1"
UNPAID_STATUS_ID = "13911fbd-daac-4fb5-b996-f04c726cd030"  # «Не сквитировано»

UA = "Mozilla/5.0 (gisgmp-relay)"


def log(*a):
    print(*a, flush=True)


def _txt(cell_html: str) -> str:
    """Видимый текст ячейки: приоритет — содержимое <div class="no-print">."""
    d = re.search(r'<div class="no-print">(.*?)</div>', cell_html, re.S)
    raw = d.group(1) if d else cell_html
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw)).strip())


def parse_page(page_html: str) -> list[dict]:
    """Парсит строки начислений (15 ячеек в фиксированном порядке)."""
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
            "bill_date": _txt(cells[2]),
            "actualize_date": _txt(cells[3]),
            "account": _txt(cells[8]),        # лицевой счёт (квартира)
            "payer_name": _txt(cells[9]),     # ФИО
            "purpose": _txt(cells[10]),       # назначение → 209/205
            "ack_status": _txt(cells[11]),    # квитирование
            "change_status": _txt(cells[12]), # эталонное/аннулирование/…
            "source": _txt(cells[13]),
            "charge_uuid": u.group(1) if u else None,
        })
    return out


def login(s: requests.Session) -> None:
    """Логин в реестр через passport (OAuth2/Hydra). Бросает SystemExit при сбое."""
    r = s.get(f"{REGISTRY}/charge/", headers={"User-Agent": UA}, timeout=30)
    if 'href="/logout"' in r.text:
        return  # уже залогинены
    ch = re.search(r"challenge=([a-f0-9]+)", r.text)
    csrf = re.search(r'name="_csrf_token"\s+value="([^"]+)"', r.text)
    if not (ch and csrf):
        raise SystemExit("Не нашёл форму входа (challenge/_csrf_token) — реестр изменился?")
    s.post(
        f"{PASSPORT}/oauth/login?challenge={ch.group(1)}",
        data={"username": USERNAME, "password": PASSWORD, "_csrf_token": csrf.group(1)},
        headers={"User-Agent": UA}, timeout=30,
    )
    chk = s.get(f"{REGISTRY}/charge/", headers={"User-Agent": UA}, timeout=30)
    if 'href="/logout"' not in chk.text:
        raise SystemExit("Вход не удался — проверь PASSPORT_USERNAME/PASSPORT_PASSWORD.")


def scrape(s: requests.Session) -> list[dict]:
    """Постранично читает начисления до пустой страницы (или MAX_PAGES)."""
    charges, hit_cap = [], True
    for page in range(1, MAX_PAGES + 1):
        params = {"page": page}
        if ONLY_UNPAID:
            params["filtration[acknowledgmentStatus]"] = UNPAID_STATUS_ID
        r = s.get(f"{REGISTRY}/charge/", params=params,
                  headers={"User-Agent": UA}, timeout=30)
        if 'href="/logout"' not in r.text:
            raise SystemExit("Сессия отвалилась во время чтения.")
        rows = parse_page(r.text)
        if not rows:
            hit_cap = False
            break
        charges.extend(rows)
        time.sleep(PAGE_SLEEP)
    if hit_cap:
        log(f"[relay] ВНИМАНИЕ: достигнут предел {MAX_PAGES} страниц — "
            "возможно, собраны не все начисления (увеличь MAX_PAGES).")
    return charges


def push(charges: list[dict]) -> dict:
    r = requests.post(
        f"{JKH_URL}/api/financier/gisgmp/sync",
        headers={"Authorization": f"Bearer {JKH_TOKEN}"},
        json={"charges": charges}, timeout=120,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    missing = [k for k in ("GISGMP_SYNC_TOKEN", "PASSPORT_USERNAME", "PASSPORT_PASSWORD")
               if not os.environ.get(k)]
    if missing:
        log(f"[relay] не заданы переменные: {', '.join(missing)} (см. relay.env)")
        return 2

    s = requests.Session()
    log("[relay] логинимся в реестр…")
    login(s)
    log("[relay] читаем начисления…")
    charges = scrape(s)
    log(f"[relay] собрано начислений: {len(charges)}")
    if not charges:
        log("[relay] пусто — выходим без отправки.")
        return 0
    res = push(charges)
    log(f"[relay] отправлено в ЖКХ: {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
