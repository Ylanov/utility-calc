"""debts_002_obor — обороты за период (debit/credit) в MeterReading.

Добавляет 4 числовые колонки для хранения движения средств из ОСВ 1С:
  - obor_debit_209  — обороты Дебет по 209 счёту (доначисления за период)
  - obor_credit_209 — обороты Кредит по 209 (поступления денег от жильца)
  - obor_debit_205  — то же для 205 (наём)
  - obor_credit_205 — то же

Зачем (Bug V): сейчас в MeterReading лежит только КОНЕЧНОЕ сальдо
(debt_209, overpayment_209 и т.д.). По нему невозможно отличить «жилец
не подавал и долгов нет» от «жилец заплатил весь долг 5000 ₽ и вышел в
ноль». Эти колонки сохраняют обороты периода — UI может показывать
«долг был 5000, оплачено −5000, итог 0».

Все колонки NULL-разрешены, default 0. Существующие reading'и получат
NULL — их можно перезаписать при следующем импорте 1С.

ВАЖНО: readings — партиционированная таблица. ALTER TABLE применяется
сразу ко всем партициям через ALTER TABLE ONLY на parent (PG автоматом
проксирует на партиции для добавления nullable-колонок без DEFAULT).
"""
from alembic import op
import sqlalchemy as sa


revision = 'debts_002_obor'
down_revision = 'meter_fmt_002_strict'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('readings', sa.Column('obor_debit_209', sa.Numeric(12, 2), nullable=True))
    op.add_column('readings', sa.Column('obor_credit_209', sa.Numeric(12, 2), nullable=True))
    op.add_column('readings', sa.Column('obor_debit_205', sa.Numeric(12, 2), nullable=True))
    op.add_column('readings', sa.Column('obor_credit_205', sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('readings', 'obor_credit_205')
    op.drop_column('readings', 'obor_debit_205')
    op.drop_column('readings', 'obor_credit_209')
    op.drop_column('readings', 'obor_debit_209')
