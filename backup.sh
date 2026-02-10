#!/bin/bash

# Устанавливаем переменные для удобства
BACKUP_DIR="/backups"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
FILENAME="$BACKUP_DIR/utility_db_backup_$TIMESTAMP.sql.gz"

echo "Starting database backup..."

# Используем pg_dump для создания дампа.
# -h $PGHOST: хост базы данных (возьмется из environment: db)
# -U $PGUSER: пользователь (postgres)
# -d $PGDB: имя базы (utility_db)
# gzip -c: сжимаем дамп на лету
# > $FILENAME: и сохраняем в файл

pg_dump -h $PGHOST -U $PGUSER -d $PGDB | gzip > $FILENAME

# Проверяем, что файл создан
if [ -f "$FILENAME" ]; then
    echo "Backup successful: $FILENAME"
else
    echo "Backup FAILED!"
fi

# (Опционально) Удаление старых бэкапов, оставляем последние 7
echo "Cleaning up old backups..."
find $BACKUP_DIR -type f -name "*.sql.gz" -mtime +7 -delete
echo "Cleanup complete."