# app/modules/utility/routers/admin_reports.py

import io
import os
import uuid
import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from celery.result import AsyncResult
from openpyxl import Workbook
from decimal import Decimal
from urllib.parse import quote

from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment, Room
from app.core.dependencies import get_current_user
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.tasks import generate_receipt_task, start_bulk_receipt_generation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin Reports"])
ZERO = Decimal("0.00")


@router.get("/api/admin/receipts/{reading_id}")
async def get_receipt_pdf(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    stmt = (
        select(MeterReading)
        .options(selectinload(MeterReading.user).selectinload(User.room), selectinload(MeterReading.period))
        .where(MeterReading.id == reading_id)
    )
    reading = (await db.execute(stmt)).scalars().first()

    if not reading or not reading.user or not reading.period or not reading.user.room:
        raise HTTPException(404, "Данные не найдены или жилец не привязан к помещению")

    user, room = reading.user, reading.user.room

    # Тариф через единый сервис: Room.tariff_id → User.tariff_id → default.
    # tariff_cache использует in-memory кеш и учитывает приоритет комнатной привязки.
    from app.modules.utility.services.tariff_cache import tariff_cache
    tariff = tariff_cache.get_effective_tariff(user=user, room=room)
    if not tariff:
        tariff = (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()
        if not tariff:
            raise HTTPException(404, "Активный тариф не найден")

    prev = (await db.execute(
        select(MeterReading)
        .where(MeterReading.room_id == room.id, MeterReading.is_approved.is_(True),
               MeterReading.created_at < reading.created_at)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    adjustments = (await db.execute(
        select(Adjustment).where(Adjustment.user_id == user.id, Adjustment.period_id == reading.period_id)
    )).scalars().all()

    try:
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            user=user, room=room, reading=reading, period=reading.period,
            tariff=tariff, prev_reading=prev, adjustments=adjustments, output_dir="/tmp"
        )
        s3_key = f"receipts/{reading.period.id}/admin_view_{user.id}_{uuid.uuid4().hex[:8]}.pdf"

        if await asyncio.to_thread(s3_service.upload_file, pdf_path, s3_key):
            await asyncio.to_thread(os.remove, pdf_path)
            download_url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
            return {"status": "success", "url": download_url}
        else:
            # S3 недоступен — перемещаем PDF в статику и отдаём прямую ссылку
            import shutil
            static_dir = "/app/static/generated_files"
            await asyncio.to_thread(os.makedirs, static_dir, exist_ok=True)
            filename = os.path.basename(pdf_path)
            static_path = os.path.join(static_dir, filename)
            await asyncio.to_thread(shutil.move, pdf_path, static_path)
            return {"status": "success", "url": f"/generated_files/{filename}"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Ошибка генерации PDF: {e}")


@router.get(
    "/api/admin/receipts/{reading_id}/download",
    summary="Скачать PDF квитанции (streaming, без S3)",
)
async def stream_admin_receipt(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    """
    Стримит PDF квитанции конкретного жильца напрямую через FastAPI с правильными
    заголовками Content-Disposition.

    Этот эндпоинт заменяет старый двухшаговый процесс (GET JSON с url → window.open),
    в котором происходил редирект на portal.html при сбое S3/протухшем токене.
    Вызывается фронтендом через api.download(...) — всё идёт одним авторизованным
    запросом, без посредников.
    """
    if current_user.role not in ("accountant", "admin", "financier"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    stmt = (
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
        )
        .where(MeterReading.id == reading_id)
    )
    reading = (await db.execute(stmt)).scalars().first()

    if not reading or not reading.user or not reading.period or not reading.user.room:
        raise HTTPException(404, "Данные не найдены или жилец не привязан к помещению")

    user, room = reading.user, reading.user.room

    # Тариф через единый сервис (Room.tariff_id → User.tariff_id → default).
    from app.modules.utility.services.tariff_cache import tariff_cache
    tariff = tariff_cache.get_effective_tariff(user=user, room=room)
    if not tariff:
        tariff = (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()
        if not tariff:
            raise HTTPException(404, "Активный тариф не найден")

    prev = (await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(True),
            MeterReading.created_at < reading.created_at,
        ).order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    adjustments = (await db.execute(
        select(Adjustment).where(
            Adjustment.user_id == user.id,
            Adjustment.period_id == reading.period_id,
        )
    )).scalars().all()

    try:
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            user=user, room=room, reading=reading, period=reading.period,
            tariff=tariff, prev_reading=prev, adjustments=adjustments, output_dir="/tmp",
        )
    except Exception as e:
        logger.error(f"PDF generation failed for reading_id={reading_id}: {e}", exc_info=True)
        raise HTTPException(500, "Ошибка генерации квитанции. Попробуйте позже.")

    if not os.path.exists(pdf_path):
        raise HTTPException(500, "Не удалось получить файл квитанции на сервере")

    username_safe = (user.username.split("_deleted_")[0] if user.is_deleted else user.username)
    username_safe = username_safe.replace(" ", "_")
    room_label = (room.room_number or "room").replace(" ", "_")
    period_label = (reading.period.name or "period").replace(" ", "_")
    filename = f"Kvitanciya_{room_label}_{username_safe}_{period_label}.pdf"
    encoded_filename = quote(filename)

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f"attachment; filename*=utf-8''{encoded_filename}",
            "Cache-Control": "no-store, must-revalidate",
        },
    )


@router.get("/api/admin/export_report", summary="Скачать отчет Excel (XLSX)")
async def export_report(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(status_code=403)

    target_period_id = period_id
    if not target_period_id:
        active_period = (
            await db.execute(select(BillingPeriod).where(BillingPeriod.is_active.is_(True)))).scalars().first()
        if active_period:
            target_period_id = active_period.id
        else:
            last_closed = (
                await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))).scalars().first()
            if not last_closed:
                raise HTTPException(404, "Нет периодов для отчета")
            target_period_id = last_closed.id

    period = await db.get(BillingPeriod, target_period_id)
    if not period: raise HTTPException(404, "Выбранный период не найден")

    # ИСПРАВЛЕНИЕ: Используем потоковое чтение (yield_per) из БД, чтобы не грузить все в RAM
    statement = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id)
        .where(
            MeterReading.period_id == target_period_id,
            MeterReading.is_approved.is_(True)
        )
        .order_by(Room.dormitory_name, Room.room_number, User.username)
    ).execution_options(yield_per=1000)

    # ИСПРАВЛЕНИЕ: Используем write_only=True для потоковой записи Excel
    # (openpyxl не хранит документ в памяти, а пишет напрямую в байтовый буфер)
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Сводная ведомость")

    headers = ["Общежитие/Комната", "ФИО (Логин)", "Площадь", "Жильцов", "ГВС (руб)", "ХВС (руб)", "Водоотв. (руб)",
               "Электроэнергия (руб)", "Содержание (руб)", "Наем (руб)", "ТКО (руб)", "Отопление + ОДН (руб)",
               "Счет 209 (Комм.)", "Счет 205 (Найм)", "ИТОГО (руб)"]
    worksheet.append(headers)

    total_sum, total_209, total_205 = ZERO, ZERO, ZERO

    # Потоковое получение результатов
    result = await db.stream(statement)

    async for row in result:
        user, reading, room = row
        total_cost = Decimal(reading.total_cost or 0)
        t_209 = Decimal(reading.total_209 or 0)
        t_205 = Decimal(reading.total_205 or 0)

        total_sum += total_cost
        total_209 += t_209
        total_205 += t_205

        worksheet.append([
            f"{room.dormitory_name} / {room.room_number}",
            user.username.split("_deleted_")[0] if user.is_deleted else user.username,
            room.apartment_area,
            f"{user.residents_count}/{room.total_room_residents}",
            reading.cost_hot_water, reading.cost_cold_water, reading.cost_sewage, reading.cost_electricity,
            reading.cost_maintenance, reading.cost_social_rent, reading.cost_waste, reading.cost_fixed_part,
            t_209, t_205, total_cost
        ])

    worksheet.append([""] * 12 + ["ИТОГО:", total_209, total_205, total_sum])
    filename = f"Report_{period.name.replace(' ', '_')}.xlsx"
    encoded_filename = quote(filename)

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)

    return StreamingResponse(
        output,
        headers={'Content-Disposition': f"attachment; filename*=utf-8''{encoded_filename}"},
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@router.post("/api/admin/receipts/{reading_id}/generate")
async def start_receipt_generation(reading_id: int, current_user: User = Depends(get_current_user)):
    if current_user.role not in ("accountant", "admin"): raise HTTPException(status_code=403)
    return {"task_id": generate_receipt_task.delay(reading_id).id, "status": "processing"}


@router.get("/api/admin/tasks/{task_id}")
async def get_task_status(task_id: str, current_user: User = Depends(get_current_user)):
    task_result = AsyncResult(task_id)
    if task_result.state == 'PENDING':
        return {"state": "PENDING", "status": "Pending..."}
    elif task_result.state != 'FAILURE':
        result = task_result.result
        if isinstance(result, dict) and result.get("status") in ["done", "ok"]:
            if s3_key := result.get("s3_key"):
                url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
                return {"state": task_result.state, "status": "done", "download_url": url}
        return {"state": task_result.state, "result": result}
    else:
        return {"state": "FAILURE", "error": str(task_result.info)}


@router.post("/api/admin/reports/bulk-zip", summary="Сгенерировать ZIP архива квитанций")
async def create_bulk_zip(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"): raise HTTPException(status_code=403)

    target_period_id = period_id
    if not target_period_id:
        period = (await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))).scalars().first()
        if not period: raise HTTPException(404, "Нет периодов")
        target_period_id = period.id

    task = start_bulk_receipt_generation.delay(target_period_id)
    return {"task_id": task.id, "status": "processing", "period_id": target_period_id}


@router.get("/api/admin/summary")
async def get_accountant_summary(
        period_id: Optional[int] = Query(None),
        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"): raise HTTPException(status_code=403, detail="Доступ запрещен")

    # ИСПРАВЛЕНИЕ: Добавляем yield_per, чтобы не грузить весь массив в RAM разом.
    stmt = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id)
        .where(MeterReading.is_approved.is_(True))
    ).execution_options(yield_per=1000)

    if period_id:
        stmt = stmt.where(MeterReading.period_id == period_id)
    else:
        last_period = (
            await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))).scalars().first()
        if not last_period: return {}
        stmt = stmt.where(MeterReading.period_id == last_period.id)

    stmt = stmt.order_by(Room.dormitory_name, Room.room_number, User.username)

    summary = {}

    # Потоковое получение
    result = await db.stream(stmt)
    async for row in result:
        user, reading, room = row
        dorm = room.dormitory_name or "Без общежития"
        if dorm not in summary: summary[dorm] = []
        summary[dorm].append({
            "reading_id": reading.id, "user_id": user.id, "username": user.username, "area": room.apartment_area,
            "residents": user.residents_count, "hot": reading.cost_hot_water or 0, "cold": reading.cost_cold_water or 0,
            "sewage": reading.cost_sewage or 0, "electric": reading.cost_electricity or 0,
            "maintenance": reading.cost_maintenance or 0, "rent": reading.cost_social_rent or 0,
            "waste": reading.cost_waste or 0, "fixed": reading.cost_fixed_part or 0,
            "total_cost": reading.total_cost or 0, "total_209": reading.total_209 or 0,
            "total_205": reading.total_205 or 0,
            "date": reading.created_at.strftime("%Y-%m-%d %H:%M")
        })

    return summary


# =====================================================================
# SUMMARY v2 — расширенная сводка с финансовыми анализаторами
# =====================================================================
# Отличия от v1 (/api/admin/summary):
#   * Группировка по общежитиям + KPI на верхнем уровне
#   * Δ vs прошлый период для каждого жильца (рост/падение суммы)
#   * Sparkline за последние 6 периодов для каждого жильца
#   * Финансовые флаги от finance_analyzer (DEBT_GROWING и т.д.)
#   * Список MISSING_RECEIPT — жильцы без квитанции в этом периоде
#   * Поддержка фильтров: only_debtors, only_anomaly, only_overpaid, search
#
# Используется новой версткой «Финансовая отчётность» в админке.
@router.get("/api/admin/summary/v2")
async def get_accountant_summary_v2(
    period_id: Optional[int] = Query(None),
    only_debtors: bool = Query(False),
    only_overpaid: bool = Query(False),
    only_anomaly: bool = Query(False),
    only_missing: bool = Query(False),
    search: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(403, "Доступ запрещён")

    from app.modules.utility.services.finance_analyzer import (
        analyze_finance, FLAG_CATALOG,
    )

    # 1) Период
    if period_id:
        period = await db.get(BillingPeriod, period_id)
    else:
        period = (await db.execute(
            select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1)
        )).scalars().first()
    if not period:
        return {"period": None, "kpi": {}, "dormitories": [], "flag_catalog": FLAG_CATALOG}

    # 2) Тянем все утверждённые показания за этот период + жильцов с комнатой
    stmt = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id)
        .where(
            MeterReading.is_approved.is_(True),
            MeterReading.period_id == period.id,
            User.is_deleted.is_(False),
        )
        .order_by(Room.dormitory_name, Room.room_number, User.username)
    )
    rows = (await db.execute(stmt)).all()

    # 3) История за 6 предыдущих периодов — для sparkline и Δ.
    # Берём ВСЕ utility-readings этих жильцов одним запросом, фильтруем в Python:
    # это дешевле чем N запросов по жильцам.
    user_ids = [r[0].id for r in rows]
    history_map: dict[int, list[MeterReading]] = {uid: [] for uid in user_ids}
    if user_ids:
        prev_periods = (await db.execute(
            select(BillingPeriod)
            .where(BillingPeriod.id < period.id)
            .order_by(BillingPeriod.id.desc())
            .limit(6)
        )).scalars().all()
        prev_period_ids = [p.id for p in prev_periods]

        if prev_period_ids:
            hist_rows = (await db.execute(
                select(MeterReading)
                .where(
                    MeterReading.user_id.in_(user_ids),
                    MeterReading.period_id.in_(prev_period_ids),
                    MeterReading.is_approved.is_(True),
                )
            )).scalars().all()
            # Сортируем по period_id (старые → новые) внутри каждого жильца
            tmp: dict[int, list] = {}
            for hr in hist_rows:
                tmp.setdefault(hr.user_id, []).append(hr)
            for uid in user_ids:
                history_map[uid] = sorted(
                    tmp.get(uid, []),
                    key=lambda r: r.period_id or 0
                )

    # 4) MISSING_RECEIPT — жильцы с комнатой, но без MeterReading в этом периоде.
    missing_users = []
    if not only_debtors and not only_overpaid and not only_anomaly:
        # Только если фильтры не отсекают этот класс жильцов
        all_residents = (await db.execute(
            select(User, Room)
            .join(Room, User.room_id == Room.id)
            .where(
                User.is_deleted.is_(False),
                User.role == "user",
            )
        )).all()
        present = {r[0].id for r in rows}
        for user, room in all_residents:
            if user.id in present:
                continue
            if search and search.lower() not in (user.username or "").lower() \
                    and search not in (room.room_number or ""):
                continue
            missing_users.append((user, room))

    # 5) Сборка ответа
    grand_billed = Decimal("0")
    grand_debt = Decimal("0")
    grand_overpay = Decimal("0")
    flagged_count = 0

    by_dorm: dict[str, dict] = {}

    def _ensure_dorm(name: str) -> dict:
        if name not in by_dorm:
            by_dorm[name] = {
                "name": name,
                "residents": [],
                "total_billed": Decimal("0"),
                "total_debt": Decimal("0"),
                "total_overpay": Decimal("0"),
                "flagged_count": 0,
            }
        return by_dorm[name]

    for user, reading, room in rows:
        debt = (reading.debt_209 or 0) + (reading.debt_205 or 0)
        overpay = (reading.overpayment_209 or 0) + (reading.overpayment_205 or 0)
        debt = Decimal(str(debt))
        overpay = Decimal(str(overpay))
        cur_cost = Decimal(str(reading.total_cost or 0))

        # Поиск
        if search:
            s = search.lower().strip()
            if s not in (user.username or "").lower() and s not in (room.room_number or ""):
                continue

        # Фильтры
        if only_debtors and debt <= 0:
            continue
        if only_overpaid and overpay <= 0:
            continue

        history = history_map.get(user.id, [])
        prev_costs = [Decimal(str(h.total_cost or 0)) for h in history]
        prev_debts = [
            Decimal(str((h.debt_209 or 0) + (h.debt_205 or 0)))
            for h in history
        ]

        flags, fin_score = analyze_finance(
            user_id=user.id,
            residents_count=user.residents_count or 1,
            current_total_cost=cur_cost,
            current_debt=debt,
            current_overpayment=overpay,
            prev_costs=prev_costs,
            prev_debts=prev_debts,
            has_reading=True,
            resident_type=getattr(user, "resident_type", "family"),
            billing_mode=getattr(user, "billing_mode", "by_meter"),
        )

        if only_anomaly and not flags and not (reading.anomaly_flags or "").strip():
            continue

        # Δ vs прошлый
        delta_amount = None
        delta_percent = None
        if prev_costs:
            last = prev_costs[-1]
            delta_amount = float(cur_cost - last)
            if last > 0:
                delta_percent = float((cur_cost - last) / last * 100)

        # Sparkline: 6 точек (включая текущий — последняя)
        sparkline = [float(c) for c in prev_costs] + [float(cur_cost)]

        # Аномалии показаний (из anomaly_detector)
        meter_flags = [
            f.strip() for f in (reading.anomaly_flags or "").split(",")
            if f.strip() and f.strip() != "PENDING"
        ]

        d = _ensure_dorm(room.dormitory_name or "Без общежития")
        d["residents"].append({
            "user_id": user.id,
            "username": user.username,
            "room_number": room.room_number,
            "room_id": room.id,
            "area": float(room.apartment_area or 0),
            "residents_count": user.residents_count or 1,
            "reading_id": reading.id,
            "total_cost": float(cur_cost),
            "total_209": float(reading.total_209 or 0),
            "total_205": float(reading.total_205 or 0),
            "debt": float(debt),
            "overpayment": float(overpay),
            "delta_amount": delta_amount,
            "delta_percent": delta_percent,
            "sparkline": sparkline,
            "finance_flags": flags,
            "finance_score": fin_score,
            "meter_flags": meter_flags,
            "anomaly_score": int(reading.anomaly_score or 0),
            "created_at": reading.created_at.isoformat() if reading.created_at else None,
        })
        d["total_billed"] += cur_cost
        d["total_debt"] += debt
        d["total_overpay"] += overpay
        if flags or meter_flags:
            d["flagged_count"] += 1
            flagged_count += 1
        grand_billed += cur_cost
        grand_debt += debt
        grand_overpay += overpay

    # MISSING_RECEIPT добавляем как «жильцов без подачи»
    if missing_users and not only_anomaly and not only_debtors and not only_overpaid:
        for user, room in missing_users:
            d = _ensure_dorm(room.dormitory_name or "Без общежития")
            d["residents"].append({
                "user_id": user.id,
                "username": user.username,
                "room_number": room.room_number,
                "room_id": room.id,
                "area": float(room.apartment_area or 0),
                "residents_count": user.residents_count or 1,
                "reading_id": None,
                "total_cost": 0.0,
                "total_209": 0.0,
                "total_205": 0.0,
                "debt": 0.0,
                "overpayment": 0.0,
                "delta_amount": None,
                "delta_percent": None,
                "sparkline": [],
                "finance_flags": ["MISSING_RECEIPT"],
                "finance_score": 40,
                "meter_flags": [],
                "anomaly_score": 0,
                "created_at": None,
            })
            d["flagged_count"] += 1
            flagged_count += 1

    if only_missing:
        # отфильтровываем всё кроме MISSING_RECEIPT
        for dn in list(by_dorm.keys()):
            by_dorm[dn]["residents"] = [
                r for r in by_dorm[dn]["residents"]
                if "MISSING_RECEIPT" in (r.get("finance_flags") or [])
            ]
            if not by_dorm[dn]["residents"]:
                del by_dorm[dn]

    # Сортируем общежития по имени, жильцов внутри — по комнате
    dormitories_out = []
    for name in sorted(by_dorm.keys()):
        d = by_dorm[name]
        d["residents"].sort(key=lambda r: (r.get("room_number") or "", r.get("username") or ""))
        dormitories_out.append({
            "name": d["name"],
            "total_billed": float(d["total_billed"]),
            "total_debt": float(d["total_debt"]),
            "total_overpay": float(d["total_overpay"]),
            "flagged_count": d["flagged_count"],
            "residents_count": len(d["residents"]),
            "residents": d["residents"],
        })

    # Топ-должники / топ-плательщики (по всем общежитиям)
    all_residents = [r for d in dormitories_out for r in d["residents"]]
    top_debtors = sorted(
        [r for r in all_residents if r["debt"] > 0],
        key=lambda r: -r["debt"],
    )[:5]
    top_overpayers = sorted(
        [r for r in all_residents if r["overpayment"] > 0],
        key=lambda r: -r["overpayment"],
    )[:5]

    return {
        "period": {"id": period.id, "name": period.name, "is_active": period.is_active},
        "kpi": {
            "total_billed": float(grand_billed),
            "total_debt": float(grand_debt),
            "total_overpay": float(grand_overpay),
            "flagged_count": flagged_count,
            "residents_count": sum(d["residents_count"] for d in dormitories_out),
            "missing_count": len(missing_users),
        },
        "top_debtors": top_debtors,
        "top_overpayers": top_overpayers,
        "dormitories": dormitories_out,
        "flag_catalog": FLAG_CATALOG,
    }


# =========================================================================
# ДЕТАЛИ ПО ОДНОМУ ЖИЛЬЦУ (expandable-панель в «Финансовой отчётности»)
# =========================================================================
# Возвращает всё, что нужно показать в развёрнутой строке:
#   * основная квитанция за выбранный период (детализация по статьям, дельты)
#   * история показаний за 6 последних периодов (счётчики + дельты + источник)
#   * корректировки (Adjustment) за эти периоды с пояснениями
#   * активный договор найма (№/дата) — чтобы не лезть в другую вкладку
# Один запрос вместо N+1 — подтягиваем всё батчами.

def _infer_source_flag(anomaly_flags):
    if not anomaly_flags:
        return "manual"
    af = anomaly_flags.split(",")[0].strip().upper()
    if "GSHEETS" in af:
        return "gsheets"
    if "BASELINE" in af:
        return "baseline"
    if "ONE_TIME_CHARGE" in af:
        return "one_time"
    if "AUTO_GENERATED" in af:
        return "auto"
    if "INITIAL_SETUP" in af:
        return "initial"
    if "METER_CLOSED" in af or "METER_REPLACEMENT" in af:
        return "meter_op"
    return "app"


@router.get("/api/admin/residents/{user_id}/finance-detail")
async def get_resident_finance_detail(
    user_id: int,
    period_id: Optional[int] = Query(None, description="Период квитанции. По умолчанию — последний."),
    history_periods: int = Query(6, ge=1, le=24),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Детальная финансовая справка по жильцу для разворачивающейся панели.

    Используется в «Финансовой отчётности» — админ кликает на строку жильца
    и получает всю финансовую картину: текущая квитанция + история счётчиков
    за 6 мес + корректировки + договор.
    """
    if current_user.role not in ("accountant", "admin", "financier"):
        raise HTTPException(403, "Доступ запрещён")

    user = (await db.execute(
        select(User)
        .options(selectinload(User.room))
        .where(User.id == user_id, User.is_deleted.is_(False))
    )).scalars().first()
    if not user:
        raise HTTPException(404, "Жилец не найден")

    # 1) Определяем целевой период.
    if period_id:
        period = await db.get(BillingPeriod, period_id)
    else:
        period = (await db.execute(
            select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1)
        )).scalars().first()
    if not period:
        raise HTTPException(400, "Периоды не заведены — нет данных для показа")

    # 2) 6 последних периодов (включая текущий) для истории.
    periods = (await db.execute(
        select(BillingPeriod)
        .where(BillingPeriod.id <= period.id)
        .order_by(BillingPeriod.id.desc())
        .limit(history_periods)
    )).scalars().all()
    period_ids = [p.id for p in periods]
    period_name_map = {p.id: p.name for p in periods}

    # 3) Одним запросом — все approved показания жильца за эти периоды.
    #    По id-шникам забираем также `created_at` для Source-логики.
    readings = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.user_id == user_id,
            MeterReading.period_id.in_(period_ids) if period_ids else False,
        )
        .order_by(MeterReading.period_id.desc())
    )).scalars().all()

    # Для дельт по счётчикам нам нужно предыдущее approved показание по КОМНАТЕ
    # для каждого reading. Соберём всю approved-историю комнаты за охваченный
    # диапазон + немного запаса (ещё 1 период), чтобы найти prev для самого
    # раннего показания.
    # История approved ЖИЛЬЦА В ЭТОЙ КОМНАТЕ — не всей комнаты.
    # При смене жильца prev другого жильца не должен влиять на дельту.
    room_hist_raw = []
    if user.room_id and period_ids:
        room_hist_raw = (await db.execute(
            select(MeterReading)
            .where(
                MeterReading.user_id == user_id,
                MeterReading.room_id == user.room_id,
                MeterReading.is_approved.is_(True),
            )
            .order_by(MeterReading.created_at.asc())
        )).scalars().all()

    def _prev_for(reading):
        """Ближайшее approved показание этого жильца в этой комнате со временем СТРОГО раньше."""
        prev = None
        for rr in room_hist_raw:
            if rr.created_at and reading.created_at and rr.created_at < reading.created_at and rr.is_approved:
                prev = rr
            elif rr.created_at and reading.created_at and rr.created_at >= reading.created_at:
                break
        return prev

    # 4) Корректировки за эти периоды.
    adjustments = []
    if period_ids:
        adj_rows = (await db.execute(
            select(Adjustment)
            .where(Adjustment.user_id == user_id, Adjustment.period_id.in_(period_ids))
            .order_by(Adjustment.period_id.desc(), Adjustment.created_at.desc())
        )).scalars().all()
        for a in adj_rows:
            adjustments.append({
                "id": a.id,
                "period_id": a.period_id,
                "period_name": period_name_map.get(a.period_id),
                "amount": float(a.amount or 0),
                "description": a.description or "",
                "account_type": a.account_type or "209",
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })

    # 5) Договор найма — активный.
    from app.modules.utility.models import RentalContract
    contract_row = (await db.execute(
        select(RentalContract)
        .where(
            RentalContract.user_id == user_id,
            RentalContract.is_active.is_(True),
        )
        .limit(1)
    )).scalars().first()
    contract_data = None
    if contract_row:
        contract_data = {
            "id": contract_row.id,
            "number": contract_row.number,
            "signed_date": contract_row.signed_date.isoformat() if contract_row.signed_date else None,
            "valid_until": contract_row.valid_until.isoformat() if contract_row.valid_until else None,
            "has_file": bool(contract_row.file_s3_key),
            "file_name": contract_row.file_name,
        }

    # 6) Сборка истории показаний (6 периодов).
    readings_by_period = {r.period_id: r for r in readings}
    history = []
    for p in periods:
        r = readings_by_period.get(p.id)
        if r is None:
            history.append({
                "period_id": p.id,
                "period_name": p.name,
                "reading_id": None,
                "is_approved": False,
                "has_pdf": False,
                "source": None,
                "flags": [],
                "hot_water": None, "cold_water": None, "electricity": None,
                "delta_hot": None, "delta_cold": None, "delta_elect": None,
                "total_cost": None, "total_209": None, "total_205": None,
            })
            continue
        prev = _prev_for(r)
        p_hot = float(prev.hot_water or 0) if prev else 0.0
        p_cold = float(prev.cold_water or 0) if prev else 0.0
        p_elect = float(prev.electricity or 0) if prev else 0.0
        cur_hot = float(r.hot_water or 0)
        cur_cold = float(r.cold_water or 0)
        cur_elect = float(r.electricity or 0)
        flags_list = [f.strip() for f in (r.anomaly_flags or "").split(",") if f.strip()]
        history.append({
            "period_id": p.id,
            "period_name": p.name,
            "reading_id": r.id,
            "is_approved": bool(r.is_approved),
            "has_pdf": bool(getattr(r, "pdf_s3_key", None)),
            "source": _infer_source_flag(r.anomaly_flags),
            "flags": flags_list,
            "hot_water": cur_hot,
            "cold_water": cur_cold,
            "electricity": cur_elect,
            "delta_hot": cur_hot - p_hot if prev else None,
            "delta_cold": cur_cold - p_cold if prev else None,
            "delta_elect": cur_elect - p_elect if prev else None,
            "total_cost": float(r.total_cost or 0),
            "total_209": float(r.total_209 or 0),
            "total_205": float(r.total_205 or 0),
        })

    # 7) Детализация текущей квитанции.
    cur = readings_by_period.get(period.id)
    current = None
    if cur:
        prev = _prev_for(cur)
        current = {
            "reading_id": cur.id,
            "is_approved": bool(cur.is_approved),
            "source": _infer_source_flag(cur.anomaly_flags),
            "anomaly_flags": cur.anomaly_flags,
            "anomaly_score": int(cur.anomaly_score or 0),
            "hot_water": float(cur.hot_water or 0),
            "cold_water": float(cur.cold_water or 0),
            "electricity": float(cur.electricity or 0),
            "prev_hot": float(prev.hot_water or 0) if prev else 0.0,
            "prev_cold": float(prev.cold_water or 0) if prev else 0.0,
            "prev_elect": float(prev.electricity or 0) if prev else 0.0,
            "delta_hot": float((cur.hot_water or 0) - (prev.hot_water or 0)) if prev else None,
            "delta_cold": float((cur.cold_water or 0) - (prev.cold_water or 0)) if prev else None,
            "delta_elect": float((cur.electricity or 0) - (prev.electricity or 0)) if prev else None,
            "total_cost": float(cur.total_cost or 0),
            "total_209": float(cur.total_209 or 0),
            "total_205": float(cur.total_205 or 0),
            "debt_209": float(cur.debt_209 or 0),
            "debt_205": float(cur.debt_205 or 0),
            "overpayment_209": float(cur.overpayment_209 or 0),
            "overpayment_205": float(cur.overpayment_205 or 0),
            "hot_correction": float(cur.hot_correction or 0),
            "cold_correction": float(cur.cold_correction or 0),
            "electricity_correction": float(cur.electricity_correction or 0),
            "sewage_correction": float(cur.sewage_correction or 0),
            "costs": {
                "cost_hot_water": float(cur.cost_hot_water or 0),
                "cost_cold_water": float(cur.cost_cold_water or 0),
                "cost_sewage": float(cur.cost_sewage or 0),
                "cost_electricity": float(cur.cost_electricity or 0),
                "cost_maintenance": float(cur.cost_maintenance or 0),
                "cost_social_rent": float(cur.cost_social_rent or 0),
                "cost_waste": float(cur.cost_waste or 0),
                "cost_fixed_part": float(cur.cost_fixed_part or 0),
            },
            "has_pdf": bool(getattr(cur, "pdf_s3_key", None)),
        }

    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
            "residents_count": user.residents_count or 1,
            "resident_type": getattr(user, "resident_type", "family"),
            "billing_mode": getattr(user, "billing_mode", "by_meter"),
            "room": (
                {
                    "id": user.room.id,
                    "dormitory_name": user.room.dormitory_name,
                    "room_number": user.room.room_number,
                    "apartment_area": float(user.room.apartment_area or 0),
                    "total_room_residents": user.room.total_room_residents or 1,
                    "hw_meter_serial": getattr(user.room, "hw_meter_serial", None),
                    "cw_meter_serial": getattr(user.room, "cw_meter_serial", None),
                    "el_meter_serial": getattr(user.room, "el_meter_serial", None),
                }
                if user.room else None
            ),
        },
        "period": {"id": period.id, "name": period.name},
        "current": current,
        "history": history,  # от свежих к старым
        "adjustments": adjustments,
        "contract": contract_data,
    }