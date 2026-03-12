#!/bin/bash
set -e

echo "Initializing databases..."

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL

SELECT 'CREATE DATABASE utility_db'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'utility_db'
)\gexec

SELECT 'CREATE DATABASE arsenal_db'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'arsenal_db'
)\gexec

EOSQL

echo "Databases are ready."