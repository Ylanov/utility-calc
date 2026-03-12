#!/bin/bash
set -e

echo "Checking required databases..."

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

echo "Databases utility_db and arsenal_db are ready."