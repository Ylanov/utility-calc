# Общее ядро пакета financier: единый APIRouter, logger и хелперы/константы,
# используемые несколькими модулями пакета. Перенесено из монолитного
# routers/financier.py механически (распил на пакет), поведение 1:1.
# ВАЖНО: этот модуль не должен импортировать модули-роуты пакета (цикл).


import os
import asyncio
import logging
import secrets
import json
from datetime import datetime
from decimal import Decimal
from app.core.time_utils import utcnow
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from app.core.config import settings
from app.modules.utility.models import User, DebtImportLog, SystemSetting


router = APIRouter(prefix="/api/financier", tags=["Financier"])
# Имя логгера оставляем историческим ("…routers.financier", как у бывшего
# модуля-монолита), чтобы настройка логирования по имени продолжала работать.
logger = logging.getLogger(__name__.rsplit(".", 1)[0])


def _nfu_fio(item) -> str:
    """Извлекает ФИО из элемента not_found_users.

    not_found_users поменял формат: legacy = list[str], новый = list[dict].
    Помогает не падать с AttributeError при работе со старыми импортами
    и при удалении/сравнении элементов.
    """
    if isinstance(item, dict):
        return str(item.get("fio", "")).strip()
    return str(item).strip()


async def _ensure_debt_alias(
    db: AsyncSession,
    *,
    alias_fio: str,
    user_id: int,
    created_by_id: int,
    note: Optional[str] = None,
) -> bool:
    """Создаёт GSheetsAlias для запоминания привязки ФИО → user.

    Общая таблица для всех типов импорта (gsheets, debt 205, debt 209) —
    привязка сделана один раз, работает везде. Если alias по этому
    normalized ФИО уже есть (даже на другого юзера) — НЕ перезаписываем,
    оставляем старый (защита от случайной перебивки чужой привязки).

    Возвращает True если действительно создал новую запись.
    """
    from app.modules.utility.models import GSheetsAlias
    from app.modules.utility.services.gsheets_sync import normalize_fio

    normalized = normalize_fio(alias_fio)
    if not normalized:
        return False
    existing = (await db.execute(
        select(GSheetsAlias).where(GSheetsAlias.alias_fio_normalized == normalized)
    )).scalars().first()
    if existing:
        return False

    db.add(GSheetsAlias(
        alias_fio=alias_fio.strip(),
        alias_fio_normalized=normalized,
        user_id=user_id,
        kind="debt_manual",
        note=note,
        created_by_id=created_by_id,
    ))
    return True


TEMP_DIR = "/app/static/temp_imports"  # legacy, для совместимости со старым кодом
# Постоянное хранение оригиналов ОСВ из 1С.
# ПУТЬ: используем существующий shared_data volume (/app/static/generated_files/).
# Раньше пробовали /app/data/debt_archives через отдельный volume,
# но это требовало `docker compose down && up -d` для создания volume —
# до рестарта web писал в эфемерный слой, а worker_heavy искал в своём
# эфемерном слое, выдавая «No such file or directory». shared_data уже
# смонтирован на web + worker_heavy + nginx, файлы шарятся сразу.
#
# Прямой доступ через nginx (/static/generated_files/debt_archives/...)
# заблокирован в nginx/conf.d/default.conf — скачивание только через
# /api/financier/debts/import-history/{id}/download с auth.
DEBT_ARCHIVE_DIR = "/app/static/generated_files/debt_archives"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(DEBT_ARCHIVE_DIR, exist_ok=True)


async def _save_uploaded_debt_file(
    file: UploadFile, account_type: str, batch_id: str
) -> tuple[str, str]:
    """Валидирует, проверяет размер/magic-bytes и сохраняет xlsx в archive.

    Возвращает (file_path, original_name). Кидает HTTPException при ошибке.
    Файлы парного импорта получают batch_id в имени для группировки на
    диске — после успешного импорта debt_import.py зальёт archive_path
    в DebtImportLog.
    """
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Поддерживаются только Excel-файлы")

    header = await file.read(8)
    await file.seek(0)
    if not (header.startswith(b"PK\x03\x04") or header.startswith(b"\xd0\xcf\x11\xe0")):
        raise HTTPException(status_code=400, detail="Вредоносный файл или поддельное расширение!")

    file.file.seek(0, 2)
    file_size = file.file.tell()
    await file.seek(0)
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Файл слишком большой. Максимум {MAX_FILE_SIZE / 1024 / 1024} MB",
        )

    ext = file.filename.rsplit(".", 1)[-1].lower()
    unique_name = f"{batch_id}_{account_type}.{ext}"
    file_path = os.path.join(DEBT_ARCHIVE_DIR, unique_name)

    try:
        content = await file.read()

        def save_file():
            with open(file_path, "wb") as buffer:
                buffer.write(content)

        await asyncio.to_thread(save_file)
    except Exception:
        logger.exception("Failed to save uploaded file")
        raise HTTPException(status_code=500, detail="Ошибка сохранения файла")

    return file_path, file.filename


def _check_gisgmp_token(authorization: Optional[str]) -> None:
    """Сверяет Bearer-токен с GISGMP_SYNC_TOKEN (constant-time)."""
    expected = (settings.GISGMP_SYNC_TOKEN or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Синхронизация ГИС ГМП не настроена: задайте GISGMP_SYNC_TOKEN в .env",
        )
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Неверный токен синхронизации ГИС ГМП")


# ─── Релей ГИС ГМП: конфиг и управление (pull-модель) ────────────────────────
# Релей на ВМ PODS2 за NAT — ЖКХ не достучится к нему внутрь. Поэтому ЖКХ хранит
# настройки/команды в SystemSetting, а релей сам их забирает (GET relay-config)
# и шлёт отчёт (POST relay-report). Админ рулит из вкладки «Долги 1С».

GISGMP_RELAY_KEY = "gisgmp_relay"
_GISGMP_RELAY_DEFAULTS = {
    "enabled": True,
    "months_back": 36,
    "interval_hours": 12,
    "daily_hour": 22,                # час ежедневного авто-запуска по МСК (вечер)
    "run_now": False,
    "last_run_at": None,
    "last_report_at": None,
    "last_status": None,
    "last_message": None,
    "last_count": 0,
    "last_updated": 0,
    "last_created": 0,
    "last_not_found": 0,
    # Управление демоном из UI (релей применяет на опросе, claim-на-выдаче):
    "self_update": False,            # подтянуть свежий relay.py и перезапуститься
    "restart": False,                # просто перезапуститься
    "passport_username": None,       # логин входа в реестр (показываем в статусе)
    "passport_password_enc": None,   # пароль, Fernet-шифр (в статус НЕ отдаём)
    "creds_pending": False,          # есть неотданная смена учётки
}


async def _load_relay_cfg(db: AsyncSession) -> dict:
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_RELAY_KEY)
    )).scalars().first()
    cfg = dict(_GISGMP_RELAY_DEFAULTS)
    if row and row.value:
        try:
            cfg.update(json.loads(row.value))
        except Exception:
            pass
    return cfg


async def _save_relay_cfg(db: AsyncSession, cfg: dict) -> None:
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_RELAY_KEY)
    )).scalars().first()
    if row is None:
        row = SystemSetting(key=GISGMP_RELAY_KEY, value="{}",
                            description="Конфиг и статус релея ГИС ГМП")
        db.add(row)
    row.value = json.dumps(cfg, ensure_ascii=False)
    await db.commit()


GISGMP_FINDINGS_KEY = "gisgmp_findings"


async def _load_findings(db: AsyncSession) -> Optional[dict]:
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_FINDINGS_KEY)
    )).scalars().first()
    if row and row.value:
        try:
            return json.loads(row.value)
        except Exception:
            return None
    return None


async def _build_reconcile(db: AsyncSession) -> dict:
    findings = await _load_findings(db)
    from app.modules.utility.services.gsheets_sync import normalize_fio
    gis: dict[int, dict] = {}
    # Несопоставленные ФИО (1С/ГИС не нашли жильца в базе) — собираем отдельно,
    # чтобы НЕ ТЕРЯТЬ людей: показываем их блоком внизу сверки и в печати.
    orphans_map: dict[str, dict] = {}

    def _orphan(fio_raw):
        n = normalize_fio(fio_raw or "")
        if not n:
            return None
        return orphans_map.setdefault(n, {
            "fio": (fio_raw or "").strip(),
            "gis_209": 0.0, "gis_205": 0.0, "c1_209": 0.0, "c1_205": 0.0,
        })

    if findings:
        for row in findings.get("summary", []):
            uid = row.get("matched_user_id")
            if uid is None:
                o = _orphan(row.get("fio"))
                if o is not None:
                    o["gis_209"] += float(row.get("debt_209") or 0)
                    o["gis_205"] += float(row.get("debt_205") or 0)
                continue
            gis[int(uid)] = {
                "209": Decimal(str(row.get("debt_209") or 0)),
                "205": Decimal(str(row.get("debt_205") or 0)),
                "username": row.get("matched_username") or row.get("fio"),
            }

    # 1С-сторона = ПОСЛЕДНИЙ Excel-импорт по каждому счёту (applied_state),
    # а не живой MeterReading — так видно КАКОЙ файл сверяем и нет примеси от
    # старого ГИС-прогона (он писал в показания на старом коде).
    from app.modules.utility.services.gisgmp_import import GISGMP_SOURCE_LABEL

    async def _last_excel_imp(acc):
        return (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.account_type == acc,
                # staged (черновик) ИЛИ completed (выгружено) — сверка видит и
                # не выгруженный черновик, чтобы можно было сверить ДО публикации.
                DebtImportLog.status.in_(["staged", "completed"]),
                DebtImportLog.file_name != GISGMP_SOURCE_LABEL,
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()

    mr: dict[int, dict] = {}
    source_1c = {}
    for acc in ("209", "205"):
        log = await _last_excel_imp(acc)
        source_1c[acc] = ({"file": log.file_name,
                           "at": log.started_at.isoformat() if log.started_at else None,
                           "log_id": log.id} if log else None)
        if log and log.applied_state:
            key = f"debt_{acc}"
            for uid_s, st in log.applied_state.items():
                if not str(uid_s).isdigit():
                    continue
                try:
                    val = Decimal(str(st.get(key) or 0))
                except Exception:
                    val = Decimal("0")
                if val:
                    mr.setdefault(int(uid_s), {})[acc] = val
        # 1С-сироты этого импорта (ФИО, которые не сматчились с жильцом базы).
        if log and log.not_found_users:
            for nf in log.not_found_users:
                o = _orphan(nf.get("fio"))
                if o is not None:
                    try:
                        o[f"c1_{acc}"] += float(nf.get("debt") or 0)
                    except Exception:
                        pass

    ids = set(gis) | set(mr)
    unames: dict[int, str] = {}
    if ids:
        for uid, uname in (await db.execute(
            select(User.id, User.username).where(User.id.in_(ids))
        )).all():
            unames[uid] = uname

    eps = Decimal("0.01")
    out = {
        "has_findings": bool(findings),
        "findings_at": (findings or {}).get("synced_at"),
        "source_1c": source_1c,
        "accounts": {},
    }
    for acc in ("209", "205"):
        matched = 0
        sum_gis = sum_1c = Decimal("0")
        for uid in ids:
            g = gis.get(uid, {}).get(acc, Decimal("0"))
            m = mr.get(uid, {}).get(acc, Decimal("0"))
            if g == 0 and m == 0:
                continue
            sum_gis += g
            sum_1c += m
            if abs(g - m) <= eps:
                matched += 1
        out["accounts"][acc] = {
            "matched": matched,
            "sum_gisgmp": float(sum_gis),
            "sum_1c": float(sum_1c),
            "delta_total": float(sum_gis - sum_1c),
        }

    # Единый разрез по жильцу + авто-флаги (анализатор сам сигналит проблемы).
    def _flag(sg, sc, d):
        if abs(d) <= eps:
            return "ok"
        if sc == 0:
            return "only_gis"      # есть в ГИС, в 1С долга нет
        if sg == 0:
            return "only_1c"       # есть в 1С, ГИС не нашёл
        return "gis_more" if d > 0 else "c1_more"

    # «За сколько месяцев» долг в ГИС у жильца — число РАЗНЫХ месяцев его
    # неоплаченных (не аннулированных) начислений в реестре, по фамилии.
    # Для колонки «ГИС, мес» в сверке и печати + сигнала «дотянуть».
    from app.modules.utility.services.gisgmp_import import (
        is_unpaid, is_annulled, parse_reg_dt, GISGMP_CACHE_KEY,
    )
    surname_months: dict[str, set] = {}
    cache_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_CACHE_KEY)
    )).scalars().first()
    if cache_row and cache_row.value:
        try:
            for ch in json.loads(cache_row.value).values():
                if is_annulled(ch.get("change_status")) or not is_unpaid(ch.get("ack_status")):
                    continue
                nm = (ch.get("payer_name") or "").strip().split()
                dt = parse_reg_dt(ch.get("bill_date"))
                if nm and dt is not None:
                    surname_months.setdefault(nm[0].lower(), set()).add((dt.year, dt.month))
        except Exception:
            pass

    residents = []
    problems: dict[str, dict] = {}
    matched_total = 0
    for uid in ids:
        g9 = gis.get(uid, {}).get("209", Decimal("0"))
        c9 = mr.get(uid, {}).get("209", Decimal("0"))
        g5 = gis.get(uid, {}).get("205", Decimal("0"))
        c5 = mr.get(uid, {}).get("205", Decimal("0"))
        if g9 == 0 and c9 == 0 and g5 == 0 and c5 == 0:
            continue
        name = unames.get(uid) or gis.get(uid, {}).get("username") or str(uid)
        sg, sc = g9 + g5, c9 + c5
        d = sg - sc
        flag = _flag(sg, sc, d)
        sev = "high" if abs(d) >= 20000 else ("mid" if abs(d) >= 5000 else "low")
        if flag == "ok":
            matched_total += 1
        else:
            pr = problems.setdefault(flag, {"count": 0, "sum_abs": 0.0, "high": 0})
            pr["count"] += 1
            pr["sum_abs"] += float(abs(d))
            if sev == "high":
                pr["high"] += 1
        sn = (name or "").strip().split()
        gmonths = len(surname_months.get(sn[0].lower(), set())) if sn else 0
        residents.append({
            "user_id": uid, "username": name,
            "g209": float(g9), "c209": float(c9), "d209": float(g9 - c9),
            "g205": float(g5), "c205": float(c5), "d205": float(g5 - c5),
            "sum_gis": float(sg), "sum_1c": float(sc), "delta": float(d),
            "flag": flag, "severity": sev,
            # «За сколько месяцев» долг в ГИС + нужно ли ещё дотянуть (ГИС занижен).
            "gis_months": gmonths,
            "need_pull": flag in ("c1_more", "only_1c"),
        })

    residents.sort(key=lambda r: -abs(r["delta"]))
    out["residents"] = residents[:1000]
    out["problems"] = problems
    out["matched_count"] = matched_total
    # Несопоставленные (1С/ГИС, нет жильца в базе) — отдельным блоком, чтобы не теряли.
    out["orphans"] = sorted(orphans_map.values(), key=lambda x: x["fio"])[:2000]
    return out


GISGMP_ACTUALIZE_KEY = "gisgmp_actualize"
GISGMP_ACTUALIZE_LOG_KEY = "gisgmp_actualize_log"   # аудит прогонов (до/после)
_ACTUALIZE_LOG_MAX_RUNS = 50                         # авто-кап истории (+ ручная чистка)


def _actualize_result(before: dict | None, after: dict | None) -> str:
    """Эффект актуализации по долгу ГИС (до→после) для аудита."""
    try:
        bg = float((before or {}).get("gis") or 0)
        ag = float((after or {}).get("gis") or 0)
    except Exception:
        return "unknown"
    eps = 1.0
    if bg <= eps:
        return "unchanged"
    if ag <= eps:
        return "annulled"        # долг ГИС обнулён (реестр аннулировал лишнее)
    if ag < bg - eps:
        return "reduced"         # долг ГИС уменьшился
    if ag > bg + eps:
        return "increased"
    return "unchanged"


async def _capture_actualize_after(db: AsyncSession) -> None:
    """После сверки: для прогонов актуализации со статусом after_pending снимаем
    долг ГИС «после» по каждому жильцу и классифицируем эффект (до→после).
    Запускается из /gisgmp/sync — кэш и находки к этому моменту уже свежие."""
    lr = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
    )).scalars().first()
    if not lr or not lr.value:
        return
    try:
        lj = json.loads(lr.value)
    except Exception:
        return
    pending = [r for r in lj.get("runs", []) if r.get("after_pending")]
    if not pending:
        return
    rec = await _build_reconcile(db)
    cur = {
        int(r["user_id"]): r for r in rec.get("residents", [])
        if r.get("user_id") is not None
    }
    now = utcnow().isoformat()
    for run in pending:
        for res in run.get("residents", []):
            uid = res.get("user_id")
            r = cur.get(int(uid)) if uid is not None else None
            if r is not None:
                after = {"gis": r.get("sum_gis"), "c1": r.get("sum_1c"), "delta": r.get("delta")}
            elif res.get("fio"):
                # Прогон по ОДНОМУ человеку (user_id нет): «после» = сумма ещё
                # несквитированных начислений (что сквитировалось — ушло из долга).
                _ch, _rev = await _load_person_charges(db, res["fio"])
                after = {"gis": round(sum(c["amount"] for c in _ch
                                          if c["unpaid"] and not c["annulled"]), 2),
                         "c1": None, "delta": None}
            else:
                # Жилец выпал из сверки — ни ГИС, ни 1С долга не осталось → ГИС обнулён.
                c1 = float((res.get("before") or {}).get("c1") or 0)
                after = {"gis": 0.0, "c1": c1, "delta": -c1}
            res["after"] = after
            res["result"] = _actualize_result(res.get("before"), after)
        run["after_pending"] = False
        run["after_at"] = now
        run["status"] = "done"
    lr.value = json.dumps(lj, ensure_ascii=False)
    await db.commit()


async def _drive_actualize_runs(db: AsyncSession) -> bool:
    """Авто-цикл доведения актуализации до «Сквитировано» (рулит по ~2-мин опросу
    релея). Для прогонов «checking»: читает СВЕЖИЙ кэш ГИС, считает ещё
    несквитированные начисления прогона (по UIN) → пишет «после»; всё сквитировано
    → done(all_paid); иначе каждые REACT_MIN мин повтор актуализации их
    несквитированных (макс MAX_ATTEMPTS попытки) → done(unpaid_left); HARD_MIN —
    аварийный финал. Возвращает need_scrape: если есть активные циклы, релею нужен
    ЛЁГКИЙ инкрементальный сбор (ловит счета, у которых ГИС двинул дату при
    сквитировании) — вместо ТЯЖЁЛОГО переопроса сотен ФИО на массовом прогоне.
    Зовётся из relay-config."""
    REACT_MIN, MAX_ATTEMPTS, HARD_MIN = 30, 2, 90
    lr = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
    )).scalars().first()
    if not lr or not lr.value:
        return False
    try:
        lj = json.loads(lr.value)
    except Exception:
        return False
    active = [r for r in lj.get("runs", []) if r.get("status") == "checking"]
    if not active:
        return False
    from app.modules.utility.services.gisgmp_import import (
        GISGMP_CACHE_KEY, is_unpaid, is_annulled,
    )
    cache = {}
    cache_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_CACHE_KEY)
    )).scalars().first()
    if cache_row and cache_row.value:
        try:
            cache = json.loads(cache_row.value)
        except Exception:
            cache = {}

    def _unpaid_now(ch: dict) -> bool:
        cc = cache.get(ch.get("uin"))
        if cc is None:
            return True  # ещё не пересканен этим циклом — считаем несквитированным
        return is_unpaid(cc.get("ack_status")) and not is_annulled(cc.get("change_status"))

    now = utcnow()
    now_iso = now.isoformat()

    def _age_min(iso) -> float:
        try:
            return (now - datetime.fromisoformat(iso)).total_seconds() / 60.0 if iso else 1e9
        except Exception:
            return 1e9

    act_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_KEY)
    )).scalars().first()
    act_busy = False
    if act_row and act_row.value:
        try:
            _av = json.loads(act_row.value)
            act_busy = bool(_av.get("running") or _av.get("uuids"))
        except Exception:
            act_busy = False

    need_scrape = False
    reactualize = None
    changed = False

    def _finalize(run, result):
        run["status"] = "done"
        run["after_pending"] = False
        run["after_at"] = now_iso
        run["loop_result"] = result

    for run in active:
        still_uuids: list[str] = []
        for res in run.get("residents", []):
            res_sum = 0.0
            for ch in res.get("charges", []):
                if _unpaid_now(ch):
                    u = ch.get("charge_uuid")
                    if u:
                        still_uuids.append(u)
                    res_sum += float(ch.get("amount") or 0)
            res["after"] = {"gis": round(res_sum, 2), "c1": None, "delta": None}
            res["result"] = _actualize_result(res.get("before"), res["after"])
        if not still_uuids:
            _finalize(run, "all_paid")
            changed = True
            continue
        if _age_min(run.get("started_at") or run.get("queued_at")) >= HARD_MIN:
            _finalize(run, "timeout")
            changed = True
            continue
        if _age_min(run.get("last_actualize_at")) >= REACT_MIN:
            if int(run.get("attempt", 1)) >= MAX_ATTEMPTS:
                _finalize(run, "unpaid_left")
                changed = True
                continue
            if not act_busy and reactualize is None:
                reactualize = (run, still_uuids)
                run["attempt"] = int(run.get("attempt", 1)) + 1
                run["last_actualize_at"] = now_iso
                run["status"] = "running"  # релей дошлёт повтор
                changed = True
                continue
        need_scrape = True  # ещё «checking» → нужен лёгкий инкрементальный сбор

    if reactualize:
        run, uuids = reactualize
        act_payload = {
            "uuids": uuids, "total": len(uuids), "done": 0, "ok": 0, "fail": 0,
            "running": False, "finished": False, "queued_at": now_iso,
            "by": "авто-цикл", "message": "", "targeting": "loop-retry",
            "run_id": run.get("id"),
        }
        if act_row is None:
            act_row = SystemSetting(key=GISGMP_ACTUALIZE_KEY, value="{}",
                                    description="Очередь массовой актуализации ГИС ГМП")
            db.add(act_row)
        act_row.value = json.dumps(act_payload, ensure_ascii=False)
    if changed or reactualize:
        lr.value = json.dumps(lj, ensure_ascii=False)
        await db.commit()
    return need_scrape


async def _load_person_charges(db: AsyncSession, fio: str) -> tuple[list[dict], list[str]]:
    """Начисления ГИС ГМП одного человека из кэша (союз по ТОЧНОМУ ФИО).
    Возвращает (charges, revocable_uuids). revocable = НЕсквитированные и НЕ
    аннулированные — именно их актуализируем/аннулируем."""
    from app.modules.utility.services.gisgmp_import import (
        GISGMP_CACHE_KEY, is_unpaid, is_annulled, classify_account,
    )
    from app.modules.utility.services.gsheets_sync import normalize_fio
    target = normalize_fio(fio or "")
    charges: list[dict] = []
    revocable: list[str] = []
    if not target:
        return charges, revocable
    cache_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_CACHE_KEY)
    )).scalars().first()
    if cache_row and cache_row.value:
        try:
            for ch in json.loads(cache_row.value).values():
                if normalize_fio(ch.get("payer_name") or "") != target:
                    continue
                unpaid = is_unpaid(ch.get("ack_status"))
                annulled = is_annulled(ch.get("change_status"))
                try:
                    amt = float(str(ch.get("amount") or "0").replace(",", "."))
                except Exception:
                    amt = 0.0
                u = ch.get("charge_uuid")
                charges.append({
                    "uin": ch.get("uin"), "account": classify_account(ch.get("purpose")),
                    "amount": amt, "bill_date": ch.get("bill_date"),
                    "ack_status": ch.get("ack_status"), "change_status": ch.get("change_status"),
                    "charge_uuid": u, "unpaid": unpaid, "annulled": annulled,
                    "purpose": ch.get("purpose"),
                })
                if unpaid and not annulled and u:
                    revocable.append(u)
        except Exception:
            pass
    charges.sort(key=lambda c: (not c["unpaid"], c.get("account") or "", c.get("bill_date") or ""))
    return charges, revocable


async def _resolve_view_period(db: AsyncSession, period_id: Optional[int]):
    """Период для ПРОСМОТРА долгов (список/KPI/квартиры). Делегирует общему
    services.period_resolver.resolve_view_period — единый источник для financier,
    ЛК жильца, дашборда и /users/stats (ревизия #5/#6)."""
    from app.modules.utility.services.period_resolver import resolve_view_period
    return await resolve_view_period(db, period_id)


def _require_finance(user: User) -> None:
    if user.role not in ("financier", "accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")


# =====================================================================
# КОНТРОЛЬ 1С ↔ ГИС ГМП (2026-07-15, запрос пользователя «разложить по
# полочкам»). Правило домена: 1С — ИСТИНА, ГИС сверяется с ней.
# Снапшот сверки пишется после каждого сбора ГИС и каждой выгрузки 1С →
# карточка-светофор в «Долги 1С» + алерты сторожа без пересчёта на лету.
# =====================================================================
GIS1C_CONTROL_KEY = "gis1c_control"


async def refresh_control_snapshot(db: AsyncSession) -> dict:
    """Пересчитывает сверку и сохраняет компактную сводку в SystemSetting.

    Категории (флаги _build_reconcile):
      ok       — суммы совпали (всё правильно, 1С выгрузился в ГИС);
      gis_more / only_gis — ГИС ЗАВЫШЕН → лечится «Актуализацией» (реестр
                 аннулирует лишнее под 1С);
      c1_more  — ГИС ЗАНИЖЕН → «Дотянуть расхождения» (глубокий переопрос
                 реестра) либо 1С ещё не довыгрузил;
      only_1c  — человека нет в ГИС вовсе (выгрузка 1С→ГИС не прошла);
      orphans  — есть в 1С/ГИС, нет в базе жильцов.
    Плюс тёзки: одно ФИО под несколькими user_id — кандидаты в дубли базы.
    """
    from collections import Counter

    rec = await _build_reconcile(db)
    residents = rec.get("residents") or []
    flags = Counter(r.get("flag") for r in residents)
    sum_gis = round(sum(r.get("sum_gis") or 0 for r in residents), 2)
    sum_1c = round(sum(r.get("sum_1c") or 0 for r in residents), 2)

    worst = sorted(
        (r for r in residents if r.get("flag") != "ok"),
        key=lambda r: -abs(r.get("delta") or 0),
    )[:10]
    top = [{
        "user_id": r.get("user_id"), "fio": r.get("username"),
        "flag": r.get("flag"),
        "gis": round(r.get("sum_gis") or 0, 2),
        "c1": round(r.get("sum_1c") or 0, 2),
        "delta": round(r.get("delta") or 0, 2),
    } for r in worst]

    # Тёзки — по БАЗЕ жильцов (одно ФИО под разными user_id = кандидат в дубли).
    # По строкам сверки считать нельзя: она матчит по ФИО и схлопывает тёзок.
    from sqlalchemy import func as _f
    dup_rows = (await db.execute(
        select(User.username, _f.count(User.id))
        .where(User.role == "user", User.is_deleted.is_(False))
        .group_by(User.username)
        .having(_f.count(User.id) > 1)
        .order_by(_f.count(User.id).desc())
        .limit(10)
    )).all()
    namesakes = [{"fio": n, "count": int(c)} for n, c in dup_rows if n]

    snapshot = {
        "ts": utcnow().isoformat(),
        "matched": len(residents),
        "flags": dict(flags),
        "sum_gis": sum_gis, "sum_1c": sum_1c,
        "delta": round(sum_gis - sum_1c, 2),
        "orphans": len(rec.get("orphans") or []),
        "top": top,
        "namesakes": namesakes,
    }

    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GIS1C_CONTROL_KEY)
    )).scalars().first()
    if row is None:
        row = SystemSetting(key=GIS1C_CONTROL_KEY, value="{}",
                            description="Сводка контроля 1С↔ГИС (светофор)")
        db.add(row)
    row.value = json.dumps(snapshot, ensure_ascii=False)
    await db.commit()
    return snapshot
