import os
import subprocess
import time
import re

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool

from app.models import User
from app.dependencies import get_current_user
from app.config import settings  # <--- Импортируем настройки

router = APIRouter(tags=["System"])

# -------------------------------------------------
# НАСТРОЙКИ ОКРУЖЕНИЯ ДЛЯ УТИЛИТ (PG_DUMP/PSQL)
# -------------------------------------------------

# Утилиты psql/pg_dump требуют пароль в переменной окружения
os.environ["PGPASSWORD"] = settings.DB_PASS

# -------------------------------------------------
# ПАПКА ДЛЯ ВРЕМЕННЫХ ФАЙЛОВ
# -------------------------------------------------

BACKUP_DIR = "/tmp/backups"
os.makedirs(BACKUP_DIR, exist_ok=True)


# -------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ БЛОКИРУЮЩИЕ ФУНКЦИИ
# -------------------------------------------------

def _fix_sql_dump_sync(filepath: str) -> None:
    """Очистка SQL дампа от мусорных команд."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()

        cleaned_lines = []
        patterns_to_remove = [
            r"SET\s+statement_timeout", r"SET\s+lock_timeout",
            r"SET\s+idle_in_transaction_session_timeout", r"SET\s+row_security",
            r"SET\s+default_table_access_method", r"SET\s+transaction_timeout",
            r"SET\s+check_function_bodies", r"SET\s+xmloption",
            r"SET\s+client_min_messages", r"--\s+Dumped from database version",
            r"--\s+Dumped by pg_dump version"
        ]

        for line in lines:
            should_remove = False
            for pattern in patterns_to_remove:
                if re.search(pattern, line, re.IGNORECASE):
                    should_remove = True
                    break
            if not should_remove:
                cleaned_lines.append(line)

        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(cleaned_lines)

    except Exception as e:
        raise RuntimeError(f"Ошибка исправления SQL: {e}")


def _kill_db_connections_sync():
    """Разрыв соединений."""
    kill_sql = f"""
    SELECT pg_terminate_backend(pg_stat_activity.pid)
    FROM pg_stat_activity
    WHERE pg_stat_activity.datname = '{settings.DB_NAME}'
      AND pid <> pg_backend_pid();
    """
    command = [
        "psql", "-h", settings.DB_HOST, "-p", settings.DB_PORT,
        "-U", settings.DB_USER, "-d", settings.DB_NAME,
        "-c", kill_sql
    ]
    subprocess.run(command, capture_output=True, text=True)


def _apply_migrations_sync():
    """Миграции для совместимости."""
    # Мы добавляем period_id и создаем таблицу periods, если её нет (хотя sqlalchemy create_all создаст таблицу)
    # Но period_id в readings надо добавить вручную
    migration_sql = """
    DO $$
    BEGIN
        -- Старые миграции
        ALTER TABLE readings ADD COLUMN IF NOT EXISTS cost_social_rent FLOAT DEFAULT 0.0;
        ALTER TABLE readings ADD COLUMN IF NOT EXISTS cost_waste FLOAT DEFAULT 0.0;

        -- НОВАЯ МИГРАЦИЯ: Периоды
        ALTER TABLE readings ADD COLUMN IF NOT EXISTS period_id INTEGER REFERENCES periods(id);

        -- Создание индекса (если нет) - чисто через SQL сложно проверить наличие индекса просто, 
        -- но CREATE INDEX IF NOT EXISTS работает в новых версиях PG. 
        -- В старых это вызовет ошибку, поэтому оставим создание индексов на откуп SQLAlchemy при старте,
        -- или админ должен сделать это вручную, если данные уже есть.
    EXCEPTION
        WHEN duplicate_column THEN RAISE NOTICE 'column already exists';
        WHEN others THEN RAISE NOTICE 'Migration warning: %', SQLERRM;
    END
    $$;
    """
    command = [
        "psql", "-h", settings.DB_HOST, "-p", settings.DB_PORT,
        "-U", settings.DB_USER, "-d", settings.DB_NAME,
        "-c", migration_sql
    ]
    subprocess.run(command, capture_output=True, text=True)


def _perform_backup_sync(filepath: str, command: list):
    """Синхронный запуск процесса бэкапа."""
    with open(filepath, "w", encoding="utf-8") as f:
        subprocess.run(command, stdout=f, stderr=subprocess.PIPE, check=True, text=True)


def _perform_restore_sync(filepath: str):
    """Синхронный запуск восстановления."""
    _fix_sql_dump_sync(filepath)
    _kill_db_connections_sync()
    time.sleep(1)

    command = [
        "psql", "-h", settings.DB_HOST, "-p", settings.DB_PORT,
        "-U", settings.DB_USER, "-d", settings.DB_NAME,
        "-v", "ON_ERROR_STOP=1", "-f", filepath
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    _apply_migrations_sync()


# -------------------------------------------------
# 1. СОЗДАНИЕ БЭКАПА
# -------------------------------------------------

@router.get("/api/admin/backup")
async def create_backup(current_user: User = Depends(get_current_user)):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    filename = f"backup_{int(time.time())}.sql"
    filepath = os.path.join(BACKUP_DIR, filename)

    command = [
        "pg_dump", "-h", settings.DB_HOST, "-p", settings.DB_PORT,
        "-U", settings.DB_USER, "-d", settings.DB_NAME,
        "--clean", "--if-exists", "--no-owner", "--no-privileges"
    ]

    try:
        await run_in_threadpool(_perform_backup_sync, filepath, command)
        return FileResponse(path=filepath, filename=filename, media_type="application/sql")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Backup error:\n{e.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------
# 2. ВОССТАНОВЛЕНИЕ
# -------------------------------------------------

@router.post("/api/admin/restore")
async def restore_backup(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if not file.filename.lower().endswith(".sql"):
        raise HTTPException(status_code=400, detail="Нужен .sql файл")

    filepath = os.path.join(BACKUP_DIR, "restore_temp.sql")

    try:
        contents = await file.read()
        with open(filepath, "wb") as f:
            f.write(contents)

        await run_in_threadpool(_perform_restore_sync, filepath)

        return {"status": "success", "message": "База успешно восстановлена!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass