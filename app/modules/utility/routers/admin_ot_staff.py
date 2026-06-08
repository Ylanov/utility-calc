"""Админ-модуль «Охрана труда»: реестр сотрудников + сигналы по срокам
(обучение по ОТ, медосмотр). Только в платформе/админке.

Структура (подразделения/должности) засевается из Word-шаблонов
(POST /seed читает app/modules/utility/data/ot_staff_seed.json). ФИО и даты
заполняет админ; ФИО опционально привязывается к жильцу (users.username).

Сигнал = дата прохождения + периодичность → next = date + period; статус
overdue (просрочено) / soon (≤30 дней) / ok / none (нет даты).
"""
from __future__ import annotations

import calendar
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import OtStaff, User
from app.modules.utility.routers.admin_dashboard import write_audit_log

router = APIRouter(prefix="/api/admin/ot", tags=["Admin OT Staff"])
logger = logging.getLogger(__name__)

# Доступ — как у прочих админ-вкладок.
allow_ot = RoleChecker(["accountant", "admin", "financier"])

_SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "ot_staff_seed.json"
_SOON_DAYS = 30


# ───────────────────────── helpers ─────────────────────────
def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _next_and_status(last: Optional[date], period_months: Optional[int]):
    """(дата следующего прохождения, статус). Без даты → ('none')."""
    if not last or not period_months or period_months <= 0:
        return None, "none"
    nxt = _add_months(last, period_months)
    today = date.today()
    if nxt < today:
        return nxt, "overdue"
    if nxt <= today + timedelta(days=_SOON_DAYS):
        return nxt, "soon"
    return nxt, "ok"


def _to_dict(s: OtStaff) -> dict:
    next_ot, ot_status = _next_and_status(s.ot_training_date, s.ot_training_period_months)
    next_med, med_status = _next_and_status(s.medical_date, s.medical_period_months)
    return {
        "id": s.id,
        "source": s.source,
        "kes_group": s.kes_group,
        "department": s.department,
        "position": s.position,
        "sort_order": s.sort_order,
        "full_name": s.full_name,
        "user_id": s.user_id,
        "birth_date": s.birth_date.isoformat() if s.birth_date else None,
        "sout_date": s.sout_date.isoformat() if s.sout_date else None,
        "sout_class": s.sout_class,
        "induction_date": s.induction_date.isoformat() if s.induction_date else None,
        "ot_instructions_date": s.ot_instructions_date.isoformat() if s.ot_instructions_date else None,
        "internship_date": s.internship_date.isoformat() if s.internship_date else None,
        "siz_note": s.siz_note,
        "eb_group": s.eb_group,
        "ot_training_date": s.ot_training_date.isoformat() if s.ot_training_date else None,
        "ot_training_period_months": s.ot_training_period_months,
        "medical_date": s.medical_date.isoformat() if s.medical_date else None,
        "medical_type": s.medical_type,
        "medical_period_months": s.medical_period_months,
        "note": s.note,
        "is_active": s.is_active,
        # вычисляемые сигналы
        "next_ot_training": next_ot.isoformat() if next_ot else None,
        "ot_status": ot_status,
        "next_medical": next_med.isoformat() if next_med else None,
        "medical_status": med_status,
    }


# ───────────────────────── schemas ─────────────────────────
class OtStaffPayload(BaseModel):
    source: Optional[str] = None
    kes_group: Optional[str] = None
    department: Optional[str] = None
    position: Optional[str] = None
    sort_order: Optional[int] = None
    full_name: Optional[str] = None
    user_id: Optional[int] = None
    birth_date: Optional[date] = None
    sout_date: Optional[date] = None
    sout_class: Optional[str] = None
    induction_date: Optional[date] = None
    ot_instructions_date: Optional[date] = None
    internship_date: Optional[date] = None
    siz_note: Optional[str] = None
    eb_group: Optional[str] = None
    ot_training_date: Optional[date] = None
    ot_training_period_months: Optional[int] = None
    medical_date: Optional[date] = None
    medical_type: Optional[str] = None
    medical_period_months: Optional[int] = None
    note: Optional[str] = None
    is_active: Optional[bool] = None


# ───────────────────────── endpoints ─────────────────────────
@router.get("/staff", dependencies=[Depends(allow_ot)])
async def list_staff(
    source: Optional[str] = Query(None),
    kes_group: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="overdue|soon|due (overdue+soon)"),
    q: Optional[str] = Query(None, description="ФИО/должность/подразделение"),
    db: AsyncSession = Depends(get_db),
):
    """Реестр сотрудников с вычисленными сигналами. Фильтр status считается
    в Python (зависит от текущей даты)."""
    query = select(OtStaff).where(OtStaff.is_active.is_(True))
    if source:
        query = query.where(OtStaff.source == source)
    if kes_group:
        query = query.where(OtStaff.kes_group == kes_group)
    if department:
        query = query.where(OtStaff.department == department)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(
            OtStaff.full_name.ilike(like),
            OtStaff.position.ilike(like),
            OtStaff.department.ilike(like),
        ))
    query = query.order_by(OtStaff.source, OtStaff.kes_group,
                           OtStaff.department, OtStaff.sort_order, OtStaff.id)
    rows = (await db.execute(query)).scalars().all()
    items = [_to_dict(s) for s in rows]
    if status in ("overdue", "soon"):
        items = [i for i in items
                 if i["ot_status"] == status or i["medical_status"] == status]
    elif status == "due":
        items = [i for i in items
                 if i["ot_status"] in ("overdue", "soon")
                 or i["medical_status"] in ("overdue", "soon")]
    return {"items": items, "total": len(items)}


@router.get("/summary", dependencies=[Depends(allow_ot)])
async def summary(db: AsyncSession = Depends(get_db)):
    """KPI и сигналы: сколько просрочено/скоро по обучению ОТ и медосмотру."""
    rows = (await db.execute(
        select(OtStaff).where(OtStaff.is_active.is_(True))
    )).scalars().all()
    items = [_to_dict(s) for s in rows]
    total = len(items)
    filled = sum(1 for i in items if (i["full_name"] or "").strip())

    def _bucket(key: str) -> dict:
        return {st: sum(1 for i in items if i[key] == st)
                for st in ("overdue", "soon", "ok", "none")}

    # Список «к действию» (просрочено/скоро) — для панели сигналов.
    alerts = []
    for i in items:
        for kind, st_key, date_key in (
            ("Обучение ОТ", "ot_status", "next_ot_training"),
            ("Медосмотр", "medical_status", "next_medical"),
        ):
            if i[st_key] in ("overdue", "soon"):
                alerts.append({
                    "id": i["id"], "full_name": i["full_name"],
                    "position": i["position"], "department": i["department"],
                    "kind": kind, "status": i[st_key], "due": i[date_key],
                })
    alerts.sort(key=lambda a: (a["status"] != "overdue", a["due"] or ""))
    return {
        "total": total,
        "filled": filled,
        "vacant": total - filled,
        "ot_training": _bucket("ot_status"),
        "medical": _bucket("medical_status"),
        "alerts": alerts,
    }


@router.get("/structure", dependencies=[Depends(allow_ot)])
async def structure(db: AsyncSession = Depends(get_db)):
    """Списки источников/КЭС/подразделений для фильтров и группировки."""
    rows = (await db.execute(
        select(OtStaff.source, OtStaff.kes_group, OtStaff.department)
        .where(OtStaff.is_active.is_(True))
    )).all()
    sources = sorted({r[0] for r in rows if r[0]})
    kes = sorted({r[1] for r in rows if r[1]})
    depts = sorted({r[2] for r in rows if r[2]})
    return {"sources": sources, "kes_groups": kes, "departments": depts}


@router.post("/staff", dependencies=[Depends(allow_ot)])
async def create_staff(
    data: OtStaffPayload,
    current: User = Depends(allow_ot),
    db: AsyncSession = Depends(get_db),
):
    if not (data.position or "").strip():
        raise HTTPException(400, "Укажите должность")
    s = OtStaff(**data.model_dump(exclude_none=True))
    db.add(s)
    await db.commit()
    await db.refresh(s)
    await write_audit_log(db, current.id, current.username, action="create",
                          entity_type="ot_staff", entity_id=s.id,
                          details={"position": s.position, "full_name": s.full_name})
    await db.commit()
    return _to_dict(s)


@router.put("/staff/{staff_id}", dependencies=[Depends(allow_ot)])
async def update_staff(
    staff_id: int,
    data: OtStaffPayload,
    current: User = Depends(allow_ot),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(OtStaff, staff_id)
    if not s:
        raise HTTPException(404, "Запись не найдена")
    # Проверка привязки к жильцу.
    if data.user_id:
        u = await db.get(User, data.user_id)
        if not u:
            raise HTTPException(404, "Жилец для привязки не найден")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(s, field, value)
    await db.commit()
    await db.refresh(s)
    return _to_dict(s)


@router.delete("/staff/{staff_id}", dependencies=[Depends(allow_ot)])
async def delete_staff(
    staff_id: int,
    current: User = Depends(allow_ot),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(OtStaff, staff_id)
    if not s:
        raise HTTPException(404, "Запись не найдена")
    await db.delete(s)
    await db.commit()
    await write_audit_log(db, current.id, current.username, action="delete",
                          entity_type="ot_staff", entity_id=staff_id, details={})
    await db.commit()
    return {"status": "deleted"}


@router.get("/link-candidates", dependencies=[Depends(allow_ot)])
async def link_candidates(
    q: str = Query("", description="ФИО / часть ФИО для привязки к жильцу"),
    limit: int = Query(15, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Поиск жильцов по ФИО (users.username) — для привязки сотрудника."""
    qs = (q or "").strip()
    if len(qs) < 2:
        return {"items": []}
    rows = (await db.execute(
        select(User).where(
            User.is_deleted.is_(False),
            User.username.ilike(f"%{qs}%"),
        ).limit(limit)
    )).scalars().all()
    return {"items": [{"id": u.id, "username": u.username} for u in rows]}


@router.post("/seed", dependencies=[Depends(allow_ot)])
async def seed_structure(
    force: bool = Query(False, description="Удалить структурные строки без ФИО и пересеять"),
    current: User = Depends(allow_ot),
    db: AsyncSession = Depends(get_db),
):
    """Засев структуры (подразделения/должности) из Word-шаблонов.

    Идемпотентно: если в реестре уже есть строки и force=false — не трогаем.
    force=true удаляет структурные строки БЕЗ ФИО (full_name пуст) и пересевает
    — заполненные сотрудником записи сохраняются.
    """
    existing = (await db.execute(select(func.count(OtStaff.id)))).scalar_one()
    if existing and not force:
        return {"status": "skipped", "reason": "already_seeded", "count": existing}

    if not _SEED_PATH.exists():
        raise HTTPException(500, f"Файл засева не найден: {_SEED_PATH.name}")
    seed = json.loads(_SEED_PATH.read_text(encoding="utf-8"))

    if force:
        # Сносим только незаполненные структурные строки (без ФИО).
        await db.execute(
            OtStaff.__table__.delete().where(
                or_(OtStaff.full_name.is_(None), func.trim(OtStaff.full_name) == "")
            )
        )

    for r in seed:
        db.add(OtStaff(
            source=r.get("source"),
            kes_group=r.get("kes_group"),
            department=r.get("department"),
            position=r.get("position") or "—",
            sort_order=r.get("sort_order") or 0,
        ))
    await db.commit()
    await write_audit_log(db, current.id, current.username, action="seed",
                          entity_type="ot_staff", entity_id=None,
                          details={"inserted": len(seed), "force": force})
    await db.commit()
    return {"status": "seeded", "inserted": len(seed)}
