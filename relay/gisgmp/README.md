# Релей ГИС ГМП → ЖКХ (`relay/gisgmp`)

Серверный мост: тянет долги жильцов из реестра ГИС ГМП (`gisgmp.cgu.mchs.ru`,
корп-сеть, OAuth2/passport) и отправляет в ЖКХ-биллинг (`asy-tk.ru`, вкладка
«Долги 1С»). Запускается на **ВМ PODS2** по таймеру раз в 12 ч.

Почему на ВМ PODS2: это единственная машина, которая видит И корп-сеть
(реестр через Cisco `10.23.0.1`), И интернет (`asy-tk.ru`). Браузера там нет —
логинимся в реестр сами (форма passport: логин/пароль + `_csrf_token`, без ЭЦП).

## Поток

```
GET /charge/ → 307 /connect/hydra → hydra.cgu.mchs.ru:4444/oauth2/auth
            → passport.cgu.mchs.ru/oauth/login  (POST username/password/_csrf_token)
            → назад через hydra → /connect/hydra/check → сессия PHPSESSID
GET /charge/?page=N → парсинг 15 ячеек/строка → POST asy-tk.ru/api/financier/gisgmp/sync
```

Классификацию (наем→205 / комуслуги→209, «Не сквитировано»=долг,
«аннулирование»→мимо) и матч ФИО→жилец делает бэкенд ЖКХ (`gisgmp_import.py`).

## Установка на ВМ PODS2

1. Положить файлы в `/opt/gisgmp-relay/` (`relay.py`, юниты).
2. `sudo apt install -y python3-requests`
3. `/etc/hosts` — IP реестра (систем-юнит ставит маршрут сам):
   ```
   10.24.200.146 gisgmp.cgu.mchs.ru
   10.24.200.132 passport.cgu.mchs.ru
   10.24.200.130 hydra.cgu.mchs.ru
   ```
4. Скопировать `relay.env.example` → `/opt/gisgmp-relay/relay.env`, заполнить
   логин/пароль passport, `chmod 600`.
5. Юниты в `/etc/systemd/system/`, затем:
   ```
   sudo systemctl daemon-reload
   sudo systemctl enable --now gisgmp-relay.timer
   sudo systemctl start gisgmp-relay.service   # тестовый прогон
   journalctl -u gisgmp-relay -n 50 --no-pager
   ```

## Параметры (relay.env)

| Переменная | Назначение |
|---|---|
| `PASSPORT_USERNAME` / `PASSPORT_PASSWORD` | корп-вход в реестр |
| `GISGMP_SYNC_TOKEN` | токен приёма в ЖКХ (= тот же в .env ЖКХ) |
| `JKH_URL` | адрес ЖКХ (`https://asy-tk.ru`) |
| `MAX_PAGES` | лимит страниц обхода (защита) |
| `ONLY_UNPAID` | `1` — только «Не сквитировано» (быстрее), `0` — все |

## Замечания

- IP реестра (`10.24.200.x`) прописаны в `/etc/hosts` — если в МЧС сменятся,
  обновить там. Резолв через корп-DNS `10.21.62.33` тоже работает (если
  настроить форвардер dnsmasq `server=/cgu.mchs.ru/10.21.62.33`).
- Долг = весь непогашенный остаток по жильцу. Полный проход (`ONLY_UNPAID=0`)
  заодно обнуляет тех, кто погасил долг.
