"""pdn_001_consent — фиксация согласия жильца на обработку ПД (152-ФЗ).

Бизнес-задача: по 152-ФЗ оператор обязан получать согласие субъекта ПД
на их обработку и фиксировать сам факт согласия. Раньше согласие
подразумевалось «по умолчанию» при использовании сервиса — это серая
зона по закону.

Теперь:
  - При первом входе в личный кабинет (PWA или старый портал) жилец
    видит модалку с текстом политики и обязательным чекбоксом «Я согласен».
  - Согласие фиксируется в БД: timestamp + IP + версия политики.
  - При смене версии политики (значительные правки) жилец видит модалку
    повторно и подписывает новую версию.
  - Если согласия нет — middleware блокирует POST /api/calculate и другие
    модифицирующие операции (соразмерно — чтения /api/me разрешены, чтобы
    жилец мог посмотреть свои данные ДО решения о согласии).

Поля:
  - pdn_consent_at      TIMESTAMP NULL — когда дано согласие
  - pdn_consent_ip      VARCHAR(45) NULL — IPv4 (15) или IPv6 (45)
  - pdn_consent_version VARCHAR(10) NULL — версия принятой политики

Все nullable — существующие жильцы попадут в «согласие не дано» и при
следующем входе увидят модалку. Это нормально с точки зрения закона —
лучше получить явное подтверждение, чем декларировать «implied consent».
"""
from alembic import op
import sqlalchemy as sa


revision = 'pdn_001_consent'
down_revision = 'meters_001_per_user_config'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('pdn_consent_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('pdn_consent_ip', sa.String(45), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('pdn_consent_version', sa.String(10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'pdn_consent_version')
    op.drop_column('users', 'pdn_consent_ip')
    op.drop_column('users', 'pdn_consent_at')
