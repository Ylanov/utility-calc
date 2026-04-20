#!/bin/bash
# =============================================================================
# Резервное копирование баз PostgreSQL.
#
# СТРАТЕГИЯ ХРАНЕНИЯ:
#   - daily/   — ежедневные бэкапы, хранятся 7 дней
#   - weekly/  — еженедельные (по воскресеньям), хранятся 4 недели
#   - monthly/ — ежемесячные (1-го числа), хранятся 6 месяцев
#
# Каждый дамп сразу после создания валидируется через `gzip -t`
# и сравнивается с минимальным размером — защита от "пустых" бэкапов
# при недоступной БД или сломанном pg_dump.
# =============================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DAILY_DIR="$BACKUP_DIR/daily"
WEEKLY_DIR="$BACKUP_DIR/weekly"
MONTHLY_DIR="$BACKUP_DIR/monthly"

DAILY_RETENTION_DAYS=7
WEEKLY_RETENTION_DAYS=28
MONTHLY_RETENTION_DAYS=180

# Минимальный допустимый размер бэкапа в байтах. Меньше = подозрение на сбой.
MIN_BACKUP_SIZE_BYTES="${MIN_BACKUP_SIZE_BYTES:-10240}"   # 10 KB

DATABASES=("${PGDB:-utility_db}" "arsenal_db" "gsm_db")

DATE=$(date +%Y-%m-%d_%H-%M-%S)
DAY_OF_WEEK=$(date +%u)   # 1..7, воскресенье = 7
DAY_OF_MONTH=$(date +%d)

mkdir -p "$DAILY_DIR" "$WEEKLY_DIR" "$MONTHLY_DIR"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

backup_database() {
    local db="$1"
    local out_file="$DAILY_DIR/${db}_${DATE}.sql.gz"

    log "Dumping $db -> $(basename "$out_file")"

    # pg_dump → gzip напрямую, без буферизации в RAM (важно для больших БД)
    if ! pg_dump -h "$PGHOST" -U "$PGUSER" -d "$db" 2>/tmp/pg_dump_err.log | gzip > "$out_file"; then
        log "ERROR: pg_dump failed for $db"
        log "$(cat /tmp/pg_dump_err.log 2>/dev/null || true)"
        rm -f "$out_file"
        return 1
    fi

    # Валидация: размер
    local size
    size=$(stat -c%s "$out_file" 2>/dev/null || stat -f%z "$out_file" 2>/dev/null || echo 0)
    if [ "$size" -lt "$MIN_BACKUP_SIZE_BYTES" ]; then
        log "ERROR: $db backup too small ($size bytes < $MIN_BACKUP_SIZE_BYTES) — likely failed"
        rm -f "$out_file"
        return 1
    fi

    # Валидация: целостность gzip-потока
    if ! gzip -t "$out_file" 2>/dev/null; then
        log "ERROR: $db backup is corrupt (gzip -t failed)"
        rm -f "$out_file"
        return 1
    fi

    log "OK: $db backup saved ($((size / 1024)) KB)"

    # По воскресеньям копируем в weekly/
    if [ "$DAY_OF_WEEK" -eq 7 ]; then
        cp "$out_file" "$WEEKLY_DIR/$(basename "$out_file")"
        log "Copied to weekly/"
    fi

    # 1-го числа копируем в monthly/
    if [ "$DAY_OF_MONTH" = "01" ]; then
        cp "$out_file" "$MONTHLY_DIR/$(basename "$out_file")"
        log "Copied to monthly/"
    fi

    return 0
}

cleanup_old() {
    local dir="$1"
    local days="$2"
    local count_before
    local count_after

    count_before=$(find "$dir" -maxdepth 1 -name '*.sql.gz' 2>/dev/null | wc -l)
    find "$dir" -maxdepth 1 -name '*.sql.gz' -type f -mtime +$days -delete 2>/dev/null || true
    count_after=$(find "$dir" -maxdepth 1 -name '*.sql.gz' 2>/dev/null | wc -l)
    local deleted=$((count_before - count_after))
    if [ "$deleted" -gt 0 ]; then
        log "Cleaned $deleted old backup(s) from $dir (kept last $days days)"
    fi
}

# =============================================================================
log "Starting backups for: ${DATABASES[*]}"
FAILED=0
for db in "${DATABASES[@]}"; do
    if ! backup_database "$db"; then
        FAILED=$((FAILED + 1))
    fi
done

# Ротация — даже если часть бэкапов упала, чистим старые корректные
cleanup_old "$DAILY_DIR" "$DAILY_RETENTION_DAYS"
cleanup_old "$WEEKLY_DIR" "$WEEKLY_RETENTION_DAYS"
cleanup_old "$MONTHLY_DIR" "$MONTHLY_RETENTION_DAYS"

# Сводка по диску
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
log "Disk usage: $TOTAL_SIZE in $BACKUP_DIR"

if [ "$FAILED" -gt 0 ]; then
    log "WARNING: $FAILED database(s) failed. Check logs above."
    exit 1
fi

log "All backups completed successfully."
