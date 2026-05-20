#!/usr/bin/env bash
# audit_system.sh — глубокая диагностика prod-инстанса ЖКХ Лидер.
#
# Запуск (от root на сервере где крутится docker compose):
#   bash audit_system.sh
#   bash audit_system.sh > /tmp/audit_$(date +%F).log
#
# Что проверяет (15 категорий):
#   1) DB connection + ROW counts всех ключевых таблиц
#   2) Активный период (должен быть ровно один)
#   3) Активные тарифы (>= 1) + новые сезонные поля + norm_coefficient
#   4) Reading'и с total_cost > 100 000 ₽ (потенциальные false-positive)
#   5) Reading'и с отрицательной дельтой (счётчик упал, как у Шияна)
#   6) Дубль-drafts: несколько не-approved reading'ов в одной (room, period)
#   7) Жильцы с user.room_id отличается от последнего room_assignment
#   8) Активные жильцы без room_id (не смогут подать показания)
#   9) gsheets stuck rows (auto_approved без reading_id) — должны быть 0
#  10) gsheets conflicts: meter_decreased, value_too_large и пр.
#  11) Кеш тарифов (через python в воркере)
#  12) Миграции alembic (текущая ревизия)
#  13) Контейнеры docker — все healthy
#  14) Объём БД и партиций readings
#  15) Recent worker errors (Celery logs за 24ч)
#
# Версия скрипта — обновляется при изменении схемы. Запускать после каждого
# крупного релиза для проверки целостности.

set -u

DB="utility_calc_db"
WORKER="utility_calc_worker_jkh_default"
PG_OPTS="-U postgres -d utility_db -A -F$'\t'"

# Цветной заголовок секции
section() {
    echo
    echo "═══════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "═══════════════════════════════════════════════════════════════"
}

# Выполнить SQL и красиво вывести (без -A для удобочитаемых ASCII-таблиц)
psql_pretty() {
    docker exec -i "$DB" psql -U postgres -d utility_db -P pager=off "$@"
}

# Проверки

section "1) DB connection + базовые счётчики"
psql_pretty <<'SQL'
SELECT 'users'           AS table, COUNT(*) FROM users
UNION ALL SELECT 'rooms', COUNT(*) FROM rooms
UNION ALL SELECT 'tariffs', COUNT(*) FROM tariffs
UNION ALL SELECT 'periods', COUNT(*) FROM periods
UNION ALL SELECT 'readings', COUNT(*) FROM readings
UNION ALL SELECT 'gsheets_import_rows', COUNT(*) FROM gsheets_import_rows
UNION ALL SELECT 'room_assignments', COUNT(*) FROM room_assignments
UNION ALL SELECT 'adjustments', COUNT(*) FROM adjustments
UNION ALL SELECT 'audit_log', COUNT(*) FROM audit_log;
SQL

section "2) Активный период (должен быть РОВНО один)"
psql_pretty <<'SQL'
SELECT id, name, is_active FROM periods WHERE is_active;
SELECT 'ALERT: несколько активных периодов!' WHERE (SELECT COUNT(*) FROM periods WHERE is_active) > 1;
SELECT 'ALERT: нет активного периода!' WHERE (SELECT COUNT(*) FROM periods WHERE is_active) = 0;
SQL

section "3) Активные тарифы + новые поля сезонности (миграция tariffs_seasonal_002 + tariffs_norm_001)"
psql_pretty <<'SQL'
SELECT id, name, is_active,
       heating_active,
       heating_season_start, heating_season_end,
       hw_heating_active,
       hw_heating_season_start, hw_heating_season_end,
       norm_coefficient
FROM tariffs WHERE is_active ORDER BY id;
SQL

section "4) Reading'и с total_cost > 100 000 ₽ (потенциальные false-positive)"
psql_pretty <<'SQL'
SELECT r.id, r.user_id, r.room_id, r.period_id, p.name AS period,
       r.hot_water, r.cold_water, r.total_cost, r.anomaly_flags
FROM readings r LEFT JOIN periods p ON p.id = r.period_id
WHERE r.total_cost > 100000
ORDER BY r.total_cost DESC LIMIT 30;
SQL

section "5) Reading'и где счётчик 'упал' (отриц. дельта vs предыдущему)"
psql_pretty <<'SQL'
WITH ranked AS (
  SELECT r.*,
         LAG(r.hot_water)   OVER (PARTITION BY r.user_id, r.room_id ORDER BY r.created_at) AS prev_hot,
         LAG(r.cold_water)  OVER (PARTITION BY r.user_id, r.room_id ORDER BY r.created_at) AS prev_cold
  FROM readings r WHERE r.is_approved
)
SELECT id, user_id, room_id, period_id,
       prev_hot, hot_water,
       prev_cold, cold_water,
       (hot_water - prev_hot)   AS delta_hot,
       (cold_water - prev_cold) AS delta_cold,
       anomaly_flags, created_at
FROM ranked
WHERE prev_hot IS NOT NULL AND (
    hot_water < prev_hot - 0.5 OR cold_water < prev_cold - 0.5
)
ORDER BY created_at DESC LIMIT 30;
SQL

section "6) Дубль-drafts: >1 не-approved в одной (room, period)"
psql_pretty <<'SQL'
SELECT room_id, period_id, COUNT(*) AS dup_count,
       array_agg(id) AS reading_ids
FROM readings
WHERE is_approved = false
GROUP BY room_id, period_id
HAVING COUNT(*) > 1
ORDER BY dup_count DESC LIMIT 20;
SQL

section "7) Жилец user.room_id ≠ последний открытый room_assignment"
psql_pretty <<'SQL'
WITH last_assign AS (
  SELECT DISTINCT ON (user_id) user_id, room_id AS assign_room_id, moved_in_at
  FROM room_assignments
  WHERE moved_out_at IS NULL
  ORDER BY user_id, moved_in_at DESC
)
SELECT u.id, u.username, u.room_id AS user_room, la.assign_room_id, la.moved_in_at
FROM users u LEFT JOIN last_assign la ON la.user_id = u.id
WHERE u.is_deleted = false AND u.role = 'user'
  AND u.room_id IS NOT NULL
  AND (la.assign_room_id IS NULL OR la.assign_room_id != u.room_id)
ORDER BY u.id LIMIT 30;
SQL

section "8) Активные жильцы без room_id"
psql_pretty <<'SQL'
SELECT id, username, role, is_deleted, room_id
FROM users
WHERE is_deleted = false AND role = 'user' AND room_id IS NULL
ORDER BY id LIMIT 30;
SQL

section "9) gsheets stuck rows (auto_approved без reading_id) — должны быть 0"
psql_pretty <<'SQL'
SELECT COUNT(*) AS stuck_total,
       COUNT(DISTINCT matched_user_id) AS stuck_users
FROM gsheets_import_rows
WHERE status = 'auto_approved' AND reading_id IS NULL
  AND matched_user_id IS NOT NULL
  AND hot_water IS NOT NULL AND cold_water IS NOT NULL;

SELECT id, sheet_timestamp, raw_fio, raw_room_number, matched_user_id, processed_at
FROM gsheets_import_rows
WHERE status = 'auto_approved' AND reading_id IS NULL
  AND matched_user_id IS NOT NULL
ORDER BY sheet_timestamp DESC LIMIT 15;
SQL

section "10) gsheets conflicts (нужна ручная проверка админом)"
psql_pretty <<'SQL'
SELECT status, COUNT(*) FROM gsheets_import_rows
WHERE status IN ('conflict', 'unmatched', 'pending')
GROUP BY status ORDER BY status;

SELECT id, sheet_timestamp, raw_fio, raw_room_number,
       status, LEFT(conflict_reason, 100) AS reason
FROM gsheets_import_rows
WHERE status = 'conflict'
ORDER BY sheet_timestamp DESC LIMIT 20;
SQL

section "11) Кеш тарифов worker (через python — должно быть >0 active)"
docker exec -i "$WORKER" python <<'PY' 2>&1 || echo "(python eval failed)"
try:
    from app.modules.utility.services.tariff_cache import tariff_cache
    tariff_cache.invalidate()
    active = tariff_cache.get_all_active()
    stats = tariff_cache.stats()
    print(f"active_count: {len(active)}")
    print(f"default_tariff_id: {stats.get('default_tariff_id')}")
    for tid, t in active.items():
        print(f"  id={tid} name={t.name} heating_active={t.heating_active} "
              f"norm_coef={t.norm_coefficient}")
except Exception as e:
    print(f"CACHE ERROR: {type(e).__name__}: {e}")
PY

section "12) Текущая alembic-ревизия (должна быть tariffs_norm_001_coefficient или новее)"
psql_pretty -c "SELECT version_num FROM alembic_version;"

section "13) Контейнеры docker — статус (все должны быть Up/healthy)"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" | grep -E "(utility|nginx|NAMES)"

section "14) Размер БД + партиций readings"
psql_pretty <<'SQL'
SELECT pg_size_pretty(pg_database_size('utility_db')) AS db_size;

SELECT inhrelid::regclass AS partition_name,
       pg_size_pretty(pg_relation_size(inhrelid)) AS size
FROM pg_inherits
WHERE inhparent = 'readings'::regclass
ORDER BY pg_relation_size(inhrelid) DESC LIMIT 20;
SQL

section "15) Worker errors за последние 24ч (если есть)"
docker logs --since 24h "$WORKER" 2>&1 | \
    grep -iE 'error|exception|traceback|critical|skip user|meter_decreased|integrity_error' | \
    tail -40 || true

section "ГОТОВО"
echo "Аудит завершён. Если есть ALERT/ERROR строки выше — пришлите вывод."
echo "Можно перенаправить: bash $0 > /tmp/audit_\$(date +%F).log 2>&1"
