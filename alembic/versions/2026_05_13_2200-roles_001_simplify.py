"""roles_001_simplify — оставляем только 2 роли: admin и user.

Бизнес-задача: упростить модель прав. Раньше было 4 роли:
admin, accountant, financier, user. Это создавало путаницу и
дыру в безопасности (бухгалтер мог создать ещё одного admin'а).

Решение: все «полу-административные» роли (accountant, financier)
переводятся в admin. Стандартный жилец остаётся user.

Что делаем:
  - UPDATE users SET role='admin' WHERE role IN ('accountant', 'financier')

Что НЕ делаем (специально):
  - НЕ удаляем колонку role и НЕ навешиваем CHECK constraint на
    Literal['user', 'admin']. Это позволит откатить миграцию если что-то
    пойдёт не так. Pydantic-schema на API-уровне теперь принимает только
    user/admin (см. AllowedRole в schemas.py).

Откат: пере-навесить роли обратно нельзя автоматически (мы не помним
кто был accountant, а кто financier). Downgrade оставляет всех админами.
"""
from alembic import op


revision = 'roles_001_simplify'
down_revision = 'tickets_001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE users SET role='admin' "
        "WHERE role IN ('accountant', 'financier')"
    )


def downgrade() -> None:
    # Откат невозможен без потери информации — пользователи остаются админами.
    pass
