# app/modules/utility/routers/client_certificates.py
"""Клиентские endpoints для заказа справок и управления профилем.

Фича «Заказ справок», волна 1 (модели + API) и волна 2 (PDF + заказ).

Endpoints в этом файле:
    Профиль жильца (паспорт, должность, ФИО, регистрация):
        GET    /api/me/profile
        PUT    /api/me/profile

    Семья:
        GET    /api/me/family
        POST   /api/me/family
        PUT    /api/me/family/{member_id}
        DELETE /api/me/family/{member_id}

    Договоры найма (read-only для жильца, загружает админ):
        GET    /api/me/rental-contracts
        GET    /api/me/rental-contracts/{id}/download

    Заказ справок:
        GET    /api/me/certificates            — история заявок
        POST   /api/me/certificates            — новая заявка
        GET    /api/me/certificates/{id}/download — скачать готовый PDF

Админские endpoints — в отдельном admin_certificates.py (волна 3).
"""
from datetime import date, datetime
from typing import Optional, List, Literal
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.utility.models import (
    User, FamilyMember, RentalContract, CertificateRequest,
)

router = APIRouter(tags=["Client Certificates"])


# =========================================================================
# СХЕМЫ
# =========================================================================

class ProfileResponse(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    position: Optional[str] = None
    passport_series: Optional[str] = None
    passport_number: Optional[str] = None
    passport_issued_by: Optional[str] = None
    passport_issued_at: Optional[date] = None
    registration_date: Optional[date] = None
    registration_address: Optional[str] = None
    lives_alone: bool = False
    # Данные о комнате — жилец их видит, но не может править
    room: Optional[dict] = None

    class Config:
        from_attributes = True


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    position: Optional[str] = None
    passport_series: Optional[str] = None
    passport_number: Optional[str] = None
    passport_issued_by: Optional[str] = None
    passport_issued_at: Optional[date] = None
    registration_date: Optional[date] = None
    registration_address: Optional[str] = None
    lives_alone: Optional[bool] = None


class FamilyMemberSchema(BaseModel):
    id: Optional[int] = None
    role: Literal["spouse", "child", "parent", "other"]
    full_name: str = Field(..., min_length=2, max_length=255)
    birth_date: Optional[date] = None
    passport_series: Optional[str] = None
    passport_number: Optional[str] = None
    registration_date: Optional[date] = None
    arrival_date: Optional[date] = None
    # permanent | temporary
    registration_type: Optional[Literal["permanent", "temporary"]] = None
    relation_to_head: Optional[str] = Field(None, max_length=64)

    class Config:
        from_attributes = True


class RentalContractBrief(BaseModel):
    id: int
    number: Optional[str] = None
    signed_date: Optional[date] = None
    valid_until: Optional[date] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    is_active: bool
    uploaded_at: datetime

    class Config:
        from_attributes = True


class CertificateRequestCreate(BaseModel):
    type: Literal["flc"] = "flc"
    # Поля специфичные для ФЛС
    period_from: Optional[date] = None
    period_to: Optional[date] = None
    purpose: str = Field(..., min_length=2, max_length=500,
                         description="Куда предоставить справку")
    contract_id: Optional[int] = Field(
        None, description="ID договора найма (если null — берётся последний активный)"
    )


class CertificateRequestOut(BaseModel):
    id: int
    type: str
    status: str
    data: Optional[dict] = None
    has_pdf: bool
    created_at: datetime
    processed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# =========================================================================
# PROFILE
# =========================================================================

async def _load_user_with_room(db: AsyncSession, user_id: int) -> User:
    """Загружает User + eager-load комнаты.
    get_current_user в нашем проекте не подгружает room (это SELECT User
    без JOIN). Обращение к `current_user.room` в async-контексте ломает
    SQLAlchemy через MissingGreenlet → 500. Поэтому в любом endpoint,
    где нужна комната, пере-запрашиваем юзера с selectinload."""
    from sqlalchemy.orm import selectinload as _selectinload
    stmt = (
        select(User)
        .options(_selectinload(User.room))
        .where(User.id == user_id)
    )
    res = await db.execute(stmt)
    user = res.scalars().first()
    if not user:
        raise HTTPException(404, "Жилец не найден")
    return user


@router.get("/api/me/profile", response_model=ProfileResponse)
async def get_my_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Перезагружаем с eager-load комнаты — иначе current_user.room триггерит
    # lazy-load и падает с MissingGreenlet в async-контексте.
    user = await _load_user_with_room(db, current_user.id)
    room = None
    if user.room:
        room = {
            "id": user.room.id,
            "dormitory_name": user.room.dormitory_name,
            "room_number": user.room.room_number,
            "apartment_area": float(user.room.apartment_area or 0),
        }
    return ProfileResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        position=user.position,
        passport_series=user.passport_series,
        passport_number=user.passport_number,
        passport_issued_by=user.passport_issued_by,
        passport_issued_at=user.passport_issued_at,
        registration_date=user.registration_date,
        registration_address=user.registration_address,
        lives_alone=bool(user.lives_alone),
        room=room,
    )


@router.put("/api/me/profile", response_model=ProfileResponse)
async def update_my_profile(
    data: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Жилец сам заполняет паспортные данные и должность.
    Админ может позже поправить через другой endpoint (волна 3)."""
    # Подгружаем с eager room (тот же селект что в get_my_profile).
    user = await _load_user_with_room(db, current_user.id)
    upd = data.model_dump(exclude_unset=True)
    for k, v in upd.items():
        setattr(user, k, v)
    await db.commit()
    await db.refresh(user)
    # Возвращаем через тот же формат, что и GET
    return await get_my_profile(current_user, db)


# =========================================================================
# FAMILY
# =========================================================================

@router.get("/api/me/family", response_model=List[FamilyMemberSchema])
async def list_my_family(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(FamilyMember).where(FamilyMember.user_id == current_user.id)
        .order_by(FamilyMember.role, FamilyMember.birth_date.asc().nulls_last())
    )).scalars().all()
    return rows


@router.post("/api/me/family", response_model=FamilyMemberSchema)
async def add_family_member(
    data: FamilyMemberSchema,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if data.role == "spouse":
        # Не больше одного супруга/супруги — защищаемся от двойного ввода.
        existing = (await db.execute(
            select(FamilyMember).where(
                FamilyMember.user_id == current_user.id,
                FamilyMember.role == "spouse",
            )
        )).scalars().first()
        if existing:
            raise HTTPException(400, "Супруг(а) уже указан(а). Отредактируйте существующую запись.")

    member = FamilyMember(
        user_id=current_user.id,
        role=data.role,
        full_name=data.full_name.strip(),
        birth_date=data.birth_date,
        passport_series=data.passport_series,
        passport_number=data.passport_number,
        registration_date=data.registration_date,
        arrival_date=data.arrival_date,
        registration_type=data.registration_type,
        relation_to_head=(data.relation_to_head or "").strip() or None,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


@router.put("/api/me/family/{member_id}", response_model=FamilyMemberSchema)
async def update_family_member(
    member_id: int,
    data: FamilyMemberSchema,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    member = await db.get(FamilyMember, member_id)
    if not member or member.user_id != current_user.id:
        raise HTTPException(404, "Член семьи не найден")

    upd = data.model_dump(exclude_unset=True, exclude={"id"})
    for k, v in upd.items():
        setattr(member, k, v)
    await db.commit()
    await db.refresh(member)
    return member


@router.delete("/api/me/family/{member_id}", status_code=204)
async def delete_family_member(
    member_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    member = await db.get(FamilyMember, member_id)
    if not member or member.user_id != current_user.id:
        raise HTTPException(404, "Член семьи не найден")
    await db.delete(member)
    await db.commit()
    return None


# =========================================================================
# RENTAL CONTRACTS (жилец сам ведёт историю договоров)
# =========================================================================
#
# Раньше жильцу был доступен только read-only список — загружать и править
# мог только админ. Теперь жилец сам добавляет свой договор при первом
# заказе справки (номер + дата — обязательно, PDF-скан опционально).
# При переезде добавляет новый договор, старые не удаляются — остаются в
# истории. "Активный" = самый свежий по signed_date (или явно отмеченный).

# Валидация файла договора — те же правила, что в admin_certificates.
# Дублируем константы здесь, чтобы не тянуть import из admin-модуля в
# клиентский роутер (они логически разные scope'ы).
_MAX_CONTRACT_SIZE = 20 * 1024 * 1024  # 20 MB
_ALLOWED_CONTRACT_EXT = {"pdf", "jpg", "jpeg", "png", "tif", "tiff"}


class RentalContractCreate(BaseModel):
    number: str = Field(..., min_length=1, max_length=64,
                        description="Полный номер договора — обязательно")
    signed_date: date = Field(..., description="Дата заключения — обязательно")
    valid_until: Optional[date] = None
    note: Optional[str] = Field(None, max_length=2000)


class RentalContractUpdate(BaseModel):
    number: Optional[str] = Field(None, min_length=1, max_length=64)
    signed_date: Optional[date] = None
    valid_until: Optional[date] = None
    note: Optional[str] = Field(None, max_length=2000)


@router.get("/api/me/rental-contracts", response_model=List[RentalContractBrief])
async def list_my_contracts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(RentalContract)
        .where(RentalContract.user_id == current_user.id)
        .order_by(desc(RentalContract.signed_date).nulls_last(),
                  desc(RentalContract.uploaded_at))
    )).scalars().all()
    return rows


async def _deactivate_others(db: AsyncSession, user_id: int, except_id: Optional[int] = None) -> None:
    """Снять флаг is_active со всех договоров жильца (кроме явно указанного).
    Используется при создании/активации договора, чтобы у жильца всегда был
    не более одного активного."""
    stmt = (
        RentalContract.__table__.update()
        .where(RentalContract.user_id == user_id)
        .values(is_active=False)
    )
    if except_id is not None:
        stmt = stmt.where(RentalContract.id != except_id)
    await db.execute(stmt)


@router.post("/api/me/rental-contracts", response_model=RentalContractBrief)
async def create_my_contract(
    data: RentalContractCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Жилец создаёт новый договор (после переезда — просто добавляет).
    Новый договор становится активным, старые переходят в историю."""
    # Новый договор всегда активный. Гасим старые.
    await _deactivate_others(db, current_user.id)

    contract = RentalContract(
        user_id=current_user.id,
        number=data.number.strip(),
        signed_date=data.signed_date,
        valid_until=data.valid_until,
        note=(data.note or "").strip() or None,
        is_active=True,
        uploaded_by_id=current_user.id,
    )
    db.add(contract)
    await db.commit()
    await db.refresh(contract)
    return contract


@router.patch("/api/me/rental-contracts/{contract_id}", response_model=RentalContractBrief)
async def update_my_contract(
    contract_id: int,
    data: RentalContractUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Правка метаданных своего договора. Файл не трогаем — для замены
    нужно загрузить новый через POST upload-pdf."""
    contract = await db.get(RentalContract, contract_id)
    if not contract or contract.user_id != current_user.id:
        raise HTTPException(404, "Договор не найден")

    upd = data.model_dump(exclude_unset=True)
    for k, v in upd.items():
        if isinstance(v, str):
            v = v.strip() or None
        setattr(contract, k, v)
    await db.commit()
    await db.refresh(contract)
    return contract


@router.post("/api/me/rental-contracts/{contract_id}/activate", response_model=RentalContractBrief)
async def activate_my_contract(
    contract_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сделать конкретный договор активным (актуальным).
    Нужно только если жилец нашёл в истории старый договор и хочет его
    вернуть — обычно новые договоры активируются автоматически при создании."""
    contract = await db.get(RentalContract, contract_id)
    if not contract or contract.user_id != current_user.id:
        raise HTTPException(404, "Договор не найден")
    await _deactivate_others(db, current_user.id, except_id=contract.id)
    contract.is_active = True
    await db.commit()
    await db.refresh(contract)
    return contract


@router.delete("/api/me/rental-contracts/{contract_id}", status_code=204)
async def delete_my_contract(
    contract_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удаление своего договора. Файл из MinIO тоже чистим.
    Блокируем удаление активного — жилец должен сначала активировать другой,
    чтобы не остаться без договора на момент заказа справки."""
    contract = await db.get(RentalContract, contract_id)
    if not contract or contract.user_id != current_user.id:
        raise HTTPException(404, "Договор не найден")
    if contract.is_active:
        raise HTTPException(
            400,
            "Нельзя удалить активный договор. Сначала активируйте другой или "
            "создайте новый.",
        )
    if contract.file_s3_key:
        from app.modules.utility.services.s3_client import s3_service
        s3_service.delete_object(contract.file_s3_key)
    await db.delete(contract)
    await db.commit()
    return None


@router.post("/api/me/rental-contracts/{contract_id}/upload-pdf", response_model=RentalContractBrief)
async def upload_my_contract_pdf(
    contract_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Прикрепить/заменить PDF-скан к своему договору.
    Если к договору уже был прикреплён файл — старый удаляется из MinIO,
    новый занимает его место. Поддерживаются PDF и картинки сканов."""
    contract = await db.get(RentalContract, contract_id)
    if not contract or contract.user_id != current_user.id:
        raise HTTPException(404, "Договор не найден")

    if not file.filename:
        raise HTTPException(400, "Нет имени файла")
    ext = file.filename.rsplit(".", 1)[-1].lower().strip() if "." in file.filename else ""
    if ext not in _ALLOWED_CONTRACT_EXT:
        raise HTTPException(
            400,
            f"Недопустимый формат .{ext}. Разрешены: {', '.join(sorted(_ALLOWED_CONTRACT_EXT))}",
        )
    body = await file.read()
    if len(body) > _MAX_CONTRACT_SIZE:
        raise HTTPException(
            413,
            f"Файл слишком большой. Максимум {_MAX_CONTRACT_SIZE // 1024 // 1024} МБ.",
        )
    if not body:
        raise HTTPException(400, "Пустой файл")

    import uuid as _uuid
    from app.modules.utility.services.s3_client import s3_service

    # Старый файл — чистим, чтобы не плодить мусор в MinIO.
    if contract.file_s3_key:
        try:
            s3_service.delete_object(contract.file_s3_key)
        except Exception:
            # Не критично — новый файл всё равно перезапишет ссылку.
            pass

    s3_key = f"rental_contracts/{current_user.id}/{_uuid.uuid4().hex}.{ext}"
    ctype = "application/pdf" if ext == "pdf" else f"image/{ext}"
    if not s3_service.upload_bytes(body, s3_key, content_type=ctype):
        raise HTTPException(500, "Не удалось сохранить файл в хранилище")

    contract.file_s3_key = s3_key
    contract.file_name = file.filename
    contract.file_size = len(body)
    await db.commit()
    await db.refresh(contract)
    return contract


@router.get("/api/me/rental-contracts/{contract_id}/download")
async def download_my_contract(
    contract_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    contract = await db.get(RentalContract, contract_id)
    if not contract or contract.user_id != current_user.id:
        raise HTTPException(404, "Договор не найден")
    if not contract.file_s3_key:
        raise HTTPException(404, "Файл договора ещё не загружен")

    from app.modules.utility.services.s3_client import s3_service
    import io
    data = s3_service.download_fileobj(contract.file_s3_key)
    if data is None:
        raise HTTPException(500, "Не удалось получить файл из хранилища")

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{contract.file_name or "contract.pdf"}"'},
    )


# =========================================================================
# CERTIFICATE REQUESTS (заказ справок)
# =========================================================================

@router.get("/api/me/certificates", response_model=List[CertificateRequestOut])
async def list_my_certificates(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(CertificateRequest)
        .where(CertificateRequest.user_id == current_user.id)
        .order_by(desc(CertificateRequest.created_at))
        .limit(50)
    )).scalars().all()
    return [
        CertificateRequestOut(
            id=r.id, type=r.type, status=r.status, data=r.data,
            has_pdf=bool(r.pdf_s3_key),
            created_at=r.created_at, processed_at=r.processed_at,
        ) for r in rows
    ]


@router.post("/api/me/certificates", response_model=CertificateRequestOut)
async def create_certificate_request(
    data: CertificateRequestCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Жилец заказывает справку. Сразу генерируем PDF и сохраняем в MinIO.

    Если обязательные данные не заполнены (паспорт/ФИО) — отдаём 400 с
    понятным сообщением и списком недостающих полей. UI подскажет
    жильцу заполнить профиль и повторить.
    """
    # Pre-flight: проверяем что профиль и семья заполнены настолько, чтобы
    # PDF-генератор не упал.
    # Базовые поля: ФИО, паспорт, дата регистрации, адрес прописки.
    # Состав семьи: либо флаг lives_alone, либо хотя бы один FamilyMember
    # со ВСЕМИ обязательными полями (ФИО, дата рождения, дата прибытия,
    # тип регистрации, отношение к нанимателю) — как в образце выписки.
    missing = []
    if not (current_user.full_name or current_user.username):
        missing.append("ФИО")
    if not current_user.passport_series or not current_user.passport_number:
        missing.append("паспорт (серия и номер)")
    if not current_user.registration_date:
        missing.append("дата регистрации по месту жительства")
    if not (current_user.registration_address or "").strip():
        missing.append("адрес прописки по паспорту")

    # Проверка семьи: либо lives_alone, либо все члены семьи с полным набором полей.
    family_rows = (await db.execute(
        select(FamilyMember).where(FamilyMember.user_id == current_user.id)
    )).scalars().all()

    if not current_user.lives_alone:
        if not family_rows:
            missing.append(
                "состав семьи (добавьте членов семьи или отметьте «проживаю один»)"
            )
        else:
            incomplete = []
            for m in family_rows:
                problems = []
                if not (m.full_name or "").strip():
                    problems.append("ФИО")
                if not m.birth_date:
                    problems.append("дата рождения")
                if not m.arrival_date:
                    problems.append("дата прибытия")
                if not m.registration_type:
                    problems.append("тип регистрации")
                if not (m.relation_to_head or "").strip():
                    problems.append("отношение к нанимателю")
                if problems:
                    incomplete.append(
                        f"{m.full_name or '(без ФИО)'}: {', '.join(problems)}"
                    )
            if incomplete:
                missing.append(
                    "заполните данные членов семьи — " + "; ".join(incomplete)
                )

    if missing:
        raise HTTPException(
            400,
            detail={
                "message": "Для заказа справки нужно сначала заполнить профиль.",
                "missing_fields": missing,
            },
        )

    # Резолвим договор найма. Если явно указан — берём его, иначе последний активный.
    # Договор обязателен: без номера и даты справка ФЛС невалидна. Если
    # договор ещё не оформлен админом — возвращаем понятную ошибку, чтобы
    # жилец знал, что надо обратиться к администрации.
    contract: Optional[RentalContract] = None
    if data.contract_id:
        contract = await db.get(RentalContract, data.contract_id)
        if not contract or contract.user_id != current_user.id:
            raise HTTPException(400, "Указанный договор не найден или не принадлежит вам")
    else:
        contract = (await db.execute(
            select(RentalContract)
            .where(RentalContract.user_id == current_user.id,
                   RentalContract.is_active.is_(True))
            .order_by(desc(RentalContract.signed_date).nulls_last())
            .limit(1)
        )).scalars().first()

    if not contract or not contract.number or not contract.signed_date:
        raise HTTPException(
            400,
            detail={
                "message": (
                    "Для заказа справки нужен договор найма жилого помещения. "
                    "Обратитесь к администратору — он внесёт номер и дату договора."
                ),
                "missing_fields": ["договор найма (№ и дата)"],
            },
        )

    # Создаём запись в БД. Если PDF-генерация упадёт — запись останется в
    # status=pending, админ сможет досоздать через свою админку (волна 3).
    cert = CertificateRequest(
        user_id=current_user.id,
        type=data.type,
        status="pending",
        data={
            "period_from": data.period_from.isoformat() if data.period_from else None,
            "period_to": data.period_to.isoformat() if data.period_to else None,
            "purpose": data.purpose,
            "contract_id": contract.id if contract else None,
            "contract_number": contract.number if contract else None,
            "contract_signed_date": contract.signed_date.isoformat() if contract and contract.signed_date else None,
        },
    )
    db.add(cert)
    await db.flush()

    # Генерируем PDF
    try:
        from app.modules.utility.services.certificate_pdf import generate_flc_pdf
        from app.modules.utility.services.s3_client import s3_service

        # family_rows уже загружены выше при валидации — не перезапрашиваем.
        # Если lives_alone, в PDF идёт пустой список (отдельно отметим флаг).
        pdf_family = [] if current_user.lives_alone else family_rows

        pdf_bytes = generate_flc_pdf(
            user=current_user,
            family=pdf_family,
            contract=contract,
            period_from=data.period_from,
            period_to=data.period_to,
            purpose=data.purpose,
        )

        s3_key = f"certificates/{current_user.id}/{cert.id}.pdf"
        uploaded = s3_service.upload_bytes(pdf_bytes, s3_key, content_type="application/pdf")
        if uploaded:
            cert.pdf_s3_key = s3_key
            cert.status = "generated"
    except Exception as e:
        # Не ломаем заявку — PDF можно сгенерировать позже из админки.
        cert.note = f"PDF не сгенерирован: {str(e)[:300]}"

    await db.commit()
    await db.refresh(cert)
    return CertificateRequestOut(
        id=cert.id, type=cert.type, status=cert.status, data=cert.data,
        has_pdf=bool(cert.pdf_s3_key),
        created_at=cert.created_at, processed_at=cert.processed_at,
    )


@router.get("/api/me/certificates/{cert_id}/download")
async def download_my_certificate(
    cert_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cert = await db.get(CertificateRequest, cert_id)
    if not cert or cert.user_id != current_user.id:
        raise HTTPException(404, "Заявка не найдена")
    if not cert.pdf_s3_key:
        raise HTTPException(404, "PDF ещё не готов")

    from app.modules.utility.services.s3_client import s3_service
    import io
    data = s3_service.download_fileobj(cert.pdf_s3_key)
    if data is None:
        raise HTTPException(500, "Не удалось получить файл")

    filename = f"Zayavlenie_FLS_{cert.id}.pdf"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
