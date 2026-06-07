# app/modules/utility/services/period_resolver.py
"""Единый резолвер «периода для просмотра долгов/сальдо».

Долг/переплата 1С — снимок ОДНОГО периода. Раньше витрины (ЛК жильца,
админ-дашборд, /users/stats) жёстко брали активный период и показывали 0 в
межмесячном окне (период закрыт, следующий не открыт), хотя долги залиты в
закрытый период. financier уже решал это локальным _resolve_view_period —
теперь логика общая, чтобы все экраны показывали одну и ту же цифру.
"""
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.utility.models import BillingPeriod, DebtImportLog


async def resolve_view_period(db: AsyncSession, period_id: Optional[int] = None):
    """Период для просмотра сальдо: явный period_id → он; иначе активный;
    иначе период последнего импорта долгов; иначе самый свежий период.
    Возвращает BillingPeriod или None (если периодов нет вовсе)."""
    if period_id:
        return (await db.execute(
            select(BillingPeriod).where(BillingPeriod.id == period_id)
        )).scalars().first()
    active = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if active:
        return active
    last_imp = (await db.execute(
        select(DebtImportLog)
        .where(DebtImportLog.period_id.isnot(None))
        .order_by(desc(DebtImportLog.started_at))
        .limit(1)
    )).scalars().first()
    if last_imp and last_imp.period_id:
        p = await db.get(BillingPeriod, last_imp.period_id)
        if p:
            return p
    return (await db.execute(
        select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1)
    )).scalars().first()
