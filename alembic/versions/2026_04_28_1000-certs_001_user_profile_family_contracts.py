"""User profile + family + rental contracts + certificate requests

Revision ID: certs_001_profile_family
Revises: aliases_001_canonical_norm
Create Date: 2026-04-28 10:00:00.000000

Первая волна фичи «Заказ справок»:
  * Расширяем users — паспортные данные + должность + дата регистрации.
    Всё nullable, чтобы старые жильцы не сломались — данные заполняет
    сам жилец при первом заказе справки, админ может поправить.
  * Новая family_members — жена/муж/дети с ФИО и датами рождения.
    При генерации справки на выписку ФЛС прилагаются члены семьи.
  * Новая rental_contracts — хранилище PDF-договоров найма с
    привязкой к жильцу. При заказе справки поля «дата/№ договора»
    автоматически берутся из последнего договора.
  * Новая certificate_requests — журнал заказанных справок.
    type сейчас только 'flc' (выписка из ФЛС), в будущем добавятся
    другие типы через ту же таблицу.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'certs_001_profile_family'
down_revision = 'aliases_001_canonical_norm'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Расширяем users — паспорт + должность + регистрация.
    op.add_column('users', sa.Column('position', sa.String(255), nullable=True))
    op.add_column('users', sa.Column('passport_series', sa.String(20), nullable=True))
    op.add_column('users', sa.Column('passport_number', sa.String(20), nullable=True))
    op.add_column('users', sa.Column('passport_issued_by', sa.String(500), nullable=True))
    op.add_column('users', sa.Column('passport_issued_at', sa.Date(), nullable=True))
    op.add_column('users', sa.Column('registration_date', sa.Date(), nullable=True))
    op.add_column('users', sa.Column('full_name', sa.String(255), nullable=True))
    # full_name — отдельное поле для «настоящего» ФИО жильца, если username
    # это логин/лицевой счёт. Если null — используем username как fallback.

    # 2. Семья
    op.create_table(
        'family_members',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        # 'spouse' | 'child' | 'parent' | 'other'
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('full_name', sa.String(255), nullable=False),
        sa.Column('birth_date', sa.Date(), nullable=True),
        sa.Column('passport_series', sa.String(20), nullable=True),
        sa.Column('passport_number', sa.String(20), nullable=True),
        sa.Column('registration_date', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    )
    op.create_index('idx_family_user', 'family_members', ['user_id'])

    # 3. Договоры найма (PDF в MinIO)
    op.create_table(
        'rental_contracts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.String(64), nullable=True),
        sa.Column('signed_date', sa.Date(), nullable=True),
        sa.Column('valid_until', sa.Date(), nullable=True),
        # Путь в MinIO: rental_contracts/<user_id>/<uuid>.pdf
        sa.Column('file_s3_key', sa.String(500), nullable=True),
        sa.Column('file_name', sa.String(255), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('uploaded_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], ondelete='SET NULL'),
    )
    op.create_index('idx_rental_user_active', 'rental_contracts',
                    ['user_id', 'is_active'])

    # 4. Заявки на справки
    op.create_table(
        'certificate_requests',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        # 'flc' (выписка из ФЛС). В будущем: 'residency' | 'composition' | ...
        sa.Column('type', sa.String(32), nullable=False, server_default='flc'),
        # 'pending' (жилец заказал) | 'generated' | 'delivered' | 'rejected'
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        # Все поля заявки: period, purpose, contract_id и т.д.
        sa.Column('data', JSONB(), nullable=True),
        # PDF хранится в MinIO: certificates/<user_id>/<uuid>.pdf
        sa.Column('pdf_s3_key', sa.String(500), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('processed_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['processed_by_id'], ['users.id'], ondelete='SET NULL'),
    )
    op.create_index('idx_cert_user_created', 'certificate_requests',
                    ['user_id', 'created_at'])
    op.create_index('idx_cert_status', 'certificate_requests', ['status'])


def downgrade() -> None:
    op.drop_index('idx_cert_status', table_name='certificate_requests')
    op.drop_index('idx_cert_user_created', table_name='certificate_requests')
    op.drop_table('certificate_requests')

    op.drop_index('idx_rental_user_active', table_name='rental_contracts')
    op.drop_table('rental_contracts')

    op.drop_index('idx_family_user', table_name='family_members')
    op.drop_table('family_members')

    op.drop_column('users', 'full_name')
    op.drop_column('users', 'registration_date')
    op.drop_column('users', 'passport_issued_at')
    op.drop_column('users', 'passport_issued_by')
    op.drop_column('users', 'passport_number')
    op.drop_column('users', 'passport_series')
    op.drop_column('users', 'position')
