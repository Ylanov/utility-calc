#!/usr/bin/env python3
# relay/gisgmp/check_payer.py
"""
ДИАГНОСТИКА ГИС ГМП по одному плательщику.

Зачем: при расхождении «в реестре одна сумма, в ЖКХ другая» этот скрипт
показывает ПО СТРОКАМ, что реестр ГИС ГМП начисляет конкретному человеку:
сумма, счёт (205 наём / 209 комуслуги), дата, статус квитирования/изменения,
идёт ли строка в долг — и итоговый долг 205/209. Так сразу видно, права ли
система или есть баг в парсинге/суммировании.

ВАЖНО:
  • Запускать ТОЛЬКО на ВМ PODS2 — единственной машине, что видит реестр
    (корп-сеть через Cisco 10.23.0.1). Из интернета реестр недоступен.
  • Креды берёт из /opt/gisgmp-relay/relay.env (те же, что у релея) → нужен
    sudo (файл chmod 600). Маршрут к корп-сети и /etc/hosts уже настроены
    релеем; если запуск ругается на сеть — см. relay/gisgmp/README.md.
  • Скрипт показывает ВСЕ месяцы (без окна по датам). Боевой релей берёт
    только последние N месяцев («Окно, мес» в панели ЖКХ) — поэтому для
    хронического должника долг в скрипте может быть БОЛЬШЕ, чем в панели.

Использование (на ВМ PODS2):
    sudo python3 /opt/gisgmp-relay/check_payer.py "Власенко"
    sudo python3 /opt/gisgmp-relay/check_payer.py "Иванов Иван"

Логика долга (как в боевом релее и бэкенде ЖКХ services/gisgmp_import.py):
    долг = «Не сквитировано» И не «аннулирование»;
    «наем/найм» → счёт 205, «комуслуги/коммунальные» → счёт 209.
"""
import html
import re
import sys
from decimal import Decimal

import requests

REG = "https://gisgmp.cgu.mchs.ru"
PAS = "https://passport.cgu.mchs.ru"
UA = "Mozilla/5.0 (gisgmp-check)"
ENV_PATH = "/opt/gisgmp-relay/relay.env"


def load_env(path=ENV_PATH):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def cell_text(cell):
    d = re.search(r'<div class="no-print">(.*?)</div>', cell, re.S)
    cell = d.group(1) if d else cell
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip())


def account_of(purpose):
    p = purpose.lower()
    if "наем" in p or "найм" in p or "наём" in p:
        return "205"
    if "комус" in p or "коммунал" in p:
        return "209"
    return " ? "


def login(s, env):
    r = s.get(f"{REG}/charge/", headers={"User-Agent": UA}, timeout=30)
    if 'href="/logout"' in r.text:
        return
    ch = re.search(r"challenge=([a-f0-9]+)", r.text)
    cs = re.search(r'name="_csrf_token"\s+value="([^"]+)"', r.text)
    if not (ch and cs):
        sys.exit("Не нашёл форму входа — реестр изменился?")
    s.post(f"{PAS}/oauth/login?challenge={ch.group(1)}",
           data={"username": env["PASSPORT_USERNAME"], "password": env["PASSPORT_PASSWORD"],
                 "_csrf_token": cs.group(1)},
           headers={"User-Agent": UA}, timeout=30)
    if 'href="/logout"' not in s.get(f"{REG}/charge/", headers={"User-Agent": UA}, timeout=30).text:
        sys.exit("Вход не удался — проверь PASSPORT_* в relay.env")


def main():
    fio = sys.argv[1] if len(sys.argv) > 1 else "Власенко"
    env = load_env()
    s = requests.Session()
    login(s, env)

    tot = {"205": Decimal("0"), "209": Decimal("0")}
    n = 0
    for page in range(1, 51):
        rr = s.get(f"{REG}/charge/", params={"page": page, "filtration[payerName]": fio},
                   headers={"User-Agent": UA}, timeout=30)
        m = re.search(r"<tbody>(.*?)</tbody>", rr.text, re.S)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1) if m else "", re.S)
        if not rows:
            break
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) < 15:
                continue
            n += 1
            amt = re.sub(r"[\s ]", "", cell_text(cells[1])).replace(",", ".")
            billd, purpose = cell_text(cells[2]), cell_text(cells[10])
            ack, chg = cell_text(cells[11]), cell_text(cells[12])
            acc = account_of(purpose)
            counted = ("не сквитировано" in ack.lower()
                       and chg.strip().lower() != "аннулирование"
                       and acc in tot)
            if counted:
                try:
                    tot[acc] += Decimal(amt)
                except Exception:
                    pass
            print(f"{amt:>11} | сч {acc} | {billd:16.16} | {ack:18.18} | {chg:13.13} | "
                  f"{'В ДОЛГ' if counted else 'пропуск':7} | {purpose[:40]}")
    print("=" * 95)
    print(f"Строк по '{fio}': {n}   "
          f"ДОЛГ 205(наём) = {tot['205']}   ДОЛГ 209(комуслуги) = {tot['209']}")
    print("Примечание: показаны ВСЕ месяцы (без окна). Боевой релей берёт только "
          "последние N мес — см. «Окно, мес» в панели ЖКХ.")


if __name__ == "__main__":
    main()
