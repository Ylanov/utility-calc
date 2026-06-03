"""users.login — отдельный ЛОГИН (учётка) от username (ФИО).

Раньше username был И логином (вход), И ФИО, И ключом сопоставления 1С/ГИС.
Жилец менял «логин» (а это его ФИО) на произвольный → сопоставление по ФИО
ломалось, и система переставала видеть его долги. Колоссальная ошибка.

Теперь:
  • username — ФИО, ключ сопоставления, правит ТОЛЬКО админ;
  • login    — учётные данные для входа, правит сам жилец (/me/change-login).

Бэкофилл: login := username (COALESCE для редких NULL/'' username, чтобы NOT NULL
и уникальный индекс не падали). Текущий вход сохраняется — люди входят прежним
ФИО как логином, пока сами не сменят. Уникальный case-insensitive индекс
uq_user_login_lower — зеркало uq_user_username_lower.
"""
from alembic import op
import sqlalchemy as sa


revision = 'users_login_001'
down_revision = 'pg_stat_statements_001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('login', sa.String(), nullable=True))
    # Бэкофилл из username. NULLIF('' → NULL), COALESCE на 'user_<id>' — чтобы
    # редкие тех. строки без username не уронили NOT NULL и уникальный индекс.
    op.execute(
        "UPDATE users SET login = COALESCE(NULLIF(username, ''), 'user_' || id) "
        "WHERE login IS NULL"
    )
    op.alter_column('users', 'login', nullable=False)
    op.create_index(
        'uq_user_login_lower',
        'users',
        [sa.text('lower(login)')],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('uq_user_login_lower', table_name='users')
    op.drop_column('users', 'login')
