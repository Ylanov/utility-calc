#!/bin/bash
set -e

# Создаем базу данных arsenal_db, если она не существует
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE arsenal_db'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'arsenal_db')\gexec
EOSQL

echo "Database 'arsenal_db' checked/created."