# Черновики долгов 1С: staged-status, rematch-base, publish.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from app.core.database import get_db
from app.modules.utility.models import User, DebtImportLog
from app.core.dependencies import get_current_user

from ._shared import (
    router,
    _require_finance,
)


@router.get("/debts/staged-status", summary="Черновики долгов 1С, ждущие выгрузки")
async def debts_staged_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Для кнопки «Выгрузить»: какие черновики (status='staged') 1С готовы."""
    _require_finance(current_user)
    out = {"staged": {}, "has_staged": False}
    for acc in ("209", "205"):
        log = (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.account_type == acc,
                DebtImportLog.status == "staged",
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()
        if log:
            out["staged"][acc] = {
                "log_id": log.id, "file": log.file_name,
                "at": log.started_at.isoformat() if log.started_at else None,
                "residents": len(log.applied_state or {}),
                "not_found": log.not_found_count or 0,
                "by": log.started_by_username,
            }
            out["has_staged"] = True
    return out


@router.post("/debts/rematch-base",
             summary="Пересопоставить «не найденных» в черновиках 1С с текущей базой")
async def debts_rematch_base(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Перепроверяет not_found последних ЧЕРНОВИКОВ (staged) 1С против ТЕКУЩЕЙ
    базы (список «не найдено» заморожен на момент загрузки — после добавления
    жильцов его надо пересопоставить). Кого теперь нашли И У КОГО ЕСТЬ КОМНАТА —
    переносим в applied_state (долг привяжется при «Выгрузить»). Кто есть в базе,
    но БЕЗ комнаты — оставляем в not_found и считаем отдельно (им нужна комната,
    долг живёт на показании, а оно требует комнату). Перезаливка 1С не нужна."""
    _require_finance(current_user)
    from app.modules.utility.services.debt_import import normalize_name, _normalize_fio_key
    from app.modules.utility.services.gisgmp_import import GISGMP_SOURCE_LABEL
    from app.modules.utility.models import GSheetsAlias
    from sqlalchemy.orm import selectinload

    users = (await db.execute(
        select(User).options(selectinload(User.room))
        .where(User.role == "user", User.is_deleted.is_(False))
    )).scalars().all()
    umap, by_id = {}, {}
    for u in users:
        if not u.username:
            continue
        info = {"id": u.id, "room_id": u.room_id, "username": u.username,
                "room_label": (u.room.format_address if u.room else None)}
        umap[normalize_name(u.username)] = info
        by_id[u.id] = info
    amap = {a: uid for a, uid in (await db.execute(
        select(GSheetsAlias.alias_fio_normalized, GSheetsAlias.user_id)
    )).all() if a}

    def _match(fio_raw: str):
        u = umap.get(normalize_name(fio_raw))
        if u:
            return u
        uid = amap.get(_normalize_fio_key(fio_raw))
        return by_id.get(uid) if uid else None

    attached = no_room = still = 0
    touched = []
    for acc in ("209", "205"):
        log = (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.account_type == acc,
                DebtImportLog.status == "staged",
                DebtImportLog.file_name != GISGMP_SOURCE_LABEL,
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()
        if not log or not log.not_found_users:
            continue
        ap = dict(log.applied_state or {})
        remaining = []
        changed = False
        for nf in log.not_found_users:
            u = _match((nf.get("fio") or "").strip())
            if not u:
                remaining.append(nf)
                still += 1
                continue
            # Долг на лицевом счёте (user_id), не на комнате: привязываем даже
            # без заселения. room_id=NULL ок — комната подцепится позже.
            key = str(u["id"])
            ent = ap.get(key) or {
                "debt_209": "0", "overpayment_209": "0",
                "debt_205": "0", "overpayment_205": "0",
            }
            ent[f"debt_{acc}"] = str(nf.get("debt") or 0)
            ent[f"overpayment_{acc}"] = str(nf.get("overpayment") or 0)
            ent["username"] = u["username"]
            ent["room_id"] = u["room_id"]
            ent["room_label"] = u["room_label"]
            ap[key] = ent
            attached += 1
            if not u["room_id"]:
                no_room += 1  # привязан, но пока без комнаты (для отчёта)
            changed = True
        if changed:
            log.applied_state = ap
            log.not_found_users = remaining
            touched.append(log.id)
    await db.commit()
    return {"attached": attached, "in_base_no_room": no_room,
            "still_not_found": still, "logs": touched}


@router.post("/debts/publish", summary="Выгрузить черновики долгов жильцам (только 1С)")
async def debts_publish(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Берёт ПОСЛЕДНИЕ черновики импорта 1С (status='staged') по 209 и 205
    и пишет долги в показания активного периода — только теперь жильцы их
    видят. 1С — ЕДИНСТВЕННЫЙ источник долгов (ГИС ГМП НЕ перебивает 1С). Полная
    замена по выгружаемому счёту (кого нет в черновике → 0). Снимок до — для отката.

    Ручная выгрузка — БЕЗ предохранителя (явное действие админа). Авто-выгрузка
    после ежедневного сбора 1С идёт через тот же код с guard=True (см.
    onec_autopublish_task)."""
    _require_finance(current_user)
    from app.modules.utility.services.onec_publish import publish_onec_debts

    res = await publish_onec_debts(db, guard=False)
    if res.get("status") == "no_active_period":
        raise HTTPException(409, "Нет активного расчётного периода")
    if res.get("status") == "no_staged":
        raise HTTPException(409, "Нет черновиков для выгрузки — сначала загрузите Excel 1С")
    return res
