#!/usr/bin/env python3
# relay/gisgmp/probe_charge.py
"""
Зонд действий по начислению ГИС ГМП — С СЕРВЕРА (без браузера).

Зачем: понять, КАКОЙ эндпоинт реально создаёт «Запрос на актуализацию платежа».
Релей сейчас бьёт в /actualize-request (это ОТКЛЮЧЁННАЯ кнопка; GET по ней =
просто отдаётся страница, заявка НЕ создаётся → 517 «ok», а в реестре пусто).
Зонд логинится теми же кредами, открывает страницу начисления и печатает
РЕАЛЬНЫЕ кнопки действий (href + активна ли) и историю запросов — чтобы выбрать
правильный эндпоинт, а потом точечно его проверить (--fire).

Запуск на aleks (где крутится релей), root уже есть:
    python3 /tmp/probe_charge.py
    python3 /tmp/probe_charge.py <charge_uuid>
    # тест-дёрнуть конкретный эндпоинт и сверить историю ДО/ПОСЛЕ:
    python3 /tmp/probe_charge.py <charge_uuid> --fire /api/charge/<uuid>/payment-request

Креды берёт из /opt/gisgmp-relay/relay.env (как check_payer.py).
Логика входа/парсинга — копия check_payer.py (ничего не импортирует, самодостаточен).
"""
import html
import re
import sys

import requests

REG = "https://gisgmp.cgu.mchs.ru"
PAS = "https://passport.cgu.mchs.ru"
UA = "Mozilla/5.0 (gisgmp-probe)"
ENV_PATH = "/opt/gisgmp-relay/relay.env"
DEFAULT_UUID = "cd2aa28e-d86b-4a6b-8985-ade326243dd0"  # УИН 17726057773110669918


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


def buttons(page):
    """Все <a class=btn ...>: текст, href, активна/отключена, ajax."""
    out = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page, re.S):
        attrs, inner = m.group(1), m.group(2)
        if "btn" not in attrs:
            continue
        href = re.search(r'href="([^"]*)"', attrs)
        cls = re.search(r'class="([^"]*)"', attrs)
        label = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", inner)).strip())
        if not label:
            continue
        out.append({
            "label": label,
            "href": href.group(1) if href else "",
            "disabled": "disabled" in (cls.group(1) if cls else ""),
            "ajax": "data-ajax" in attrs,
        })
    return out


def requests_history(page):
    """Строки истории запросов: ровно 4 ячейки, последняя — дата dd.mm.yyyy.
    (не зависит от кириллицы в заголовке — устойчиво к кодировке вставки)."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [cell_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) == 4 and re.search(r"\d{2}\.\d{2}\.\d{4}", cells[-1]):
            out.append(cells)
    return out


def show(page):
    print("\n=== КНОПКИ ДЕЙСТВИЙ (что реально доступно по начислению) ===")
    for b in buttons(page):
        flag = "ОТКЛ   " if b["disabled"] else "АКТИВНА"
        ajax = " [ajax]" if b["ajax"] else ""
        print(f"  [{flag}] {b['label']:34.34} -> {b['href']}{ajax}")
    print("\n=== ИСТОРИЯ ЗАПРОСОВ (Тип | Статус | Автор | Дата) ===")
    rows = requests_history(page)
    if not rows:
        print("  (пусто или не распознано)")
    for cells in rows:
        print("  " + " | ".join(cells))


def main():
    args = list(sys.argv[1:])
    fire = None
    if "--fire" in args:
        i = args.index("--fire")
        fire = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    uuid = args[0] if args else DEFAULT_UUID

    env = load_env()
    s = requests.Session()
    login(s, env)
    print(f"Начисление: {REG}/charge/{uuid}")
    page = s.get(f"{REG}/charge/{uuid}", headers={"User-Agent": UA}, timeout=30).text
    show(page)

    if fire:
        print(f"\n>>> ТЕСТ-ДЁРГАЮ: GET {fire}")
        r = s.get(f"{REG}{fire}",
                  headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"},
                  timeout=60, allow_redirects=True)
        body = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text)).strip())
        print(f"    HTTP {r.status_code}; ответ (первые 400 симв):")
        print("    " + body[:400])
        print("\n>>> ИСТОРИЯ ПОСЛЕ (перечитал страницу — появилась ли новая заявка?):")
        page2 = s.get(f"{REG}/charge/{uuid}", headers={"User-Agent": UA}, timeout=30).text
        for cells in requests_history(page2):
            print("  " + " | ".join(cells))


if __name__ == "__main__":
    main()
