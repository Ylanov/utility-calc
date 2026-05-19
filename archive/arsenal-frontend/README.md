# СТРОБ Арсенал — Frontend (архив)

Эта папка — **архив frontend-кода** системы СТРОБ Арсенал (учёт вооружения),
перенесённый из основного проекта `utility-calc` 19 мая 2026.

## Зачем

Раньше Арсенал был «вкладкой» в общей платформе asy-tk.ru. Решено вынести его
в отдельный проект — он не связан с ЖКХ и имеет совершенно другую аудиторию
(военная учётная политика vs. квитанции жильцов). Frontend физически перенесён
сюда, чтобы:

1. Не отдаваться через `StaticFiles(directory="static")` — `archive/` не входит
   в этот mount, файлы недоступны через `https://asy-tk.ru/arsenal_*`.
2. Сохранить полную историю кода (`git log` на этих файлах работает).
3. Когда будет создан отдельный проект — просто скопировать сюда содержимое
   `archive/arsenal-frontend/` и запустить.

## Что сохранено

```
html/  — 8 HTML страниц (login, dashboard, inventory, users, reports, audit, modals, reset_password)
js/    — 9 JS модулей (arsenal-app, arsenal-core, arsenal-data, arsenal-inventory,
         arsenal-users, arsenal-reports, arsenal-audit, arsenal-docs-form, arsenal-docs-list)
css/   — arsenal.css (Tailwind Play CDN-стилизация)
```

## Что НЕ перенесено (пока остаётся в основном проекте)

- **Backend модули:** `app/modules/arsenal/` и связанные роутеры `/api/arsenal/*`
- **Миграции БД:** `alembic_arsenal/` — отдельная цепочка миграций для арсенальной БД
- **Docker контейнеры:** `utility_calc_web_arsenal_gsm`, `utility_calc_worker_arsenal_gsm`
  — продолжают работать, потому что админам нужен доступ к данным.

Эти части переедут в отдельный проект **во вторую очередь**, после того как
будет готова инфраструктура нового проекта.

## Как поднять временно (если нужно)

Если в какой-то момент потребуется временный доступ к арсенальному UI:

1. Перенести `html/`, `js/`, `css/` обратно в `static/`:
   ```bash
   git mv archive/arsenal-frontend/html/*.html static/
   git mv archive/arsenal-frontend/js/*.js static/js/
   git mv archive/arsenal-frontend/css/arsenal.css static/css/
   ```
2. Восстановить карточку «СТРОБ Арсенал» в `static/portal.html`
   (см. git history portal.html до коммита переноса).
