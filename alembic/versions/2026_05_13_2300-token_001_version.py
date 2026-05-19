"""token_001_version — счётчик версий токенов для отзыва.

Бизнес-задача: JWT по умолчанию stateless, отозвать сессию нельзя.
Если у админа украли токен через XSS — атакующий имеет полный доступ
ещё 2 часа (до expires_delta). Logout / change-password / setup
ничего не делают с уже выданными токенами.

Решение — короткий int-счётчик на каждого пользователя:
  - User.token_version — стартует с 0
  - JWT при выдаче содержит `tv: <текущая версия>`
  - При decode JWT проверяем: tv == user.token_version
  - При logout / change-password / pdn-consent-revoke — инкрементируем
    счётчик в БД → все ранее выданные токены сразу невалидны

Реализация: одна колонка int с default 0. Без индекса (всегда читается
вместе с пользователем по PK).
"""
from alembic import op
import sqlalchemy as sa


revision = 'token_001_version'
down_revision = 'roles_001_simplify'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'token_version', sa.Integer(),
            nullable=False, server_default='0',
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'token_version')
