"""contracts_001_dedupe — дедуп договоров найма + уникальный (user_id, number).

Импорт ОСВ при РАЗДЕЛЬНОЙ/параллельной загрузке счетов 209 и 205 мог создать
ДВА одинаковых договора одному жильцу (у Ярощука обнаружены два №1682 — один
«из счёта 209», другой «из счёта 205»). Per-import кеш дедупа не спасал при
гонке двух задач. Чистим существующие дубли (оставляем самый ранний по id) и
ставим уникальный индекс (user_id, number); импорт переходит на
INSERT ... ON CONFLICT DO NOTHING.

NULL-номера (ручные загрузки договора без номера) НЕ затрагиваются — в
PostgreSQL NULL-значения в уникальном индексе считаются различными.
"""
from alembic import op


revision = 'contracts_001_dedupe'
down_revision = 'llm_003_call_text_and_prompt'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Удаляем дубли (user_id, number), оставляя самый ранний (min id).
    #    Только для непустых номеров — безномерные ручные загрузки не трогаем.
    op.execute(
        """
        DELETE FROM rental_contracts a
        USING rental_contracts b
        WHERE a.user_id = b.user_id
          AND a.number = b.number
          AND a.number IS NOT NULL
          AND a.id > b.id
        """
    )
    # 2. Уникальный индекс — повторная вставка того же номера жильцу больше
    #    невозможна (импорт использует ON CONFLICT DO NOTHING, ручные эндпоинты
    #    ловят IntegrityError → 409).
    op.create_index(
        "uq_rental_contract_user_number",
        "rental_contracts",
        ["user_id", "number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_rental_contract_user_number", table_name="rental_contracts")
