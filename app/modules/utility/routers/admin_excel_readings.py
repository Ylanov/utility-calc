"""Импорт показаний из Excel — превью с анализом + утверждение в финотчётность.

POST /api/admin/readings/excel/preview  — загрузить Excel, получить разбор по
                                          каждому жильцу (матч + анализаторы +
                                          предварительная сумма). Без записи.
POST /api/admin/readings/excel/commit   — утвердить отобранных → создаются
                                          is_approved=True MeterReading на период.
POST /api/admin/readings/excel/ensure-period — найти/создать период по имени
                                          (для селектора месяца в модалке).

Роль: accountant/admin (как у остального биллинга).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import BillingPeriod, User
from app.modules.utility.services import excel_readings_import as svc

router = APIRouter(prefix="/api/admin/readings/excel", tags=["Admin Excel Readings"])
allow_billing = RoleChecker(["accountant", "admin", "financier"])
logger = logging.getLogger(__name__)


@router.post("/preview")
async def excel_preview(
    file: UploadFile = File(...),
    period_id: Optional[int] = Form(None),
    current_user: User = Depends(allow_billing),
    db: AsyncSession = Depends(get_db),
):
    """Парсит Excel и возвращает повердиктный разбор по каждому жильцу.
    period_id — для корректных сумм (корректировки периода); опционален."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Поддерживаются только файлы Excel (.xlsx)")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Файл пустой")
    try:
        parsed = await asyncio.to_thread(svc.parse_readings_workbook, content)
    except Exception as e:  # noqa: BLE001
        logger.warning("[EXCEL-IMPORT] parse failed: %s", e)
        raise HTTPException(400, f"Не удалось прочитать Excel: {e}")
    if not parsed["people"]:
        raise HTTPException(
            400,
            "В файле не найдено строк с показаниями. Ожидаются листы «горячая»/"
            "«холодная»/«электричество» и колонки: ФИО | Предыдущий месяц | Текущий месяц.",
        )
    return await svc.build_preview(db, parsed, period_id)


class ExcelResource(BaseModel):
    prev: Optional[float] = None
    cur: Optional[float] = None


class ExcelDecision(BaseModel):
    user_id: int
    status: str = Field("submitted", pattern="^(submitted|norm)$")
    hot: Optional[ExcelResource] = None
    cold: Optional[ExcelResource] = None
    elect: Optional[ExcelResource] = None


class ExcelCommitBody(BaseModel):
    period_id: int
    decisions: list[ExcelDecision] = Field(..., max_length=5000)


@router.post("/commit")
async def excel_commit(
    body: ExcelCommitBody,
    current_user: User = Depends(allow_billing),
    db: AsyncSession = Depends(get_db),
):
    """Создаёт утверждённые показания за период по отобранным решениям.
    Падает сразу в финотчётность (is_approved=True, period_id)."""
    if not body.decisions:
        raise HTTPException(400, "Нет записей для утверждения")
    decisions = [d.model_dump() for d in body.decisions]
    return await svc.commit_import(db, body.period_id, decisions, current_user)


class EnsurePeriodBody(BaseModel):
    name: str = Field(..., min_length=3, max_length=50)


@router.post("/ensure-period")
async def ensure_period(
    body: EnsurePeriodBody,
    current_user: User = Depends(allow_billing),
    db: AsyncSession = Depends(get_db),
):
    """Найти период по имени или создать новый (для квитанций за выбранный
    месяц). Создаётся БЕЗ закрытия активного (аддитивно): is_active ставится
    только если активного периода ещё нет — иначе период исторический/целевой,
    финотчёт фильтрует по period_id независимо от is_active.

    Имя обязано парситься как «Месяц ГГГГ» (иначе хронология сломается)."""
    from app.modules.utility.services.period_helpers import parse_period_name

    name = body.name.strip()
    existing = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.name == name)
    )).scalars().first()
    if existing:
        return {"id": existing.id, "name": existing.name,
                "is_active": existing.is_active, "created": False}

    if parse_period_name(name) == (0, 0):
        raise HTTPException(
            400, "Имя периода должно быть в формате «Месяц ГГГГ», например «Май 2026».")

    has_active = (await db.execute(
        select(BillingPeriod.id).where(BillingPeriod.is_active.is_(True)).limit(1)
    )).scalars().first()

    period = BillingPeriod(name=name, is_active=not has_active)
    db.add(period)
    await db.commit()
    await db.refresh(period)

    from app.modules.utility.routers.admin_dashboard import write_audit_log
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="create_period_for_excel", entity_type="period", entity_id=period.id,
        details={"name": name, "is_active": period.is_active},
    )
    await db.commit()
    return {"id": period.id, "name": period.name,
            "is_active": period.is_active, "created": True}
