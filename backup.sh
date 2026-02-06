#!/bin/bash
set -e # Прерываем выполнение при любой ошибке

# Настройки
BACKUP_DIR="/backups"
DATE=$(date +"%Y-%m-%d_%H-%M-%S")
FILE="$BACKUP_DIR/backup_$DATE.sql"

echo "--- [START] Backup Routine: $DATE ---"

# 1. Создание дампа
# Используем переменные окружения PGHOST, PGUSER, PGPASSWORD, переданные через Docker
echo "Creating dump from host: $PGHOST, db: $PGDB..."
pg_dump -h "$PGHOST" -U "$PGUSER" -d "$PGDB" > "$FILE"

if [ -f "$FILE" ]; then
    echo "✅ Backup created successfully: $FILE"

    # Размер файла для логов
    SIZE=$(du -h "$FILE" | cut -f1)
    echo "Size: $SIZE"

    # 2. Очистка старых бэкапов (старше 30 дней)
    echo "Checking for old backups..."
    find "$BACKUP_DIR" -name "backup_*.sql" -mtime +30 -print -delete
    echo "🧹 Old backups cleaned up."
else
    echo "❌ Error: Backup file was not created!"
    exit 1
fi

echo "--- [END] Backup Routine ---"