# app/modules/utility/routers/admin_certificates.py
"""Админские endpoints для обработки заявок на справки (волна 3).

Админ может:
  * видеть все заявки с фильтрами (статус / тип / поиск / период);
  * открыть конкретную заявку со всеми данными жильца и семьи;
  * редактировать данные заявки (period / purpose / contract) — если жилец
    при заказе ошибся и просит поправить;
  * перегенерировать PDF после правок;
  * менять статус: generated → delivered (выдано), или reject с причиной;
  * редактировать профиль жильца (паспорт, должность, ФИО) и семью —
    если жилец что-то забыл указать.

Клиентские endpoints живут в client_certificates.py и никаких проверок
ролей не делают (get_current_user + автоматически свой user_id).
Здесь все endpoint-ы требуют роль accountant/admin.
"""
from datetime import date, datetime
from typing import Optional, List, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, or_, func
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import (
    User, FamilyMember, RentalContract, CertificateRequest, Room,
)
from app.modules.utility.routers.admin_dashboard import write_audit_log

router = APIRouter(prefix="/api/admin", tags=["Admin Certificates"])
allow_certs = RoleChecker(["accountant", "admin"])
allow_admin = RoleChecker(["admin"])


# =========================================================================
# СХЕМЫ
# =========================================================================

class CertListItem(BaseModel):
    id: int
    user_id: int
    username: str
    full_name: Optional[str] = None
    dormitory: Optional[str] = None
    room_number: Optional[str] = None
    type: str
    status: str
    data: Optional[dict] = None
    has_pdf: bool
    created_at: datetime
    processed_at: Optional[datetime] = None
    processed_by_username: Optional[str] = None


class CertDetail(BaseModel):
    id: int
    type: str
    status: str
    data: Optional[dict] = None
    has_pdf: bool
    note: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None
    processed_by_username: Optional[str] = None
    # Сведения о жильце
    user: dict
    family: List[dict]
    contract: Optional[dict] = None


class CertUpdate(BaseModel):
    purpose: Optional[str] = None
    period_from: Optional[date] = None
    period_to: Optional[date] = None
    contract_id: Optional[int] = None
    note: Optional[str] = None
    status: Optional[Literal["pending", "generated", "delivered", "rejected"]] = None


class AdminUserProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    position: Optional[str] = None
    passport_series: Optional[str] = None
    passport_number: Optional[str] = None
    passport_issued_by: Optional[str] = None
    passport_issued_at: Optional[date] = None
    registration_date: Optional[date] = None


class AdminFamilyMember(BaseModel):
    id: Optional[int] = None
    role: Literal["spouse", "child", "parent", "other"]
    full_name: str = Field(..., min_length=2, max_length=255)
    birth_date: Optional[date] = None
    passport_series: Optional[str] = None
    passport_number: Optional[str] = None
    registration_date: Optional[date] = None


# =========================================================================
# LIST / STATS / DETAIL
# =========================================================================

@router.get("/certificates/stats", dependencies=[Depends(allow_certs)])
async def certs_stats(db: AsyncSession = Depends(get_db)):
    """KPI-сводка для шапки админской вкладки «Справки»."""
    rows = (await db.execute(
        select(CertificateRequest.status, func.count(CertificateRequest.id))
        .group_by(CertificateRequest.status)
    )).all()
    by_status = {s: int(c) for s, c in rows}
    total = sum(by_status.values())
    last = (await db.execute(
        select(CertificateRequest).order_by(desc(CertificateRequest.created_at)).limit(1)
    )).scalars().first()
    return {
        "total": total,
        "pending": by_status.get("pending", 0),
        "generated": by_status.get("generated", 0),
        "delivered": by_status.get("delivered", 0),
        "rejected": by_status.get("rejected", 0),
        "last_at": last.created_at.isoformat() if last and last.created_at else None,
    }


@router.get("/certificates", dependencies=[Depends(allow_certs)])
async def list_certs(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    status: Optional[str] = Query(None, pattern="^(pending|generated|delivered|rejected)$"),
    cert_type: Optional[str] = Query(None, alias="type"),
    search: Optional[str] = Query(None, description="ФИО / username / номер комнаты"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Список заявок с фильтрами + пагинация."""
    q = (
        select(CertificateRequest, User, Room)
        .join(User, User.id == CertificateRequest.user_id)
        .outerjoin(Room, Room.id == User.room_id)
    )
    if status:
        q = q.where(CertificateRequest.status == status)
    if cert_type:
        q = q.where(CertificateRequest.type == cert_type)
    if date_from:
        q = q.where(CertificateRequest.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        q = q.where(CertificateRequest.created_at <= datetime.combine(date_to, datetime.max.time()))
    if search:
        sv = f"%{search.lower()}%"
        q = q.where(or_(
            func.lower(User.username).like(sv),
            func.lower(User.full_name).like(sv),
            func.lower(Room.room_number).like(sv),
            func.lower(Room.dormitory_name).like(sv),
        ))

    # count — отдельно, без JOIN-а если можно
    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = q.order_by(desc(CertificateRequest.created_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(q)).all()

    # Processed_by — в отдельном запросе для избежания N+1
    processed_ids = {r[0].processed_by_id for r in rows if r[0].processed_by_id}
    processed_map = {}
    if processed_ids:
        users = (await db.execute(
            select(User.id, User.username).where(User.id.in_(processed_ids))
        )).all()
        processed_map = {uid: name for uid, name in users}

    items = []
    for cert, user, room in rows:
        items.append({
            "id": cert.id,
            "user_id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "dormitory": room.dormitory_name if room else None,
            "room_number": room.room_number if room else None,
            "type": cert.type,
            "status": cert.status,
            "data": cert.data,
            "has_pdf": bool(cert.pdf_s3_key),
            "created_at": cert.created_at,
            "processed_at": cert.processed_at,
            "processed_by_username": processed_map.get(cert.processed_by_id),
        })

    return {"total": total, "page": page, "size": limit, "items": items}


@router.get("/certificates/{cert_id}", dependencies=[Depends(allow_certs)])
async def get_cert_detail(cert_id: int, db: AsyncSession = Depends(get_db)):
    cert = await db.get(CertificateRequest, cert_id)
    if not cert:
        raise HTTPException(404, "Заявка не найдена")

    user = await db.get(User, cert.user_id, options=[selectinload(User.room)])
    if not user:
        raise HTTPException(404, "Жилец заявки не найден")

    family = (await db.execute(
        select(FamilyMember).where(FamilyMember.user_id == cert.user_id)
        .order_by(FamilyMember.role, FamilyMember.birth_date.asc().nulls_last())
    )).scalars().all()

    contract = None
    contract_id = (cert.data or {}).get("contract_id")
    if contract_id:
        contract_obj = await db.get(RentalContract, contract_id)
        if contract_obj:
            contract = {
                "id": contract_obj.id,
                "number": contract_obj.number,
                "signed_date": contract_obj.signed_date.isoformat() if contract_obj.signed_date else None,
                "valid_until": contract_obj.valid_until.isoformat() if contract_obj.valid_until else None,
                "file_name": contract_obj.file_name,
            }

    processed_by_username = None
    if cert.processed_by_id:
        pu = await db.get(User, cert.processed_by_id)
        processed_by_username = pu.username if pu else None

    return {
        "id": cert.id,
        "type": cert.type,
        "status": cert.status,
        "data": cert.data,
        "has_pdf": bool(cert.pdf_s3_key),
        "note": cert.note,
        "created_at": cert.created_at,
        "processed_at": cert.processed_at,
        "processed_by_username": processed_by_username,
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "position": user.position,
            "passport_series": user.passport_series,
            "passport_number": user.passport_number,
            "passport_issued_by": user.passport_issued_by,
            "passport_issued_at": user.passport_issued_at.isoformat() if user.passport_issued_at else None,
            "registration_date": user.registration_date.isoformat() if user.registration_date else None,
            "room": (
                {
                    "id": user.room.id,
                    "dormitory_name": user.room.dormitory_name,
                    "room_number": user.room.room_number,
                    "apartment_area": float(user.room.apartment_area or 0),
                }
                if user.room else None
            ),
        },
        "family": [
            {
                "id": m.id, "role": m.role, "full_name": m.full_name,
                "birth_date": m.birth_date.isoformat() if m.birth_date else None,
                "passport_series": m.passport_series, "passport_number": m.passport_number,
                "registration_date": m.registration_date.isoformat() if m.registration_date else None,
            }
            for m in family
        ],
        "contract": contract,
    }


@router.patch("/certificates/{cert_id}", dependencies=[Depends(allow_certs)])
async def update_cert(
    cert_id: int,
    data: CertUpdate,
    current_user: User = Depends(allow_certs),
    db: AsyncSession = Depends(get_db),
):
    """Редактирование полей заявки (purpose, период, договор) и/или статуса.
    Смена статуса → фиксируем processed_by/processed_at + audit log."""
    cert = await db.get(CertificateRequest, cert_id)
    if not cert:
        raise HTTPException(404, "Заявка не найдена")

    upd = data.model_dump(exclude_unset=True)
    status_changed = "status" in upd and upd["status"] != cert.status

    # Обновление полей заявки — data (JSON) хранит period/purpose/contract_id.
    payload = dict(cert.data or {})
    for k in ("purpose", "contract_id"):
        if k in upd and upd[k] is not None:
            payload[k] = upd[k]
    if "period_from" in upd:
        payload["period_from"] = upd["period_from"].isoformat() if upd["period_from"] else None
    if "period_to" in upd:
        payload["period_to"] = upd["period_to"].isoformat() if upd["period_to"] else None
    # Обновляем «снапшот» договора если админ переключил contract_id
    if "contract_id" in upd and upd["contract_id"]:
        new_contract = await db.get(RentalContract, upd["contract_id"])
        if new_contract and new_contract.user_id == cert.user_id:
            payload["contract_number"] = new_contract.number
            payload["contract_signed_date"] = (
                new_contract.signed_date.isoformat() if new_contract.signed_date else None
            )
    cert.data = payload

    if "note" in upd:
        cert.note = upd["note"]

    if status_changed:
        cert.status = upd["status"]
        cert.processed_by_id = current_user.id
        cert.processed_at = datetime.utcnow()
        await write_audit_log(
            db, user_id=current_user.id, username=current_user.username,
            action="cert_status_change", entity_type="certificate_request", entity_id=cert.id,
            details={"new_status": cert.status, "type": cert.type},
        )

    await db.commit()
    await db.refresh(cert)
    return {"status": "ok", "id": cert.id, "new_status": cert.status}


@router.post("/certificates/{cert_id}/regenerate", dependencies=[Depends(allow_certs)])
async def regenerate_cert_pdf(
    cert_id: int,
    current_user: User = Depends(allow_certs),
    db: AsyncSession = Depends(get_db),
):
    """Перегенерирует PDF по актуальным данным жильца/семьи/договора/заявки.
    Используется после правок в data заявки или в профиле жильца."""
    cert = await db.get(CertificateRequest, cert_id)
    if not cert:
        raise HTTPException(404, "Заявка не найдена")

    user = await db.get(User, cert.user_id)
    if not user:
        raise HTTPException(400, "Жилец заявки не найден")

    family = (await db.execute(
        select(FamilyMember).where(FamilyMember.user_id == cert.user_id)
    )).scalars().all()

    contract = None
    cid = (cert.data or {}).get("contract_id")
    if cid:
        contract = await db.get(RentalContract, cid)

    # Собираем период из data
    d = cert.data or {}
    period_from = date.fromisoformat(d["period_from"]) if d.get("period_from") else None
    period_to = date.fromisoformat(d["period_to"]) if d.get("period_to") else None
    purpose = d.get("purpose", "")

    try:
        from app.modules.utility.services.certificate_pdf import generate_flc_pdf
        from app.modules.utility.services.s3_client import s3_service

        pdf_bytes = generate_flc_pdf(
            user=user, family=family, contract=contract,
            period_from=period_from, period_to=period_to, purpose=purpose,
        )
        s3_key = f"certificates/{user.id}/{cert.id}.pdf"
        if not s3_service.upload_bytes(pdf_bytes, s3_key, content_type="application/pdf"):
            raise RuntimeError("Не удалось загрузить PDF в хранилище")
        cert.pdf_s3_key = s3_key
        if cert.status == "pending":
            cert.status = "generated"
        cert.processed_by_id = current_user.id
        cert.processed_at = datetime.utcnow()

        await write_audit_log(
            db, user_id=current_user.id, username=current_user.username,
            action="cert_regenerate", entity_type="certificate_request", entity_id=cert.id,
            details={"type": cert.type},
        )
        await db.commit()
        return {"status": "ok", "has_pdf": True}
    except Exception as e:
        raise HTTPException(500, f"Ошибка генерации: {e}")


@router.get("/certificates/{cert_id}/download", dependencies=[Depends(allow_certs)])
async def admin_download_cert(cert_id: int, db: AsyncSession = Depends(get_db)):
    cert = await db.get(CertificateRequest, cert_id)
    if not cert:
        raise HTTPException(404, "Заявка не найдена")
    if not cert.pdf_s3_key:
        raise HTTPException(404, "PDF ещё не сгенерирован")

    from app.modules.utility.services.s3_client import s3_service
    import io
    data = s3_service.download_fileobj(cert.pdf_s3_key)
    if data is None:
        raise HTTPException(500, "Файл не найден в хранилище")

    fname = f"Zayavlenie_FLS_{cert.id}.pdf"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.delete("/certificates/{cert_id}", dependencies=[Depends(allow_admin)])
async def delete_cert(
    cert_id: int,
    current_user: User = Depends(allow_admin),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет заявку и PDF. Только admin — для учёта."""
    cert = await db.get(CertificateRequest, cert_id)
    if not cert:
        raise HTTPException(404, "Заявка не найдена")

    if cert.pdf_s3_key:
        from app.modules.utility.services.s3_client import s3_service
        s3_service.delete_object(cert.pdf_s3_key)

    await write_audit_log(
        db, user_id=current_user.id, username=current_user.username,
        action="cert_delete", entity_type="certificate_request", entity_id=cert.id,
        details={"type": cert.type, "user_id": cert.user_id},
    )
    await db.delete(cert)
    await db.commit()
    return {"status": "ok"}


# =========================================================================
# ADMIN: редактирование профиля/семьи жильца
# (нужно когда жилец просит поправить паспорт или состав семьи)
# =========================================================================

@router.patch("/users/{user_id}/profile", dependencies=[Depends(allow_certs)])
async def admin_update_user_profile(
    user_id: int,
    data: AdminUserProfileUpdate,
    current_user: User = Depends(allow_certs),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Жилец не найден")

    upd = data.model_dump(exclude_unset=True)
    for k, v in upd.items():
        setattr(user, k, v)

    await write_audit_log(
        db, user_id=current_user.id, username=current_user.username,
        action="admin_edit_profile", entity_type="user", entity_id=user.id,
        details={"fields": list(upd.keys())},
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/users/{user_id}/family", dependencies=[Depends(allow_certs)])
async def admin_add_family(
    user_id: int,
    data: AdminFamilyMember,
    current_user: User = Depends(allow_certs),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Жилец не найден")
    member = FamilyMember(
        user_id=user.id,
        role=data.role,
        full_name=data.full_name.strip(),
        birth_date=data.birth_date,
        passport_series=data.passport_series,
        passport_number=data.passport_number,
        registration_date=data.registration_date,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return {"status": "ok", "id": member.id}


@router.put("/users/{user_id}/family/{member_id}", dependencies=[Depends(allow_certs)])
async def admin_update_family(
    user_id: int,
    member_id: int,
    data: AdminFamilyMember,
    db: AsyncSession = Depends(get_db),
):
    member = await db.get(FamilyMember, member_id)
    if not member or member.user_id != user_id:
        raise HTTPException(404, "Член семьи не найден")
    upd = data.model_dump(exclude_unset=True, exclude={"id"})
    for k, v in upd.items():
        setattr(member, k, v)
    await db.commit()
    return {"status": "ok"}


@router.delete("/users/{user_id}/family/{member_id}", dependencies=[Depends(allow_certs)], status_code=204)
async def admin_delete_family(
    user_id: int, member_id: int,
    db: AsyncSession = Depends(get_db),
):
    member = await db.get(FamilyMember, member_id)
    if not member or member.user_id != user_id:
        raise HTTPException(404, "Член семьи не найден")
    await db.delete(member)
    await db.commit()
    return None


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
    file: UploadFile = File(...),
    number: Optional[str] = Query(None),
    signed_date: Optional[date] = Query(None),
    valid_until: Optional[date] = Query(None),
    note: Optional[str] = Query(None),
    activate: bool = Query(True, description="Сделать этот договор активным (деактивирует остальные)"),
    current_user: User = Depends(allow_certs),
    db: AsyncSession = Depends(get_db),
):
    """Загружает PDF-договор в MinIO и создаёт запись в БД.
    При activate=True все остальные договоры этого жильца становятся is_active=False —
    так у нас всегда ровно один «текущий» договор на жильца."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Жилец не найден")

    # Валидация файла
    if not file.filename:
        raise HTTPException(400, "Нет имени файла")
    ext = file.filename.rsplit(".", 1)[-1].lower().strip() if "." in file.filename else ""
    if ext not in ALLOWED_CONTRACT_EXT:
        raise HTTPException(400, f"Недопустимый формат .{ext}. Разрешены: {', '.join(sorted(ALLOWED_CONTRACT_EXT))}")

    # Читаем байты + проверяем размер
    body = await file.read()
    if len(body) > MAX_CONTRACT_SIZE:
        raise HTTPException(413, f"Файл слишком большой. Максимум {MAX_CONTRACT_SIZE // 1024 // 1024} МБ.")
    if not body:
        raise HTTPException(400, "Пустой файл")

    # S3 upload
    import uuid as _uuid
    from app.modules.utility.services.s3_client import s3_service
    s3_key = f"rental_contracts/{user_id}/{_uuid.uuid4().hex}.{ext}"
    ctype = "application/pdf" if ext == "pdf" else f"image/{ext}"
    if not s3_service.upload_bytes(body, s3_key, content_type=ctype):
        raise HTTPException(500, "Не удалось сохранить файл в хранилище")

    # При activate=True — деактивируем старые договоры
    if activate:
        await db.execute(
            RentalContract.__table__.update()
            .where(RentalContract.user_id == user_id)
            .values(is_active=False)
        )

    contract = RentalContract(
        user_id=user_id,
        number=number,
        signed_date=signed_date,
        valid_until=valid_until,
        file_s3_key=s3_key,
        file_name=file.filename,
        file_size=len(body),
        note=note,
        is_active=activate,
        uploaded_by_id=current_user.id,
    )
    db.add(contract)
    await write_audit_log(
        db, user_id=current_user.id, username=current_user.username,
        action="contract_upload", entity_type="rental_contract",
        details={"user_id": user_id, "number": number, "file_name": file.filename, "size": len(body)},
    )
    await db.commit()
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
        s3_service.delete_object(contract.file_s3_key)
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
    data = s3_service.download_fileobj(contract.file_s3_key)
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
