"""Эндпоинты согласия жильца на обработку ПД + выгрузка/удаление данных.

По 152-ФЗ субъект ПД имеет право:
  - дать или отозвать согласие на обработку (ст. 9);
  - запросить информацию об обработке своих данных (ст. 14);
  - требовать уточнения, блокирования, удаления (ст. 14, 21).

Этот роутер реализует:
  - GET  /api/me/consent-status   — есть ли валидное согласие на текущую версию
  - POST /api/me/consent-pdn      — дать согласие (фиксируем IP + timestamp + version)
  - GET  /api/me/data-export      — выгрузить все ПД жильца (право доступа)
  - POST /api/me/data-deletion-request — заявка на удаление (попадает в очередь админу)

Все эндпоинты только для role=user (require_resident).
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import require_resident
from app.core.time_utils import utcnow
from app.modules.utility.models import (
    User, MeterReading, FamilyMember, RentalContract,
    CertificateRequest, Adjustment,
)
from app.modules.utility.routers.admin_dashboard import write_audit_log


router = APIRouter(prefix="/api/me", tags=["Client — Consent & Data Rights"])


# Текущая версия Политики обработки ПД. Должна совпадать с тем что в
# static/privacy.html (там тоже Версия 1.0). При значительных правках
# политики поднимаем здесь — пользователи будут вынуждены переподписать.
PDN_CURRENT_VERSION = "1.0"


def _client_ip(request: Request) -> str:
    """Достаём IP клиента с учётом proxy-chain (X-Forwarded-For).

    Внешний nginx (VPS) и внутренний (aleks) добавляют X-Forwarded-For —
    берём САМЫЙ ПЕРВЫЙ (изначальный клиент). Без proxy fallback на
    request.client.host. Truncate до 45 символов (IPv6 max).
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        if ip:
            return ip[:45]
    if request.client:
        return (request.client.host or "")[:45]
    return ""


# =========================================================================
# CONSENT STATUS
# =========================================================================
class ConsentStatusResponse(BaseModel):
    has_consent: bool
    current_version: str
    accepted_version: Optional[str] = None
    accepted_at: Optional[datetime] = None


@router.get("/consent-status", response_model=ConsentStatusResponse)
async def get_consent_status(
    current_user: User = Depends(require_resident),
):
    """Жилец проверяет — нужно ли ему подписать политику ПД.

    Используется PWA / порталом при первом входе. Если has_consent=False
    или accepted_version != current_version — показывается модалка.
    """
    accepted = current_user.pdn_consent_version
    has_valid = bool(accepted and accepted == PDN_CURRENT_VERSION)
    return ConsentStatusResponse(
        has_consent=has_valid,
        current_version=PDN_CURRENT_VERSION,
        accepted_version=accepted,
        accepted_at=current_user.pdn_consent_at,
    )


# =========================================================================
# CONSENT — accept
# =========================================================================
class ConsentAcceptRequest(BaseModel):
    version: str = Field(..., min_length=1, max_length=10)


@router.post("/consent-pdn")
async def accept_consent(
    body: ConsentAcceptRequest,
    request: Request,
    current_user: User = Depends(require_resident),
    db: AsyncSession = Depends(get_db),
):
    """Жилец подтверждает согласие на обработку ПД.

    Сохраняем в БД:
      - текущий timestamp (utcnow);
      - IP клиента (для аудита, требование 152-ФЗ);
      - версия принятой политики.

    Доп. логируем в audit_log — чтобы у админа было неотрицание (жилец
    не сможет потом сказать «я не соглашался»).
    """
    if body.version != PDN_CURRENT_VERSION:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Версия политики {body.version} устарела. "
                f"Текущая — {PDN_CURRENT_VERSION}. Перезагрузите страницу."
            ),
        )

    current_user.pdn_consent_at = utcnow()
    current_user.pdn_consent_ip = _client_ip(request)
    current_user.pdn_consent_version = body.version

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="pdn_consent_accept", entity_type="user", entity_id=current_user.id,
        details={
            "version": body.version,
            "ip": current_user.pdn_consent_ip,
        },
    )
    await db.commit()
    return {
        "status": "ok",
        "version": body.version,
        "accepted_at": current_user.pdn_consent_at.isoformat(),
    }


# =========================================================================
# DATA EXPORT (право доступа, 152-ФЗ ст. 14)
# =========================================================================
@router.get("/data-export")
async def export_my_data(
    current_user: User = Depends(require_resident),
    db: AsyncSession = Depends(get_db),
):
    """Жилец видит ВСЕ свои персональные данные, которые система о нём
    хранит. Один JSON со всеми связными сущностями.

    Цели:
      - Реализация ст. 14 152-ФЗ (право на доступ).
      - Прозрачность — жилец видит «как обо мне знает оператор».
      - Подготовка к удалению (видя что есть, осознанно решает).
    """
    # Перезапрашиваем юзера с relationship'ами одним запросом.
    u = (await db.execute(
        select(User).options(
            selectinload(User.room),
            selectinload(User.tariff),
        ).where(User.id == current_user.id)
    )).scalars().first()
    if not u:
        raise HTTPException(404, "Жилец не найден")

    # Семья, договоры, заявки на справки.
    family = (await db.execute(
        select(FamilyMember).where(FamilyMember.user_id == u.id)
    )).scalars().all()
    contracts = (await db.execute(
        select(RentalContract).where(RentalContract.user_id == u.id)
    )).scalars().all()
    cert_requests = (await db.execute(
        select(CertificateRequest).where(CertificateRequest.user_id == u.id)
    )).scalars().all()

    # Показания и корректировки (свёрнуто — только периоды + суммы,
    # без сырых hot/cold/elect значений — их жилец видит в /history).
    readings_count = (await db.execute(
        select(MeterReading).where(MeterReading.user_id == u.id)
    )).scalars().all()
    adjustments = (await db.execute(
        select(Adjustment).where(Adjustment.user_id == u.id)
    )).scalars().all()

    def _date(v):
        return v.isoformat() if v else None

    return {
        "exported_at": utcnow().isoformat(),
        "subject": {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "full_name": u.full_name,
            "position": u.position,
            "workplace": u.workplace,
            "residents_count": u.residents_count,
            "resident_type": u.resident_type,
            "billing_mode": u.billing_mode,
            "is_2fa_enabled": u.is_2fa_enabled,
            "registration_date": _date(u.registration_date),
            "registration_address": u.registration_address,
            "passport": {
                "series": u.passport_series,
                "number": u.passport_number,
                "issued_by": u.passport_issued_by,
                "issued_at": _date(u.passport_issued_at),
            },
            "pdn_consent": {
                "accepted_at": _date(u.pdn_consent_at),
                "version": u.pdn_consent_version,
                "ip": u.pdn_consent_ip,
            },
            "created_at": _date(u.created_at) if hasattr(u, "created_at") else None,
            "last_login_at": _date(u.last_login_at),
        },
        "room": {
            "dormitory_name": u.room.dormitory_name if u.room else None,
            "room_number": u.room.room_number if u.room else None,
            "apartment_area": float(u.room.apartment_area) if u.room and u.room.apartment_area else None,
        } if u.room else None,
        "tariff": {
            "id": u.tariff.id if u.tariff else None,
            "name": u.tariff.name if u.tariff else None,
        } if u.tariff else None,
        "family_members": [
            {
                "id": f.id,
                "full_name": f.full_name,
                "relation": getattr(f, "relation", None),
                "birth_date": _date(f.birth_date) if hasattr(f, "birth_date") else None,
            } for f in family
        ],
        "rental_contracts": [
            {
                "id": c.id,
                "number": c.number,
                "signed_date": _date(c.signed_date) if hasattr(c, "signed_date") else None,
                "is_active": c.is_active,
            } for c in contracts
        ],
        "certificate_requests": [
            {
                "id": r.id,
                "kind": getattr(r, "kind", None),
                "status": r.status,
                "created_at": _date(r.created_at) if hasattr(r, "created_at") else None,
            } for r in cert_requests
        ],
        "readings_count": len(readings_count),
        "adjustments_count": len(adjustments),
    }


# =========================================================================
# DATA DELETION REQUEST (право требовать удаления, 152-ФЗ ст. 21)
# =========================================================================
class DeletionRequestBody(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


@router.post("/data-deletion-request")
async def request_deletion(
    body: DeletionRequestBody,
    current_user: User = Depends(require_resident),
    db: AsyncSession = Depends(get_db),
):
    """Жилец подаёт заявку на удаление своих ПД.

    ВАЖНО: не удаляем сразу автоматически — это бы создало юридические
    проблемы (квитанции и расчёты должны храниться 5 лет по жилищному
    кодексу). Заявка попадает в audit_log с особым флагом — админ
    в своей очереди обработает её вручную (анонимизирует ФИО/паспорт,
    оставив только обезличенный финансовый след).
    """
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="data_deletion_request", entity_type="user",
        entity_id=current_user.id,
        details={
            "reason": body.reason or "",
            "username": current_user.username,
            "full_name": current_user.full_name,
        },
    )
    await db.commit()
    return {
        "status": "received",
        "message": (
            "Заявка принята. Срок рассмотрения — до 30 дней с даты получения "
            "(ст. 14 152-ФЗ). С вами свяжутся по контактам, указанным в личном кабинете."
        ),
    }
