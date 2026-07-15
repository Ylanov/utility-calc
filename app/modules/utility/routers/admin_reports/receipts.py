# Квитанции админа: PDF одной квитанции (S3/статика) и прямой стриминг.
# Вербатим-перенос из admin_reports.py (строки 64-229), поведение 1:1.

import os
import shutil
import tempfile
import uuid
import asyncio
from urllib.parse import quote

from starlette.background import BackgroundTask
from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.utility.models import User, MeterReading, Tariff, Adjustment
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.s3_client import s3_service

from ._shared import logger, router


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
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
            # _build_receipt_context обращается к reading.room (тариф) —
            # без eager-load в async будет MissingGreenlet.
            selectinload(MeterReading.room),
        )
        .where(MeterReading.id == reading_id)
    )
    reading = (await db.execute(stmt)).scalars().first()

    if not reading or not reading.user or not reading.period or not reading.user.room:
        raise HTTPException(404, "Данные не найдены или жилец не привязан к помещению")

    user, room = reading.user, reading.user.room

    # ЕДИНЫЙ контекст квитанции (тариф/prev/корректировки) — тот же helper,
    # что у QR-портала. Раньше здесь был СВОЙ prev по created_at: запись того
    # же периода (debt-черновик 1С, повтор) становилась prev с теми же цифрами
    # → в PDF «Объём 0.00» при верных суммах (инцидент Мороз, июнь 2026).
    from app.modules.utility.routers.client_readings import _build_receipt_context
    tariff, prev, adjustments = await _build_receipt_context(reading, db)

    # tempfile.TemporaryDirectory вместо хардкода "/tmp" — Sonar
    # python:S5443. Изолированная dir с правами 700, удаление при выходе
    # автоматическое (включая случай exception). Возврат JSON, не
    # FileResponse, поэтому файл уже не нужен после return — БЕЗОПАСНО
    # очистить здесь же.
    try:
        with tempfile.TemporaryDirectory(prefix="utility_pdf_") as output_dir:
            pdf_path = await asyncio.to_thread(
                generate_receipt_pdf,
                user=user, room=room, reading=reading, period=reading.period,
                tariff=tariff, prev_reading=prev, adjustments=adjustments, output_dir=output_dir
            )
            s3_key = f"receipts/{reading.period.id}/admin_view_{user.id}_{uuid.uuid4().hex[:8]}.pdf"

            if await asyncio.to_thread(s3_service.upload_file, pdf_path, s3_key):
                # TemporaryDirectory сам удалит локальную копию, явный os.remove не нужен.
                download_url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
                return {"status": "success", "url": download_url}
            else:
                # S3 недоступен — копируем PDF в статику. Используем copy
                # (не move): TemporaryDirectory корректно удалит исходник.
                static_dir = "/app/static/generated_files"
                await asyncio.to_thread(os.makedirs, static_dir, exist_ok=True)
                filename = os.path.basename(pdf_path)
                static_path = os.path.join(static_dir, filename)
                await asyncio.to_thread(shutil.copy, pdf_path, static_path)
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

    # mkdtemp + BackgroundTask вместо "/tmp": Sonar python:S5443 +
    # попутно фикс утечки PDF в /tmp (раньше после стрима файл не
    # удалялся). FileResponse читает файл АСИНХРОННО уже после возврата
    # из эндпоинта, поэтому with TemporaryDirectory здесь не подходит —
    # директория удалилась бы до того, как клиент успел скачать файл.
    output_dir = tempfile.mkdtemp(prefix="utility_pdf_")
    try:
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            user=user, room=room, reading=reading, period=reading.period,
            tariff=tariff, prev_reading=prev, adjustments=adjustments, output_dir=output_dir,
        )
    except Exception as e:
        shutil.rmtree(output_dir, ignore_errors=True)
        logger.error(f"PDF generation failed for reading_id={reading_id}: {e}", exc_info=True)
        raise HTTPException(500, "Ошибка генерации квитанции. Попробуйте позже.")

    if not os.path.exists(pdf_path):
        shutil.rmtree(output_dir, ignore_errors=True)
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
        # Cleanup срабатывает после того как Starlette отстримит весь файл клиенту.
        background=BackgroundTask(shutil.rmtree, output_dir, ignore_errors=True),
    )
