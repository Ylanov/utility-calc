"""Анонимный QR-портал по квартире (без ФИО/адреса).

/api/q/{token}/state  — что показать на портале (окно подачи, нужны ли счётчики,
                        текущий черновик для «исправить», статус квитанции).
/api/q/{token}/submit — подача показаний; резолв комнаты по токену →
                        «представитель комнаты» → ОБЩИЙ сервис perform_reading_submission
                        (тот же биллинг-путь, что и резидентская ручка).

Авторизация = сам токен (неугадываемый, QR внутри квартиры у счётчика).
Никаких ФИО/адресов в ответах. Фаза 2 добавит сюда receipt + contact.
"""
from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_password_hash, verify_password
from app.core.database import get_db
from app.modules.utility.models import (
    BillingPeriod, MeterReading, SupportTicket, User,
)
from app.modules.utility.schemas import ReadingSchema
from app.modules.utility.routers.client_readings import (
    perform_reading_submission, _is_submission_day_open,
    _build_receipt_context, generate_receipt_pdf,
)
from app.modules.utility.services.qr_portal import (
    QR_TICKET_SUBJECT, resolve_room_by_token, pick_representative_user_id,
)

router = APIRouter(prefix="/api/q", tags=["QR Portal (public)"])
logger = logging.getLogger(__name__)


async def _resolve_or_404(db: AsyncSession, token: str):
    room = await resolve_room_by_token(db, token)
    if not room:
        # Намеренно глухой 404 — не раскрываем, существует токен или нет.
        raise HTTPException(status_code=404, detail="Код не найден или больше не действует.")
    return room


async def _require_password(room, x_qr_key: str | None) -> None:
    """Парольный гейт портала. Пароль — второй фактор к токену (QR-наклейку
    могут сфотографировать посторонние). Хеш argon2 на Room.qr_password_hash.

    Не установлен → 403 password_setup_required (фронт покажет установку).
    Нет/неверный ключ → 401 password_required (фронт спросит пароль).
    Брутфорс упирается в nginx-rate-limit /api/q/ (8r/s) + медленный argon2.
    """
    if not room.qr_password_hash:
        raise HTTPException(status_code=403, detail="password_setup_required")
    ok = bool(x_qr_key) and await asyncio.to_thread(
        verify_password, x_qr_key, room.qr_password_hash
    )
    if not ok:
        raise HTTPException(status_code=401, detail="password_required")


def _is_house(room) -> bool:
    pt = getattr(room.place_type, "value", room.place_type)
    return pt == "house"


async def _active_period(db: AsyncSession):
    return (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()


@router.get("/{token}/state")
async def portal_state(
    token: str,
    db: AsyncSession = Depends(get_db),
    x_qr_key: str | None = Header(None, alias="X-QR-Key"),
):
    """Состояние портала для квартиры. Без ФИО/адреса.

    Пароль не установлен → отдаём ТОЛЬКО флаг установки (фронт покажет
    модалку «придумайте пароль»). Кто первый зашёл — тот и установил:
    для анонимного портала это неустранимо, но жилец сразу заметит
    (его попросят чужой пароль) и админ сбросит. Дальше — обычный гейт."""
    room = await _resolve_or_404(db, token)
    if not room.qr_password_hash:
        return {"password_setup_required": True}
    await _require_password(room, x_qr_key)
    period = await _active_period(db)
    rep_id = await pick_representative_user_id(db, room.id, period.id if period else None)

    # Нужно ли вообще подавать счётчики (дом / койко-место / тариф без счётчиков)?
    metered = not _is_house(room)
    rep = None
    charge = {"hot": True, "cold": True, "el": True}
    if rep_id:
        rep = (await db.execute(select(User).where(User.id == rep_id))).scalars().first()
        if rep and getattr(rep, "billing_mode", "by_meter") == "per_capita":
            metered = False
        if metered and rep:
            from app.modules.utility.services.tariff_cache import tariff_cache
            t = tariff_cache.get_effective_tariff(user=rep, room=room)
            if t:
                charge = {
                    "hot": bool(getattr(t, "charge_hot_water", True)),
                    "cold": bool(getattr(t, "charge_cold_water", True)),
                    "el": bool(getattr(t, "charge_electricity", True)),
                }
                if not any(charge.values()):
                    metered = False

    # Какие счётчики СПРАШИВАТЬ на портале: есть физически у комнаты
    # (Room.has_*_meter, приоритет комнаты — fallback на жильца, как в
    # биллинге) И начисляется тарифом. Дом «только вода» → электричество
    # не спрашиваем (его вносят электрики вручную через админку).
    def _has(attr: str) -> bool:
        rv = getattr(room, attr, None)
        if rv is not None:
            return bool(rv)
        return bool(getattr(rep, attr, True)) if rep else True
    meters = {
        "hot": bool(metered and charge["hot"] and _has("has_hw_meter")),
        "cold": bool(metered and charge["cold"] and _has("has_cw_meter")),
        "el": bool(metered and charge["el"] and _has("has_el_meter")),
    }
    if metered and not any(meters.values()):
        metered = False

    day_open, today_day, start_day, end_day = await _is_submission_day_open(db)

    # Текущий черновик периода (для предзаполнения формы «исправить») и
    # утверждённое показание (квитанция готова, форма заблокирована).
    draft = approved = None
    if period:
        draft = (await db.execute(
            select(MeterReading).where(
                MeterReading.room_id == room.id,
                MeterReading.period_id == period.id,
                MeterReading.is_approved.is_(False),
            )
        )).scalars().first()
        approved = (await db.execute(
            select(MeterReading).where(
                MeterReading.room_id == room.id,
                MeterReading.period_id == period.id,
                MeterReading.is_approved.is_(True),
            )
        )).scalars().first()

    cur = None
    src = draft or approved
    if src and metered:
        cur = {
            "hot_water": str(src.hot_water) if src.hot_water is not None else "",
            "cold_water": str(src.cold_water) if src.cold_water is not None else "",
            "electricity": str(src.electricity) if src.electricity is not None else "",
        }

    # Все утверждённые показания комнаты (свежие первыми). Из них:
    #  - latest_approved → доступность квитанции;
    #  - last_actual → последнее РЕАЛЬНО ПОДАННОЕ жильцом (is_meaningful_prev:
    #    не норматив/авто) — то, с чем сверяется монотонность (#2);
    #  - norm_since → периоды ПОСЛЕ него, где начислено по нормативу (пропуски).
    from app.modules.utility.services.reading_calculator import is_meaningful_prev
    approved_all = (await db.execute(
        select(MeterReading).options(selectinload(MeterReading.period))
        .where(MeterReading.room_id == room.id, MeterReading.is_approved.is_(True))
        .order_by(MeterReading.period_id.desc(), MeterReading.created_at.desc())
    )).scalars().all()
    latest_approved = approved_all[0] if approved_all else None

    last_actual_obj = next((r for r in approved_all if is_meaningful_prev(r)), None)
    last_actual = None
    if last_actual_obj and metered:
        last_actual = {
            "period": last_actual_obj.period.name if last_actual_obj.period else None,
            "hot_water": str(last_actual_obj.hot_water) if last_actual_obj.hot_water is not None else "—",
            "cold_water": str(last_actual_obj.cold_water) if last_actual_obj.cold_water is not None else "—",
            "electricity": str(last_actual_obj.electricity) if last_actual_obj.electricity is not None else "—",
        }
    norm_since = []
    if last_actual_obj:
        for r in approved_all:
            if (r.period_id and last_actual_obj.period_id
                    and r.period_id > last_actual_obj.period_id
                    and not is_meaningful_prev(r)):
                norm_since.append({
                    "period": r.period.name if r.period else None,
                    "amount": round(float(r.total_cost or 0), 2),
                })

    return {
        "period": period.name if period else None,
        "has_period": bool(period),
        "metered": metered,
        "meters": meters,            # какие счётчики спрашивать (hot/cold/el)
        "no_residents": rep_id is None,
        "window_open": day_open,
        "window": {"start": start_day, "end": end_day, "today": today_day},
        "submitted": bool(draft or approved),
        "approved": bool(approved),         # утверждено → правка закрыта, квитанция готова
        "editable": bool(draft) and not bool(approved),
        "current": cur,
        "receipt_available": bool(latest_approved),
        "receipt_period": latest_approved.period.name if (latest_approved and latest_approved.period) else None,
        "last_actual": last_actual,   # последние ВАШИ показания (не норматив)
        "norm_since": norm_since,     # периоды по нормативу после них (пропуски)
    }


class PasswordBody(BaseModel):
    password: str = Field(..., min_length=4, max_length=64)


@router.post("/{token}/password")
async def portal_set_password(
    token: str, body: PasswordBody, db: AsyncSession = Depends(get_db),
):
    """Первичная установка пароля портала (модалка первого входа).
    Только если пароль ещё НЕ установлен — менять установленный нельзя
    (забыли → админ сбрасывает в модалке QR, и портал снова попросит новый)."""
    room = await _resolve_or_404(db, token)
    if room.qr_password_hash:
        raise HTTPException(
            status_code=409,
            detail="Пароль уже установлен. Если вы его забыли — обратитесь к администратору.",
        )
    room.qr_password_hash = await asyncio.to_thread(get_password_hash, body.password)
    db.add(room)
    await db.commit()
    logger.info("[QR-PORTAL] установлен пароль room=%s", room.id)
    return {"status": "ok"}


@router.post("/{token}/submit")
async def portal_submit(
    token: str, data: ReadingSchema, db: AsyncSession = Depends(get_db),
    x_qr_key: str | None = Header(None, alias="X-QR-Key"),
):
    """Подача/правка показаний по QR-токену. Вся логика — общий сервис."""
    room = await _resolve_or_404(db, token)
    await _require_password(room, x_qr_key)
    period = await _active_period(db)
    rep_id = await pick_representative_user_id(db, room.id, period.id if period else None)
    if not rep_id:
        raise HTTPException(
            status_code=400,
            detail="В этой квартире нет зарегистрированных жильцов. Обратитесь к администратору.",
        )
    # perform_reading_submission сам проверит дом/койко-место/окно/формат 5+3
    # и вернёт понятную 400-ошибку — пробрасываем как есть.
    result = await perform_reading_submission(db, rep_id, data)
    logger.info("[QR-PORTAL] подача room=%s rep_user=%s", room.id, rep_id)
    return result


@router.get("/{token}/receipt")
async def portal_receipt(
    token: str, db: AsyncSession = Depends(get_db),
    x_qr_key: str | None = Header(None, alias="X-QR-Key"),
):
    """Скачать PDF квитанции квартиры (последнее утверждённое показание).
    Доступ = токен + пароль портала (фронт качает fetch'ем с заголовком)."""
    room = await _resolve_or_404(db, token)
    await _require_password(room, x_qr_key)
    reading = (await db.execute(
        select(MeterReading)
        .options(
            selectinload(MeterReading.user),
            selectinload(MeterReading.period),
            selectinload(MeterReading.room),
        )
        .where(MeterReading.room_id == room.id, MeterReading.is_approved.is_(True))
        .order_by(MeterReading.period_id.desc(), MeterReading.created_at.desc())
        .limit(1)
    )).scalars().first()
    if not reading:
        raise HTTPException(404, "Квитанция ещё не сформирована.")

    tariff, prev, adjustments = await _build_receipt_context(reading, db)
    try:
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            reading=reading, user=reading.user, room=reading.room,
            period=reading.period, tariff=tariff, prev_reading=prev,
            adjustments=adjustments,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[QR-PORTAL] receipt gen failed room=%s: %s", room.id, e, exc_info=True)
        raise HTTPException(500, "Ошибка генерации квитанции. Попробуйте позже.")

    if not os.path.exists(pdf_path):
        raise HTTPException(500, "Не удалось получить файл квитанции.")

    period_label = (reading.period.name or "period").replace(" ", "_")
    filename = quote(f"Kvitanciya_{period_label}.pdf")
    return FileResponse(
        path=pdf_path, media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=utf-8''{filename}",
            "Cache-Control": "no-store, must-revalidate",
        },
    )


class ContactBody(BaseModel):
    message: str = Field(..., min_length=5, max_length=2000)


@router.post("/{token}/contact")
async def portal_contact(
    token: str, body: ContactBody, db: AsyncSession = Depends(get_db),
    x_qr_key: str | None = Header(None, alias="X-QR-Key"),
):
    """Связаться с админом по коду квартиры → создаёт обращение (SupportTicket),
    привязанное к «представителю комнаты» (админ видит, от какой квартиры)."""
    room = await _resolve_or_404(db, token)
    await _require_password(room, x_qr_key)
    rep_id = await pick_representative_user_id(db, room.id, None)
    if not rep_id:
        raise HTTPException(
            status_code=400,
            detail="По этому коду нет зарегистрированных жильцов. Обратитесь к администратору лично.",
        )
    # Лёгкий анти-спам: не плодим открытые тикеты по одной квартире.
    open_cnt = (await db.execute(
        select(SupportTicket).where(
            SupportTicket.user_id == rep_id,
            SupportTicket.status.in_(["open", "in_progress"]),
        ).limit(5)
    )).scalars().all()
    if len(open_cnt) >= 5:
        raise HTTPException(429, "Слишком много открытых обращений. Дождитесь ответа администратора.")

    ticket = SupportTicket(
        user_id=rep_id,
        subject=QR_TICKET_SUBJECT,
        message=body.message.strip(),
        status="open",
    )
    db.add(ticket)
    await db.commit()
    logger.info("[QR-PORTAL] обращение room=%s rep_user=%s", room.id, rep_id)
    return {"status": "ok"}


@router.get("/{token}/messages")
async def portal_messages(
    token: str, db: AsyncSession = Depends(get_db),
    x_qr_key: str | None = Header(None, alias="X-QR-Key"),
):
    """Переписка квартиры с админом по QR (последние 20). Включает ответ админа,
    чтобы жилец видел его на портале. Авто-удаляются через 5 дней.

    Фильтр — по ВСЕМ жильцам комнаты, не по «представителю»: представитель
    вычисляется на момент запроса и может смениться (черновик удалили,
    жильца перевели) — тогда тикеты, созданные на прежнего, пропадали бы
    из переписки вместе с ответами админа."""
    room = await _resolve_or_404(db, token)
    await _require_password(room, x_qr_key)
    user_ids = (await db.execute(
        select(User.id).where(User.room_id == room.id, User.is_deleted.is_(False))
    )).scalars().all()
    if not user_ids:
        return {"messages": []}
    rows = (await db.execute(
        select(SupportTicket)
        .where(
            SupportTicket.user_id.in_(user_ids),
            SupportTicket.subject == QR_TICKET_SUBJECT,
        )
        .order_by(desc(SupportTicket.created_at))
        .limit(20)
    )).scalars().all()
    return {"messages": [{
        "id": t.id,
        "message": t.message,
        "status": t.status,
        "admin_response": t.admin_response,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "responded_at": t.responded_at.isoformat() if t.responded_at else None,
    } for t in rows]}
