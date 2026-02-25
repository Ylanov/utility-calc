"""partition_readings_table_1

Revision ID: 1a8bb7c5c09d
Revises: 69b925d46c53
Create Date: 2026-02-25 16:02:31.945408

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '1a8bb7c5c09d'
down_revision: Union[str, Sequence[str], None] = '69b925d46c53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ==========================================================
    # 1. ОБНОВЛЕНИЕ ТАБЛИЦЫ USERS (Безопасность)
    # ==========================================================
    # Добавляем флаг первичной настройки
    op.add_column('users', sa.Column('is_initial_setup_done', sa.Boolean(), server_default='false', nullable=False))

    # ==========================================================
    # 2. ПАРТИЦИРОВАНИЕ ТАБЛИЦЫ READINGS
    # ==========================================================

    # 1. Переименовываем текущую таблицу, чтобы освободить имя
    # Мы используем IF EXISTS, чтобы скрипт не падал на чистой базе
    op.execute("ALTER TABLE IF EXISTS readings RENAME TO readings_old;")

    # 2. Создаем новую ПАРТИЦИРОВАННУЮ таблицу
    # Структура полностью совпадает с updated app/models.py
    # Включает составной Primary Key (id, created_at)
    op.execute("""
        CREATE TABLE readings (
            id SERIAL,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
            
            user_id INTEGER NOT NULL,
            period_id INTEGER,
            
            hot_water NUMERIC(12, 3),
            cold_water NUMERIC(12, 3),
            electricity NUMERIC(12, 3),
            
            debt_209 NUMERIC(12, 2) DEFAULT 0.00,
            overpayment_209 NUMERIC(12, 2) DEFAULT 0.00,
            debt_205 NUMERIC(12, 2) DEFAULT 0.00,
            overpayment_205 NUMERIC(12, 2) DEFAULT 0.00,
            
            hot_correction NUMERIC(12, 3) DEFAULT 0.0,
            cold_correction NUMERIC(12, 3) DEFAULT 0.0,
            electricity_correction NUMERIC(12, 3) DEFAULT 0.0,
            sewage_correction NUMERIC(12, 3) DEFAULT 0.0,
            
            total_209 NUMERIC(12, 2) DEFAULT 0.00,
            total_205 NUMERIC(12, 2) DEFAULT 0.00,
            total_cost NUMERIC(12, 2) DEFAULT 0.00,
            
            cost_hot_water NUMERIC(12, 2) DEFAULT 0.00,
            cost_cold_water NUMERIC(12, 2) DEFAULT 0.00,
            cost_electricity NUMERIC(12, 2) DEFAULT 0.00,
            cost_sewage NUMERIC(12, 2) DEFAULT 0.00,
            cost_maintenance NUMERIC(12, 2) DEFAULT 0.00,
            cost_social_rent NUMERIC(12, 2) DEFAULT 0.00,
            cost_waste NUMERIC(12, 2) DEFAULT 0.00,
            cost_fixed_part NUMERIC(12, 2) DEFAULT 0.00,
            
            anomaly_flags VARCHAR,
            is_approved BOOLEAN DEFAULT FALSE,
            
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at);
    """)

    # 3. Восстанавливаем внешние ключи (Constraints)
    op.execute("ALTER TABLE readings ADD CONSTRAINT fk_readings_user_id FOREIGN KEY (user_id) REFERENCES users(id);")
    op.execute("ALTER TABLE readings ADD CONSTRAINT fk_readings_period_id FOREIGN KEY (period_id) REFERENCES periods(id);")

    # 4. Создаем партиции (сегменты) с 2024 по 2035 год
    for year in range(2024, 2036):
        op.execute(f"CREATE TABLE IF NOT EXISTS readings_{year} PARTITION OF readings FOR VALUES FROM ('{year}-01-01') TO ('{year+1}-01-01');")

    # Партиция по умолчанию (для ошибочных дат или старых архивов)
    op.execute("CREATE TABLE IF NOT EXISTS readings_default PARTITION OF readings DEFAULT;")

    # 5. Копируем данные из старой таблицы в новую (если старая таблица существовала)
    # PostgreSQL автоматически распределит строки по нужным годам
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'readings_old') THEN
                INSERT INTO readings (
                    id, created_at, user_id, period_id, 
                    hot_water, cold_water, electricity, 
                    debt_209, overpayment_209, debt_205, overpayment_205,
                    hot_correction, cold_correction, electricity_correction, sewage_correction,
                    total_209, total_205, total_cost,
                    cost_hot_water, cost_cold_water, cost_electricity, cost_sewage, cost_maintenance, cost_social_rent, cost_waste, cost_fixed_part,
                    anomaly_flags, is_approved
                )
                SELECT 
                    id, COALESCE(created_at, now()), user_id, period_id, 
                    hot_water, cold_water, electricity, 
                    debt_209, overpayment_209, debt_205, overpayment_205,
                    hot_correction, cold_correction, electricity_correction, sewage_correction,
                    total_209, total_205, total_cost,
                    cost_hot_water, cost_cold_water, cost_electricity, cost_sewage, cost_maintenance, cost_social_rent, cost_waste, cost_fixed_part,
                    anomaly_flags, is_approved
                FROM readings_old;
            END IF;
        END $$;
    """)

    # 6. Синхронизируем счетчик ID (Sequence), чтобы новые записи не конфликтовали
    op.execute("SELECT setval(pg_get_serial_sequence('readings', 'id'), coalesce(max(id), 1), max(id) IS NOT null) FROM readings;")

    # 7. Удаляем старую таблицу
    op.execute("DROP TABLE IF EXISTS readings_old CASCADE;")

    # 8. Создаем индексы на новой таблице
    op.create_index('idx_reading_user_period', 'readings', ['user_id', 'period_id'])
    op.create_index('idx_reading_approved_period', 'readings', ['is_approved', 'period_id'])
    op.create_index('idx_reading_user_approved', 'readings', ['user_id', 'is_approved'])


def downgrade() -> None:
    # Откат изменений

    # 1. Удаляем колонку из users
    op.drop_column('users', 'is_initial_setup_done')

    # 2. Внимание! Откат партицирования сложен и опасен потерей данных в автоматическом режиме.
    # Мы просто сообщаем об ошибке, чтобы предотвратить случайный даунгрейд продакшена.
    raise NotImplementedError(
        "Автоматический откат партицированной таблицы 'readings' невозможен. "
        "Требуется ручное вмешательство администратора БД (экспорт данных -> пересоздание таблицы)."
    )