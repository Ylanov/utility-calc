# Релей ГИС ГМП → ЖКХ (`relay/gisgmp`)

Серверный мост-демон: тянет долги жильцов из реестра ГИС ГМП
(`gisgmp.cgu.mchs.ru`, корп-сеть, OAuth2/passport) и шлёт в ЖКХ-биллинг
(`asy-tk.ru`, вкладка «Долги 1С»). Запускается на **ВМ PODS2** — единственной
машине, что видит И корп-сеть (реестр через Cisco `10.23.0.1`), И интернет.

## Управление — из ЖКХ (pull)

Релей за NAT, ЖКХ к нему внутрь не достучится. Поэтому **ЖКХ хранит
настройки/команды, релей сам их забирает** раз в ~2 мин и отчитывается.
В панели «Долги 1С» → «Авто-подгрузка ГИС ГМП»:

- вкл/выкл, окно в месяцах (по умолч. 2), интервал (12 ч),
- кнопка «Запустить сейчас» (релей подхватит на следующем опросе),
- статус последнего прогона (когда, сколько, ошибки).

Эндпоинты ЖКХ:
- `GET /api/financier/gisgmp/relay-config` — релей берёт конфиг (token).
- `POST /api/financier/gisgmp/relay-report` — релей шлёт отчёт (token).
- `POST /api/financier/gisgmp/sync` — релей шлёт начисления (token).
- `PUT /api/financier/gisgmp/relay-config`, `POST /gisgmp/run-now` — админ (JWT).

## Поток одного прогона

```
GET /charge/ → hydra:4444 → passport/oauth/login (POST username/password/_csrf_token)
            → сессия → GET /charge/?page=N&filtration[billDate_from]=<сегодня−N мес>
            → парсинг 15 ячеек → POST /gisgmp/sync → POST /gisgmp/relay-report
```

Сбор — **скользящее окно**: только последние `months_back` месяцев (фильтр по
дате начисления). Долг = «Не сквитировано» (бэкенд `gisgmp_import` классифицирует).

## Установка на ВМ PODS2

1. Файлы в `/opt/gisgmp-relay/` (`relay.py`, `gisgmp-relay.service`).
2. `sudo apt install -y python3-requests`
3. `/etc/hosts` (систем-юнит сам ставит маршрут к корп-сети):
   ```
   10.24.200.146 gisgmp.cgu.mchs.ru
   10.24.200.132 passport.cgu.mchs.ru
   10.24.200.130 hydra.cgu.mchs.ru
   ```
4. `relay.env.example` → `/opt/gisgmp-relay/relay.env`, вписать логин/пароль
   passport и токен, `chmod 600`.
5. Юнит в `/etc/systemd/system/`, затем:
   ```
   sudo systemctl daemon-reload
   sudo systemctl enable --now gisgmp-relay.service
   journalctl -u gisgmp-relay -f
   ```

## relay.env

| Переменная | Назначение |
|---|---|
| `PASSPORT_USERNAME` / `PASSPORT_PASSWORD` | корп-вход в реестр |
| `GISGMP_SYNC_TOKEN` | токен ЖКХ (= тот же в .env ЖКХ) |
| `JKH_URL` | адрес ЖКХ (`https://asy-tk.ru`) |
| `POLL_SECONDS` | период опроса конфига (по умолч. 120) |
| `MAX_PAGES` | лимит страниц обхода (защита) |
| `ONLY_UNPAID` | `1` — только «Не сквитировано»; `0` — все (бэкенд сам отсеет оплаченные) |

Окно (`months_back`), интервал и вкл/выкл — **не здесь**, а в панели ЖКХ
(хранятся на сервере, релей подхватывает).

## Замечания

- IP реестра (`10.24.200.x`) — в `/etc/hosts`; при смене в МЧС обновить.
- Долг считается за окно `months_back` мес. Долги старше окна не учитываются
  (так и задумано — «последние N месяцев»). Хочешь полную историю — увеличь окно.
