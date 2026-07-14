"""certs_purge_001: вырезание фичи «Справки» (решение пользователя 2026-07-14).

Дропаем таблицы фичи (по прецеденту lk_ai_purge_001 — идемпотентно,
исторические миграции certs_001/certs_002 НЕ трогаем):
- certificate_requests — заявки на справки (+PDF);
- family_members — состав семьи (вёлся только для справок, UI удалён);
- users.registration_address / users.lives_alone — читал только PDF-генератор
  справки ФЛС (certificate_pdf.py, удалён).

ОСТАЮТСЯ: rental_contracts (общая: импорт долгов 1С + отчёты + вкладка
«Жильцы»), профильные колонки users (full_name/passport_*/… — используются
широко за пределами справок).
"""
from alembic import op

revision = "certs_purge_001"
down_revision = "uq_reading_001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TABLE IF EXISTS certificate_requests")
    op.execute("DROP TABLE IF EXISTS family_members")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS registration_address")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS lives_alone")


def downgrade():
    # Данные справок восстановлению не подлежат (фича вырезана намеренно).
    # Каркас таблиц можно вернуть повторным прогоном certs_001/certs_002.
    pass
