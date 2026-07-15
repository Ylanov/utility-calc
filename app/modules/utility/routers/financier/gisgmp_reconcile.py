# ГИС ГМП: начисления плательщика, находки, сверки, создание/привязка жильцов, purge.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

import secrets
import json
from typing import Optional
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, text
from app.core.database import get_db
from app.modules.utility.models import User, Room, DebtImportLog, SystemSetting
from app.core.dependencies import get_current_user
from app.modules.utility.services.user_service import countable_resident_condition

from ._shared import (
    router,
    GISGMP_FINDINGS_KEY,
    _load_findings,
    _build_reconcile,
    _require_finance,
)


@router.get("/gisgmp/payer-charges", summary="Все начисления одного плательщика (клик по ФИО)")
async def gisgmp_payer_charges(
    q: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """По фамилии (первое слово q) — ВСЕ начисления человека из кэша ГИС ГМП
    с разбивкой: долг (не сквитировано) / оплачено / аннулировано."""
    _require_finance(current_user)
    from app.modules.utility.services.gisgmp_import import (
        classify_account, is_unpaid, is_annulled, parse_reg_dt, GISGMP_CACHE_KEY,
    )
    from app.modules.utility.services.debt_import import clean_decimal

    parts = (q or "").strip().split()
    surname = parts[0].lower() if parts else ""
    if not surname:
        return {"query": q, "count": 0, "charges": [], "totals": {}}

    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_CACHE_KEY)
    )).scalars().first()
    cache = {}
    if row and row.value:
        try:
            cache = json.loads(row.value)
        except Exception:
            cache = {}

    out = []
    tot = {"debt_209": 0.0, "debt_205": 0.0, "paid": 0.0, "annulled": 0, "count": 0}
    for ch in cache.values():
        name = (ch.get("payer_name") or "").strip()
        if surname not in name.lower():
            continue
        acc = classify_account(ch.get("purpose"))
        annul = is_annulled(ch.get("change_status"))
        unpaid = is_unpaid(ch.get("ack_status"))
        amt = float(clean_decimal(ch.get("amount")))
        status = "annulled" if annul else ("unpaid" if unpaid else "paid")
        out.append({
            "payer_name": name,
            "account_type": acc,
            "account": ch.get("account"),
            "amount": amt,
            "bill_date": ch.get("bill_date"),
            "actualize_date": ch.get("actualize_date"),
            "purpose": ch.get("purpose"),
            "ack_status": ch.get("ack_status"),
            "status": status,
            "uin": ch.get("uin"),
        })
        tot["count"] += 1
        if annul:
            tot["annulled"] += 1
        elif unpaid:
            if acc == "209":
                tot["debt_209"] += amt
            elif acc == "205":
                tot["debt_205"] += amt
        else:
            tot["paid"] += amt

    _order = {"unpaid": 0, "paid": 1, "annulled": 2}

    def _sk(c):
        d = parse_reg_dt(c.get("bill_date"))
        return (_order.get(c["status"], 3), -(d.toordinal() if d else 0))

    out.sort(key=_sk)
    return {"query": q, "surname": surname, "count": len(out),
            "charges": out[:500], "totals": tot}


@router.get("/gisgmp/findings", summary="Что нашёл ГИС ГМП (отладка, хранится отдельно)")
async def gisgmp_findings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Полные находки последнего прогона релея: сводка по жильцам + сырые
    начисления. Пока ОТДЕЛЬНО от долгов 1С (Excel), в показания не пишется —
    показываем для отладки в отдельном окне «Долги 1С»."""
    _require_finance(current_user)
    findings = await _load_findings(db)
    if not findings:
        return {"empty": True}
    # Сырые начисления теперь в отдельном ключе (находки лёгкие) — подмешиваем
    # их сюда для поиска по фамилии в «Показать найденное».
    from app.modules.utility.services.gisgmp_import import GISGMP_FINDINGS_CHARGES_KEY
    chrow = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_FINDINGS_CHARGES_KEY)
    )).scalars().first()
    if chrow and chrow.value:
        try:
            findings = {**findings, "charges": json.loads(chrow.value).get("charges", [])}
        except Exception:
            pass
    return findings


@router.get("/gisgmp/reconcile", summary="Сверка ГИС ГМП (находки) ↔ долги 1С (Excel)")
async def gisgmp_reconcile(
    period_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сверяет два источника по жильцам и счетам 209/205:
      • ГИС ГМП — последние находки релея (SystemSetting 'gisgmp_findings',
        summary[].debt_209/205 по matched_user_id);
      • 1С — текущие долги в показаниях (MeterReading.debt_209/205 за период,
        залитые Excel-импортом ОСВ).
    На каждый счёт: совпало / расхождение (Δ) / только в ГИС / только в 1С.
    Окно ГИС влияет на полноту — для честной сверки ставь окно побольше."""
    _require_finance(current_user)
    return await _build_reconcile(db)


@router.get("/gisgmp/reconcile-fio", summary="3-сторонняя сверка ФИО: 1С ↔ ГИС ГМП ↔ база")
async def gisgmp_reconcile_fio(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """СОЮЗ по ТОЧНОМУ ФИО трёх источников: 1С (последний импорт/черновик),
    ГИС ГМП (ВЕСЬ кэш — и должники, и оплаченные «Сквитировано»), база жильцов.
    Одна строка на уникальное нормализованное ФИО, колонки «в 1С / в ГИС / в базе»
    + долги (из находок: 0, если в ГИС всё оплачено) + флаги «нет в …». Никаких
    «похожих» — только точное совпадение ФИО."""
    _require_finance(current_user)
    from app.modules.utility.services.gisgmp_import import GISGMP_SOURCE_LABEL, GISGMP_CACHE_KEY
    from app.modules.utility.services.gsheets_sync import normalize_fio

    rows: dict[str, dict] = {}

    def _row(fio_raw):
        n = normalize_fio(fio_raw or "")
        if not n:
            return None
        r = rows.get(n)
        if r is None:
            r = {"fio": (fio_raw or "").strip(), "in_1c": False, "in_gis": False,
                 "in_db": False, "d209_1c": 0.0, "d205_1c": 0.0,
                 "o209_1c": 0.0, "o205_1c": 0.0,  # переплата 1С (ГИС её не отдаёт)
                 "d209_gis": 0.0, "d205_gis": 0.0, "user_id": None, "username": None}
            rows[n] = r
        return r

    # --- база: активные жильцы (источник in_db + приоритетное отображение ФИО) ---
    db_rows = (await db.execute(
        select(User.id, User.username).where(
            User.role == "user", User.is_deleted.is_(False),
            # «свои дома» (безкомнатные без долга) в сверку не берём
            countable_resident_condition())
    )).all()
    for uid, un in db_rows:
        r = _row(un)
        if r is None:
            continue
        r["in_db"] = True
        r["user_id"] = uid
        r["username"] = un
        r["fio"] = un

    # --- 1С: последний импорт/черновик по каждому счёту (staged ИЛИ completed) ---
    sources: dict = {}
    for acc in ("209", "205"):
        log = (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.account_type == acc,
                DebtImportLog.status.in_(["staged", "completed"]),
                DebtImportLog.file_name != GISGMP_SOURCE_LABEL,
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()
        sources[acc] = ({"file": log.file_name, "status": log.status,
                         "at": log.started_at.isoformat() if log.started_at else None}
                        if log else None)
        if not log:
            continue
        for st in (log.applied_state or {}).values():
            un = st.get("username")
            r = _row(un) if un else None
            if r is None:
                continue
            r["in_1c"] = True
            try:
                r[f"d{acc}_1c"] += float(st.get(f"debt_{acc}") or 0)
                r[f"o{acc}_1c"] += float(st.get(f"overpayment_{acc}") or 0)
            except Exception:
                pass
        for nf in (log.not_found_users or []):
            r = _row(nf.get("fio"))
            if r is None:
                continue
            r["in_1c"] = True
            try:
                r[f"d{acc}_1c"] += float(nf.get("debt") or 0)
                r[f"o{acc}_1c"] += float(nf.get("overpayment") or 0)
            except Exception:
                pass

    # --- ГИС, СУЩЕСТВОВАНИЕ: по ВСЕМУ кэшу начислений (вкл. оплаченных
    # «Сквитировано»). Человек может быть в ГИС с НУЛЕВЫМ долгом (всё оплачено) —
    # раньше in_gis брался только из находок (должники), и оплаченные показывались
    # «нет в ГИС», хотя в ГИС ГМП они есть. DISTINCT payer_name извлекаем в
    # Postgres (парс кэша один раз, в Python приходят только имена). ---
    try:
        gis_names = (await db.execute(
            text("SELECT DISTINCT c->>'payer_name' AS fio "
                 "FROM (SELECT value FROM system_settings WHERE key = :k) s, "
                 "LATERAL jsonb_each(s.value::jsonb) AS e(uin, c)"),
            {"k": GISGMP_CACHE_KEY},
        )).all()
        for (fio,) in gis_names:
            r = _row(fio)
            if r is not None:
                r["in_gis"] = True
    except Exception:
        pass

    # --- ГИС, ДОЛГ: summary находок (только неоплаченные 209/205). Достаём
    # summary JSON-экстрактом в Postgres (без сырых charges — иначе союз тормозит). ---
    fres = (await db.execute(
        text("SELECT (value::jsonb -> 'summary')::text AS s, "
             "value::jsonb ->> 'synced_at' AS at "
             "FROM system_settings WHERE key = :k"),
        {"k": GISGMP_FINDINGS_KEY},
    )).first()
    gis_synced = None
    if fres and fres.s:
        gis_synced = fres.at
        try:
            for frow in json.loads(fres.s):
                r = _row(frow.get("fio"))
                if r is None:
                    continue
                r["in_gis"] = True
                r["d209_gis"] += float(frow.get("debt_209") or 0)
                r["d205_gis"] += float(frow.get("debt_205") or 0)
        except Exception:
            pass

    items = list(rows.values())
    summary = {
        "total": len(items),
        "all_three": sum(1 for r in items if r["in_1c"] and r["in_gis"] and r["in_db"]),
        "not_in_db": sum(1 for r in items if not r["in_db"]),
        "not_in_gis": sum(1 for r in items if not r["in_gis"]),
        "not_in_1c": sum(1 for r in items if not r["in_1c"]),
        "with_overpay": sum(1 for r in items if (r["o209_1c"] + r["o205_1c"]) > 0.005),
    }
    # Проблемные (хоть где-то «нет») — сверху, дальше по ФИО.
    items.sort(key=lambda r: ((r["in_1c"] and r["in_gis"] and r["in_db"]), r["fio"] or ""))
    return {
        "sources": sources,
        "gis_synced_at": gis_synced,
        "summary": summary,
        "rows": items[:3000],
    }


# ─── bulk-создание жильцов из «сирот» сверки (есть в 1С/ГИС, нет в базе) ─────
_FIO_STOPWORDS = (
    "пао", "ооо", "оао", "зао", "акционерн", "фгау", "фгбу", "фгуп", "гуп",
    "муп", "банк", "сбербанк", "втб", "филиал", "возмещен", "росжил",
    "бухгалтер", "директор", "начальник", "казначейств", "уфк", "ифнс",
    "фссп", "департамент", "комплекс",
)


def _clean_orphan_fio(raw: str) -> str:
    """ФИО → чистый вид: коллапс пробелов + убрать хвостовой «(общ)»/«(0)»."""
    s = " ".join((raw or "").split())
    if s.endswith(")") and "(" in s:
        s = s[:s.rfind("(")].strip()
    return s


def _looks_like_person_fio(s: str) -> tuple[bool, str]:
    """Грубый фильтр «похоже на ФИО человека?» → (ok, причина_если_нет)."""
    if not s:
        return False, "пусто"
    low = s.lower()
    if any(ch.isdigit() for ch in s):
        return False, "содержит цифры (не ФИО)"
    if any(kw in low for kw in _FIO_STOPWORDS):
        return False, "организация/должность"
    words = [w for w in s.split() if w]
    if len(words) < 2:
        return False, "одно слово (нет имени)"
    if len(words) > 5:
        return False, "слишком длинно для ФИО"
    if sum(1 for ch in low if "а" <= ch <= "я" or ch == "ё") < 5:
        return False, "не кириллица"
    return True, ""


@router.post("/gisgmp/create-missing-residents",
             summary="Создать жильцов (только ФИО) для тех, кто есть в 1С/ГИС, но не в базе")
async def gisgmp_create_missing_residents(
    dry_run: bool = Query(True),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Массово завести личные кабинеты для ФИО из 1С/ГИС, которых НЕТ в базе.

    Создаём ТОЛЬКО ФИО + учётку (без комнаты, адреса и прочего — админ заполнит
    сам). Мусор отсеиваем: организации/должности (ПАО, ФГАУ, банк, бухгалтер…),
    строки с цифрами, одно слово без имени. Дубли гасим: точное совпадение с
    базой/между собой + почти-дубли (типосы в отчестве, ≥90). dry_run=true
    (по умолчанию) — только предпросмотр, ничего не создаёт."""
    _require_finance(current_user)
    from app.modules.utility.services.gsheets_sync import normalize_fio
    from app.modules.utility.services.gisgmp_import import GISGMP_SOURCE_LABEL, GISGMP_CACHE_KEY
    from rapidfuzz import fuzz

    db_users = (await db.execute(
        select(User.username).where(User.role == "user", User.is_deleted.is_(False))
    )).scalars().all()
    db_norm = {normalize_fio(u): u for u in db_users if u}
    db_keys = list(db_norm.keys())

    # Сырые ФИО из 1С (последний импорт по счёту) + ГИС (payer_name из кэша).
    raw_by_norm: dict[str, str] = {}
    for acc in ("209", "205"):
        log = (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.account_type == acc,
                DebtImportLog.status.in_(["staged", "completed"]),
                DebtImportLog.file_name != GISGMP_SOURCE_LABEL,
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()
        if not log:
            continue
        for st in (log.applied_state or {}).values():
            un = (st.get("username") or "").strip()
            if un:
                raw_by_norm.setdefault(normalize_fio(un), un)
        for nf in (log.not_found_users or []):
            fi = (nf.get("fio") or "").strip()
            if fi:
                raw_by_norm.setdefault(normalize_fio(fi), fi)
    try:
        gis_rows = (await db.execute(
            text("SELECT DISTINCT c->>'payer_name' AS fio "
                 "FROM (SELECT value FROM system_settings WHERE key = :k) s, "
                 "LATERAL jsonb_each(s.value::jsonb) AS e(uin, c)"),
            {"k": GISGMP_CACHE_KEY},
        )).all()
        for (fi,) in gis_rows:
            fi = (fi or "").strip()
            if fi:
                raw_by_norm.setdefault(normalize_fio(fi), fi)
    except Exception:
        pass

    to_create: dict[str, str] = {}  # norm(clean) -> clean fio
    skip_not_fio, skip_in_db, skip_similar = [], [], []
    for nrm, raw in raw_by_norm.items():
        if not nrm or nrm in db_norm:
            continue  # пусто или уже точно в базе — не сирота
        cleaned = _clean_orphan_fio(raw)
        ok, reason = _looks_like_person_fio(cleaned)
        if not ok:
            skip_not_fio.append({"fio": raw, "reason": reason})
            continue
        cnrm = normalize_fio(cleaned)
        if cnrm in db_norm:
            skip_in_db.append({"fio": raw, "match": db_norm[cnrm]})
            continue
        if cnrm in to_create:
            continue
        # Почти-дубль с базой ИЛИ с уже добавленным в очередь (типос в отчестве).
        best, best_sc = None, 0
        for k in db_keys:
            sc = fuzz.token_sort_ratio(cnrm, k)
            if sc > best_sc:
                best_sc, best = sc, db_norm[k]
        for k, v in to_create.items():
            sc = fuzz.token_sort_ratio(cnrm, k)
            if sc > best_sc:
                best_sc, best = sc, v
        if best_sc >= 90:
            skip_similar.append({"fio": raw, "match": best, "score": int(best_sc)})
            continue
        to_create[cnrm] = cleaned

    create_list = sorted(to_create.values())
    if dry_run:
        return {
            "dry_run": True,
            "to_create_count": len(create_list),
            "to_create": create_list,
            "skip_not_fio": skip_not_fio,
            "skip_in_db": skip_in_db,
            "skip_similar": skip_similar,
        }

    from app.core.auth import get_password_hash
    from app.modules.utility.routers.admin_dashboard import write_audit_log

    existing_logins = {
        (lg or "").lower() for lg in (await db.execute(select(User.login))).scalars().all()
    }
    created = []
    for fio in create_list:
        login = fio
        if login.lower() in existing_logins:
            login = f"{fio} {secrets.token_hex(4)}"
        existing_logins.add(login.lower())
        db.add(User(
            username=fio, login=login,
            hashed_password=get_password_hash(secrets.token_urlsafe(12)),
            role="user", is_deleted=False, is_initial_setup_done=False,
        ))
        created.append(fio)

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="create", entity_type="user",
        details={"bulk_create_missing": len(created),
                 "skipped_not_fio": len(skip_not_fio),
                 "skipped_dup": len(skip_in_db) + len(skip_similar)},
    )
    await db.commit()
    return {
        "dry_run": False,
        "created_count": len(created),
        "created": created,
        "skip_not_fio": len(skip_not_fio),
        "skip_in_db": len(skip_in_db),
        "skip_similar": len(skip_similar),
    }


@router.get("/gisgmp/link-candidates", summary="Кандидаты-жильцы по фамилии для привязки сироты")
async def gisgmp_link_candidates(
    fio: str = Query(..., description="ФИО из 1С/ГИС"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Для кнопки «Привязать» в союзе: жильцы базы с ТОЙ ЖЕ фамилией (точно,
    первое слово) — кандидаты на привязку сироты 1С/ГИС к учётке. Не fuzzy:
    предлагаем по фамилии, выбор подтверждает админ."""
    _require_finance(current_user)
    parts = (fio or "").strip().split()
    surname = parts[0].lower() if parts else ""
    if not surname:
        return {"candidates": []}
    rows = (await db.execute(
        select(User.id, User.username,
               Room.dormitory_name, Room.room_number,
               Room.street, Room.house_number, Room.apartment_number)
        .outerjoin(Room, User.room_id == Room.id)
        .where(User.role == "user", User.is_deleted.is_(False))
    )).all()
    out = []
    for uid, un, dorm, rno, street, house, apt in rows:
        first = (un or "").strip().split()[:1]
        if first and first[0].lower() == surname:
            if dorm:
                addr = f"{dorm}, ком. {rno or '—'}"
            elif street:
                addr = f"ул. {street}, д. {house or '—'}, кв. {apt or '—'}"
            else:
                addr = "—"
            out.append({"id": uid, "username": un, "address": addr})
    out.sort(key=lambda x: x["username"] or "")
    return {"surname": surname, "candidates": out}


class GisgmpLinkFioIn(BaseModel):
    fio: str
    user_id: int
    rename: bool = True


@router.post("/gisgmp/link-fio", summary="Привязать ФИО из 1С/ГИС к жильцу (алиас + переименование)")
async def gisgmp_link_fio(
    payload: GisgmpLinkFioIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Связывает ФИО из 1С/ГИС с учёткой жильца: создаёт/перенаводит алиас
    (долг привяжется при выгрузке) и, если rename=True, переименовывает жильца
    в имя из 1С/ГИС (источник истины). Это явная привязка админа, не fuzzy."""
    _require_finance(current_user)
    from app.modules.utility.models import GSheetsAlias
    from app.modules.utility.services.gsheets_sync import normalize_fio
    from app.modules.utility.routers.admin_dashboard import write_audit_log
    fio = (payload.fio or "").strip()
    user = await db.get(User, payload.user_id)
    if not user or user.is_deleted:
        raise HTTPException(404, "Жилец не найден")
    normalized = normalize_fio(fio)
    if not normalized:
        raise HTTPException(400, "Пустое ФИО")
    # Алиас: явный выбор админа — перенаводим, если был на другого жильца.
    existing = (await db.execute(
        select(GSheetsAlias).where(GSheetsAlias.alias_fio_normalized == normalized)
    )).scalars().first()
    if existing:
        existing.user_id = user.id
        existing.alias_fio = fio
        existing.kind = "debt_manual"
    else:
        db.add(GSheetsAlias(
            alias_fio=fio, alias_fio_normalized=normalized, user_id=user.id,
            kind="debt_manual", note="link-fio (союз)", created_by_id=current_user.id,
        ))
    # Переименование в имя из 1С/ГИС (приоритет источника), если нет конфликта.
    renamed, warning, old_name = False, None, user.username
    if payload.rename and fio and fio != user.username:
        clash = (await db.execute(
            select(User.id).where(User.username == fio, User.id != user.id)
        )).first()
        if clash:
            warning = f"Имя «{fio}» уже занято другим жильцом — переименование пропущено, алиас создан."
        else:
            user.username = fio
            renamed = True
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gisgmp_link_fio", entity_type="user", entity_id=user.id,
        details={"fio": fio, "old_name": old_name, "renamed": renamed},
    )
    await db.commit()
    return {"ok": True, "renamed": renamed, "warning": warning,
            "user_id": user.id, "username": user.username}


@router.post("/gisgmp/purge", summary="Очистить данные ГИС ГМП (кэш, находки, курсор, очередь)")
async def gisgmp_purge(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Обнуляет рабочие данные ГИС ГМП: кэш начислений, находки, курсор инкремента
    и очередь актуализации. Долги жильцов НЕ трогает (они из 1С через гейт).
    После очистки нужен новый сбор («Запустить сбор») — соберёт с нуля точным
    матчингом. Аудит-историю актуализаций (gisgmp_actualize_log) не чистим."""
    _require_finance(current_user)
    from app.modules.utility.routers.admin_dashboard import write_audit_log
    keys = ["gisgmp_cache", "gisgmp_findings", "gisgmp_cursor", "gisgmp_actualize"]
    rows = (await db.execute(
        select(SystemSetting).where(SystemSetting.key.in_(keys))
    )).scalars().all()
    cleared = 0
    for row in rows:
        row.value = "{}"
        cleared += 1
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gisgmp_purge", entity_type="system_setting", entity_id=None,
        details={"keys": keys, "cleared": cleared},
    )
    await db.commit()
    return {"ok": True, "cleared": cleared}


@router.get("/gisgmp/control", summary="Контроль 1С-ГИС: светофор сверки (сводка)")
async def gisgmp_control(
    refresh: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Компактная сводка «разложено по полочкам» (правило: 1С — истина):
    сколько совпало, где ГИС завышен (лечится актуализацией), где занижен
    (дотянуть/1С не довыгрузил), кого нет в ГИС/в базе, топ расхождений,
    тёзки (кандидаты в дубли базы). Снапшот пишется автоматически после
    каждого сбора ГИС и каждой выгрузки 1С; refresh=true — пересчитать сейчас.
    """
    _require_finance(current_user)
    import json as _json
    from ._shared import GIS1C_CONTROL_KEY, refresh_control_snapshot

    if refresh:
        return await refresh_control_snapshot(db)
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GIS1C_CONTROL_KEY)
    )).scalars().first()
    if row and row.value:
        try:
            return _json.loads(row.value)
        except Exception:
            pass
    return await refresh_control_snapshot(db)
