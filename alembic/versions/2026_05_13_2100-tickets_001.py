"""tickets_001 — таблица обращений жильцов (support tickets).

Бизнес-задача: жилец из ЛК пишет вопрос → попадает в админку → админ
отвечает. Альтернатива «звоните в бухгалтерию» — экономит время всем.

Простая модель без переписки (как в Help Scout / Intercom): один вопрос
от жильца + один ответ от админа. При повторных вопросах жилец создаёт
новый тикет. Это сознательное упрощение — для общежития «диалог из
20 сообщений» избыточен.

Поля:
  - user_id          — кто задал вопрос (FK users)
  - subject          — короткая тема (макс. 200)
  - message          — текст вопроса (TEXT)
  - status           — open / in_progress / answered / closed
  - admin_response   — текст ответа админа (nullable)
  - responded_by_id  — кто из админов ответил (FK users, nullable)
  - responded_at     — когда ответил (nullable)
  - created_at       — когда жилец создал
"""
from alembic import op
import sqlalchemy as sa


revision = 'tickets_001'
down_revision = 'pdn_001_consent'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'support_tickets',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column(
            'user_id', sa.Integer(),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False, index=True,
        ),
        sa.Column('subject', sa.String(200), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        # Статусы соответствуют простой воронке:
        # open       — только что создан, никто не взял в работу
        # in_progress — админ открыл, ещё не ответил
        # answered   — админ написал ответ; жилец видит его в ЛК
        # closed     — закрыт (либо ответом, либо без — например, спам)
        sa.Column('status', sa.String(20), nullable=False, server_default='open', index=True),
        sa.Column('admin_response', sa.Text(), nullable=True),
        sa.Column(
            'responded_by_id', sa.Integer(),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('responded_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    )
    # Композитный индекс — для админ-списка с фильтром по статусу:
    # «все open отсортированные по created_at desc» (самые свежие сверху).
    op.create_index(
        'ix_support_tickets_status_created',
        'support_tickets', ['status', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_support_tickets_status_created', table_name='support_tickets')
    op.drop_table('support_tickets')
