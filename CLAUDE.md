# CLAUDE.md — быстрый старт для ИИ-сессий (utility-calc / ЖКХ «Лидер»)

> Цель файла: чтобы новая сессия НЕ гадала. Здесь — что это за проект, где что лежит,
> ключевые доменные правила и подводные камни. Обновляй при крупных изменениях.
> Прод: **asy-tk.ru**. Владелец: Ylanov (русский, быстрый темп, коммит в main без PR).

## Что это
Биллинг ЖКХ для общежитий/домов: расчёт коммуналки (ГВС/ХВС/электр + найм/ТКО/отопление),
импорт долгов из 1С, подача показаний жильцами. Три части:
- **Backend**: FastAPI, `app/modules/utility/` (основной модуль). Также `arsenal` (ДЕПРЕКЕЙТ — не трогать) и `llm` (ИИ-пилот на GigaChat).
- **Admin SPA**: `static/` — ES-модули (`static/js/modules/*.js`), компоненты `static/components/admin/*.html`. Без сборщика, нативные модули.
- **Flutter-приложение**: `AndroidStudioProjects/jkh_lider_app_1/` (Android, RuStore, пакет `ru.asytk.jkhlider`), ходит в `asy-tk.ru/api`.

## Архитектура / где что
- **Роутеры**: `app/modules/utility/routers/` — `client_readings` (подача/расчёт), `financier` (долги 1С, KPI, сверка), `admin_reports` (финсводка v2, residents-finance), `admin_analyzer` (Центр анализа), `rooms` (Жилфонд), `settings`, `admin_*`.
- **Сервисы**: `app/modules/utility/services/` — `calculations` (расчёт квитанции), `debt_import` (парсер ОСВ 1С), `billing` (закрытие периода/auto-fill), `finance_analyzer`, `resident_problem_scanner` (Монитор проблем), `auto_recalc_drift`, `room_audit` (типы квартир), `gsheets_sync`.
- **Модели**: `app/modules/utility/models.py`. **Миграции**: `alembic/versions/` (линейная цепочка, 1 head).
- **Celery**: `app/worker.py` (beat-расписание) + `tasks.py` + `llm/celery_tasks.py`.
- **Frontend-модули**: `debts.js` (Долги 1С), `summary.js` (Финансовая отчётность v2), `analyzer.js` (Центр анализа), `housing.js` (Жилфонд), `tariffs.js`, `users.js`, `readings.js`, `dashboard.js`. Хелперы — `static/js/core/dom.js`, `api.js`.

## Ключевые доменные правила (НЕ нарушать)
1. **Статика на КОМНАТУ, не на жильце.** Счётчики (`Room.has_*_meter` + серийники), тариф (`Room.tariff_id`), тип квартиры (`Room.is_singles_apartment`), площадь — свойства Room. Жилец наследует через резолв **Room > User > default**. Вопрос «это про помещение или про человека?». `User.residents_count` = размер семьи (≠ `Room.total_room_residents`).
2. **Долг 1С НЕ в ИТОГО.** `total_209 = cost + adj` (без долга). Долг/переплата хранятся в `MeterReading.debt_*/overpayment_*`, агрегируются ОТДЕЛЬНО (SUM), показываются справочным блоком. НЕ возвращать `+ debt` в формулу total.
3. **Долг привязан к user_id, не к room_id** (коммуналки с холостяками). `applied_state` импорта ключуется по `str(user_id)`.
4. **Долг требует комнату**: `MeterReading.room_id` NOT NULL. Жилец без комнаты → импорт кладёт в not_found. Неразнесённый долг виден отдельным блоком в «Долги 1С».
5. **Окно подачи может переходить через месяц** (start>end, напр. 15→3 следующего). Дефолт 15/3. Логика wrap во ВСЕХ точках (`_is_submission_day_open`, `check_auto_period_task`, `remind_submit_readings_task`, `readings.js`).
6. **Холостяцкая квартира**: `is_singles_apartment=True` → жильцы `resident_type='single'`, найм/ТКО/отопление = площадь÷`max_capacity`, счётчики делятся на факт. число жильцов. Перевод одной кнопкой: `POST /rooms/{id}/make-singles` (флаг+площадь+вместимость+синк жильцов).
7. **Валидация показаний** — единый источник `services/reading_validators.py` (потолки: вода 10000 м³, total_cost 100k ₽). Был инцидент 1.48 млрд ₽ из-за потери десятичной точки.
8. **UI: никаких нативных `confirm/prompt/alert`** — только `showConfirm/showPrompt/showAlert/showDialog` из `core/dom.js` (функция → `async`).

## Деплой / CI (полностью автоматический)
- Push в `main` → `.github/workflows/build.yml`: проверки (ruff/trivy/gitleaks) → build образ GHCR → деплой на **self-hosted runner** (rsync → `docker compose pull` с retry → `up -d` → сервис `utility_calc_migrations` гонит `alembic upgrade head` АВТОМАТОМ → force-recreate nginx → health). **Миграции применяются сами.** APK — вручную.
- Прод-хост **общий, диск 15G** — забивается; деплой чистит builder cache до pull (инцидент 31.05: диск 93% → setns/nginx fail). Восстановление: `docker builder prune -af && docker compose up -d`.
- `gh run watch --exit-status` ВРЁТ → проверять `gh run view <id> --json conclusion`.

## Коммиты
Русское описание, в конце: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Коммит в main напрямую (Ylanov не использует PR). py_compile + ruff перед коммитом backend; `node --check` (как .mjs) для JS-модулей.

## Где детальная память
Подробные заметки по фичам/багам — в auto-memory сессий (`~/.claude/.../memory/*.md`): `room_static_architecture`, `debts_per_user`, `meter_reading_validation`, `singles_apartment`, `tech_gotchas_devops/python`, `deploy_environment`, `llm_pilot_gigachat`. Этот CLAUDE.md — точка входа, там — детали.
