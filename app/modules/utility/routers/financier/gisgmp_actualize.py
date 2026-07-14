# ГИС ГМП: очереди дотягивания/актуализации, аннулирование, прогресс/аудит, relay.py, bridge.zip.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

import os
import io
import json
import zipfile
from pathlib import Path
from datetime import timedelta
from app.core.time_utils import utcnow
from typing import Optional
from fastapi import Depends, HTTPException, Query, Header
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core.config import settings
from app.core.database import get_db
from app.modules.utility.models import User, SystemSetting
from app.core.dependencies import get_current_user

from ._shared import (
    router,
    logger,
    GISGMP_ACTUALIZE_KEY,
    GISGMP_ACTUALIZE_LOG_KEY,
    _ACTUALIZE_LOG_MAX_RUNS,
    _check_gisgmp_token,
    _load_relay_cfg,
    _save_relay_cfg,
    _load_findings,
    _build_reconcile,
    _load_person_charges,
    _require_finance,
)


GISGMP_RECHECK_KEY = "gisgmp_recheck"


@router.post("/gisgmp/recheck-build", summary="Очередь дотягивания: проблемные ФИО (ГИС<1С)")
async def gisgmp_recheck_build(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Берёт из сверки жильцов, где ГИС занижен (флаги «1С>ГИС» и «нет в ГИС»),
    и ставит их фамилии в очередь. Релей точечно дотянет их полную историю
    (36 мес) по фамилии — сошедшихся и «ГИС>1С» не трогаем."""
    _require_finance(current_user)
    rec = await _build_reconcile(db)
    surnames, seen = [], set()
    for r in rec.get("residents", []):
        if r.get("flag") not in ("c1_more", "only_1c"):
            continue
        parts = (r.get("username") or "").strip().split()
        if not parts:
            continue
        s = parts[0]
        if s.lower() not in seen:
            seen.add(s.lower())
            surnames.append(s)
    payload = {"surnames": surnames, "deep_months": 36, "requested_at": utcnow().isoformat()}
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_RECHECK_KEY)
    )).scalars().first()
    if row is None:
        row = SystemSetting(key=GISGMP_RECHECK_KEY, value="{}",
                            description="Очередь точечного дотягивания ГИС ГМП")
        db.add(row)
    row.value = json.dumps(payload, ensure_ascii=False)
    await db.commit()
    return {"queued": len(surnames)}


def _run_surnames(run: dict) -> set[str]:
    """Фамилии (первое слово ФИО) жильцов прогона — для глубокого переопроса."""
    out: set[str] = set()
    for p in run.get("residents", []):
        fio = (p.get("fio") or "").strip()
        if fio:
            out.add(fio.split()[0])
    return out


async def _enqueue_recheck_surnames(db: AsyncSession, surnames: set[str]) -> None:
    """Добавляет фамилии в очередь глубокого переопроса ГИС (gisgmp_recheck),
    мёржит с уже стоящими. Релей заберёт на ближайшем опросе (do_recheck)."""
    if not surnames:
        return
    rc_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == "gisgmp_recheck")
    )).scalars().first()
    cur = {}
    if rc_row and rc_row.value:
        try:
            cur = json.loads(rc_row.value)
        except Exception:
            cur = {}
    merged = sorted(set(cur.get("surnames") or []) | surnames)
    if rc_row is None:
        rc_row = SystemSetting(key="gisgmp_recheck", value="{}",
                               description="Очередь точечного дотягивания ГИС ГМП")
        db.add(rc_row)
    rc_row.value = json.dumps({"surnames": merged, "deep_months": 36}, ensure_ascii=False)


@router.post("/gisgmp/actualize-recheck", summary="Проверить результат актуализации (переопрос ГИС по ФИО)")
async def gisgmp_actualize_recheck(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ставит ФИО последнего прогона актуализации (sent/processing/rechecking) в
    ГЛУБОКИЙ переопрос ГИС + взводит after_pending — релей переопросит, на
    ближайшей сверке снимется «после». Ручной дубль авто-перепроверки: нужен,
    т.к. ГИС обрабатывает «Отправлен в ГИС ГМП» асинхронно и снимок сразу всегда
    показывал «без изменений»."""
    _require_finance(current_user)
    lr = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
    )).scalars().first()
    if not lr or not lr.value:
        return {"queued": 0, "reason": "нет прогонов актуализации"}
    try:
        lj = json.loads(lr.value)
    except Exception:
        return {"queued": 0, "reason": "история повреждена"}
    # Форсируем ЛЁГКИЙ инкрементальный сбор (run_now) — авто-цикл подхватит свежий
    # кэш на ближайшем опросе и обновит «после»/финал. НЕ переопрашиваем сотни ФИО
    # (тяжело для ГИС на массовом прогоне): сбор ловит сквитированные по дате.
    active = [r for r in lj.get("runs", []) if r.get("status") == "checking"]
    if not active:
        return {"queued": 0, "reason": "нет активных циклов актуализации"}
    rc = await _load_relay_cfg(db)
    rc["run_now"] = True
    await _save_relay_cfg(db, rc)
    return {"queued": len(active), "runs": len(active)}


@router.get("/gisgmp/person-charges", summary="Начисления ГИС ГМП по одному ФИО (проваливание в сверке)")
async def gisgmp_person_charges(
    fio: str = Query(..., max_length=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read-only: все начисления человека из кэша ГИС (UIN, счёт, сумма, дата,
    статус квитирования/изменения, uuid). Питает модалку проваливания + кнопки."""
    _require_finance(current_user)
    charges, revocable = await _load_person_charges(db, fio)
    summary = {
        "total": len(charges),
        "revocable": len(revocable),
        "annulled": sum(1 for c in charges if c["annulled"]),
        "sum_revocable": round(sum(c["amount"] for c in charges if c["unpaid"] and not c["annulled"]), 2),
    }
    return {"fio": fio, "charges": charges, "summary": summary}


class GisgmpPersonIn(BaseModel):
    fio: str


@router.post("/gisgmp/actualize-person", summary="Актуализировать начисления ОДНОГО человека")
async def gisgmp_actualize_person(
    payload: GisgmpPersonIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ставит несквитированные начисления одного человека (проваливание в сверке)
    в очередь актуализации — релей дёрнет actualize-request по каждому; результат
    снимется отложенной перепроверкой, как у массовой. Не пишем поверх идущей."""
    _require_finance(current_user)
    fio = (payload.fio or "").strip()
    charges, revocable = await _load_person_charges(db, fio)
    if not revocable:
        return {"queued": 0, "reason": "нет несквитированных начислений у этого ФИО"}
    act_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_KEY)
    )).scalars().first()
    if act_row and act_row.value:
        try:
            cur = json.loads(act_row.value)
            if cur.get("running") or cur.get("uuids"):
                return {"queued": 0, "reason": "идёт другая актуализация — дождитесь завершения"}
        except Exception:
            pass
    run_id = utcnow().isoformat()
    qpayload = {
        "uuids": revocable, "total": len(revocable), "done": 0, "ok": 0, "fail": 0,
        "running": False, "finished": False, "queued_at": run_id,
        "by": current_user.username, "message": "", "targeting": "person", "run_id": run_id,
    }
    if act_row is None:
        act_row = SystemSetting(key=GISGMP_ACTUALIZE_KEY, value="{}",
                                description="Очередь массовой актуализации ГИС ГМП")
        db.add(act_row)
    act_row.value = json.dumps(qpayload, ensure_ascii=False)
    run = {
        "id": run_id, "queued_at": run_id, "by": current_user.username,
        "targeting": f"один человек: {fio}", "total_charges": len(revocable),
        "residents_count": 1, "status": "running", "done": 0, "ok": 0, "fail": 0,
        "started_at": None, "finished_at": None, "after_pending": False, "after_at": None,
        "attempt": 1, "last_actualize_at": run_id,
        "residents": [{
            "user_id": None, "fio": fio, "username": None, "flag": None,
            "before": {"gis": round(sum(c["amount"] for c in charges
                                        if c["unpaid"] and not c["annulled"]), 2),
                       "c1": None, "delta": None},
            "after": None, "result": None,
            "charges": [{"uin": c["uin"], "account": c["account"], "charge_uuid": c["charge_uuid"],
                         "amount": c["amount"], "bill_date": c["bill_date"]}
                        for c in charges if c["unpaid"] and not c["annulled"]],
        }],
    }
    log_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
    )).scalars().first()
    if log_row is None:
        log_row = SystemSetting(key=GISGMP_ACTUALIZE_LOG_KEY, value='{"runs": []}',
                                description="Аудит массовых актуализаций ГИС ГМП (до/после)")
        db.add(log_row)
    try:
        logj = json.loads(log_row.value) if log_row.value else {"runs": []}
    except Exception:
        logj = {"runs": []}
    runs = logj.get("runs", [])
    runs.insert(0, run)
    del runs[_ACTUALIZE_LOG_MAX_RUNS:]
    logj["runs"] = runs
    log_row.value = json.dumps(logj, ensure_ascii=False)
    await db.commit()
    return {"queued": len(revocable), "fio": fio}


# =========================================================================
# АННУЛИРОВАНИЕ начислений ГИС ГМП (разрушительно, но ОБРАТИМО через
# де-аннулирование). Очередь gisgmp_annul → релей do_revoke по revoke-request.
# Только АДМИН + слово-подтверждение. Аудит — gisgmp_annul_log.
# =========================================================================
GISGMP_ANNUL_KEY = "gisgmp_annul"
GISGMP_ANNUL_LOG_KEY = "gisgmp_annul_log"


class GisgmpAnnulIn(BaseModel):
    fio: str
    confirm: str = ""


@router.post("/gisgmp/annul-person", summary="Аннулировать ВСЕ несквитированные начисления человека (ТОЛЬКО админ)")
async def gisgmp_annul_person(
    payload: GisgmpAnnulIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """РАЗРУШИТЕЛЬНО (но ОБРАТИМО де-аннулированием): ставит в очередь аннулирования
    ВСЕ несквитированные (и не аннулированные) начисления ОДНОГО человека. Релей
    дёргает revoke-request по каждому uuid. ТОЛЬКО АДМИН + слово «АННУЛИРОВАТЬ»."""
    if (current_user.role or "") != "admin":
        raise HTTPException(403, "Аннулирование — только администратор")
    if (payload.confirm or "").strip().upper() != "АННУЛИРОВАТЬ":
        raise HTTPException(400, "Нужно подтверждение: впишите слово АННУЛИРОВАТЬ")
    fio = (payload.fio or "").strip()
    charges, revocable = await _load_person_charges(db, fio)
    if not revocable:
        return {"queued": 0, "reason": "нет несквитированных начислений у этого ФИО"}
    an_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ANNUL_KEY)
    )).scalars().first()
    if an_row and an_row.value:
        try:
            cur = json.loads(an_row.value)
            if cur.get("running") or cur.get("uuids"):
                return {"queued": 0, "reason": "идёт другое аннулирование — дождитесь завершения"}
        except Exception:
            pass
    run_id = utcnow().isoformat()
    rev_sum = round(sum(c["amount"] for c in charges if c["unpaid"] and not c["annulled"]), 2)
    qpayload = {
        "uuids": revocable, "total": len(revocable), "done": 0, "ok": 0, "fail": 0,
        "running": False, "finished": False, "queued_at": run_id,
        "by": current_user.username, "fio": fio, "message": "", "run_id": run_id,
    }
    if an_row is None:
        an_row = SystemSetting(key=GISGMP_ANNUL_KEY, value="{}",
                               description="Очередь аннулирования ГИС ГМП")
        db.add(an_row)
    an_row.value = json.dumps(qpayload, ensure_ascii=False)
    run = {
        "id": run_id, "queued_at": run_id, "by": current_user.username, "fio": fio,
        "total_charges": len(revocable), "sum": rev_sum, "status": "running",
        "done": 0, "ok": 0, "fail": 0, "finished_at": None,
        "charges": [{"uin": c["uin"], "account": c["account"], "amount": c["amount"],
                     "charge_uuid": c["charge_uuid"], "bill_date": c["bill_date"]}
                    for c in charges if c["unpaid"] and not c["annulled"]],
    }
    log_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ANNUL_LOG_KEY)
    )).scalars().first()
    if log_row is None:
        log_row = SystemSetting(key=GISGMP_ANNUL_LOG_KEY, value='{"runs": []}',
                                description="Аудит аннулирований ГИС ГМП")
        db.add(log_row)
    try:
        logj = json.loads(log_row.value) if log_row.value else {"runs": []}
    except Exception:
        logj = {"runs": []}
    runs = logj.get("runs", [])
    runs.insert(0, run)
    del runs[50:]
    logj["runs"] = runs
    log_row.value = json.dumps(logj, ensure_ascii=False)
    await db.commit()
    logger.warning("[gisgmp] АННУЛИРОВАНИЕ: %s ставит %d счетов (%.2f) по «%s»",
                   current_user.username, len(revocable), rev_sum, fio)
    return {"queued": len(revocable), "fio": fio, "sum": rev_sum}


class GisgmpAnnulProgressIn(BaseModel):
    done: int = 0
    ok: int = 0
    fail: int = 0
    finished: bool = False
    message: str = ""


@router.post("/gisgmp/annul-progress", summary="Релей шлёт прогресс аннулирования (token)")
async def gisgmp_annul_progress(
    payload: GisgmpAnnulProgressIn,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    _check_gisgmp_token(authorization)
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ANNUL_KEY)
    )).scalars().first()
    if not row or not row.value:
        return {"ok": True}
    try:
        av = json.loads(row.value)
    except Exception:
        av = {}
    av["done"] = payload.done
    av["ok"] = payload.ok
    av["fail"] = payload.fail
    av["last_at"] = utcnow().isoformat()
    if payload.message:
        av["message"] = payload.message[:300]
    if payload.finished:
        av["running"] = False
        av["finished"] = True
        av["finished_at"] = utcnow().isoformat()
        av["uuids"] = []
        rid = av.get("run_id")
        lr = (await db.execute(
            select(SystemSetting).where(SystemSetting.key == GISGMP_ANNUL_LOG_KEY)
        )).scalars().first()
        if lr and lr.value:
            try:
                lj = json.loads(lr.value)
                for run in lj.get("runs", []):
                    if run.get("id") == rid or (rid is None and run.get("status") == "running"):
                        run["status"] = "done"
                        run["finished_at"] = av["finished_at"]
                        run["done"] = payload.done
                        run["ok"] = payload.ok
                        run["fail"] = payload.fail
                        break
                lr.value = json.dumps(lj, ensure_ascii=False)
            except Exception:
                pass
    row.value = json.dumps(av, ensure_ascii=False)
    await db.commit()
    return {"ok": True}


@router.get("/gisgmp/annul-status", summary="Прогресс аннулирования (для UI)")
async def gisgmp_annul_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ANNUL_KEY)
    )).scalars().first()
    if not row or not row.value:
        return {"total": 0, "done": 0, "running": False, "finished": False}
    try:
        av = json.loads(row.value)
    except Exception:
        return {"total": 0, "done": 0, "running": False, "finished": False}
    return {k: av.get(k) for k in (
        "total", "done", "ok", "fail", "running", "finished", "fio", "by", "message", "finished_at")}


@router.post("/gisgmp/actualize-build", summary="Очередь массовой актуализации: только ГИС > 1С (ошибка ГИС ГМП)")
async def gisgmp_actualize_build(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ставит в очередь актуализации НЕОПЛАЧЕННЫЕ (не аннулированные) счета ТОЛЬКО
    тех жильцов, у кого ГИС > 1С (флаги gis_more и only_gis) — это и есть «ошибка
    ГИС ГМП», где актуализация имеет смысл (реестр может аннулировать лишний долг).
    Случаи ГИС < 1С актуализация не лечит — для них «Дотянуть расхождения».

    Матчинг счёт→жилец — по ПОЛНОМУ ФИО (как в находках), НЕ по фамилии, чтобы не
    тянуть однофамильцев. Параллельно пишем аудит-прогон со снимком «до»; «после»
    снимется на ближайшей сверке (релей дёрнут авто через run_now)."""
    _require_finance(current_user)
    rec = await _build_reconcile(db)
    TARGET_FLAGS = ("gis_more", "only_gis")
    flagged = {
        int(r["user_id"]): r for r in rec.get("residents", [])
        if r.get("flag") in TARGET_FLAGS and r.get("user_id") is not None
    }

    # Точная карта ФИО(реестр)→user_id из находок (та же логика, что свела долги).
    findings = await _load_findings(db)
    fio_to_uid: dict[str, int] = {}
    if findings:
        for frow in findings.get("summary", []):
            uid = frow.get("matched_user_id")
            fio = (frow.get("fio") or "").strip()
            if uid is not None and fio:
                fio_to_uid[fio] = int(uid)
    target_fios = {fio for fio, uid in fio_to_uid.items() if uid in flagged}

    from app.modules.utility.services.gisgmp_import import (
        is_unpaid, is_annulled, classify_account, parse_reg_dt, GISGMP_CACHE_KEY,
    )
    # Окно актуализации = окно сбора (months_back из настроек релея). «Всё время»
    # (>=600 мес) → без фильтра; иначе актуализируем только начисления, чьё
    # bill_date не старше окна (напр. 1 год / полгода).
    rcfg = await _load_relay_cfg(db)
    months_back = int(rcfg.get("months_back") or 999)
    cutoff = None if months_back >= 600 else (utcnow() - timedelta(days=months_back * 31))
    cache_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_CACHE_KEY)
    )).scalars().first()
    per_user: dict[int, dict] = {}
    uuids, seen = [], set()
    if cache_row and cache_row.value and target_fios:
        try:
            for ch in json.loads(cache_row.value).values():
                if is_annulled(ch.get("change_status")) or not is_unpaid(ch.get("ack_status")):
                    continue
                fio = (ch.get("payer_name") or "").strip()
                if fio not in target_fios:
                    continue
                if cutoff is not None:
                    dt = parse_reg_dt(ch.get("bill_date"))
                    if dt is not None and dt < cutoff:
                        continue
                u = ch.get("charge_uuid")
                if not u or u in seen:
                    continue
                seen.add(u)
                uuids.append(u)
                uid = fio_to_uid.get(fio)
                slot = per_user.setdefault(uid, {"user_id": uid, "fio": fio, "charges": []})
                try:
                    amt = float(str(ch.get("amount") or "0").replace(",", "."))
                except Exception:
                    amt = 0.0
                slot["charges"].append({
                    "uin": ch.get("uin"), "account": classify_account(ch.get("purpose")),
                    "charge_uuid": u, "amount": amt, "bill_date": ch.get("bill_date"),
                })
        except Exception:
            pass

    # Снимок «до» по каждому затронутому жильцу (из сверки), сорт по |Δ|.
    residents_snap = []
    for uid, slot in per_user.items():
        fr = flagged.get(uid, {})
        residents_snap.append({
            "user_id": uid, "fio": slot["fio"],
            "username": fr.get("username"), "flag": fr.get("flag"),
            "before": {"gis": fr.get("sum_gis"), "c1": fr.get("sum_1c"), "delta": fr.get("delta")},
            "after": None, "result": None,
            "charges": slot["charges"],
        })
    residents_snap.sort(key=lambda x: -abs((x.get("before") or {}).get("delta") or 0))

    run_id = utcnow().isoformat()
    payload = {
        "uuids": uuids, "total": len(uuids), "done": 0, "ok": 0, "fail": 0,
        "running": False, "finished": False,
        "queued_at": run_id, "by": current_user.username, "message": "",
        "targeting": "gis_more+only_gis", "run_id": run_id,
    }
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_KEY)
    )).scalars().first()
    if row is None:
        row = SystemSetting(key=GISGMP_ACTUALIZE_KEY, value="{}",
                            description="Очередь массовой актуализации ГИС ГМП")
        db.add(row)
    row.value = json.dumps(payload, ensure_ascii=False)

    # Аудит-прогон (до/после). «После» снимется на ближайшей сверке.
    run = {
        "id": run_id, "queued_at": run_id, "by": current_user.username,
        "targeting": "ГИС > 1С (gis_more + only_gis)",
        "total_charges": len(uuids), "residents_count": len(residents_snap),
        "status": "running", "done": 0, "ok": 0, "fail": 0,
        "started_at": None, "finished_at": None,
        "after_pending": False, "after_at": None,
        "attempt": 1, "last_actualize_at": run_id,
        "residents": residents_snap,
    }
    log_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
    )).scalars().first()
    if log_row is None:
        log_row = SystemSetting(key=GISGMP_ACTUALIZE_LOG_KEY, value='{"runs": []}',
                                description="Аудит массовых актуализаций ГИС ГМП (до/после)")
        db.add(log_row)
    try:
        logj = json.loads(log_row.value) if log_row.value else {"runs": []}
    except Exception:
        logj = {"runs": []}
    runs = logj.get("runs", [])
    runs.insert(0, run)
    del runs[_ACTUALIZE_LOG_MAX_RUNS:]
    logj["runs"] = runs
    log_row.value = json.dumps(logj, ensure_ascii=False)

    await db.commit()
    return {"queued": len(uuids), "residents": len(residents_snap), "targeting": "gis_more+only_gis"}


@router.post("/gisgmp/actualize-all", summary="Очередь массовой актуализации: ВСЕ (не только ГИС>1С)")
async def gisgmp_actualize_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ставит в очередь актуализации ВСЕ несквитированные (не аннулированные)
    начисления ВСЕХ людей из кэша ГИС ГМП — а не только проблемных (ГИС>1С).
    Эквивалент «нажать кнопку актуализации за каждого» сразу для всех. Релей
    проходит очередь по одному uuid с паузой (do_actualize → ACTUALIZE_SLEEP),
    т.е. «по чуть-чуть», не загружая систему. Результат снимется авто-циклом
    доведения (_drive_actualize_runs) — как у остальных актуализаций.

    Окно — months_back из настроек релея (как у actualize-build). Не стартуем
    поверх идущей актуализации."""
    _require_finance(current_user)

    # Не запускаем, если уже идёт другая актуализация.
    act_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_KEY)
    )).scalars().first()
    if act_row and act_row.value:
        try:
            cur = json.loads(act_row.value)
            if cur.get("running") or cur.get("uuids"):
                return {"queued": 0, "reason": "идёт другая актуализация — дождитесь завершения"}
        except Exception:
            pass

    # ФИО(реестр)→user_id из находок (для группировки аудита по жильцам).
    findings = await _load_findings(db)
    fio_to_uid: dict[str, int] = {}
    if findings:
        for frow in findings.get("summary", []):
            uid = frow.get("matched_user_id")
            fio = (frow.get("fio") or "").strip()
            if uid is not None and fio:
                fio_to_uid[fio] = int(uid)

    from app.modules.utility.services.gisgmp_import import (
        is_unpaid, is_annulled, classify_account, parse_reg_dt, GISGMP_CACHE_KEY,
    )
    rcfg = await _load_relay_cfg(db)
    months_back = int(rcfg.get("months_back") or 999)
    cutoff = None if months_back >= 600 else (utcnow() - timedelta(days=months_back * 31))
    cache_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_CACHE_KEY)
    )).scalars().first()

    per_fio: dict[str, dict] = {}
    uuids, seen = [], set()
    if cache_row and cache_row.value:
        try:
            for ch in json.loads(cache_row.value).values():
                # ВСЕ неоплаченные не-аннулированные — без фильтра по флагам.
                if is_annulled(ch.get("change_status")) or not is_unpaid(ch.get("ack_status")):
                    continue
                if cutoff is not None:
                    dt = parse_reg_dt(ch.get("bill_date"))
                    if dt is not None and dt < cutoff:
                        continue
                u = ch.get("charge_uuid")
                if not u or u in seen:
                    continue
                seen.add(u)
                uuids.append(u)
                fio = (ch.get("payer_name") or "").strip()
                slot = per_fio.setdefault(fio, {"fio": fio, "user_id": fio_to_uid.get(fio),
                                                "charges": [], "gis": 0.0})
                try:
                    amt = float(str(ch.get("amount") or "0").replace(",", "."))
                except Exception:
                    amt = 0.0
                slot["gis"] += amt
                slot["charges"].append({
                    "uin": ch.get("uin"), "account": classify_account(ch.get("purpose")),
                    "charge_uuid": u, "amount": amt, "bill_date": ch.get("bill_date"),
                })
        except Exception:
            pass

    if not uuids:
        return {"queued": 0, "reason": "нет несквитированных начислений в кэше ГИС ГМП"}

    residents_snap = [{
        "user_id": s["user_id"], "fio": s["fio"], "username": None, "flag": None,
        "before": {"gis": round(s["gis"], 2), "c1": None, "delta": None},
        "after": None, "result": None, "charges": s["charges"],
    } for s in per_fio.values()]
    residents_snap.sort(key=lambda x: -((x.get("before") or {}).get("gis") or 0))

    run_id = utcnow().isoformat()
    payload = {
        "uuids": uuids, "total": len(uuids), "done": 0, "ok": 0, "fail": 0,
        "running": False, "finished": False,
        "queued_at": run_id, "by": current_user.username, "message": "",
        "targeting": "all", "run_id": run_id,
    }
    if act_row is None:
        act_row = SystemSetting(key=GISGMP_ACTUALIZE_KEY, value="{}",
                                description="Очередь массовой актуализации ГИС ГМП")
        db.add(act_row)
    act_row.value = json.dumps(payload, ensure_ascii=False)

    run = {
        "id": run_id, "queued_at": run_id, "by": current_user.username,
        "targeting": "ВСЕ начисления (актуализация за всех)",
        "total_charges": len(uuids), "residents_count": len(residents_snap),
        "status": "running", "done": 0, "ok": 0, "fail": 0,
        "started_at": None, "finished_at": None,
        "after_pending": False, "after_at": None,
        "attempt": 1, "last_actualize_at": run_id,
        "residents": residents_snap,
    }
    log_row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
    )).scalars().first()
    if log_row is None:
        log_row = SystemSetting(key=GISGMP_ACTUALIZE_LOG_KEY, value='{"runs": []}',
                                description="Аудит массовых актуализаций ГИС ГМП (до/после)")
        db.add(log_row)
    try:
        logj = json.loads(log_row.value) if log_row.value else {"runs": []}
    except Exception:
        logj = {"runs": []}
    runs = logj.get("runs", [])
    runs.insert(0, run)
    del runs[_ACTUALIZE_LOG_MAX_RUNS:]
    logj["runs"] = runs
    log_row.value = json.dumps(logj, ensure_ascii=False)

    await db.commit()
    return {"queued": len(uuids), "residents": len(residents_snap), "targeting": "all"}


class GisgmpActualizeProgressIn(BaseModel):
    done: int = 0
    ok: int = 0
    fail: int = 0
    finished: bool = False
    message: str = ""


@router.post("/gisgmp/actualize-progress", summary="Релей шлёт прогресс актуализации (token)")
async def gisgmp_actualize_progress(
    payload: GisgmpActualizeProgressIn,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    _check_gisgmp_token(authorization)
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_KEY)
    )).scalars().first()
    if not row or not row.value:
        return {"ok": True}
    try:
        av = json.loads(row.value)
    except Exception:
        av = {}
    av["done"] = payload.done
    av["ok"] = payload.ok
    av["fail"] = payload.fail
    av["last_at"] = utcnow().isoformat()
    if payload.message:
        av["message"] = payload.message[:300]
    if payload.finished:
        av["running"] = False
        av["finished"] = True
        av["finished_at"] = utcnow().isoformat()
        av["uuids"] = []  # выполнено — очищаем список
        # Прогон «отправлен в ГИС». Снимок «после» НЕ снимаем сразу: ГИС
        # обрабатывает запрос АСИНХРОННО («Отправлен в ГИС ГМП» → «Завершен»,
        # часы) — преждевременный снимок всегда показывал «без изменений».
        # «После» снимется на ПЕРЕПРОВЕРКЕ: авто (через ~2ч) или кнопкой
        # «Проверить результат» — оба ставят ФИО прогона в глубокий переопрос
        # ГИС + взводят after_pending, и снимок берётся уже по свежим данным.
        run_id = av.get("run_id")
        if run_id:
            lr = (await db.execute(
                select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
            )).scalars().first()
            if lr and lr.value:
                try:
                    lj = json.loads(lr.value)
                    for run in lj.get("runs", []):
                        if run.get("id") == run_id:
                            run["status"] = "checking"  # авто-цикл начинает опрос «Сквитировано»
                            run["finished_at"] = av["finished_at"]
                            run["done"] = payload.done
                            run["ok"] = payload.ok
                            run["fail"] = payload.fail
                            run["after_pending"] = False
                            break
                    lr.value = json.dumps(lj, ensure_ascii=False)
                except Exception:
                    pass
    row.value = json.dumps(av, ensure_ascii=False)
    await db.commit()
    return {"ok": True}


@router.get("/gisgmp/actualize-status", summary="Прогресс массовой актуализации (для UI)")
async def gisgmp_actualize_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_KEY)
    )).scalars().first()
    if not row or not row.value:
        return {"total": 0, "done": 0, "running": False, "finished": False}
    try:
        av = json.loads(row.value)
    except Exception:
        return {"total": 0, "done": 0, "running": False, "finished": False}
    return {k: av.get(k) for k in (
        "total", "done", "ok", "fail", "running", "finished",
        "queued_at", "started_at", "finished_at", "last_at", "message", "by")}


@router.get("/gisgmp/actualize-log", summary="История массовых актуализаций (аудит до/после)")
async def gisgmp_actualize_log(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    lr = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
    )).scalars().first()
    if not lr or not lr.value:
        return {"runs": []}
    try:
        return {"runs": json.loads(lr.value).get("runs", [])}
    except Exception:
        return {"runs": []}


class GisgmpActualizePruneIn(BaseModel):
    clear_all: bool = False
    delete_id: Optional[str] = None
    older_than_days: Optional[int] = None
    keep_last: Optional[int] = None


@router.post("/gisgmp/actualize-log/prune", summary="Чистка истории актуализаций (на выбор админа)")
async def gisgmp_actualize_log_prune(
    payload: GisgmpActualizePruneIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    lr = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == GISGMP_ACTUALIZE_LOG_KEY)
    )).scalars().first()
    if not lr or not lr.value:
        return {"remaining": 0}
    try:
        lj = json.loads(lr.value)
    except Exception:
        lj = {"runs": []}
    runs = lj.get("runs", [])
    if payload.clear_all:
        runs = []
    elif payload.delete_id:
        runs = [r for r in runs if r.get("id") != payload.delete_id]
    elif payload.older_than_days:
        cutoff = (utcnow() - timedelta(days=int(payload.older_than_days))).isoformat()
        runs = [r for r in runs if (r.get("queued_at") or "") >= cutoff]
    elif payload.keep_last is not None:
        runs = runs[:max(0, int(payload.keep_last))]
    lj["runs"] = runs
    lr.value = json.dumps(lj, ensure_ascii=False)
    await db.commit()
    return {"remaining": len(runs)}


@router.get("/gisgmp/relay.py", summary="Отдать актуальный relay.py (самообновление релея на ВМ)")
async def gisgmp_relay_py(authorization: Optional[str] = Header(None)):
    """Релей на ВМ берёт свежий код одной командой (token-auth), чтобы не
    вставлять его вручную при каждом апгрейде."""
    _check_gisgmp_token(authorization)
    p = Path(__file__).resolve().parents[5] / "relay" / "gisgmp" / "relay.py"  # parents[5]: модуль лежит на уровень глубже (пакет financier/)
    if not p.is_file():
        raise HTTPException(404, "relay.py не найден в образе")
    src = p.read_text(encoding="utf-8")
    headers = {"Cache-Control": "no-store"}
    # Подпись кода (RCE-защита #19): релей проверит HMAC ПЕРЕД execv. Ключ
    # RELAY_UPDATE_SECRET — отдельный, по сети не ходит. Пусто → без подписи.
    _relay_secret = (settings.RELAY_UPDATE_SECRET or "").strip()
    if _relay_secret:
        import hmac as _hmac
        import hashlib as _hashlib
        headers["X-Relay-Signature"] = _hmac.new(
            _relay_secret.encode(), src.encode("utf-8"), _hashlib.sha256
        ).hexdigest()
    return Response(
        content=src,
        media_type="text/x-python; charset=utf-8",
        headers=headers,
    )


# Каталог расширения в репозитории. Модуль лежит в
# app/modules/utility/routers/financier/ → parents[5] = корень репо (в Docker
# это /app, куда Dockerfile COPY кладёт extension/). Внутри — gisgmp-bridge.
_GISGMP_EXT_DIR = Path(__file__).resolve().parents[5] / "extension" / "gisgmp-bridge"


@router.get("/gisgmp/bridge.zip", summary="Скачать ZIP расширения-моста ГИС ГМП")
async def gisgmp_bridge_zip(current_user: User = Depends(get_current_user)):
    """Отдаёт ZIP папки gisgmp-bridge — пользователь распаковывает и грузит в
    браузер как «распакованное расширение». Только финансист/админ."""
    _require_finance(current_user)
    if not _GISGMP_EXT_DIR.is_dir():
        raise HTTPException(404, "Каталог расширения не найден на сервере.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(_GISGMP_EXT_DIR):
            for fname in files:
                fpath = Path(root) / fname
                rel = Path("gisgmp-bridge") / fpath.relative_to(_GISGMP_EXT_DIR)
                zf.write(fpath, arcname=str(rel))
    body = buf.getvalue()
    return Response(
        content=body,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="gisgmp-bridge.zip"',
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
