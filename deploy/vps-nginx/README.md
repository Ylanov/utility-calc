# VPS nginx — конфиги внешнего шлюза asy-tk.ru

Эта папка — **референс конфигов внешнего nginx** на VPS (`194.58.95.102`).
Файлы из репо нужно скопировать на VPS вручную:

```bash
ssh root@194.58.95.102
```

## Файлы

| Файл | Куда на VPS | Зачем |
|---|---|---|
| `asy-tk.conf` | `/etc/nginx/sites-available/asy-tk.conf` | HTTP→HTTPS + проксирование на aleks |
| `sonar.asy-tk.conf` | `/etc/nginx/sites-available/sonar.asy-tk.conf` | (если нужно) SonarQube прокси |

## Как обновить asy-tk.conf на VPS

1. **С локального ПК** скопируй файл на VPS:
   ```bash
   scp deploy/vps-nginx/asy-tk.conf root@194.58.95.102:/etc/nginx/sites-available/asy-tk.conf
   ```

2. **На VPS** проверь синтаксис и перезагрузи:
   ```bash
   ssh root@194.58.95.102
   sudo nginx -t                 # должно вывести "syntax is ok" + "test is successful"
   sudo systemctl reload nginx   # без даунтайма
   ```

3. **Проверь работу:**
   ```bash
   curl -I http://asy-tk.ru/      # 301 с HSTS-заголовком
   curl -I https://asy-tk.ru/     # 200 + полный набор security headers
   ```

4. **Прогони security-headers.com**:
   ```
   https://securityheaders.com/?q=https%3A%2F%2Fasy-tk.ru%2F&followRedirects=on
   ```
   Должна быть оценка **A+** (раньше была A с warning «Site is using HTTP»).

## Что добавлено (если сравнивать с предыдущей версией)

- HSTS на HTTP-редиректе (301 ответе) — сигнал что мы https-only
- `max-age=63072000` (2 года) + `preload` — для попадания в HSTS preload list
- Cross-Origin-Opener-Policy: same-origin
- Cross-Origin-Resource-Policy: same-origin
- Permissions-Policy расширен: + payment, usb, magnetometer
- `server_tokens off` — скрывает версию nginx
- Дублирование заголовков с `always` — выставляются на 4xx/5xx тоже

CSP **намеренно НЕ задан** в VPS nginx — он уже выставлен внутренним nginx
на aleks с детальной настройкой под наши страницы. Двойной CSP сломал бы
наследование.
