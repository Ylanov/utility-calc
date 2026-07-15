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

from datetime import timedelta
from app.core.time_utils import utcnow
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, desc, or_, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import get_current_user
from app.core.database import get_db
from app.modules.utility.models import (
    AnalyzerSetting, AnomalyDismissal, MeterReading, User,
    GSheetsImportRow, GSheetsAlias, BillingPeriod, ResidentProblem, Room,
)
from app.modules.utility.routers.admin_dashboard import write_audit_log
from app.modules.utility.services.analyzer_config import config, dismissals
from app.modules.utility.services.anomaly_flags import (
    is_source_marker as _is_source_marker,
)

router = APIRouter(prefix="/api/admin/analyzer", tags=["Admin Analyzer"])


def _require_admin(user: User) -> None:
    if user.role not in ("admin", "accountant"):
        raise HTTPException(status_code=403, detail="Доступ запрещён")


# =========================================================================
# DANGER ZONE — полная очистка показаний («начать заново»)
# =========================================================================
_WIPE_CONFIRM_PHRASE = "СТЕРЕТЬ ВСЁ"


class WipeReadingsRequest(BaseModel):
    confirm: str


@router.post("/wipe-readings")
async def wipe_all_readings(
    payload: WipeReadingsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """ОПАСНО И НЕОБРАТИМО: удаляет ВСЕ показания счётчиков (включая
    «Начальные»/baseline) по всему биллингу и обнуляет room.last_* —
    чистый старт «начать заново».

    НЕ трогает: жильцов, комнаты, тарифы, периоды, а также 1С-долги и
    корректировки (Adjustment) — это отдельные данные.

    Требует точную фразу-подтверждение в теле запроса (defensive UX —
    кнопка спрятана в Центре анализа). Пишет аудит-лог.
    """
    _require_admin(current_user)
    if (payload.confirm or "").strip() != _WIPE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f"Для очистки введите точную фразу: {_WIPE_CONFIRM_PHRASE}",
        )

    total = (await db.execute(select(func.count(MeterReading.id)))).scalar_one()

    # FK: GSheetsImportRow.reading_id → readings.id (без ON DELETE) — обнуляем,
    # иначе DELETE упадёт на foreign key violation.
    await db.execute(
        update(GSheetsImportRow)
        .where(GSheetsImportRow.reading_id.is_not(None))
        .values(reading_id=None)
    )
    # Удаляем ВСЕ показания.
    await db.execute(delete(MeterReading))
    # Обнуляем «последнее показание» всех комнат — следующая подача = старт.
    await db.execute(update(Room).values(
        last_hot_water=0, last_cold_water=0, last_electricity=0,
    ))

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="wipe_all_readings", entity_type="reading", entity_id=None,
        details={"deleted_readings": int(total), "scope": "all_billing",
                 "reset_room_last": True},
    )
    await db.commit()
    return {"status": "ok", "deleted_readings": int(total)}


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
    cutoff = utcnow() - timedelta(days=days)

    # 1) Аномалии: разложение по типу флага.
    # MeterReading.anomaly_flags хранится как 'SPIKE_HOT,FROZEN_COLD,...'
    rows = (await db.execute(
        select(MeterReading.anomaly_flags, MeterReading.anomaly_score)
        .where(MeterReading.created_at >= cutoff)
        .where(MeterReading.anomaly_flags.is_not(None))
        .where(MeterReading.anomaly_flags != "")
    )).all()

    # Source-маркеры (GSHEETS_AUTO, DATA_OVERFLOW_RESET и т.п.) — это
    # служебные пометки источника записи, НЕ аномалии. Раньше dashboard
    # считал их в total_flagged_readings и в score_buckets — KPI показывал
    # сотни «аномалий», но Inbox (который правильно фильтрует) был пустой.
    # Используем тот же _SOURCE_MARKERS что и в /inbox для консистентности.
    flag_counts: dict[str, int] = {}
    score_buckets = {"low (1-39)": 0, "medium (40-79)": 0, "critical (80-100)": 0}
    total_flagged = 0
    for flags_str, score in rows:
        if not flags_str or flags_str == "PENDING":
            continue
        # Оставляем только реальные флаги, без source-маркеров.
        # is_source_marker умеет матчить prefix-патчи типа BASELINE_LEGACY_*.
        # Также поддерживает разделитель '|' (skip_recalc формат).
        real_flags = [
            f.strip()
            for f in flags_str.replace("|", ",").split(",")
            if f.strip() and not _is_source_marker(f.strip())
        ]
        if not real_flags:
            continue  # Только маркеры источника — это не аномалия.
        total_flagged += 1
        for f in real_flags:
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

    setting.updated_at = utcnow()
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


# =========================================================================
# РУЧНАЯ ОЧИСТКА СТАРЫХ СТРОК GSHEETS (по кнопке из админки)
# =========================================================================
# По расписанию всё автоматически убирает cleanup_gsheets_old_rows_task
# (раз в сутки в 03:00), но админ иногда хочет почистить прямо сейчас —
# например, после крупного импорта или при исследовании базы.
#
# Удаляются только approved / auto_approved / rejected старше N дней.
# pending/conflict/unmatched не трогаются — их ждут админы в буфере.
# =========================================================================

class GSheetsCleanupRequest(BaseModel):
    retention_days: Optional[int] = None  # если None — берём из settings


@router.post("/gsheets/cleanup-now")
async def cleanup_gsheets_now(
    data: GSheetsCleanupRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ручной запуск очистки завершённых строк импорта.

    Выполняется синхронно (без Celery) — у админа ожидание «не уходит в
    фон», а сразу видит итог. Для безопасности ограничиваем retention_days
    снизу (30), чтобы случайный ноль не снёс всё.
    """
    _require_admin(current_user)

    from app.core.config import settings as _settings
    days = data.retention_days if data.retention_days is not None else _settings.GSHEETS_CLEANUP_DAYS
    if days is None or days < 30:
        raise HTTPException(
            400,
            "Минимум 30 дней — защита от случайной полной очистки буфера.",
        )

    # Выполняем ту же логику, что и Celery-задача, но в рамках async-сессии.
    from datetime import timedelta
    cutoff = utcnow() - timedelta(days=days)
    # superseded — автопогашенные (месяц решён другим путём), терминальны как rejected.
    terminal = ("approved", "auto_approved", "rejected", "superseded")

    # Считаем сколько будет удалено (для audit_log) + удаляем пачкой
    count_q = select(func.count(GSheetsImportRow.id)).where(
        GSheetsImportRow.created_at < cutoff,
        GSheetsImportRow.status.in_(terminal),
    )
    to_delete = (await db.execute(count_q)).scalar_one()

    if to_delete == 0:
        return {"deleted": 0, "cutoff": cutoff.isoformat(), "retention_days": days}

    # Удаление батчами: у нас может быть 10k+ строк, одиночный DELETE
    # с большим IN обойдётся быстрее в Celery-таске. Но для UI-ответа
    # важно вернуть число — вызовем напрямую SQL-delete через .execute.
    from sqlalchemy import delete as _delete
    await db.execute(
        _delete(GSheetsImportRow).where(
            GSheetsImportRow.created_at < cutoff,
            GSheetsImportRow.status.in_(terminal),
        )
    )

    await write_audit_log(
        db=db, user_id=current_user.id, username=current_user.username,
        action="gsheets_cleanup", entity_type="gsheets_import_row",
        details={"deleted": to_delete, "retention_days": days, "cutoff": cutoff.isoformat()},
    )
    await db.commit()

    return {
        "deleted": to_delete,
        "cutoff": cutoff.isoformat(),
        "retention_days": days,
    }


# =========================================================================
# RECALC DRIFT — батчевая проверка расхождений «текущий пересчёт vs БД»
# =========================================================================
@router.get("/recalc-drift")
async def get_recalc_drift(
    period_id: int = Query(..., description="ID периода для проверки"),
    threshold: float = Query(0.01, ge=0, description="Порог расхождения в ₽"),
    limit: Optional[int] = Query(None, ge=1, le=5000, description="Ограничить выборку"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Анализирует расхождение между сохранённым total_cost и пересчётом
    по текущим формулам/тарифам для всех approved-readings периода.

    Полезно после:
      - изменения тарифа задним числом (часть reading'ов «уехала»)
      - багфиксов формул (как баг с 1.48 млрд в мае 2026)
      - подозрений на ручные правки БД
    """
    _require_admin(current_user)
    from decimal import Decimal as _Dec
    from app.modules.utility.services.recalc_drift_analyzer import (
        detect_drift_in_period,
    )
    return await detect_drift_in_period(
        db=db,
        period_id=period_id,
        threshold=_Dec(str(threshold)),
        limit=limit,
    )


# =========================================================================
# TELEMETRY — сводная статистика подач по периоду
# =========================================================================
@router.get("/telemetry")
async def get_period_telemetry(
    period_id: int = Query(..., description="ID периода для сводки"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сводка по периоду: источники подач (gsheets/мобилка/ручной),
    % с аномалиями, распределение по дням, p95/median сумм, топ флагов.

    Отвечает на вопросы:
      - откуда вообще приходят показания (доля каждого канала)
      - когда люди подают (равномерно или в последний день)
      - какой % требует внимания админа (anomaly score > 0)
      - какие типы аномалий встречаются чаще всего
    """
    _require_admin(current_user)
    from app.modules.utility.services.telemetry_analyzer import (
        collect_period_telemetry,
    )
    return await collect_period_telemetry(db=db, period_id=period_id)


# =========================================================================
# TARIFF DRIFT — быстрый screening «что устарело по тарифу»
# =========================================================================
@router.get("/tariff-drift")
async def get_tariff_drift(
    tariff_id: Optional[int] = Query(None, description="ID тарифа. Не указан — активный."),
    period_id: Optional[int] = Query(None, description="Ограничить периодом"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cheap-screening: какие reading'и созданы ДО последнего изменения
    тарифа и потенциально устарели. Не делает пересчёт — только сравнение
    дат. Для точной оценки используйте /recalc-drift.
    """
    _require_admin(current_user)
    from app.modules.utility.services.tariff_drift_analyzer import (
        analyze_tariff_drift,
    )
    return await analyze_tariff_drift(db=db, tariff_id=tariff_id, period_id=period_id)


# =========================================================================
# COHORT — peer-сравнение жильцов по группам
# =========================================================================
@router.get("/cohorts")
async def get_cohort_analysis(
    period_id: int = Query(..., description="ID периода"),
    metric: str = Query(
        "total_cost",
        pattern="^(total_cost|hot_water|cold_water|electricity)$",
        description="total_cost|hot_water|cold_water|electricity",
    ),
    outlier_factor: float = Query(2.0, ge=1.0, le=10.0, description="Множитель median для outliers"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сравнение жильцов в когортах: общежитие, размер семьи, площадь.
    Возвращает median/p95/max + outliers (с показателем > N×median).
    """
    _require_admin(current_user)
    from app.modules.utility.services.cohort_analyzer import analyze_cohorts
    return await analyze_cohorts(
        db=db, period_id=period_id, metric=metric, outlier_factor=outlier_factor,
    )


# =========================================================================
# HEATMAP — флаги × общежития (для UI dashboard)
# =========================================================================
@router.get("/flag-heatmap")
async def get_flag_heatmap(
    period_id: int = Query(..., description="ID периода"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Тепловая карта: флаг × общежитие. Для каждого флага в каждом
    общежитии — количество reading'ов с этим флагом в указанном периоде.

    UI использует это для quick-spot «в 4дв.стр.5 много FLAT_COLD».
    """
    _require_admin(current_user)
    # Тянем reading'и периода + комнату для группировки по общежитию
    rows = (await db.execute(
        select(MeterReading.anomaly_flags, MeterReading.user_id)
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True),
            MeterReading.anomaly_flags.is_not(None),
        )
    )).all()

    if not rows:
        return {"period_id": period_id, "cells": [], "dormitories": [], "flags": []}

    # Подтягиваем dormitory жильцов одним JOIN-запросом — нужен для
    # группировки cells по «общежитие × флаг».
    from app.modules.utility.models import Room as _Room
    user_ids = list({uid for _, uid in rows if uid})
    user_dorm: dict[int, str] = {}
    if user_ids:
        dorm_q = await db.execute(
            select(User.id, _Room.dormitory_name)
            .select_from(User)
            .join(_Room, User.room_id == _Room.id)
            .where(User.id.in_(user_ids))
        )
        user_dorm = {uid: (dorm or "—") for uid, dorm in dorm_q.all()}

    # Считаем cells: (dormitory, flag) → count.
    # _SOURCE_MARKERS — общий список служебных пометок (см. вверху модуля),
    # их в heatmap не показываем — это не реальные аномалии.
    from collections import Counter
    cells: dict[tuple[str, str], int] = Counter()
    flags_set: set[str] = set()
    dorms_set: set[str] = set()
    for raw_flags, uid in rows:
        if not raw_flags or not uid:
            continue
        dorm = user_dorm.get(uid, "—")
        for token in raw_flags.replace("|", ",").split(","):
            t = token.strip()
            if not t or _is_source_marker(t):
                continue
            cells[(dorm, t)] += 1
            flags_set.add(t)
            dorms_set.add(dorm)

    # Convert to dense response
    response_cells = [
        {"dormitory": d, "flag": f, "count": c}
        for (d, f), c in cells.items()
    ]
    response_cells.sort(key=lambda x: -x["count"])

    return {
        "period_id": period_id,
        "dormitories": sorted(dorms_set),
        "flags": sorted(flags_set),
        "cells": response_cells,
    }


# =========================================================================
# DRILLDOWN — список reading'ов с конкретным флагом
# =========================================================================
@router.get("/by-flag")
async def get_readings_by_flag(
    # Pattern ограничивает flag до канонической формы [A-Z][A-Z0-9_]+ — это
    # защита от LIKE-injection: без него flag='%' или '_' выдал бы full-scan
    # readings (потенциальный DoS). Все реальные флаги (SPIKE_HOT, FLAT_COLD,
    # COPY_NEIGHBOR_PARTIAL) под паттерн подходят.
    flag: str = Query(
        ...,
        pattern="^[A-Z][A-Z0-9_]+$",
        max_length=64,
        description="Точное имя флага (например, SPIKE_HOT)",
    ),
    period_id: Optional[int] = Query(None, description="Ограничить периодом"),
    limit: int = Query(100, ge=1, le=500, description="Сколько вернуть"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает список approved-reading'ов содержащих указанный флаг.
    Используется для drilldown из heatmap или topFlags на dashboard.
    """
    _require_admin(current_user)
    # anomaly_flags хранится как CSV. Ищем flag как substring с границами
    # запятой/начала/конца — строгое совпадение токена.
    # LIKE wildcard'ы (%/_) в flag блокирует pattern-валидация выше.
    pattern = f"%{flag}%"
    stmt = (
        select(MeterReading)
        .options(selectinload(MeterReading.user).selectinload(User.room),
                 selectinload(MeterReading.period))
        .where(
            MeterReading.is_approved.is_(True),
            MeterReading.anomaly_flags.like(pattern),
        )
        .order_by(MeterReading.anomaly_score.desc().nullslast(), MeterReading.id.desc())
        .limit(limit)
    )
    if period_id is not None:
        stmt = stmt.where(MeterReading.period_id == period_id)

    raw = (await db.execute(stmt)).scalars().all()
    # Фильтруем точное соответствие токена (LIKE может ложно совпасть на
    # подстроке, но FLAT_HOT в LIKE %FLAT_% совпадёт — нам же нужно
    # ровно «FLAT_HOT»).
    items = []
    for r in raw:
        tokens = [t.strip() for t in (r.anomaly_flags or "").split(",")]
        if flag not in tokens:
            continue
        room = r.user.room if r.user else None
        items.append({
            "reading_id": r.id,
            "user_id": r.user.id if r.user else None,
            "username": r.user.username if r.user else None,
            "period_id": r.period_id,
            "period_name": r.period.name if r.period else None,
            "dormitory_name": room.dormitory_name if room else None,
            "room_number": room.room_number if room else None,
            "anomaly_flags": r.anomaly_flags,
            "anomaly_score": r.anomaly_score,
            "total_cost": float(r.total_cost or 0),
        })

    return {
        "flag": flag,
        "period_id": period_id,
        "count": len(items),
        "items": items,
    }


# =========================================================================
# BULK DISMISS — массовое отметить «не аномалия»
# =========================================================================
class BulkDismissRequest(BaseModel):
    flag_code: str
    user_ids: list[int]  # если пусто — глобальный dismiss (user_id=NULL)
    reason: Optional[str] = None


@router.post("/dismissals/bulk")
async def bulk_dismiss(
    data: BulkDismissRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Массовая dismissal: пометить флаг «не аномалия» для нескольких
    жильцов сразу или глобально (если user_ids пуст).
    """
    _require_admin(current_user)

    if not data.flag_code:
        raise HTTPException(400, "flag_code обязателен")

    created = 0
    skipped_existing = 0

    if not data.user_ids:
        # Глобальный dismiss (user_id=NULL)
        existing = (await db.execute(
            select(AnomalyDismissal).where(
                AnomalyDismissal.user_id.is_(None),
                AnomalyDismissal.flag_code == data.flag_code,
            )
        )).scalars().first()
        if existing:
            skipped_existing += 1
        else:
            db.add(AnomalyDismissal(
                user_id=None,
                flag_code=data.flag_code,
                reason=data.reason or "bulk-dismiss (global)",
                created_by_id=current_user.id,
            ))
            created += 1
    else:
        # Per-user. Проверим какие уже существуют чтобы не дублировать.
        existing_q = await db.execute(
            select(AnomalyDismissal.user_id).where(
                AnomalyDismissal.user_id.in_(data.user_ids),
                AnomalyDismissal.flag_code == data.flag_code,
            )
        )
        existing_user_ids = {row[0] for row in existing_q.all()}
        for uid in data.user_ids:
            if uid in existing_user_ids:
                skipped_existing += 1
                continue
            db.add(AnomalyDismissal(
                user_id=uid,
                flag_code=data.flag_code,
                reason=data.reason or "bulk-dismiss",
                created_by_id=current_user.id,
            ))
            created += 1

    await db.commit()
    # Сбросить кеш dismissals — иначе следующий анализ ещё видит флаги.
    dismissals.invalidate()

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="bulk_dismiss", entity_type="anomaly_dismissal", entity_id=None,
        details={
            "flag_code": data.flag_code,
            "user_ids_count": len(data.user_ids),
            "global": not data.user_ids,
            "created": created, "skipped_existing": skipped_existing,
        },
    )
    await db.commit()

    return {
        "flag_code": data.flag_code,
        "created": created,
        "skipped_existing": skipped_existing,
    }


# =========================================================================
# ДВОЙНИКИ — жильцы с >1 approved MeterReading в одном периоде
# =========================================================================
@router.get("/duplicate-readings")
async def get_duplicate_readings(
    period_id: Optional[int] = Query(None, description="ID периода; None = активный"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает группы дубликатов: жильцы у которых в одном периоде
    более одного approved MeterReading.

    Возникает например когда manual_receipt создал новую квитанцию НЕ
    обнаружив уже существующую (баг до 0c17797), или после ручных
    манипуляций в БД. Админу нужна возможность найти эти дубли и
    удалить лишние через UI.
    """
    _require_admin(current_user)

    if period_id is None:
        active = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
        )).scalars().first()
        if not active:
            return {"period_id": None, "duplicates": [], "total_dup_groups": 0}
        period_id = active.id

    # Группы (user_id, room_id) с >1 approved reading в этом периоде
    dup_query = (
        select(
            MeterReading.user_id,
            func.count(MeterReading.id).label("cnt"),
        )
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True),
            MeterReading.user_id.is_not(None),
        )
        .group_by(MeterReading.user_id)
        .having(func.count(MeterReading.id) > 1)
    )
    dup_rows = (await db.execute(dup_query)).all()

    if not dup_rows:
        return {
            "period_id": period_id,
            "duplicates": [],
            "total_dup_groups": 0,
        }

    dup_user_ids = [r[0] for r in dup_rows]

    # Достаём все reading-и этих жильцов в этом периоде + user/room для UI
    detail_query = (
        select(MeterReading, User)
        .join(User, MeterReading.user_id == User.id)
        .options(selectinload(User.room))
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True),
            MeterReading.user_id.in_(dup_user_ids),
        )
        .order_by(MeterReading.user_id, MeterReading.created_at.desc())
    )
    rows = (await db.execute(detail_query)).all()

    # Группируем по user_id
    grouped: dict[int, dict] = {}
    for reading, user in rows:
        if user.id not in grouped:
            room = user.room
            grouped[user.id] = {
                "user_id": user.id,
                "username": user.username,
                "room_label": (
                    room.format_address if room else "без комнаты"
                ),
                "readings": [],
            }
        grouped[user.id]["readings"].append({
            "id": reading.id,
            "created_at": reading.created_at.isoformat() if reading.created_at else None,
            "total_cost": float(reading.total_cost or 0),
            "total_209": float(reading.total_209 or 0),
            "total_205": float(reading.total_205 or 0),
            "anomaly_flags": reading.anomaly_flags,
            "anomaly_score": int(reading.anomaly_score or 0),
            "hot_water": float(reading.hot_water) if reading.hot_water is not None else None,
            "cold_water": float(reading.cold_water) if reading.cold_water is not None else None,
            "electricity": float(reading.electricity) if reading.electricity is not None else None,
        })

    duplicates = list(grouped.values())
    duplicates.sort(key=lambda g: g["username"].lower())

    return {
        "period_id": period_id,
        "duplicates": duplicates,
        "total_dup_groups": len(duplicates),
        "total_extra_readings": sum(
            len(g["readings"]) - 1 for g in duplicates
        ),  # сколько reading-ов «лишние»
    }


# =========================================================================
# INBOX — единый список «всего что требует внимания админа» с подсказкой
# что делать и кнопками одного действия.
#
# Идея: вместо разрозненных списков (аномалии в одном таб-е, gsheets-
# конфликты в другом, ручные пересчёты в третьем) — одна таблица,
# отсортированная по severity. На каждой строке система предлагает
# «рекомендуемое действие» — админ нажимает кнопку и проблема закрыта.
# Если рекомендация не подходит — есть выбор альтернативных actions.
# =========================================================================

# Подсказки «что делать» по флагу аномалии. Маппится из реальных кейсов:
#   - dismiss   — отметить как не-аномалия (создаст AnomalyDismissal,
#                 self-learning перестанет триггериться у этого жильца).
#   - verify    — открыть запись в реестре, чтобы посмотреть глазами.
#   - reset     — обнулить расчёт (для DATA_OVERFLOW-подобных случаев).
#   - ignore    — пропустить (обычно для нейтральных трендов).
# Если флаг не в словаре — default 'verify'.
_FLAG_SUGGESTED_ACTION = {
    # Откат счётчика — почти всегда требует ручной проверки физически.
    "NEGATIVE_HOT": "verify", "NEGATIVE_COLD": "verify", "NEGATIVE_ELECT": "verify",
    # Скачки — часто крупная семья / гости / стирка → dismiss.
    "SPIKE_HOT": "dismiss", "SPIKE_COLD": "dismiss", "SPIKE_ELECT": "dismiss",
    "HIGH_HOT": "dismiss", "HIGH_COLD": "dismiss", "HIGH_ELECT": "dismiss",
    "HIGH_VS_PEERS_HOT": "dismiss", "HIGH_VS_PEERS_COLD": "dismiss",
    "HIGH_VS_PEERS_ELECT": "dismiss",
    "HIGH_PER_PERSON_COLD": "dismiss", "HIGH_PER_PERSON_ELECT": "dismiss",
    # «Замёрз» / «плоский» / «спад после скачка» — проверять физически.
    "FLAT_HOT": "verify", "FLAT_COLD": "verify", "FLAT_ELECT": "verify",
    "FROZEN_HOT": "verify", "FROZEN_COLD": "verify", "FROZEN_ELECT": "verify",
    "DROP_AFTER_SPIKE_HOT": "verify", "DROP_AFTER_SPIKE_COLD": "verify",
    "DROP_AFTER_SPIKE_ELECT": "verify",
    "HOT_GT_COLD": "verify",
    # Округление — часто привычка жильца, dismiss + (опционально) уведомить.
    "ROUND_NUMBER_HOT": "dismiss", "ROUND_NUMBER_COLD": "dismiss",
    "ROUND_NUMBER_ELECT": "dismiss",
    # Списано у соседа — связаться вручную.
    "COPY_NEIGHBOR": "verify", "COPY_NEIGHBOR_PARTIAL": "verify",
    # Тренды и нули — нет немедленного действия, просто следить.
    "TREND_UP_HOT": "ignore", "TREND_UP_COLD": "ignore", "TREND_UP_ELECT": "ignore",
    "ZERO_HOT": "ignore", "ZERO_COLD": "ignore", "ZERO_ELECT": "ignore",
    "GAP_RECOVERY": "ignore",
}

_FLAG_HUMAN_TITLE = {
    "NEGATIVE_HOT": "Откат счётчика ГВС",
    "NEGATIVE_COLD": "Откат счётчика ХВС",
    "NEGATIVE_ELECT": "Откат счётчика электр.",
    "SPIKE_HOT": "Резкий скачок ГВС",
    "SPIKE_COLD": "Резкий скачок ХВС",
    "SPIKE_ELECT": "Резкий скачок электр.",
    "HIGH_HOT": "Высокий расход ГВС",
    "HIGH_COLD": "Высокий расход ХВС",
    "HIGH_ELECT": "Высокий расход электр.",
    "FLAT_HOT": "Одинаковая подача ГВС",
    "FLAT_COLD": "Одинаковая подача ХВС",
    "FLAT_ELECT": "Одинаковая подача электр.",
    "FROZEN_HOT": "Замёрзший счётчик ГВС",
    "FROZEN_COLD": "Замёрзший счётчик ХВС",
    "FROZEN_ELECT": "Замёрзший счётчик электр.",
    "ZERO_HOT": "Перестал расходовать ГВС",
    "ZERO_COLD": "Перестал расходовать ХВС",
    "ZERO_ELECT": "Перестал расходовать электр.",
    "ROUND_NUMBER_HOT": "Подозрение на округление (ГВС)",
    "ROUND_NUMBER_COLD": "Подозрение на округление (ХВС)",
    "ROUND_NUMBER_ELECT": "Подозрение на округление (электр.)",
    "HOT_GT_COLD": "ГВС > ХВС (нефизично)",
    "COPY_NEIGHBOR": "Списано у соседа",
    "COPY_NEIGHBOR_PARTIAL": "Частичное совпадение с соседом",
    "GAP_RECOVERY": "Большая подача после паузы",
    "TREND_UP_HOT": "Постоянный рост ГВС",
    "TREND_UP_COLD": "Постоянный рост ХВС",
    "TREND_UP_ELECT": "Постоянный рост электр.",
    "DROP_AFTER_SPIKE_HOT": "Резкий спад после скачка ГВС",
    "DROP_AFTER_SPIKE_COLD": "Резкий спад после скачка ХВС",
    "DROP_AFTER_SPIKE_ELECT": "Резкий спад после скачка электр.",
    "HIGH_VS_PEERS_HOT": "Выше соседей по ГВС",
    "HIGH_VS_PEERS_COLD": "Выше соседей по ХВС",
    "HIGH_VS_PEERS_ELECT": "Выше соседей по электр.",
    "HIGH_PER_PERSON_COLD": "Много ХВС на 1 человека",
    "HIGH_PER_PERSON_ELECT": "Много электр. на 1 человека",
}

def _severity_from_score(score: int) -> str:
    if score >= 80:
        return "critical"
    if score >= 40:
        return "high"
    if score > 0:
        return "medium"
    return "low"


def _pick_primary_flag(flags_csv: str | None) -> str | None:
    """Из строки 'FLAG1,FLAG2,SOURCE_MARKER' выбираем первый реальный флаг
    (не source-marker). Поддерживаем prefix-патчи (BASELINE_LEGACY_*) и
    разделитель '|' (используется skip_recalc для маркеров RECALCED_*)."""
    if not flags_csv:
        return None
    for token in flags_csv.replace("|", ",").split(","):
        t = token.strip()
        if t and not _is_source_marker(t):
            return t
    return None


@router.get("/inbox")
async def get_inbox(
    period_days: int = Query(30, ge=1, le=365),
    kind: str = Query(
        "all",
        pattern="^(all|anomalies|gsheets)$",
        description="Фильтр по типу проблемы",
    ),
    severity: Optional[str] = Query(
        None,
        pattern="^(critical|high|medium)$",
        description="Минимальная серьёзность (только для anomalies)",
    ),
    limit: int = Query(100, ge=1, le=300),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Единый список проблем требующих внимания админа.

    Объединяет в одном ответе:
      - anomalies: MeterReading с anomaly_score > 0 за период
      - gsheets:   GSheetsImportRow со status conflict/unmatched

    Для каждой проблемы возвращает suggested_action — что админ может
    сделать одной кнопкой (см. _FLAG_SUGGESTED_ACTION). Если рекомендация
    не подходит — список available_actions с альтернативами.
    """
    _require_admin(current_user)
    cutoff = utcnow() - timedelta(days=period_days)

    issues: list[dict] = []

    # 1) Anomalies
    if kind in ("all", "anomalies"):
        score_threshold = {"critical": 80, "high": 40, "medium": 1}.get(
            severity or "medium", 1
        )
        anom_q = (
            select(MeterReading)
            .options(
                selectinload(MeterReading.user).selectinload(User.room),
                selectinload(MeterReading.period),
            )
            .where(
                MeterReading.created_at >= cutoff,
                MeterReading.anomaly_score >= score_threshold,
                MeterReading.anomaly_flags.is_not(None),
                MeterReading.anomaly_flags != "",
            )
            .order_by(
                MeterReading.anomaly_score.desc().nullslast(),
                MeterReading.id.desc(),
            )
            .limit(limit)
        )
        for r in (await db.execute(anom_q)).scalars().all():
            primary_flag = _pick_primary_flag(r.anomaly_flags)
            if not primary_flag:
                continue  # Только source-markers — это не аномалия.
            room = r.user.room if r.user else None
            suggested = _FLAG_SUGGESTED_ACTION.get(primary_flag, "verify")
            title = _FLAG_HUMAN_TITLE.get(primary_flag, primary_flag)
            issues.append({
                "issue_id": f"anomaly:{r.id}",
                "kind": "anomaly",
                "severity": _severity_from_score(int(r.anomaly_score or 0)),
                "title": title,
                "flag": primary_flag,
                "all_flags": r.anomaly_flags,
                "score": int(r.anomaly_score or 0),
                "context": {
                    "reading_id": r.id,
                    "user_id": r.user.id if r.user else None,
                    "username": r.user.username if r.user else None,
                    "period_id": r.period_id,
                    "period_name": r.period.name if r.period else None,
                    "dormitory": room.dormitory_name if room else None,
                    "room_number": room.room_number if room else None,
                    "total_cost": float(r.total_cost or 0),
                    "hot_water": float(r.hot_water) if r.hot_water is not None else None,
                    "cold_water": float(r.cold_water) if r.cold_water is not None else None,
                    "electricity": float(r.electricity) if r.electricity is not None else None,
                },
                "suggested_action": suggested,
                "available_actions": ["dismiss", "verify", "ignore"],
            })

    # 2) GSheets конфликты + unmatched
    if kind in ("all", "gsheets"):
        gs_q = (
            select(GSheetsImportRow)
            .where(
                GSheetsImportRow.created_at >= cutoff,
                GSheetsImportRow.status.in_(["conflict", "unmatched"]),
            )
            .order_by(GSheetsImportRow.created_at.desc())
            .limit(limit)
        )
        for g in (await db.execute(gs_q)).scalars().all():
            is_unmatched = g.status == "unmatched"
            issues.append({
                "issue_id": f"gsheets:{g.id}",
                "kind": "gsheets",
                "severity": "high" if is_unmatched else "medium",
                "title": (
                    "GSheets: ФИО не найдено в базе"
                    if is_unmatched
                    else "GSheets: конфликт сопоставления"
                ),
                "flag": g.status,
                "score": 100 - int(g.match_score or 0),
                "context": {
                    "row_id": g.id,
                    "fio_raw": g.raw_fio,
                    "dormitory_raw": g.raw_dormitory,
                    "room_raw": g.raw_room_number,
                    "hot_water_raw": g.raw_hot_water,
                    "cold_water_raw": g.raw_cold_water,
                    "match_score": int(g.match_score or 0),
                    "conflict_reason": g.conflict_reason,
                    "sheet_timestamp": (
                        g.sheet_timestamp.isoformat() if g.sheet_timestamp else None
                    ),
                },
                "suggested_action": "open",
                "available_actions": ["open", "reject"],
            })

    # 3) Битый формат показаний (>99999 = потеряна десятичная точка) — критично,
    #    т.к. даёт счёт в сотни тысяч ₽ (инцидент 1.48 млрд май 2026).
    if kind in ("all", "anomalies"):
        fmt_q = (
            select(MeterReading)
            .options(
                selectinload(MeterReading.user).selectinload(User.room),
                selectinload(MeterReading.period),
            )
            .where(
                MeterReading.created_at >= cutoff,
                or_(MeterReading.hot_water > 99999,
                    MeterReading.cold_water > 99999),
            )
            .order_by(MeterReading.total_cost.desc().nullslast())
            .limit(limit)
        )
        for r in (await db.execute(fmt_q)).scalars().all():
            room = r.user.room if r.user else None
            issues.append({
                "issue_id": f"format:{r.id}",
                "kind": "format",
                "severity": "critical",
                "title": "Битый формат показания (потеряна точка)",
                "flag": "FORMAT_SUSPECT",
                "score": 90,
                "context": {
                    "reading_id": r.id,
                    "user_id": r.user.id if r.user else None,
                    "username": r.user.username if r.user else None,
                    "period_id": r.period_id,
                    "period_name": r.period.name if r.period else None,
                    "dormitory": room.dormitory_name if room else None,
                    "room_number": room.room_number if room else None,
                    "total_cost": float(r.total_cost or 0),
                    "hot_water": float(r.hot_water) if r.hot_water is not None else None,
                    "cold_water": float(r.cold_water) if r.cold_water is not None else None,
                },
                "suggested_action": "verify",
                "available_actions": ["verify", "ignore"],
            })

    # Сортируем итоговый список: critical → high → medium → low,
    # внутри по score убыванию.
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues.sort(key=lambda i: (sev_order.get(i["severity"], 9), -i.get("score", 0)))

    return {
        "period_days": period_days,
        "kind": kind,
        "total": len(issues),
        "issues": issues[:limit],
        "summary": {
            "anomalies": sum(1 for i in issues if i["kind"] == "anomaly"),
            "gsheets":   sum(1 for i in issues if i["kind"] == "gsheets"),
            "format":    sum(1 for i in issues if i["kind"] == "format"),
            "critical":  sum(1 for i in issues if i["severity"] == "critical"),
            "high":      sum(1 for i in issues if i["severity"] == "high"),
        },
    }


class InboxResolveRequest(BaseModel):
    issue_id: str  # "anomaly:578" | "gsheets:42"
    action: str    # "dismiss" | "verify" | "ignore" | "reject" | "open"
    note: Optional[str] = None


@router.post("/inbox/resolve")
async def resolve_inbox_issue(
    data: InboxResolveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Применить действие к проблеме из inbox.

    Поддерживаемые actions:
      - anomaly + dismiss → создать AnomalyDismissal (user_id, flag_code).
                           Self-learning перестанет триггериться.
      - anomaly + ignore  → no-op (просто отметить «видел, всё ок»).
      - anomaly + verify  → no-op на бэке (фронт откроет reading в реестре).
      - gsheets + reject  → пометить gsheets_row status='rejected'.
      - gsheets + open    → no-op (фронт откроет gsheets-modal).

    Действия approve gsheets row / reset reading — пока не реализованы,
    их делают через специализированные эндпоинты с дополнительной логикой
    (создание MeterReading, обнуление компонент).
    """
    _require_admin(current_user)

    # Разбираем issue_id
    parts = data.issue_id.split(":", 1)
    if len(parts) != 2:
        raise HTTPException(400, "issue_id должен быть в формате '<kind>:<id>'")
    kind, raw_id = parts
    try:
        entity_id = int(raw_id)
    except ValueError:
        raise HTTPException(400, "id должен быть числом")

    if data.action not in ("dismiss", "verify", "ignore", "reject", "open"):
        raise HTTPException(400, f"Неизвестное действие: {data.action!r}")

    # === ANOMALY ===
    if kind == "anomaly":
        reading = await db.get(MeterReading, entity_id)
        if not reading:
            raise HTTPException(404, f"Reading {entity_id} не найден")

        if data.action == "dismiss":
            primary_flag = _pick_primary_flag(reading.anomaly_flags)
            if not primary_flag:
                raise HTTPException(400, "У reading нет аномального флага")
            if not reading.user_id:
                raise HTTPException(400, "У reading нет привязки к жильцу")

            # Проверим дубликат
            existing = (await db.execute(
                select(AnomalyDismissal).where(
                    AnomalyDismissal.user_id == reading.user_id,
                    AnomalyDismissal.flag_code == primary_flag,
                )
            )).scalars().first()
            if existing:
                if data.note:
                    existing.reason = data.note
                    await db.commit()
                    dismissals.invalidate()
                return {"status": "exists", "dismissal_id": existing.id}

            dism = AnomalyDismissal(
                user_id=reading.user_id,
                flag_code=primary_flag,
                reason=data.note or "inbox-resolve",
                created_by_id=current_user.id,
            )
            db.add(dism)
            await write_audit_log(
                db, current_user.id, current_user.username,
                action="inbox_resolve_dismiss", entity_type="meter_reading",
                entity_id=entity_id,
                details={"flag": primary_flag, "user_id": reading.user_id},
            )
            await db.commit()
            dismissals.invalidate()
            return {"status": "ok", "action": "dismiss", "dismissal_id": dism.id}

        if data.action in ("verify", "open", "ignore"):
            # Бэкенду делать нечего — фронт сам откроет reading в реестре.
            # Для ignore просто audit-логируем, чтобы было видно что админ
            # принял решение (даже если оно — «оставить как есть»).
            if data.action == "ignore":
                await write_audit_log(
                    db, current_user.id, current_user.username,
                    action="inbox_resolve_ignore", entity_type="meter_reading",
                    entity_id=entity_id,
                    details={"note": data.note},
                )
                await db.commit()
            return {"status": "ok", "action": data.action}

        raise HTTPException(400, f"Действие {data.action!r} не применимо к anomaly")

    # === GSHEETS ===
    if kind == "gsheets":
        gs_row = await db.get(GSheetsImportRow, entity_id)
        if not gs_row:
            raise HTTPException(404, f"GSheets-row {entity_id} не найдена")

        if data.action == "reject":
            if gs_row.status not in ("conflict", "unmatched", "pending"):
                raise HTTPException(
                    400,
                    f"Нельзя отклонить строку со статусом {gs_row.status!r}",
                )
            gs_row.status = "rejected"
            await write_audit_log(
                db, current_user.id, current_user.username,
                action="inbox_resolve_reject_gsheets", entity_type="gsheets_import_row",
                entity_id=entity_id,
                details={"old_status": gs_row.status, "note": data.note},
            )
            await db.commit()
            return {"status": "ok", "action": "reject", "row_id": entity_id}

        if data.action == "open":
            # Фронт сам откроет нужную модалку gsheets-матчинга.
            return {"status": "ok", "action": "open", "row_id": entity_id}

        raise HTTPException(400, f"Действие {data.action!r} не применимо к gsheets")

    # === FORMAT (битый формат показания) ===
    if kind == "format":
        # verify/ignore — no-op на бэке (фронт откроет reading в реестре для
        # ручного исправления точки). ignore логируем для аудита.
        if data.action in ("verify", "ignore"):
            if data.action == "ignore":
                await write_audit_log(
                    db, current_user.id, current_user.username,
                    action="inbox_resolve_ignore_format", entity_type="meter_reading",
                    entity_id=entity_id,
                    details={"note": data.note},
                )
                await db.commit()
            return {"status": "ok", "action": data.action}
        raise HTTPException(400, f"Действие {data.action!r} не применимо к format")

    raise HTTPException(400, f"Неизвестный kind: {kind!r}")


# =========================================================================
# STUCK DRAFTS — заблокированные показания, ждущие разбора админом
# =========================================================================
@router.get("/stuck-drafts")
async def list_stuck_drafts(
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список черновиков с флагом DATA_OVERFLOW_RESET — выставлен автоматом
    при auto-cleanup (cleanup_outlier_readings_task) или вручную через
    app/scripts/cleanup_anomaly_readings.py. Это reading'и с нереалистичными
    значениями счётчиков (вода > MAX_WATER_METER_VALUE, электр > MAX_…)
    или итогом > MAX_TOTAL_COST_PER_READING — система их отказалась
    утверждать, ждут решения админа.

    Что админ может сделать (через отдельные endpoint'ы):
      - DELETE /api/admin/readings/{id} — удалить (если жилец переподаст)
      - POST /api/admin/readings/{id}/approve — принять с корректировкой
    """
    _require_admin(current_user)
    stmt = (
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
        )
        .where(
            MeterReading.is_approved.is_(False),
            MeterReading.anomaly_flags.contains("DATA_OVERFLOW_RESET"),
        )
        .order_by(desc(MeterReading.created_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()

    items = []
    for r in rows:
        user = r.user
        room = user.room if user else None
        items.append({
            "reading_id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "period_id": r.period_id,
            "period_name": r.period.name if r.period else None,
            "user_id": user.id if user else None,
            "username": user.username if user else None,
            "full_name": user.full_name if user else None,
            "dormitory_name": room.dormitory_name if room else None,
            "room_number": room.room_number if room else None,
            "hot_water": float(r.hot_water or 0),
            "cold_water": float(r.cold_water or 0),
            "electricity": float(r.electricity or 0),
            "total_cost_was": float(r.total_cost or 0),
            "anomaly_flags": r.anomaly_flags,
        })

    return {"count": len(items), "items": items}


# =========================================================================
# HIGH-DELTA READINGS — уже-утверждённые с подозрительной дельтой от prev
# =========================================================================
@router.get("/high-delta-readings")
async def list_high_delta_readings(
    threshold: float = Query(50.0, ge=1.0, le=500.0,
                              description="Порог дельты в м³ за период"),
    include_initial: bool = Query(False,
                              description="Включать начальные показания (нет предыдущего "
                                          "reading). По умолчанию скрыты: дельта «от 0» = "
                                          "абсолютное показание счётчика, а не расход за месяц."),
    sort_by: str = Query("delta",
                              description="Сортировка: delta (по приросту м³) | "
                                          "cost (по сумме квитанции — деньги важнее)"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Уже-утверждённые reading'и где дельта от предыдущего (или от
    AUTO_GENERATED 0/0/0 baseline) превышает порог — выявляет ситуации
    типа Пегарькова (май 2026: +236 м³ ХВС поверх AUTO_GENERATED → счёт
    81 485 ₽). Раньше дельта > порога была warning и пропускалась.

    Реализация: для каждого MeterReading с period_id находим prev approved
    reading того же user+room с меньшим period_id, считаем delta_hot/cold.
    Если хоть одна > threshold — в выдачу.

    Что админ дальше делает:
      - DELETE /api/admin/readings/{id} — снести аномальный reading;
      - POST /api/admin/readings/manual-entry — поставить корректный baseline.
    """
    _require_admin(current_user)
    from decimal import Decimal as _D

    th = _D(str(threshold))

    # Загружаем последние 500 утверждённых reading'ов с period_id, чтобы
    # можно было соединить с предыдущим по (user_id, room_id).
    stmt = (
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
        )
        .where(
            MeterReading.is_approved.is_(True),
            MeterReading.period_id.isnot(None),
        )
        .order_by(desc(MeterReading.created_at))
        # 10000 вместо 2000: при 320+ жильцах × многих периодах топ-2000 по
        # created_at мог не содержать хронологически предыдущий reading →
        # ложный is_initial / неверный prev. 10000 покрывает реальный объём.
        .limit(10000)
    )
    rows = (await db.execute(stmt)).scalars().all()

    # Группируем по (user_id, room_id) → список reading'ов отсортирован
    # по period_id descending. Для каждого считаем дельту от следующего
    # (хронологически предыдущего) reading'а.
    by_key: dict[tuple, list] = {}
    for r in rows:
        key = (r.user_id, r.room_id)
        by_key.setdefault(key, []).append(r)

    # Хронологическая сортировка по (год, месяц) из period.name, НЕ по
    # period_id: периоды могли заводиться не по порядку (напр. «Март 2026»
    # создан позже «Май 2026» → больший id, но раньше по календарю). Без
    # этого prev определялся неверно → мусорные/отрицательные дельты
    # (кейс Колемагина: period Март, prev Май, ГВС −10.93).
    _months_ru = {
        "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
        "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
    }

    def _chrono_key(r):
        name = (r.period.name if r.period else "") or ""
        parts = name.strip().lower().split()
        if len(parts) == 2 and parts[0] in _months_ru:
            try:
                return (int(parts[1]), _months_ru[parts[0]], r.period_id or 0)
            except (ValueError, TypeError):
                pass
        return (0, 0, r.period_id or 0)  # fallback: по id

    for key in by_key:
        by_key[key].sort(key=_chrono_key, reverse=True)

    items = []
    for key, lst in by_key.items():
        for i, r in enumerate(lst):
            prev = lst[i + 1] if i + 1 < len(lst) else None
            # Начальное показание (нет предыдущего approved reading) — это НЕ
            # расход за месяц, а абсолютное значение счётчика при первой подаче
            # (5 целых + 3 дробных знака). Дельта «от 0» = само показание и
            # засоряет топ ложными срабатываниями. По умолчанию скрываем —
            # начальные показания и есть baseline, их не «чистят».
            is_initial = prev is None
            if is_initial and not include_initial:
                continue
            prev_hot = (prev.hot_water if prev else _D("0")) or _D("0")
            prev_cold = (prev.cold_water if prev else _D("0")) or _D("0")
            cur_hot = r.hot_water or _D("0")
            cur_cold = r.cold_water or _D("0")
            d_hot = cur_hot - prev_hot
            d_cold = cur_cold - prev_cold
            max_d = max(d_hot, d_cold)
            if max_d <= th:
                continue
            user = r.user
            room = user.room if user else None
            items.append({
                "reading_id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "period_id": r.period_id,
                "period_name": r.period.name if r.period else None,
                "user_id": user.id if user else None,
                "username": user.username if user else None,
                "full_name": user.full_name if user else None,
                "dormitory_name": room.dormitory_name if room else None,
                "room_number": room.room_number if room else None,
                "hot_water": float(cur_hot),
                "cold_water": float(cur_cold),
                "prev_hot_water": float(prev_hot),
                "prev_cold_water": float(prev_cold),
                "delta_hot": float(d_hot),
                "delta_cold": float(d_cold),
                "delta_max": float(max_d),
                "is_initial": is_initial,
                # Формат-подозрение: целая часть >5 знаков (>99999 м³) = потеряна
                # десятичная точка (напр. 775930 вместо 775.930) — баг ввода.
                "format_suspect": bool(cur_hot > 99999 or cur_cold > 99999),
                "prev_period_name": prev.period.name if prev and prev.period else None,
                "prev_is_synth": bool(
                    prev and prev.anomaly_flags and (
                        "AUTO_GENERATED" in prev.anomaly_flags
                        or "DATA_OVERFLOW_RESET" in prev.anomaly_flags
                        or "AUTO_NO_HISTORY" in prev.anomaly_flags
                    )
                ),
                "total_cost": float(r.total_cost or 0),
                "anomaly_flags": r.anomaly_flags,
            })

    # Сортировка: по сумме квитанции (деньги важнее) или по дельте.
    sort_field = "total_cost" if sort_by == "cost" else "delta_max"
    items.sort(key=lambda it: it[sort_field], reverse=True)
    items = items[:limit]

    return {"count": len(items), "threshold": float(th), "sort_by": sort_by, "items": items}


@router.get("/cloned-baselines")
async def list_cloned_baselines(
    period_id: int | None = Query(None, description="ID периода; по умолчанию активный"),
    min_group: int = Query(3, ge=2, le=50,
                           description="Минимум разных жильцов с идентичными показаниями = группа"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Группы жильцов с ИДЕНТИЧНЫМИ показаниями (ГВС+ХВС) в одном периоде —
    признак шаблонного («клонированного») залива baseline, а не реальных подач.

    Пример (май 2026): у группы жильцов prev-показание было ровно 9.00/21.00
    — слишком одинаково для реальных счётчиков. Такой baseline потом даёт
    ложные «аномальные дельты» при первой настоящей подаче.

    Что админ дальше делает: проверяет группу, при необходимости пересобирает
    baseline из реальных первых показаний (manual-entry / reload period).
    """
    _require_admin(current_user)
    from decimal import Decimal as _D

    # Определяем период (активный по умолчанию).
    if period_id is None:
        period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active)
        )).scalars().first()
        period_id = period.id if period else None
    else:
        period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.id == period_id)
        )).scalars().first()
    if not period_id:
        return {"count": 0, "period_id": None, "period_name": None,
                "min_group": min_group, "groups": []}

    stmt = (
        select(MeterReading)
        .options(selectinload(MeterReading.user).selectinload(User.room))
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True),
        )
    )
    rows = (await db.execute(stmt)).scalars().all()

    # Группируем по (hot_water, cold_water). Исключаем нулевые — 0/0 это
    # легитимное «не пользовался / нет счётчика», а не клон.
    groups_map: dict[tuple, list] = {}
    for r in rows:
        h = r.hot_water or _D("0")
        c = r.cold_water or _D("0")
        if h <= 0 and c <= 0:
            continue
        groups_map.setdefault((str(h), str(c)), []).append(r)

    groups = []
    for (h_s, c_s), lst in groups_map.items():
        users = {r.user_id for r in lst}  # уникальные жильцы
        if len(users) < min_group:
            continue
        members = []
        for r in lst:
            u = r.user
            room = u.room if u else None
            members.append({
                "reading_id": r.id,
                "user_id": u.id if u else None,
                "username": u.username if u else None,
                "full_name": u.full_name if u else None,
                "dormitory_name": room.dormitory_name if room else None,
                "room_number": room.room_number if room else None,
                "total_cost": float(r.total_cost or 0),
            })
        groups.append({
            "hot_water": float(_D(h_s)),
            "cold_water": float(_D(c_s)),
            "user_count": len(users),
            "members": members,
        })

    groups.sort(key=lambda g: -g["user_count"])  # крупнейшие клоны сверху

    return {
        "count": len(groups),
        "period_id": period_id,
        "period_name": period.name if period else None,
        "min_group": min_group,
        "groups": groups,
    }


@router.get("/room-type-mismatches")
async def list_room_type_mismatches(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Квартиры, чей состав жильцов не соответствует типу квартиры.

    Полный аудит несоответствий:
      • multi_family       — 2+ семейных аккаунта в семейной квартире;
      • unmarked_singles   — 2+ холостяка, но квартира не помечена холостяцкой;
      • mixed_types        — и семейные, и холостяки в одной несхолостяцкой;
      • singles_with_family— семейный аккаунт в холостяцкой квартире.

    Считаем по привязке к комнате (User.room_id), не по подачам — ловит и тех,
    кто не подаёт показания, и «мёртвые души». Тот же источник, что и сигнал
    ROOM_TYPE_MISMATCH в Мониторе проблем. Что админ делает: проверяет состав,
    помечает квартиру холостяцкой / правит тип жильца / убирает дубль-призрак.
    """
    _require_admin(current_user)
    from app.modules.utility.services.room_audit import find_room_type_mismatches
    items = await find_room_type_mismatches(db)
    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    return {"count": len(items), "by_kind": by_kind, "items": items}


@router.get("/pending-anomalies")
async def list_pending_anomalies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Аномалии активного периода, ожидающие решения — РОВНО та же выборка, что
    считает карточка «Аномалий» на дашборде (черновики активного периода, не
    утверждены, anomaly_score >= 80). count совпадает с числом на карточке, а
    список даёт детализацию по строке. Раньше модалка показывала флаги за 30
    дней и расходилась с карточкой по определению."""
    _require_admin(current_user)
    active = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if not active:
        return {"count": 0, "period_name": None, "items": []}

    rows = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.user).selectinload(User.room))
        .where(
            MeterReading.period_id == active.id,
            MeterReading.is_approved.is_(False),
            MeterReading.anomaly_score >= 80,
        )
        .order_by(MeterReading.anomaly_score.desc())
    )).scalars().all()

    items = []
    for r in rows:
        u = r.user
        room = u.room if u else None
        items.append({
            "reading_id": r.id,
            "user_id": u.id if u else None,
            "username": u.username if u else "—",
            "dormitory_name": room.dormitory_name if room else None,
            "room_number": room.room_number if room else None,
            "anomaly_score": int(r.anomaly_score or 0),
            "flags": [f.strip() for f in (r.anomaly_flags or "").split(",") if f.strip()],
            "total_cost": float(r.total_cost or 0),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"count": len(items), "period_name": active.name, "items": items}


# =========================================================================
# МОНИТОР ПРОБЛЕМ ЖИЛЬЦОВ (система сигнализации о реальных проблемах)
# =========================================================================
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@router.post("/scan-problems")
async def scan_problems_now(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запустить сканер проблем жильцов вручную (обычно — фоновый celery)."""
    _require_admin(current_user)
    from app.modules.utility.services.resident_problem_scanner import (
        scan_resident_problems,
    )
    return await scan_resident_problems(db)


@router.get("/resident-problems")
async def list_resident_problems(
    status: str = Query("active",
                        description="active | open | acknowledged | resolved | all"),
    severity: Optional[str] = Query(None, description="critical|high|medium|low"),
    limit: int = Query(300, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список сигналов о проблемах жильцов — для колокольчика / Inbox /
    светофора. active = open+acknowledged без отложенных (snooze)."""
    _require_admin(current_user)
    now = utcnow()
    q = (
        select(ResidentProblem)
        .options(selectinload(ResidentProblem.user).selectinload(User.room))
    )
    if status == "active":
        q = q.where(ResidentProblem.status.in_(["open", "acknowledged"]))
    elif status != "all":
        q = q.where(ResidentProblem.status == status)
    if severity:
        q = q.where(ResidentProblem.severity == severity)
    rows = (await db.execute(q.limit(1000))).scalars().all()

    items = []
    for p in rows:
        if status == "active" and p.snooze_until and p.snooze_until > now:
            continue  # отложенные не показываем в активных
        u = p.user
        room = u.room if u else None
        items.append({
            "id": p.id,
            "user_id": p.user_id,
            "username": u.username if u else None,
            "full_name": getattr(u, "full_name", None) if u else None,
            "dormitory": room.dormitory_name if room else None,
            "room_number": room.room_number if room else None,
            "problem_type": p.problem_type,
            "severity": p.severity,
            "score": p.score,
            "title": p.title,
            "details": p.details,
            "status": p.status,
            "first_detected_at": p.first_detected_at.isoformat() if p.first_detected_at else None,
            "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
        })
    items.sort(key=lambda x: (_SEV_ORDER.get(x["severity"], 9), -x["score"]))
    items = items[:limit]
    summary = {}
    for it in items:
        summary[it["severity"]] = summary.get(it["severity"], 0) + 1
    return {"count": len(items), "summary": summary, "items": items}


class ResidentProblemAction(BaseModel):
    action: str            # acknowledge | resolve | snooze
    snooze_days: int = 7


@router.post("/resident-problems/{problem_id}/action")
async def resident_problem_action(
    problem_id: int,
    data: ResidentProblemAction,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Действие админа над сигналом: acknowledge (видел) / resolve (решено) /
    snooze (отложить на N дней)."""
    _require_admin(current_user)
    p = await db.get(ResidentProblem, problem_id)
    if not p:
        raise HTTPException(404, "Сигнал не найден")
    now = utcnow()
    if data.action == "acknowledge":
        p.status = "acknowledged"
        p.acknowledged_by_id = current_user.id
        p.acknowledged_at = now
    elif data.action == "resolve":
        p.status = "resolved"
        p.resolved_at = now
    elif data.action == "snooze":
        p.snooze_until = now + timedelta(days=max(1, data.snooze_days))
    else:
        raise HTTPException(400, f"Неизвестное действие: {data.action!r}")
    await db.commit()
    return {"status": "ok", "action": data.action, "problem_id": problem_id}
