# 1С (БГУ): авто-подгрузка долгов через релей — конфиг, креды, запуск, relay-config, sync.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

import uuid
import json
from datetime import datetime, timedelta
from app.core.time_utils import utcnow
from typing import Optional
from fastapi import Depends, HTTPException, UploadFile, File, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from app.core.config import settings
from app.core.database import get_db
from app.modules.utility.models import User, DebtImportLog, SystemSetting
from app.core.dependencies import get_current_user
from app.core.auth import fernet
from app.modules.utility.tasks import import_debts_task, onec_autopublish_task

from ._shared import (
    router,
    logger,
    _save_uploaded_debt_file,
    _check_gisgmp_token,
    _require_finance,
)


# =========================================================================
# 1С (БГУ) — АВТО-ПОДГРУЗКА ДОЛГОВ/ПЕРЕПЛАТ ЧЕРЕЗ РЕЛЕЙ (браузер-автоматизация)
# =========================================================================
# Тот же релей-демон, что ходит в ГИС ГМП, headless-браузером (Playwright)
# заходит в веб-клиент 1С:БГУ (sv00web19.mchs.ru), формирует ОСВ по счёту за
# период (с начала года → сегодня), сохраняет в Excel и шлёт сюда. Файл проходит
# через ТОТ ЖЕ парсер, что и ручная загрузка Excel-ОСВ (sync_import_debts_process,
# stage_only) → черновик → ручная кнопка «Выгрузить». Креды 1С вводятся в UI,
# шифруются (Fernet, ENCRYPTION_KEY) и отдаются релею ОДИН раз через relay-config
# (token+HTTPS). Машинный канал авторизуется тем же GISGMP_SYNC_TOKEN (один
# доверенный демон на одной ВМ). Дебетовое сальдо ОСВ = долг, кредитовое = переплата.

ONEC_RELAY_KEY = "onec_relay"
_ONEC_RELAY_DEFAULTS = {
    "enabled": False,
    "base_url": "https://sv00web19.mchs.ru",   # хост публикации 1С
    "infobase_path": "",                        # путь инфобазы в URL (если есть)
    "login": None,                             # логин 1С (показываем в статусе)
    "password_enc": None,                      # Fernet-шифр пароля (в статус НЕ отдаём)
    "creds_pending": False,                    # есть неотданная смена учётки
    "creds_version": 0,                         # версия учётки (ack-доставка релею)
    "report_name": "Оборотно-сальдовая ведомость по счёту",
    "account_naem": "205.31",                  # счёт наёма 1С → наш account_type «205»
    "account_comm": "",                        # счёт коммуналки 1С → наш «209» (заполнить)
    "headless": True,
    "daily_hour": 6,                           # час ежедневного авто-сбора (МСК)
    "run_now": False,
    "probe": False,                            # режим разведки: логин + скрины/DOM, без сбора
    "last_run_at": None, "last_poll_at": None,
    "last_report_at": None, "last_status": None, "last_message": None,
    "last_count_205": 0, "last_count_209": 0,
    "relay_version": None,
}


async def _load_onec_cfg(db: AsyncSession) -> dict:
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == ONEC_RELAY_KEY)
    )).scalars().first()
    cfg = dict(_ONEC_RELAY_DEFAULTS)
    if row and row.value:
        try:
            cfg.update(json.loads(row.value))
        except Exception:
            pass
    return cfg


async def _save_onec_cfg(db: AsyncSession, cfg: dict) -> None:
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == ONEC_RELAY_KEY)
    )).scalars().first()
    if row is None:
        row = SystemSetting(key=ONEC_RELAY_KEY, value="{}",
                            description="Конфиг и статус авто-подгрузки 1С (релей, браузер)")
        db.add(row)
    row.value = json.dumps(cfg, ensure_ascii=False)
    await db.commit()


def _onec_public(cfg: dict) -> dict:
    """Конфиг для UI — без пароля (только факт его наличия)."""
    out = {k: v for k, v in cfg.items() if k != "password_enc"}
    out["has_password"] = bool(cfg.get("password_enc"))
    return out


def _onec_allowed_hosts() -> set:
    return {h.strip().lower() for h in (settings.ONEC_ALLOWED_HOSTS or "").split(",") if h.strip()}


def _onec_host_ok(base_url: str) -> bool:
    """https + хост в белом списке — иначе релею НЕ отдаём расшифрованные креды
    (защита от подмены base_url → увода учётки 1С на чужой хост)."""
    from urllib.parse import urlparse
    try:
        u = urlparse((base_url or "").strip())
    except Exception:
        return False
    allow = _onec_allowed_hosts()
    return u.scheme == "https" and bool(u.hostname) and u.hostname.lower() in allow


# ─── UI (финансист) ──────────────────────────────────────────────────────
@router.get("/onec/status", summary="Статус авто-подгрузки 1С (UI)")
async def onec_status(current_user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    _require_finance(current_user)
    cfg = await _load_onec_cfg(db)
    online = False
    lp = cfg.get("last_poll_at")
    if lp:
        try:
            online = (utcnow() - datetime.fromisoformat(lp)).total_seconds() < 360
        except Exception:
            online = False
    pub = _onec_public(cfg)
    pub["online"] = online
    return pub


@router.get("/onec/last-found", summary="Что нашёл релей в последнем сборе 1С (ФИО + долги/переплаты)")
async def onec_last_found(current_user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """Последний АВТО-сбор релея из 1С: ФИО с долгами/переплатами по 209/205 —
    и сопоставленные с базой, и не найденные. Read-only: читает applied_state/
    not_found_users последних staged|completed DebtImportLog с пометкой «(авто)»."""
    _require_finance(current_user)
    rows: dict = {}
    meta: dict = {}
    for acc in ("209", "205"):
        log = (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.account_type == acc,
                DebtImportLog.status.in_(["staged", "completed"]),
                DebtImportLog.file_name.ilike("%(авто)%"),
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()
        meta[acc] = ({"file": log.file_name, "status": log.status,
                      "at": log.started_at.isoformat() if log.started_at else None,
                      "matched": log.processed, "not_found": log.not_found_count}
                     if log else None)
        if not log:
            continue

        def _row(fio):
            return rows.setdefault(fio, {"fio": fio, "matched": False,
                                         "debt_209": 0.0, "over_209": 0.0,
                                         "debt_205": 0.0, "over_205": 0.0})
        for st in (log.applied_state or {}).values():
            fio = (st.get("username") or "").strip()
            if not fio:
                continue
            r = _row(fio)
            r["matched"] = True   # есть в applied_state → сопоставлен с жильцом
            r[f"debt_{acc}"] += float(st.get(f"debt_{acc}") or 0)
            r[f"over_{acc}"] += float(st.get(f"overpayment_{acc}") or 0)
        for nf in (log.not_found_users or []):
            fio = (nf.get("fio") or "").strip()
            if not fio:
                continue
            r = _row(fio)   # matched остаётся False, если только в not_found
            r[f"debt_{acc}"] += float(nf.get("debt") or 0)
            r[f"over_{acc}"] += float(nf.get("overpayment") or 0)

    items = sorted(rows.values(), key=lambda x: (x["matched"], x["fio"]))
    totals = {
        "people": len(items),
        "matched": sum(1 for x in items if x["matched"]),
        "not_found": sum(1 for x in items if not x["matched"]),
        "debt_209": round(sum(x["debt_209"] for x in items), 2),
        "debt_205": round(sum(x["debt_205"] for x in items), 2),
        "over_209": round(sum(x["over_209"] for x in items), 2),
        "over_205": round(sum(x["over_205"] for x in items), 2),
    }
    return {"meta": meta, "totals": totals, "items": items[:3000]}


class OnecConfigIn(BaseModel):
    enabled: Optional[bool] = None
    base_url: Optional[str] = None
    infobase_path: Optional[str] = None
    account_naem: Optional[str] = None
    account_comm: Optional[str] = None
    daily_hour: Optional[int] = None
    headless: Optional[bool] = None


@router.put("/onec/config", summary="Настройки авто-подгрузки 1С")
async def onec_config_put(payload: OnecConfigIn,
                          current_user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    _require_finance(current_user)
    cfg = await _load_onec_cfg(db)
    data = payload.model_dump(exclude_none=True)
    if "daily_hour" in data:
        data["daily_hour"] = max(0, min(23, int(data["daily_hour"])))
    if "base_url" in data:
        data["base_url"] = data["base_url"].strip().rstrip("/")
        if not _onec_host_ok(data["base_url"]):
            allow = ", ".join(sorted(_onec_allowed_hosts())) or "(список пуст)"
            raise HTTPException(
                status_code=400,
                detail=f"Недопустимый адрес 1С: нужен https и хост из белого списка "
                       f"({allow}). Иначе учётка 1С могла бы уйти на чужой хост.",
            )
    if "infobase_path" in data:
        data["infobase_path"] = data["infobase_path"].strip()
    cfg.update(data)
    await _save_onec_cfg(db, cfg)
    return _onec_public(cfg)


class OnecCredsIn(BaseModel):
    login: str
    password: str


@router.post("/onec/credentials", summary="Логин/пароль 1С (шифруется, уходит релею один раз)")
async def onec_credentials(payload: OnecCredsIn,
                           current_user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    _require_finance(current_user)
    u = (payload.login or "").strip()
    p = payload.password or ""
    if not u or not p:
        raise HTTPException(status_code=400, detail="Укажите логин и пароль 1С")
    # \n/\r ломают построчный onec.env на релее (молчаливое обрезание пароля).
    if any(ch in (u + p) for ch in ("\n", "\r")):
        raise HTTPException(status_code=400, detail="Логин/пароль не должны содержать переводы строк")
    cfg = await _load_onec_cfg(db)
    cfg["login"] = u
    cfg["password_enc"] = fernet.encrypt(p.encode()).decode()
    cfg["creds_pending"] = True
    cfg["creds_version"] = int(cfg.get("creds_version", 0)) + 1
    await _save_onec_cfg(db, cfg)
    return {"ok": True, "queued": True}


@router.post("/onec/run-now", summary="Запустить сбор 1С сейчас (probe=true — только разведка)")
async def onec_run_now(probe: bool = False,
                       current_user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    _require_finance(current_user)
    cfg = await _load_onec_cfg(db)
    if not cfg.get("enabled"):
        # Иначе run_now «зависнет» взведённым и сработает неожиданно при включении.
        raise HTTPException(status_code=400, detail="Сначала включите авто-подгрузку 1С (галка «Включена»)")
    if not cfg.get("login") or not cfg.get("password_enc"):
        raise HTTPException(status_code=400, detail="Сначала задайте логин/пароль 1С")
    if not probe and not (cfg.get("account_naem") or cfg.get("account_comm")):
        raise HTTPException(status_code=400, detail="Укажите хотя бы один счёт (наём/коммуналка)")
    cfg["run_now"] = True
    cfg["probe"] = bool(probe)
    await _save_onec_cfg(db, cfg)
    return {"ok": True, "probe": bool(probe)}


# ─── Релей (token-auth, тот же GISGMP_SYNC_TOKEN) ─────────────────────────
@router.get("/onec/relay-config", summary="Релей берёт конфиг 1С (token-auth)")
async def onec_relay_config(v: Optional[str] = None,
                            creds_ack: Optional[int] = None,
                            authorization: Optional[str] = Header(None),
                            db: AsyncSession = Depends(get_db)):
    _check_gisgmp_token(authorization)
    cfg = await _load_onec_cfg(db)
    cfg["last_poll_at"] = utcnow().isoformat()
    if v:
        cfg["relay_version"] = v
    # Релей подтвердил приём учётки версии creds_ack → гасим pending. Это
    # at-least-once: пока ack не пришёл, учётка отдаётся снова (потеря ответа не
    # теряет пароль навсегда).
    if creds_ack is not None and int(creds_ack) == int(cfg.get("creds_version", 0)):
        cfg["creds_pending"] = False

    should_run, reason = False, ""
    if cfg.get("enabled"):
        if cfg.get("run_now"):
            should_run, reason = True, ("probe" if cfg.get("probe") else "run_now")
        else:
            msk = timedelta(hours=3)
            now_msk = utcnow() + msk
            daily_hour = max(0, min(23, int(cfg.get("daily_hour", 6))))
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

    probe = bool(cfg.get("probe")) and should_run
    # Креды отдаём ПОКА pending (гасит только ack релея, см. выше) и ТОЛЬКО на
    # разрешённый хост — иначе подменённый base_url увёл бы пароль 1С на чужой хост.
    creds = None
    if cfg.get("creds_pending") and cfg.get("password_enc"):
        if not _onec_host_ok(cfg.get("base_url")):
            cfg["last_status"] = "error"
            cfg["last_message"] = ("Учётка 1С НЕ отдана релею: base_url вне белого списка "
                                   "(ONEC_ALLOWED_HOSTS). Исправьте адрес 1С.")
        else:
            try:
                creds = {"login": cfg.get("login"),
                         "version": int(cfg.get("creds_version", 0)),
                         "password": fernet.decrypt(cfg["password_enc"].encode()).decode()}
            except Exception:
                # Битый шифр / сменился ENCRYPTION_KEY — не зацикливаемся каждый опрос.
                logger.error("[onec] пароль 1С не расшифровывается — сбрасываю, нужна повторная установка")
                cfg["password_enc"] = None
                cfg["creds_pending"] = False
                cfg["last_status"] = "error"
                cfg["last_message"] = "Пароль 1С не расшифровывается (сменился ключ?) — введите учётку заново."

    if should_run:
        cfg["run_now"] = False
        cfg["last_run_at"] = utcnow().isoformat()
        if probe:
            cfg["probe"] = False  # разведка одноразовая

    await _save_onec_cfg(db, cfg)

    now_msk = utcnow() + timedelta(hours=3)
    period = {"from": f"01.01.{now_msk.year}", "to": now_msk.strftime("%d.%m.%Y")}
    accounts = [a for a in [
        ({"code": cfg.get("account_naem"), "account_type": "205"} if cfg.get("account_naem") else None),
        ({"code": cfg.get("account_comm"), "account_type": "209"} if cfg.get("account_comm") else None),
    ] if a]
    return {
        "enabled": bool(cfg.get("enabled")),
        "should_run": should_run, "reason": reason, "probe": probe,
        "base_url": cfg.get("base_url"), "infobase_path": cfg.get("infobase_path"),
        "headless": bool(cfg.get("headless", True)),
        "report_name": cfg.get("report_name"),
        "accounts": accounts,
        "period": period,
        "credentials": creds,   # None пока ack не получен / нет смены / хост запрещён
        "creds_version": int(cfg.get("creds_version", 0)),
    }


class OnecReportIn(BaseModel):
    ok: bool = True
    status: Optional[str] = None
    message: str = ""
    count_205: int = 0
    count_209: int = 0


@router.post("/onec/relay-report", summary="Релей отчитывается о прогоне 1С (token-auth)")
async def onec_relay_report(payload: OnecReportIn,
                            authorization: Optional[str] = Header(None),
                            db: AsyncSession = Depends(get_db)):
    _check_gisgmp_token(authorization)
    cfg = await _load_onec_cfg(db)
    cfg["last_report_at"] = utcnow().isoformat()
    cfg["last_status"] = (payload.status or ("ok" if payload.ok else "error"))
    cfg["last_message"] = (payload.message or "")[:2000]
    cfg["last_count_205"] = int(payload.count_205 or 0)
    cfg["last_count_209"] = int(payload.count_209 or 0)
    await _save_onec_cfg(db, cfg)
    return {"ok": True}


@router.post("/onec/sync", summary="Релей шлёт Excel-ОСВ из 1С → черновик долгов (token-auth)")
async def onec_sync(
    file_205: UploadFile = File(None),
    file_209: UploadFile = File(None),
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Принимает один-два Excel-ОСВ (наём=205, коммуналка=209), сохраняет и
    ставит ТЕ ЖЕ задачи импорта, что и ручная парная загрузка — stage_only,
    т.е. ЧЕРНОВИК. Применит долги жильцам отдельная кнопка «Выгрузить»."""
    _check_gisgmp_token(authorization)
    if not file_205 and not file_209:
        raise HTTPException(status_code=400, detail="Передайте хотя бы один файл (205/209)")
    batch_id = str(uuid.uuid4())
    from celery import chain
    signatures = []
    out = []
    # 209 первым (как в ручной парной загрузке — сериализация через chain).
    for f, account in [(file_209, "209"), (file_205, "205")]:
        if f is None:
            continue
        file_path, _orig = await _save_uploaded_debt_file(f, account, batch_id)
        signatures.append(import_debts_task.si(
            file_path, account,
            started_by_id=None,
            started_by_username="1С (авто, релей)",
            batch_id=batch_id,
            original_file_name=f"1С ОСВ {account} (авто).xlsx",
        ))
        out.append({"account_type": account})
    if not signatures:
        return {"status": "noop", "batch_id": batch_id}
    # После staged-импортов (209→205) — авто-выгрузка долгов жильцам (guard=True):
    # кошелёк в ЛК/квитанция/админка всегда свежие из 1С, без ручной кнопки.
    signatures.append(onec_autopublish_task.si(batch_id=batch_id))
    chain(*signatures).apply_async()
    logger.info("[ONEC] staged import + автовыгрузка batch=%s accounts=%s",
                batch_id, [o["account_type"] for o in out])
    return {"status": "processing", "batch_id": batch_id, "accounts": out}
