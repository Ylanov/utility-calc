"""GSheets aliases — запомненные соответствия «стороннее ФИО → жилец»

Revision ID: gsheets_002_aliases
Revises: app_001_releases
Create Date: 2026-04-20 12:00:00.000000

Жильцы часто подают через таблицу за родственников (жёны за мужей и т.д.).
В базе только зарегистрированный жилец, ФИО в Sheets — другое.

После того как админ подтвердил «эта подача от Иванова И.И. (его супругой
Марией Петровной)», запись попадает сюда. На следующих синхронизациях
подача от «Иванова Мария Петровна» подцепится автоматически.

Note по DAG: эта миграция и `app_001_releases` создавались параллельно и
обе указывали на `gsheets_001_import_rows` — alembic ругался "Multiple head
revisions". Перенаправили на `app_001_releases` чтобы цепочка была линейной.
Содержимое таблицы `app_releases` нашему `gsheets_aliases` не нужно, никаких
кросс-зависимостей нет — порядок применения произвольный.
"""
from alembic import op
import sqlalchemy as sa


revision = 'gsheets_002_aliases'
down_revision = 'app_001_releases'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'gsheets_aliases',
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=True),

        # Оригинальное написание — для отображения админу.
        sa.Column('alias_fio', sa.String(), nullable=False),
        # Нормализованная форма (lower, коллапс пробелов) — для быстрого поиска.
        sa.Column('alias_fio_normalized', sa.String(), nullable=False),

        sa.Column('user_id', sa.Integer(), nullable=False),

        # 'manual' — админ просто ткнул другого жильца в reassign,
        # 'relative' — подтвердил подсказку «возможно, это супруга/родственник».
        sa.Column('kind', sa.String(), nullable=False, server_default='manual'),
        sa.Column('note', sa.Text(), nullable=True),

        sa.Column('created_at', sa.DateTime(),
                  nullable=False, server_default=sa.text('NOW()')),
        sa.Column('created_by_id', sa.Integer(), nullable=True),

        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # Один и тот же ФИО НЕ может быть привязан к разным жильцам — иначе
    # автоматический матчинг недетерминирован. Если админ ошибся, удалит запись.
    op.create_index(
        'uq_gsheets_alias_fio',
        'gsheets_aliases',
        ['alias_fio_normalized'],
        unique=True,
    )
    # Поиск всех алиасов конкретного жильца — для admin UI «история связок».
    op.create_index(
        'idx_gsheets_alias_user',
        'gsheets_aliases',
        ['user_id'],
    )
    # Сортировка по дате создания — для аудита.
    op.create_index(
        'idx_gsheets_alias_created',
        'gsheets_aliases',
        ['created_at'],
    )


def downgrade() -> None:
    op.drop_index('idx_gsheets_alias_created', table_name='gsheets_aliases')
    op.drop_index('idx_gsheets_alias_user', table_name='gsheets_aliases')
    op.drop_index('uq_gsheets_alias_fio', table_name='gsheets_aliases')
    op.drop_table('gsheets_aliases')
