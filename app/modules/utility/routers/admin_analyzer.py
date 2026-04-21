# app/modules/utility/routers/admin_analyzer.py
"""
Центр управления анализаторами.

Один экран в админке, который объединяет:
  * настройки порогов всех анализаторов (analyzer_settings → редактируем);
  * метрики срабатываний (по типу флага, за последний месяц);
  * список dismissed-аномалий (self-learning) с возможностью удалить;
  * список GSheets-алиасов («ФИО ↔ жилец»);
  * сводка про fuzzy-импорт долгов.

Эндпоинты:
  GET   /api/admin/analyzer/dashboard   — единая сводка для главного UI
  GET   /api/admin/analyzer/settings    — список всех настроек
  PATCH /api/admin/analyzer/settings/{key} — изменить значение / включить-выключить
  POST  /api/admin/analyzer/cache/invalidate — сбросить кеш конфига (применить изменения сразу)
  GET   /api/admin/analyzer/dismissals  — список «не аномалий» (self-learning)
  POST  /api/admin/analyzer/dismissals  — пометить флаг для жильца как false-positive
  DELETE /api/admin/analyzer/dismissals/{id} — снять пометку
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import get_current_user
from app.core.database import get_db
from app.modules.utility.models import (
    AnalyzerSetting, AnomalyDismissal, MeterReading, User,
    GSheetsImportRow, GSheetsAlias,
)
from app.modules.utility.routers.admin_dashboard import write_audit_log
from app.modules.utility.services.analyzer_config import config, dismissals

router = APIRouter(prefix="/api/admin/analyzer", tags=["Admin Analyzer"])


def _require_admin(user: User) -> None:
    if user.role not in ("admin", "accountant"):
        raise HTTPException(status_code=403, detail="Доступ запрещён")


# =========================================================================
# DASHBOARD
# =========================================================================
@router.get("/dashboard")
async def analyzer_dashboard(
    days: int = Query(30, ge=1, le=365),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сводка состояния всех анализаторов за последние N дней."""
    _require_admin(current_user)
    cutoff = datetime.utcnow() - timedelta(days=days)

    # 1) Аномалии: разложение по типу флага.
    # MeterReading.anomaly_flags хранится как 'SPIKE_HOT,FROZEN_COLD,...'
    rows = (await db.execute(
        select(MeterReading.anomaly_flags, MeterReading.anomaly_score)
        .where(MeterReading.created_at >= cutoff)
        .where(MeterReading.anomaly_flags.is_not(None))
        .where(MeterReading.anomaly_flags != "")
    )).all()

    flag_counts: dict[str, int] = {}
    score_buckets = {"low (1-39)": 0, "medium (40-79)": 0, "critical (80-100)": 0}
    total_flagged = 0
    for flags_str, score in rows:
        if not flags_str or flags_str == "PENDING":
            continue
        total_flagged += 1
        for f in flags_str.split(","):
            f = f.strip()
            if f:
                flag_counts[f] = flag_counts.get(f, 0) + 1
        s = int(score or 0)
        if s >= 80:
            score_buckets["critical (80-100)"] += 1
        elif s >= 40:
            score_buckets["medium (40-79)"] += 1
        elif s > 0:
            score_buckets["low (1-39)"] += 1

    top_flags = sorted(flag_counts.items(), key=lambda kv: -kv[1])[:15]

    # 2) GSheets матчер: разложение статусов
    gs_rows = (await db.execute(
        select(GSheetsImportRow.status, func.count(GSheetsImportRow.id))
        .where(GSheetsImportRow.created_at >= cutoff)
        .group_by(GSheetsImportRow.status)
    )).all()
    gsheets_stats = {st: cnt for st, cnt in gs_rows}

    # Сколько алиасов всего и сколько за период
    total_aliases = (await db.execute(
        select(func.count(GSheetsAlias.id))
    )).scalar_one()
    new_aliases = (await db.execute(
        select(func.count(GSheetsAlias.id))
        .where(GSheetsAlias.created_at >= cutoff)
    )).scalar_one()

    # 3) Dismissals — текущее количество self-learning записей
    dism_total = (await db.execute(
        select(func.count(AnomalyDismissal.id))
    )).scalar_one()
    dism_global = (await db.execute(
        select(func.count(AnomalyDismissal.id))
        .where(AnomalyDismissal.user_id.is_(None))
    )).scalar_one()

    # 4) Текущие настройки (краткий список для UI индикатора «настроено»)
    settings_summary = (await db.execute(
        select(AnalyzerSetting.category, func.count(AnalyzerSetting.key))
        .group_by(AnalyzerSetting.category)
    )).all()

    return {
        "period_days": days,
        "anomalies": {
            "total_flagged_readings": total_flagged,
            "by_severity": score_buckets,
            "top_flags": [{"flag": k, "count": v} for k, v in top_flags],
        },
        "gsheets": {
            "by_status": gsheets_stats,
            "aliases_total": total_aliases,
            "aliases_new_in_period": new_aliases,
        },
        "self_learning": {
            "total_dismissals": dism_total,
            "global_dismissals": dism_global,  # действуют на всех жильцов
        },
        "settings": {
            "by_category": dict(settings_summary),
        },
    }


# =========================================================================
# SETTINGS
# =========================================================================
class SettingPatch(BaseModel):
    value: Optional[str] = None
    is_enabled: Optional[bool] = None


@router.get("/settings")
async def list_settings(
    category: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    q = select(AnalyzerSetting).order_by(AnalyzerSetting.category, AnalyzerSetting.key)
    if category:
        q = q.where(AnalyzerSetting.category == category)
    rows = (await db.execute(q)).scalars().all()
    return {
        "items": [
            {
                "key": r.key,
                "value": r.value,
                "value_type": r.value_type,
                "category": r.category,
                "description": r.description,
                "min_value": r.min_value,
                "max_value": r.max_value,
                "is_enabled": r.is_enabled,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
    }


def _validate_setting_value(setting: AnalyzerSetting, raw_value: str) -> str:
    """Парсим и проверяем диапазон. Возвращаем нормализованное строковое значение."""
    raw_value = (raw_value or "").strip()
    vt = setting.value_type
    if vt == "int":
        try:
            v = int(raw_value)
        except ValueError:
            raise HTTPException(400, f"Значение должно быть целым числом (получено: {raw_value!r})")
    elif vt == "float":
        try:
            v = float(raw_value.replace(",", "."))
        except ValueError:
            raise HTTPException(400, f"Значение должно быть числом (получено: {raw_value!r})")
    elif vt == "bool":
        if raw_value.lower() not in ("true", "false", "1", "0", "yes", "no"):
            raise HTTPException(400, f"Допустимо: true/false (получено: {raw_value!r})")
        return "true" if raw_value.lower() in ("true", "1", "yes") else "false"
    elif vt == "str":
        return raw_value
    else:
        raise HTTPException(500, f"Неизвестный value_type: {vt}")

    # Диапазон min/max — для int/float
    if setting.min_value is not None:
        try:
            mn = float(setting.min_value)
            if v < mn:
                raise HTTPException(400, f"Значение меньше минимума ({mn})")
        except (TypeError, ValueError):
            pass
    if setting.max_value is not None:
        try:
            mx = float(setting.max_value)
            if v > mx:
                raise HTTPException(400, f"Значение больше максимума ({mx})")
        except (TypeError, ValueError):
            pass
    return str(v)


@router.patch("/settings/{key}")
async def update_setting(
    key: str,
    patch: SettingPatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    setting = await db.get(AnalyzerSetting, key)
    if not setting:
        raise HTTPException(404, f"Настройка {key!r} не найдена")

    changes: dict = {}
    if patch.value is not None and patch.value != setting.value:
        new_val = _validate_setting_value(setting, patch.value)
        changes["value"] = {"old": setting.value, "new": new_val}
        setting.value = new_val
    if patch.is_enabled is not None and patch.is_enabled != setting.is_enabled:
        changes["is_enabled"] = {"old": setting.is_enabled, "new": patch.is_enabled}
        setting.is_enabled = patch.is_enabled

    if not changes:
        return {"status": "noop", "key": key}

    setting.updated_at = datetime.utcnow()
    setting.updated_by_id = current_user.id

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="analyzer_setting_update", entity_type="analyzer_setting", entity_id=None,
        details={"key": key, "changes": changes},
    )
    await db.commit()

    # Сбрасываем кеш — изменение становится активным с этого момента,
    # без перезапуска и без 60-секундного TTL.
    config.invalidate()
    return {"status": "ok", "key": key, "changes": changes}


@router.post("/cache/invalidate")
async def invalidate_caches(
    current_user: User = Depends(get_current_user),
):
    """Принудительный сброс in-memory кешей (settings + dismissals)."""
    _require_admin(current_user)
    config.invalidate()
    dismissals.invalidate()
    return {"status": "ok"}


# =========================================================================
# DISMISSALS — self-learning
# =========================================================================
class DismissalCreate(BaseModel):
    user_id: Optional[int] = None  # None = глобально, для всех
    flag_code: str
    reason: Optional[str] = None


@router.get("/dismissals")
async def list_dismissals(
    user_id: Optional[int] = Query(None),
    flag_code: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    q = (
        select(AnomalyDismissal)
        .options(
            selectinload(AnomalyDismissal.user),
            selectinload(AnomalyDismissal.created_by),
        )
        .order_by(desc(AnomalyDismissal.created_at))
    )
    cnt_q = select(func.count(AnomalyDismissal.id))
    if user_id is not None:
        q = q.where(AnomalyDismissal.user_id == user_id)
        cnt_q = cnt_q.where(AnomalyDismissal.user_id == user_id)
    if flag_code:
        q = q.where(AnomalyDismissal.flag_code == flag_code)
        cnt_q = cnt_q.where(AnomalyDismissal.flag_code == flag_code)

    total = (await db.execute(cnt_q)).scalar_one()
    rows = (await db.execute(
        q.offset((page - 1) * limit).limit(limit)
    )).scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id": d.id,
                "user_id": d.user_id,
                "username": d.user.username if d.user else None,
                "flag_code": d.flag_code,
                "reason": d.reason,
                "created_at": d.created_at,
                "created_by": d.created_by.username if d.created_by else None,
                "is_global": d.user_id is None,
            }
            for d in rows
        ],
    }


@router.post("/dismissals")
async def create_dismissal(
    data: DismissalCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    if not data.flag_code:
        raise HTTPException(400, "flag_code обязателен")
    # Уникальный (user_id, flag_code) — если уже есть, обновим reason.
    existing = (await db.execute(
        select(AnomalyDismissal).where(
            AnomalyDismissal.user_id.is_(data.user_id) if data.user_id is None
            else AnomalyDismissal.user_id == data.user_id,
            AnomalyDismissal.flag_code == data.flag_code,
        )
    )).scalars().first()
    if existing:
        existing.reason = data.reason or existing.reason
        await db.commit()
        dismissals.invalidate()
        return {"status": "exists", "id": existing.id}

    dism = AnomalyDismissal(
        user_id=data.user_id,
        flag_code=data.flag_code,
        reason=data.reason,
        created_by_id=current_user.id,
    )
    db.add(dism)
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="anomaly_dismiss", entity_type="anomaly_dismissal", entity_id=None,
        details={
            "user_id": data.user_id,
            "flag_code": data.flag_code,
            "reason": data.reason,
        },
    )
    await db.commit()
    dismissals.invalidate()
    return {"status": "ok", "id": dism.id}


@router.delete("/dismissals/{dismissal_id}")
async def delete_dismissal(
    dismissal_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    dism = await db.get(AnomalyDismissal, dismissal_id)
    if not dism:
        raise HTTPException(404, "Запись не найдена")
    await db.delete(dism)
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="anomaly_dismiss_delete", entity_type="anomaly_dismissal",
        entity_id=dismissal_id,
        details={"user_id": dism.user_id, "flag_code": dism.flag_code},
    )
    await db.commit()
    dismissals.invalidate()
    return {"status": "ok"}


# =========================================================================
# DOCUMENTATION ENDPOINT — какие флаги существуют, что они значат
# =========================================================================
@router.get("/flags-catalog")
async def flags_catalog(current_user: User = Depends(get_current_user)):
    """Справочник всех аномалий — для UI: показать что значит SPIKE_HOT и т.д.
    Маленький каталог, можно держать здесь — изменяется редко."""
    _require_admin(current_user)
    return {
        "flags": [
            # Критические
            {"code": "NEGATIVE_HOT", "severity": "critical", "title": "Откат счётчика ГВС",
             "desc": "Текущее показание меньше предыдущего — счётчик не может убывать."},
            {"code": "NEGATIVE_COLD", "severity": "critical", "title": "Откат счётчика ХВС",
             "desc": "Текущее показание меньше предыдущего."},
            {"code": "NEGATIVE_ELECT", "severity": "critical", "title": "Откат счётчика электричества",
             "desc": "Текущее показание меньше предыдущего."},
            # Спайки
            {"code": "SPIKE_HOT", "severity": "high", "title": "Резкий скачок ГВС",
             "desc": "Расход больше median + N×MAD от истории жильца."},
            {"code": "SPIKE_COLD", "severity": "high", "title": "Резкий скачок ХВС", "desc": ""},
            {"code": "SPIKE_ELECT", "severity": "high", "title": "Резкий скачок электр.", "desc": ""},
            # Soft spikes
            {"code": "HIGH_HOT", "severity": "medium", "title": "Высокий ГВС",
             "desc": "Расход в 2× выше медианы."},
            {"code": "HIGH_COLD", "severity": "medium", "title": "Высокий ХВС", "desc": ""},
            {"code": "HIGH_ELECT", "severity": "medium", "title": "Высокий электр.", "desc": ""},
            # Подозрительная нулёвка
            {"code": "ZERO_HOT", "severity": "medium", "title": "Перестал расходовать ГВС",
             "desc": "Дельта = 0 при медиане > 1 м³."},
            {"code": "ZERO_COLD", "severity": "medium", "title": "Перестал расходовать ХВС", "desc": ""},
            {"code": "ZERO_ELECT", "severity": "medium", "title": "Перестал расходовать электр.", "desc": ""},
            {"code": "FROZEN_HOT", "severity": "high", "title": "Замёрзший счётчик ГВС",
             "desc": "Три месяца подряд 0 — возможно, счётчик не работает или умышленно не пишет."},
            {"code": "FROZEN_COLD", "severity": "high", "title": "Замёрзший счётчик ХВС", "desc": ""},
            {"code": "FROZEN_ELECT", "severity": "high", "title": "Замёрзший счётчик электр.", "desc": ""},
            # Подделка
            {"code": "FLAT_HOT", "severity": "high", "title": "Одинаковая подача ГВС",
             "desc": "3+ месяца подряд ровно одно и то же — счётчик «рисуется»."},
            {"code": "FLAT_COLD", "severity": "high", "title": "Одинаковая подача ХВС", "desc": ""},
            {"code": "FLAT_ELECT", "severity": "high", "title": "Одинаковая подача электр.", "desc": ""},
            {"code": "TREND_UP_HOT", "severity": "medium", "title": "Постоянный рост ГВС",
             "desc": "4 периода подряд расход растёт — возможно скрытая утечка."},
            {"code": "TREND_UP_COLD", "severity": "medium", "title": "Постоянный рост ХВС",
             "desc": "4 периода подряд расход растёт — возможно скрытая утечка."},
            {"code": "TREND_UP_ELECT", "severity": "medium", "title": "Постоянный рост электр.", "desc": ""},
            {"code": "DROP_AFTER_SPIKE_HOT", "severity": "high", "title": "Резкий спад после скачка ГВС",
             "desc": "После большой подачи следующая подача аномально низкая — возможен сброс перед проверкой."},
            {"code": "DROP_AFTER_SPIKE_COLD", "severity": "high", "title": "Резкий спад после скачка ХВС", "desc": ""},
            {"code": "DROP_AFTER_SPIKE_ELECT", "severity": "high", "title": "Резкий спад после скачка электр.", "desc": ""},
            # Контекст
            {"code": "HIGH_VS_PEERS_HOT", "severity": "medium", "title": "Выше соседей по ГВС",
             "desc": "Расход в 3× больше среднего по группе жильцов."},
            {"code": "HIGH_VS_PEERS_COLD", "severity": "medium", "title": "Выше соседей по ХВС", "desc": ""},
            {"code": "HIGH_VS_PEERS_ELECT", "severity": "medium", "title": "Выше соседей по электр.", "desc": ""},
            {"code": "HIGH_PER_PERSON_COLD", "severity": "high", "title": "Слишком много ХВС на 1 человека",
             "desc": "Больше 12 м³ ХВС на 1 проживающего — нереально для бытового потребления."},
            # Новые правила v3
            {"code": "ROUND_NUMBER_HOT", "severity": "low", "title": "Подозрение на округление (ГВС)",
             "desc": "Целое число без дробей — реальный счётчик не показывает ровные значения."},
            {"code": "ROUND_NUMBER_COLD", "severity": "low", "title": "Подозрение на округление (ХВС)", "desc": ""},
            {"code": "ROUND_NUMBER_ELECT", "severity": "low", "title": "Подозрение на округление (электр.)", "desc": ""},
            {"code": "HOT_GT_COLD", "severity": "high", "title": "ГВС > ХВС",
             "desc": "Физически странно: ХВС обычно ≥ ГВС. Возможно, счётчики перепутаны или подделка."},
            {"code": "COPY_NEIGHBOR", "severity": "high", "title": "Списали у соседа",
             "desc": "Дельты идеально совпадают с соседом по комнате — почти наверняка списано."},
            {"code": "COPY_NEIGHBOR_PARTIAL", "severity": "medium", "title": "Частичное совпадение с соседом",
             "desc": "Один из ресурсов точно совпал с соседом — стоит проверить."},
            {"code": "GAP_RECOVERY", "severity": "medium", "title": "Большая подача после паузы",
             "desc": "Жилец не подавал 3+ месяца, затем сразу пришёл с большим расходом."},
        ]
    }
