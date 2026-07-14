"""reading_source_001: честный источник показания + метка правки админа.

Раньше источник ГАДАЛСЯ по anomaly_flags (_reading_source в admin_registry):
всё безфлаговое падало в «QR-портал», правки админа были неотличимы от подач
жильцов (жалоба пользователя 2026-07-14). Теперь:
- readings.source VARCHAR(16): qr | admin | gsheets | auto | excel | saldo;
- readings.admin_edited BOOL: админ правил запись после создания.

Бэкфилл по прежней эвристике (лучшее приближение для истории; правки админа
задним числом неотличимы — остаются admin_edited=false).
ALTER на партиционированном родителе PG распространяет на все партиции.
"""
from alembic import op

revision = "reading_source_001"
down_revision = "certs_purge_001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS source VARCHAR(16)")
    op.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS admin_edited BOOLEAN NOT NULL DEFAULT FALSE")
    # Бэкфилл истории по прежней эвристике флагов.
    op.execute("""
        UPDATE readings SET source = CASE
            WHEN source IS NOT NULL THEN source
            WHEN hot_water IS NULL AND cold_water IS NULL AND electricity IS NULL
                 AND coalesce(anomaly_flags, '') = '' THEN 'saldo'
            WHEN upper(coalesce(anomaly_flags, '')) LIKE '%GSHEETS%' THEN 'gsheets'
            WHEN upper(coalesce(anomaly_flags, '')) LIKE '%MANUAL_RECEIPT%' THEN 'admin'
            WHEN upper(coalesce(anomaly_flags, '')) LIKE '%AUTO_NORM%'
              OR upper(coalesce(anomaly_flags, '')) LIKE '%AUTO_AVG%'
              OR upper(coalesce(anomaly_flags, '')) LIKE '%AUTO_GENERATED%'
              OR upper(coalesce(anomaly_flags, '')) LIKE '%AUTO_NO_HISTORY%'
              OR upper(coalesce(anomaly_flags, '')) LIKE '%STATIC_RENT%'
              OR upper(coalesce(anomaly_flags, '')) LIKE '%NORM_UNCONDITIONAL%' THEN 'auto'
            ELSE 'qr'
        END
        WHERE source IS NULL
    """)


def downgrade():
    op.execute("ALTER TABLE readings DROP COLUMN IF EXISTS admin_edited")
    op.execute("ALTER TABLE readings DROP COLUMN IF EXISTS source")
