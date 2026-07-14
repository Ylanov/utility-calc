# ГИС ГМП: приём начислений от моста-расширения (sync/status) + конфиг и управление релеем.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

import asyncio
import json
from pathlib import Path
from datetime import datetime, timedelta
from app.core.time_utils import utcnow
from typing import Optional
from fastapi import Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text
from app.core.config import settings
from app.core.database import get_db
from app.modules.utility.models import User, SystemSetting
from app.core.dependencies import get_current_user
from app.core.auth import fernet

from ._shared import (
    router,
    logger,
    GISGMP_FINDINGS_KEY,
    _check_gisgmp_token,
    _load_relay_cfg,
    _save_relay_cfg,
    _capture_actualize_after,
    _drive_actualize_runs,
    _require_finance,
)


# =========================================================================
# ГИС ГМП — АВТО-ПОДГРУЗКА ДОЛГОВ (мост-расширение gisgmp-bridge)
# =========================================================================
# Расширение под ЭЦП-сессией пользователя парсит «Начисления» реестра
# gisgmp.cgu.mchs.ru и POST'ит сюда массив. Авторизация — статический
# GISGMP_SYNC_TOKEN (не пользовательский JWT: фоновый синк раз в 12 ч,
# короткий токен протух бы). Сервис матчит ФИО → жильца и пишет долги
# 209/205 в активный период, создавая пару DebtImportLog.

class GisgmpChargeIn(BaseModel):
    """Одна строка «Начислений» реестра (как её распарсило расширение)."""
    uin: Optional[str] = None
    amount: str = "0"                  # сумма начисления (строкой, чистим на бэке)
    bill_date: Optional[str] = None    # дата начисления
    actualize_date: Optional[str] = None
    payer_name: str = ""               # ФИО плательщика — ключ матчинга
    account: Optional[str] = None      # лицевой счёт (привязан к квартире)
    purpose: str = ""                  # «Назначение» → 209/205
    ack_status: str = ""               # статус квитирования (оплачено/нет)
    change_status: str = ""            # статус изменения (эталонное/аннулирование/…)
    charge_uuid: Optional[str] = None
    source: Optional[str] = None


class GisgmpSyncIn(BaseModel):
    charges: list[GisgmpChargeIn]


# Защита от случайного гигантского POST'а. Реальный объём — сотни жильцов ×
# ~24 начисления = единицы тысяч строк; 50k берём с большим запасом.
_GISGMP_MAX_CHARGES = 50_000


def _run_gisgmp_sync(charges: list[dict]) -> dict:
    """Синхронный прогон импортёра в отдельной сессии (вызывается в потоке)."""
    from app.core.database import sync_db_session
    from app.modules.utility.services.gisgmp_import import sync_import_gisgmp_charges
    with sync_db_session() as db:
        return sync_import_gisgmp_charges(charges, db)


@router.post("/gisgmp/sync", summary="Авто-подгрузка долгов из реестра ГИС ГМП (мост-расширение)")
async def gisgmp_sync(
    payload: GisgmpSyncIn,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Принимает распарсенные начисления от расширения и заливает долги.

    Авторизация — статический GISGMP_SYNC_TOKEN в заголовке Authorization:
    Bearer. Обработка синхронная (в пуле потоков) — расширению нужен прямой
    результат синка, а объём данных небольшой.
    """
    _check_gisgmp_token(authorization)

    charges = [c.model_dump() for c in payload.charges]
    if not charges:
        raise HTTPException(status_code=400, detail="Пустой список начислений")
    if len(charges) > _GISGMP_MAX_CHARGES:
        raise HTTPException(
            status_code=413,
            detail=f"Слишком много начислений за раз (>{_GISGMP_MAX_CHARGES}). Разбейте на части.",
        )

    result = await asyncio.to_thread(_run_gisgmp_sync, charges)
    if result.get("status") == "error":
        # Нет активного периода и т.п. — 409, расширение покажет в статусе.
        raise HTTPException(status_code=409, detail=result.get("message", "Импорт не выполнен"))
    # Кэш/находки только что обновились — если есть прогон актуализации, ждущий
    # снимка «после», снимаем его сейчас (по свежей сверке). Тихо, без влияния на синк.
    try:
        await _capture_actualize_after(db)
    except Exception:
        logger.exception("[gisgmp] снимок «после» актуализации не удался")
    return result


@router.get("/gisgmp/status", summary="Статус последней авто-подгрузки ГИС ГМП")
async def gisgmp_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Для карточки «Авто-подгрузка ГИС ГМП» во вкладке «Долги 1С»: когда был
    последний синк, сколько жильцов затронуто, не настроен ли токен."""
    _require_finance(current_user)
    relay = await _load_relay_cfg(db)
    # Только счётчики находок (а не весь блоб) — статус опрашивается часто (15с).
    fres = (await db.execute(
        text("WITH f AS (SELECT value::jsonb AS j FROM system_settings WHERE key = :k) "
             "SELECT j->>'synced_at' AS synced_at, j->>'total_charges' AS total_charges, "
             "j->>'residents' AS residents, j->>'matched' AS matched, "
             "j->>'not_found' AS not_found FROM f"),
        {"k": GISGMP_FINDINGS_KEY},
    )).first()
    fsum = None
    if fres and fres.synced_at is not None:
        def _ci(v):
            try:
                return int(v)
            except Exception:
                return 0
        fsum = {"synced_at": fres.synced_at, "total_charges": _ci(fres.total_charges),
                "residents": _ci(fres.residents), "matched": _ci(fres.matched),
                "not_found": _ci(fres.not_found)}

    # Онлайн релея (по последнему опросу конфига) + версия (актуальна ли она).
    poll_s = int(relay.get("relay_poll_seconds") or 120)
    poll_age, online = None, False
    lp = relay.get("last_poll_at")
    if lp:
        try:
            poll_age = (utcnow() - datetime.fromisoformat(lp)).total_seconds()
            online = poll_age < max(300, poll_s * 3)
        except Exception:
            pass
    relay_latest = None
    try:
        rp = Path(__file__).resolve().parents[5] / "relay" / "gisgmp" / "relay.py"  # parents[5]: модуль лежит на уровень глубже (пакет financier/)
        for line in rp.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("RELAY_VERSION"):
                # split('#') срезает хвостовой комментарий — иначе он попадал в
                # версию и панель вечно показывала «обновление доступно».
                relay_latest = line.split("#", 1)[0].split("=", 1)[1].strip().strip('"').strip("'")
                break
    except Exception:
        pass

    return {
        "configured": bool((settings.GISGMP_SYNC_TOKEN or "").strip()),
        "registry_url": "https://gisgmp.cgu.mchs.ru/charge/",
        # Состояние релея — управляется из этой же вкладки (pull-модель).
        "relay": {
            "enabled": relay.get("enabled", True),
            "months_back": relay.get("months_back", 2),
            "interval_hours": relay.get("interval_hours", 12),
            "daily_hour": relay.get("daily_hour", 22),
            "run_now": relay.get("run_now", False),
            "last_run_at": relay.get("last_run_at"),
            "last_report_at": relay.get("last_report_at"),
            "last_status": relay.get("last_status"),
            "last_message": relay.get("last_message"),
            "last_count": relay.get("last_count", 0),
            "online": online,
            "last_poll_at": relay.get("last_poll_at"),
            "poll_age_seconds": poll_age,
            "relay_poll_seconds": poll_s,
            "relay_version": relay.get("relay_version"),
            "relay_latest_version": relay_latest,
            "passport_username": relay.get("passport_username"),
            "pending": {
                "self_update": bool(relay.get("self_update")),
                "restart": bool(relay.get("restart")),
                "credentials": bool(relay.get("creds_pending")),
            },
        },
        # Сводка последних находок (ГИС ГМП пока хранится ОТДЕЛЬНО от долгов).
        "findings": fsum,
    }


@router.get("/gisgmp/relay-config", summary="Релей берёт свой конфиг (token-auth)")
async def gisgmp_relay_config(
    v: Optional[str] = None,
    poll: Optional[int] = None,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Релей опрашивает этот эндпоинт раз в ~2 мин. Если пора запускаться
    (run_now или истёк интервал) — сервер атомарно «забирает» запуск (сдвигает
    last_run_at, гасит run_now) и возвращает should_run=true.
    v/poll — версия релея и его интервал опроса (для индикатора онлайн/версия)."""
    _check_gisgmp_token(authorization)
    cfg = await _load_relay_cfg(db)
    # Отметка «релей на связи» + его версия/интервал (для статуса в UI).
    cfg["last_poll_at"] = utcnow().isoformat()
    if v:
        cfg["relay_version"] = v
    if poll:
        cfg["relay_poll_seconds"] = int(poll)

    should_run, reason = False, ""
    if cfg.get("enabled", True):
        if cfg.get("run_now"):
            should_run, reason = True, "run_now"
        else:
            # Раз в сутки вечером, когда нет нагрузки. Час задаётся по МСК
            # (Москва = UTC+3 круглый год, без перехода). Запуск на ПЕРВОМ опросе
            # после daily_hour, если сегодня ещё не запускались.
            msk = timedelta(hours=3)
            now_msk = utcnow() + msk
            daily_hour = max(0, min(23, int(cfg.get("daily_hour", 22))))
            target = now_msk.replace(hour=daily_hour, minute=0, second=0, microsecond=0)
            last = cfg.get("last_run_at")
            if now_msk >= target:
                if not last:
                    should_run, reason = True, "daily_first"
                else:
                    try:
                        if datetime.fromisoformat(last) + msk < target:
                            should_run, reason = True, "daily"
                    except Exception:
                        should_run, reason = True, "bad_last_run"

    if should_run:
        cfg["run_now"] = False
        cfg["last_run_at"] = utcnow().isoformat()

    # Курсор инкремента: релей дотягивает только начисления новее этой даты
    # актуализации (всё, что старше, уже в кэше). None → первый полный проход.
    cursor_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == "gisgmp_cursor")
    )).scalars().first()
    since = None
    if cursor_row and cursor_row.value:
        try:
            since = json.loads(cursor_row.value).get("since")
        except Exception:
            since = None

    # Авто-перепроверка результата актуализации: «sent»-прогоны старше ~2ч ставим
    # их ФИО в gisgmp_recheck ДО чтения очереди ниже — чтобы релей забрал этим же
    # опросом и снял «после» по уже обработанным ГИС начислениям.
    try:
        if await _drive_actualize_runs(db) and not should_run:
            should_run, reason = True, "loop-check"  # лёгкий инкрем. сбор для активных циклов
    except Exception:
        logger.exception("[gisgmp] авто-цикл актуализации не удался")

    # Очередь точечного дотягивания проблемных ФИО (где ГИС занижен):
    # отдаём список и сразу гасим (claim), чтобы не повторять.
    rc_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == "gisgmp_recheck")
    )).scalars().first()
    recheck = None
    if rc_row and rc_row.value:
        try:
            rc = json.loads(rc_row.value)
        except Exception:
            rc = {}
        if rc.get("surnames"):
            recheck = {"surnames": rc["surnames"], "deep_months": int(rc.get("deep_months", 36))}
            rc_row.value = json.dumps({}, ensure_ascii=False)
            await db.commit()

    # Команды управления демоном из UI: отдаём и сразу гасим (claim-на-выдаче),
    # чтобы выполнились РОВНО один раз. Релей применит и перезапустится (execv).
    control = {}
    if cfg.get("self_update"):
        control["self_update"] = True
        cfg["self_update"] = False
    if cfg.get("restart"):
        control["restart"] = True
        cfg["restart"] = False
    if cfg.get("creds_pending") and cfg.get("passport_username") and cfg.get("passport_password_enc"):
        try:
            control["credentials"] = {
                "username": cfg["passport_username"],
                "password": fernet.decrypt(cfg["passport_password_enc"].encode()).decode(),
            }
            cfg["creds_pending"] = False
        except Exception:
            pass

    # Очереди аннулирования/актуализации: отдаём UUID один раз, помечаем
    # running. САМОВОССТАНОВЛЕНИЕ (fix 2026-07-14): если релей умер посреди
    # списка (деплой/рестарт веба, падение), finished не приходит и running
    # висел НАВСЕГДА — очередь больше не выдавалась, прогон застревал
    # («отправка 1070 из 1125» несколько дней), а act_busy блокировал ещё и
    # авто-доведение. Теперь: running при молчащем прогрессе (last_at/
    # started_at старше 30 мин; прогресс идёт каждые ~12с) считается мёртвым —
    # очередь выдаётся заново (повторный actualize/у релея идемпотентен).
    def _claim_queue(qv: dict) -> tuple[bool, bool]:
        """(выдать_сейчас, был_ли_рестарт_мёртвого)."""
        if not qv.get("uuids"):
            return False, False
        if not qv.get("running"):
            return True, False
        ts = qv.get("last_at") or qv.get("started_at")
        try:
            stale = ts is None or (
                utcnow() - datetime.fromisoformat(ts)
            ).total_seconds() > 1800
        except Exception:
            stale = True
        return stale, stale

    an_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == "gisgmp_annul")
    )).scalars().first()
    annul = None
    if an_row and an_row.value:
        try:
            anv = json.loads(an_row.value)
        except Exception:
            anv = {}
        _give, _restarted = _claim_queue(anv)
        if _give:
            annul = {"uuids": anv["uuids"]}
            anv["running"] = True
            anv["started_at"] = utcnow().isoformat()
            if _restarted:
                anv["restarted"] = int(anv.get("restarted") or 0) + 1
                logger.warning("[gisgmp] очередь аннулирования перевыдана после "
                               "мёртвого прогона (restarted=%s)", anv["restarted"])
            an_row.value = json.dumps(anv, ensure_ascii=False)
            await db.commit()

    act_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == "gisgmp_actualize")
    )).scalars().first()
    actualize = None
    if act_row and act_row.value:
        try:
            av = json.loads(act_row.value)
        except Exception:
            av = {}
        _give, _restarted = _claim_queue(av)
        if _give:
            actualize = {"uuids": av["uuids"]}
            av["running"] = True
            av["started_at"] = utcnow().isoformat()
            if _restarted:
                av["restarted"] = int(av.get("restarted") or 0) + 1
                logger.warning("[gisgmp] очередь актуализации перевыдана после "
                               "мёртвого прогона (restarted=%s, uuids=%s)",
                               av["restarted"], len(av.get("uuids") or []))
            act_row.value = json.dumps(av, ensure_ascii=False)
            await db.commit()

    # Один сейв в конце: last_poll_at/версия + claim команд + claim запуска.
    await _save_relay_cfg(db, cfg)

    return {
        "enabled": cfg.get("enabled", True),
        "months_back": cfg.get("months_back", 36),
        "should_run": should_run,
        "reason": reason,
        "since": since,
        "recheck": recheck,
        "control": control or None,
        "actualize": actualize,
        "annul": annul,
    }


class GisgmpRelayReportIn(BaseModel):
    ok: bool
    count: int = 0
    updated: int = 0
    created: int = 0
    not_found: int = 0
    message: str = ""


@router.post("/gisgmp/relay-report", summary="Релей шлёт отчёт о прогоне (token-auth)")
async def gisgmp_relay_report(
    payload: GisgmpRelayReportIn,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    _check_gisgmp_token(authorization)
    cfg = await _load_relay_cfg(db)
    cfg["last_status"] = "ok" if payload.ok else "error"
    cfg["last_message"] = (payload.message or "")[:500]
    cfg["last_count"] = payload.count
    cfg["last_updated"] = payload.updated
    cfg["last_created"] = payload.created
    cfg["last_not_found"] = payload.not_found
    cfg["last_report_at"] = utcnow().isoformat()
    await _save_relay_cfg(db, cfg)
    return {"ok": True}


class GisgmpRelaySettingsIn(BaseModel):
    enabled: Optional[bool] = None
    months_back: Optional[int] = None
    interval_hours: Optional[int] = None
    daily_hour: Optional[int] = None


@router.put("/gisgmp/relay-config", summary="Админ меняет настройки релея")
async def gisgmp_relay_set(
    payload: GisgmpRelaySettingsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    cfg = await _load_relay_cfg(db)
    if payload.enabled is not None:
        cfg["enabled"] = bool(payload.enabled)
    if payload.months_back is not None:
        cfg["months_back"] = max(1, min(60, int(payload.months_back)))
    if payload.interval_hours is not None:
        cfg["interval_hours"] = max(1, min(168, int(payload.interval_hours)))
    if payload.daily_hour is not None:
        cfg["daily_hour"] = max(0, min(23, int(payload.daily_hour)))
    await _save_relay_cfg(db, cfg)
    return cfg


@router.post("/gisgmp/run-now", summary="Админ ставит флаг «запустить релей сейчас»")
async def gisgmp_run_now(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    cfg = await _load_relay_cfg(db)
    cfg["run_now"] = True
    await _save_relay_cfg(db, cfg)
    return {"ok": True, "queued": True}


@router.post("/gisgmp/relay-update", summary="Админ: релей подтянет свежий код и перезапустится")
async def gisgmp_relay_update(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    cfg = await _load_relay_cfg(db)
    cfg["self_update"] = True
    await _save_relay_cfg(db, cfg)
    return {"ok": True, "queued": True}


@router.post("/gisgmp/relay-restart", summary="Админ: перезапустить демон релея")
async def gisgmp_relay_restart(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    cfg = await _load_relay_cfg(db)
    cfg["restart"] = True
    await _save_relay_cfg(db, cfg)
    return {"ok": True, "queued": True}


class GisgmpRelayCredsIn(BaseModel):
    username: str
    password: str


@router.post("/gisgmp/relay-credentials", summary="Админ: сменить логин/пароль входа в реестр")
async def gisgmp_relay_credentials(
    payload: GisgmpRelayCredsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Логин/пароль passport для релея. Пароль шифруется (Fernet, ключ
    ENCRYPTION_KEY) и хранится в SystemSetting; релей заберёт его ОДИН раз через
    relay-config (token+HTTPS), запишет в свой relay.env и перезапустится.
    В статус пароль не отдаётся — только логин."""
    _require_finance(current_user)
    u = (payload.username or "").strip()
    p = payload.password or ""
    if not u or not p:
        raise HTTPException(status_code=400, detail="Укажите логин и пароль")
    cfg = await _load_relay_cfg(db)
    cfg["passport_username"] = u
    cfg["passport_password_enc"] = fernet.encrypt(p.encode()).decode()
    cfg["creds_pending"] = True
    await _save_relay_cfg(db, cfg)
    return {"ok": True, "queued": True}
