#!/bin/bash
set -e

BACKUP_DIR="/backups"

# Имена файлов для базы ЖКХ (Utility)
UTILITY_LATEST="$BACKUP_DIR/utility_db_latest.sql.gz"
UTILITY_PREV="$BACKUP_DIR/utility_db_previous.sql.gz"

# Имена файлов для базы Оружия (Arsenal)
ARSENAL_LATEST="$BACKUP_DIR/arsenal_db_latest.sql.gz"
ARSENAL_PREV="$BACKUP_DIR/arsenal_db_previous.sql.gz"

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Starting database backups..."

# 1. Сдвигаем старые бэкапы (сохраняем вчерашние копии)
if [ -f "$UTILITY_LATEST" ]; then
    mv "$UTILITY_LATEST" "$UTILITY_PREV"
fi
if [ -f "$ARSENAL_LATEST" ]; then
    mv "$ARSENAL_LATEST" "$ARSENAL_PREV"
fi

# 2. Создаем новые бэкапы (PGHOST берется из docker-compose, минуя PgBouncer)
echo "Dumping utility_db..."
pg_dump -h $PGHOST -U $PGUSER -d $PGDB | gzip > "$UTILITY_LATEST"

echo "Dumping arsenal_db..."
pg_dump -h $PGHOST -U $PGUSER -d arsenal_db | gzip > "$ARSENAL_LATEST"

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Backup process completed successfully. Disk space is protected."