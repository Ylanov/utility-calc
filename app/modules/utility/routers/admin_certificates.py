# app/modules/utility/routers/admin_certificates.py
"""Договоры найма жильцов (CRUD + файлы) — зовёт вкладка «Жильцы».

Фича «Справки» ВЫРЕЗАНА целиком (2026-07-14, решение пользователя):
заявки/PDF/профиль/семья удалены (таблицы дропает certs_purge_001).
Договоры найма ОСТАЛИСЬ — они общие: их читает импорт долгов 1С
(debt_import) и отчёты (admin_reports), управляет вкладка «Жильцы».
"""
import asyncio
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import User, RentalContract
from app.modules.utility.routers.admin_dashboard import write_audit_log

router = APIRouter(prefix="/api/admin", tags=["Rental Contracts"])
allow_certs = RoleChecker(["accountant", "admin"])
allow_admin = RoleChecker(["admin"])


# =========================================================================
# ADMIN: ДОГОВОРЫ НАЙМА (ВОЛНА 4)
# Хранилище PDF договоров с привязкой к жильцу. Нужно чтобы при заказе
# справки ФЛС поля «дата/№ договора» автоматически подставлялись из
# активного договора. На одного жильца может быть несколько договоров
# (переезд между комнатами) — актуальный определяется по is_active.
# =========================================================================

MAX_CONTRACT_SIZE = 20 * 1024 * 1024  # 20 MB — стандартный скан договора в 2-3 МБ
ALLOWED_CONTRACT_EXT = {"pdf", "jpg", "jpeg", "png", "tif", "tiff"}


class RentalContractMetaUpdate(BaseModel):
    number: Optional[str] = None
    signed_date: Optional[date] = None
    valid_until: Optional[date] = None
    note: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/users/{user_id}/rental-contracts", dependencies=[Depends(allow_certs)])
async def admin_list_user_contracts(
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Все договоры жильца — отсортированы по дате подписания (свежие сверху)."""
    rows = (await db.execute(
        select(RentalContract).where(RentalContract.user_id == user_id)
        .order_by(desc(RentalContract.signed_date).nulls_last(),
                  desc(RentalContract.uploaded_at))
    )).scalars().all()
    return [
        {
            "id": c.id,
            "number": c.number,
            "signed_date": c.signed_date.isoformat() if c.signed_date else None,
            "valid_until": c.valid_until.isoformat() if c.valid_until else None,
            "file_name": c.file_name,
            "file_size": c.file_size,
            "note": c.note,
            "is_active": c.is_active,
            "uploaded_at": c.uploaded_at.isoformat() if c.uploaded_at else None,
        }
        for c in rows
    ]


@router.post("/users/{user_id}/rental-contracts", dependencies=[Depends(allow_certs)])
async def admin_upload_contract(
    user_id: int,
    # Договор = № + дата = обязательны. Без них справка ФЛС невалидна.
    # Файл можно не прикреплять (старые договоры могут быть только бумажные),
    # но метаданные должны быть всегда.
    number: str = Form(..., min_length=1, max_length=64),
    signed_date: date = Form(...),
    valid_until: Optional[date] = Form(None),
    note: Optional[str] = Form(None),
    activate: bool = Form(True, description="Сделать этот договор активным (деактивирует остальные)"),
    file: Optional[UploadFile] = File(None),
    current_user: User = Depends(allow_certs),
    db: AsyncSession = Depends(get_db),
):
    """Регистрирует договор найма (номер + дата обязательны, PDF-скан
    опционально) и при activate=True деактивирует остальные договоры этого
    жильца — так у нас всегда ровно один «текущий» договор."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Жилец не найден")

    s3_key: Optional[str] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None

    # Файл опциональный: если админ прикрепил — валидируем и загружаем.
    if file is not None and file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower().strip() if "." in file.filename else ""
        if ext not in ALLOWED_CONTRACT_EXT:
            raise HTTPException(
                400,
                f"Недопустимый формат .{ext}. Разрешены: {', '.join(sorted(ALLOWED_CONTRACT_EXT))}",
            )
        body = await file.read()
        if len(body) > MAX_CONTRACT_SIZE:
            raise HTTPException(
                413,
                f"Файл слишком большой. Максимум {MAX_CONTRACT_SIZE // 1024 // 1024} МБ.",
            )
        if not body:
            raise HTTPException(400, "Пустой файл")

        import uuid as _uuid
        from app.modules.utility.services.s3_client import s3_service
        s3_key = f"rental_contracts/{user_id}/{_uuid.uuid4().hex}.{ext}"
        ctype = "application/pdf" if ext == "pdf" else f"image/{ext}"
        if not await asyncio.to_thread(s3_service.upload_bytes, body, s3_key, content_type=ctype):
            raise HTTPException(500, "Не удалось сохранить файл в хранилище")
        file_name = file.filename
        file_size = len(body)

    # При activate=True — деактивируем старые договоры
    if activate:
        await db.execute(
            RentalContract.__table__.update()
            .where(RentalContract.user_id == user_id)
            .values(is_active=False)
        )

    contract = RentalContract(
        user_id=user_id,
        number=number.strip(),
        signed_date=signed_date,
        valid_until=valid_until,
        file_s3_key=s3_key,
        file_name=file_name,
        file_size=file_size,
        note=note,
        is_active=activate,
        uploaded_by_id=current_user.id,
    )
    db.add(contract)
    await write_audit_log(
        db, user_id=current_user.id, username=current_user.username,
        action="contract_upload", entity_type="rental_contract",
        details={
            "user_id": user_id,
            "number": number,
            "file_name": file_name,
            "size": file_size,
        },
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            f"У жильца уже есть договор №{number.strip()} — "
            f"отредактируйте существующий вместо создания дубля.",
        )
    await db.refresh(contract)
    return {
        "id": contract.id,
        "number": contract.number,
        "signed_date": contract.signed_date.isoformat() if contract.signed_date else None,
        "valid_until": contract.valid_until.isoformat() if contract.valid_until else None,
        "file_name": contract.file_name,
        "file_size": contract.file_size,
        "is_active": contract.is_active,
        "uploaded_at": contract.uploaded_at.isoformat(),
    }


@router.patch("/rental-contracts/{contract_id}", dependencies=[Depends(allow_certs)])
async def admin_update_contract(
    contract_id: int,
    data: RentalContractMetaUpdate,
    current_user: User = Depends(allow_certs),
    db: AsyncSession = Depends(get_db),
):
    """Редактирование метаданных договора (номер, даты, note, active-флаг).
    Сам файл не меняем — для подмены файла нужно загрузить новый договор."""
    contract = await db.get(RentalContract, contract_id)
    if not contract:
        raise HTTPException(404, "Договор не найден")

    upd = data.model_dump(exclude_unset=True)
    # is_active=True требует снять флаг с остальных договоров жильца
    if upd.get("is_active") is True and not contract.is_active:
        await db.execute(
            RentalContract.__table__.update()
            .where(
                RentalContract.user_id == contract.user_id,
                RentalContract.id != contract.id,
            )
            .values(is_active=False)
        )

    for k, v in upd.items():
        setattr(contract, k, v)

    await write_audit_log(
        db, user_id=current_user.id, username=current_user.username,
        action="contract_update", entity_type="rental_contract", entity_id=contract.id,
        details={"fields": list(upd.keys())},
    )
    await db.commit()
    return {"status": "ok"}


@router.delete("/rental-contracts/{contract_id}", dependencies=[Depends(allow_admin)], status_code=204)
async def admin_delete_contract(
    contract_id: int,
    current_user: User = Depends(allow_admin),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет договор (+ файл из MinIO). Только admin — контракт нужен как
    документ-основание для справок, обычный бухгалтер удалить не должен."""
    contract = await db.get(RentalContract, contract_id)
    if not contract:
        raise HTTPException(404, "Договор не найден")
    if contract.file_s3_key:
        from app.modules.utility.services.s3_client import s3_service
        await asyncio.to_thread(s3_service.delete_object, contract.file_s3_key)
    await write_audit_log(
        db, user_id=current_user.id, username=current_user.username,
        action="contract_delete", entity_type="rental_contract", entity_id=contract.id,
        details={"user_id": contract.user_id, "number": contract.number},
    )
    await db.delete(contract)
    await db.commit()
    return None


@router.get("/rental-contracts/{contract_id}/download", dependencies=[Depends(allow_certs)])
async def admin_download_contract(
    contract_id: int,
    db: AsyncSession = Depends(get_db),
):
    contract = await db.get(RentalContract, contract_id)
    if not contract:
        raise HTTPException(404, "Договор не найден")
    if not contract.file_s3_key:
        raise HTTPException(404, "Файл не загружен")

    from app.modules.utility.services.s3_client import s3_service
    import io
    data = await asyncio.to_thread(s3_service.download_fileobj, contract.file_s3_key)
    if data is None:
        raise HTTPException(500, "Файл не найден в хранилище")

    ext = (contract.file_name or "contract.pdf").rsplit(".", 1)[-1].lower()
    media_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{contract.file_name or "contract.pdf"}"'},
    )


@router.post("/rental-contracts/{contract_id}/activate", dependencies=[Depends(allow_certs)])
async def admin_activate_contract(
    contract_id: int,
    current_user: User = Depends(allow_certs),
    db: AsyncSession = Depends(get_db),
):
    """Сделать один договор активным, остальные этого жильца — неактивными."""
    contract = await db.get(RentalContract, contract_id)
    if not contract:
        raise HTTPException(404, "Договор не найден")

    await db.execute(
        RentalContract.__table__.update()
        .where(RentalContract.user_id == contract.user_id)
        .values(is_active=False)
    )
    contract.is_active = True

    await write_audit_log(
        db, user_id=current_user.id, username=current_user.username,
        action="contract_activate", entity_type="rental_contract", entity_id=contract.id,
        details={"user_id": contract.user_id},
    )
    await db.commit()
    return {"status": "ok"}
