"""Add tariff profiles

Revision ID: 9f3c34acea67
Revises: add_telegram_id_fix
Create Date: 2026-03-05 09:19:10.995167

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9f3c34acea67'
# Убедитесь, что down_revision совпадает с ID вашей предыдущей миграции!
# В вашем оригинальном файле это было 'add_telegram_id_fix' (или реальный хеш)
down_revision: Union[str, Sequence[str], None] = 'add_telegram_id_fix'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column('periods', sa.Column('tariff_id', sa.Integer(), nullable=True))
    op.create_foreign_key(None, 'periods', 'tariffs', ['tariff_id'], ['id'])
    op.add_column('tariffs', sa.Column('name', sa.String(), nullable=True))
    # Заполняем дефолтным значением существующие строки, чтобы избежать ошибки not-null
    op.execute("UPDATE tariffs SET name = 'Базовый тариф' WHERE name IS NULL")
    # Теперь можно сделать колонку не-nullable
    op.alter_column('tariffs', 'name', nullable=False)

    # 2. Добавляем поле 'tariff_id' в таблицу пользователей
    op.add_column('users', sa.Column('tariff_id', sa.Integer(), nullable=True))
    op.create_foreign_key(None, 'users', 'tariffs', ['tariff_id'], ['id'])

    # 3. Изменяем дефолтное значение для is_initial_setup_done (это было в автогенерации, видимо нужно)
    op.alter_column('users', 'is_initial_setup_done',
               existing_type=sa.BOOLEAN(),
               nullable=True,
               existing_server_default=sa.text('false'))


def downgrade() -> None:
    """Downgrade schema."""
    # Откат изменений
    op.alter_column('users', 'is_initial_setup_done',
               existing_type=sa.BOOLEAN(),
               nullable=False,
               existing_server_default=sa.text('false'))

    op.drop_constraint(None, 'users', type_='foreignkey')
    op.drop_column('users', 'tariff_id')
    op.drop_column('tariffs', 'name')