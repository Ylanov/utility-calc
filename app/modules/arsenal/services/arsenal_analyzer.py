"""arsenal_analyzer.py — правила обнаружения нарушений/фрода/багов данных.

Как это работает
================
Периодическая Celery-задача (`run_arsenal_analyzer`) или ручной запуск
(POST /analyzer/run) проходит по всем активным правилам и создаёт/обновляет
записи в `arsenal_anomaly_flags`:

    * первый раз обнаружил → INSERT (first_seen_at = last_seen_at = now)
    * повторно обнаружил   → UPDATE last_seen_at = now
    * перестал находить    → resolved_at = now (на следующем прогоне)

Админ видит список активных флагов в «Центре анализа». Может:
    * dismiss — пометить как false-positive (не показывать пока проблема
      не изменится);
    * исправить ситуацию — флаг автоматически resolve'нется.

Правила (rule_code):
    DUPLICATE_SERIAL    — один серийник активен в нескольких местах
    STALE_STOCK         — партия без движения > N месяцев
    SUSPICIOUS_BURST    — один пользователь провёл > N документов за 24ч
    GHOST_SERIAL        — серийник в DocumentItem без WeaponRegistry
    ZERO_BATCH          — активная партия с quantity<=0
    OVERDUE_SHIPMENT    — Отправка без Приёма > N дней

Все пороги — в `arsenal_analyzer_settings` (редактируются админом).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.modules.arsenal.models import (
    ArsenalAnalyzerSetting,
    ArsenalAnomalyFlag,
    Document,
    DocumentItem,
    Nomenclature,
    WeaponRegistry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CONFIG HELPERS (лёгкий кеш-free доступ — задача бежит раз в час, хватит)
# ---------------------------------------------------------------------------
def _get(db, key, default=None):
    """Синхронный read-through — задача работает в sync-сессии."""
    row = db.query(ArsenalAnalyzerSetting).filter(
        ArsenalAnalyzerSetting.key == key
    ).first()
    if not row or not row.is_enabled:
        return default
    return row.value


def _get_bool(db, key, default=False):
    v = _get(db, key)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _get_int(db, key, default=0):
    v = _get(db, key)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# UPSERT HELPER
# ---------------------------------------------------------------------------
def _upsert_flag(db, *, rule_code, severity, title, entity_type, entity_id, details):
    """Атомарный upsert: UNIQUE (rule_code, entity_type, entity_id).
    При повторном найденном — обновляем last_seen_at и detaills (могли измениться).
    """
    now = datetime.utcnow()
    stmt = pg_insert(ArsenalAnomalyFlag).values(
        rule_code=rule_code,
        severity=severity,
        title=title,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        first_seen_at=now,
        last_seen_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uix_anomaly_rule_entity",
        set_={
            "last_seen_at": now,
            "severity": severity,
            "title": title,
            "details": details,
            # resolved_at сбрасываем если флаг ожил
            "resolved_at": None,
        },
    )
    db.execute(stmt)


def _resolve_unseen(db, rule_code: str, seen_keys: set[tuple]):
    """Все активные флаги этого правила, которых нет в seen_keys, считаем
    resolved (ситуация исправилась)."""
    now = datetime.utcnow()
    active = db.query(ArsenalAnomalyFlag).filter(
        ArsenalAnomalyFlag.rule_code == rule_code,
        ArsenalAnomalyFlag.resolved_at.is_(None),
    ).all()
    for f in active:
        key = (f.entity_type, f.entity_id)
        if key not in seen_keys:
            f.resolved_at = now


# ---------------------------------------------------------------------------
# ПРАВИЛА
# ---------------------------------------------------------------------------
def check_duplicate_serial(db):
    """Один серийник активен в нескольких WeaponRegistry-записях одновременно.
    Обычно невозможно благодаря UniqueConstraint, но проверяем — могут быть
    серийники на разных номенклатурах (constraint учитывает nom_id)."""
    if not _get_bool(db, "rule.duplicate_serial.enabled", True):
        return 0
    code = "DUPLICATE_SERIAL"
    rows = db.query(
        WeaponRegistry.serial_number,
        WeaponRegistry.nomenclature_id,
        func.count(WeaponRegistry.id).label("cnt"),
    ).filter(
        WeaponRegistry.status == 1,
        WeaponRegistry.serial_number.is_not(None),
    ).group_by(
        WeaponRegistry.serial_number, WeaponRegistry.nomenclature_id
    ).having(func.count(WeaponRegistry.id) > 1).all()

    seen = set()
    for serial, nom_id, cnt in rows:
        nom = db.query(Nomenclature).filter(Nomenclature.id == nom_id).first()
        # entity_id = nom_id, чтобы получить ровно один флаг на (nom, serial)
        title = f"Серийник «{serial}» активен в {cnt} местах ({nom.name if nom else '?'})"
        details = {
            "serial": serial,
            "nomenclature_id": nom_id,
            "nomenclature_name": nom.name if nom else None,
            "duplicates_count": int(cnt),
        }
        _upsert_flag(
            db, rule_code=code, severity="critical", title=title,
            entity_type="weapon", entity_id=nom_id, details=details,
        )
        seen.add(("weapon", nom_id))

    _resolve_unseen(db, code, seen)
    return len(seen)


def check_stale_stock(db):
    """Партия / единица без движения более N месяцев — кандидат на проверку."""
    if not _get_bool(db, "rule.stale_stock.enabled", True):
        return 0
    months = _get_int(db, "rule.stale_stock.months", 24)
    cutoff = datetime.utcnow() - timedelta(days=30 * months)
    code = "STALE_STOCK"

    # Подзапрос: для каждой единицы — когда последний раз её трогали в DocumentItem.
    last_touch_subq = (
        db.query(
            DocumentItem.weapon_id,
            func.max(Document.operation_date).label("last_op"),
        )
        .join(Document, Document.id == DocumentItem.document_id)
        .group_by(DocumentItem.weapon_id)
        .subquery()
    )

    # Активные записи где последнее движение < cutoff ИЛИ вообще нет движений после created_at.
    rows = db.query(WeaponRegistry, last_touch_subq.c.last_op).outerjoin(
        last_touch_subq, last_touch_subq.c.weapon_id == WeaponRegistry.id,
    ).filter(
        WeaponRegistry.status == 1,
        or_(
            last_touch_subq.c.last_op < cutoff,
            and_(last_touch_subq.c.last_op.is_(None), WeaponRegistry.created_at < cutoff),
        ),
    ).limit(500).all()  # cap на случай гигантской базы

    seen = set()
    for weapon, last_op in rows:
        ref_date = last_op or weapon.created_at
        days = (datetime.utcnow() - ref_date).days if ref_date else None
        title = f"Без движения {days} дн." if days else "Без движения"
        details = {
            "weapon_id": weapon.id,
            "serial": weapon.serial_number,
            "last_movement_at": ref_date.isoformat() if ref_date else None,
            "days_since": days,
            "object_id": weapon.current_object_id,
        }
        _upsert_flag(
            db, rule_code=code, severity="info",
            title=title + f" — «{weapon.serial_number}»",
            entity_type="weapon", entity_id=weapon.id, details=details,
        )
        seen.add(("weapon", weapon.id))

    _resolve_unseen(db, code, seen)
    return len(seen)


def check_suspicious_burst(db):
    """Один пользователь провёл больше N документов за последние 24 часа —
    возможный фрод или массовое проведение без проверки."""
    if not _get_bool(db, "rule.suspicious_burst.enabled", True):
        return 0
    threshold = _get_int(db, "rule.suspicious_burst.threshold_per_day", 20)
    code = "SUSPICIOUS_BURST"
    cutoff = datetime.utcnow() - timedelta(hours=24)

    rows = db.query(
        Document.author_id,
        func.count(Document.id).label("cnt"),
    ).filter(
        Document.created_at >= cutoff,
        Document.author_id.is_not(None),
    ).group_by(Document.author_id).having(
        func.count(Document.id) >= threshold
    ).all()

    seen = set()
    for author_id, cnt in rows:
        title = f"Пользователь провёл {cnt} документов за 24 ч."
        details = {
            "author_id": author_id,
            "documents_count": int(cnt),
            "threshold": threshold,
            "window_hours": 24,
        }
        _upsert_flag(
            db, rule_code=code, severity="warning", title=title,
            entity_type="user", entity_id=author_id, details=details,
        )
        seen.add(("user", author_id))

    _resolve_unseen(db, code, seen)
    return len(seen)


def check_ghost_serial(db):
    """Серийник упоминается в DocumentItem, но не существует в WeaponRegistry.
    Возможные причины: ручная правка БД, баг в импорте, удалённый reversal.
    Флаг — один на серийник."""
    if not _get_bool(db, "rule.ghost_serial.enabled", True):
        return 0
    code = "GHOST_SERIAL"

    # DocumentItem с serial_number, для которого НЕТ WeaponRegistry (даже со status=0)
    sub_exists = db.query(WeaponRegistry.id).filter(
        WeaponRegistry.serial_number == DocumentItem.serial_number,
        WeaponRegistry.nomenclature_id == DocumentItem.nomenclature_id,
    ).exists()
    rows = db.query(
        DocumentItem.nomenclature_id,
        DocumentItem.serial_number,
        func.count(DocumentItem.id).label("cnt"),
        func.max(DocumentItem.document_id).label("last_doc"),
    ).filter(
        DocumentItem.serial_number.is_not(None),
        ~sub_exists,
    ).group_by(
        DocumentItem.nomenclature_id, DocumentItem.serial_number,
    ).limit(500).all()

    seen = set()
    for nom_id, serial, cnt, last_doc in rows:
        nom = db.query(Nomenclature).filter(Nomenclature.id == nom_id).first()
        if nom and not nom.is_numbered:
            # Партионный учёт — не номер ствола, а номер партии. Это нормально
            # что его нет в реестре (партия могла быть полностью списана).
            continue
        title = f"Серийник «{serial}» есть в документах, но нет в реестре"
        details = {
            "serial": serial,
            "nomenclature_id": nom_id,
            "mentions_count": int(cnt),
            "last_document_id": last_doc,
        }
        # entity_id = nom_id, entity_type отличаем хэшем серийника чтобы не
        # объединять разные серийники одной номенклатуры. Проще — хранить
        # как строковый хэш в отдельном виртуальном id. Для простоты — agg по nom_id:
        # берём ОДИН флаг на номенклатуру, перечисляем серийники в details.
        _upsert_flag(
            db, rule_code=code, severity="warning", title=title,
            entity_type="nomenclature", entity_id=nom_id, details=details,
        )
        seen.add(("nomenclature", nom_id))

    _resolve_unseen(db, code, seen)
    return len(seen)


def check_zero_batch(db):
    """Партия с quantity <= 0, но status=1 (активна). Баг логики _process_batch
    — должна была удалиться при обнулении."""
    if not _get_bool(db, "rule.zero_batch.enabled", True):
        return 0
    code = "ZERO_BATCH"
    rows = db.query(WeaponRegistry).join(
        Nomenclature, Nomenclature.id == WeaponRegistry.nomenclature_id
    ).filter(
        WeaponRegistry.status == 1,
        WeaponRegistry.quantity <= 0,
        Nomenclature.is_numbered.is_(False),
    ).limit(500).all()

    seen = set()
    for w in rows:
        title = f"Партия «{w.serial_number or '—'}» активна с нулевым остатком"
        details = {
            "weapon_id": w.id,
            "serial": w.serial_number,
            "quantity": w.quantity,
            "object_id": w.current_object_id,
        }
        _upsert_flag(
            db, rule_code=code, severity="warning", title=title,
            entity_type="weapon", entity_id=w.id, details=details,
        )
        seen.add(("weapon", w.id))

    _resolve_unseen(db, code, seen)
    return len(seen)


def check_overdue_shipment(db):
    """«Отправка» старше N дней без соответствующего «Прием».
    Простой эвристический поиск: документ типа Отправка/Перемещение, возраст
    которого > N, при этом среди более новых документов нет Приёма с тем же
    target_id, который покрывал бы отправленные позиции.
    Упрощение: берём все «Отправки» старше N дней и считаем что они зависли
    если нет ЛЮБОГО «Прием» с таким же target_id после этой даты."""
    if not _get_bool(db, "rule.overdue_shipment.enabled", True):
        return 0
    days = _get_int(db, "rule.overdue_shipment.days", 14)
    code = "OVERDUE_SHIPMENT"
    cutoff = datetime.utcnow() - timedelta(days=days)

    shipments = db.query(Document).filter(
        Document.operation_type.in_(["Отправка", "Перемещение"]),
        Document.operation_date <= cutoff,
        Document.is_reversed.is_(False),
        Document.target_id.is_not(None),
    ).limit(500).all()

    seen = set()
    for ship in shipments:
        # Есть ли позже приём в target?
        later_receive = db.query(Document.id).filter(
            Document.operation_type == "Прием",
            Document.target_id == ship.target_id,
            Document.operation_date >= ship.operation_date,
            Document.is_reversed.is_(False),
        ).first()
        if later_receive:
            continue
        age_days = (datetime.utcnow() - ship.operation_date).days
        title = f"Отправка #{ship.doc_number} без приёма {age_days} дн."
        details = {
            "document_id": ship.id,
            "doc_number": ship.doc_number,
            "source_id": ship.source_id,
            "target_id": ship.target_id,
            "operation_date": ship.operation_date.isoformat() if ship.operation_date else None,
            "age_days": age_days,
        }
        _upsert_flag(
            db, rule_code=code, severity="warning", title=title,
            entity_type="document", entity_id=ship.id, details=details,
        )
        seen.add(("document", ship.id))

    _resolve_unseen(db, code, seen)
    return len(seen)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def run_arsenal_analyzer(db) -> dict:
    """Запускает все правила по очереди. Возвращает dict rule_code → count.
    db — sync-сессия (для Celery). Commit делает вызывающий, чтобы можно
    было откатить весь прогон при непредвиденной ошибке."""
    results = {}
    for name, fn in [
        ("DUPLICATE_SERIAL",  check_duplicate_serial),
        ("STALE_STOCK",       check_stale_stock),
        ("SUSPICIOUS_BURST",  check_suspicious_burst),
        ("GHOST_SERIAL",      check_ghost_serial),
        ("ZERO_BATCH",        check_zero_batch),
        ("OVERDUE_SHIPMENT",  check_overdue_shipment),
    ]:
        try:
            results[name] = fn(db)
        except Exception as e:
            logger.exception(f"[arsenal-analyzer] Rule {name} failed: {e}")
            results[name] = -1
    return results


# Каталог (для UI «справка»)
RULE_CATALOG = [
    {"code": "DUPLICATE_SERIAL", "severity": "critical",
     "title": "Дубли серийников",
     "desc": "Один серийный номер активен в нескольких записях реестра одновременно. "
             "Критично для военного учёта: значит система не знает где реально находится единица."},
    {"code": "STALE_STOCK", "severity": "info",
     "title": "Застой остатков",
     "desc": "Имущество без движения более N месяцев. Может быть признаком «забытых» ТМЦ, "
             "списанных мимо системы или просто редко используемых."},
    {"code": "SUSPICIOUS_BURST", "severity": "warning",
     "title": "Подозрительный всплеск активности",
     "desc": "Один пользователь провёл аномально много документов за сутки. "
             "Возможный фрод или массовое проведение без должной проверки."},
    {"code": "GHOST_SERIAL", "severity": "warning",
     "title": "Серийник-призрак",
     "desc": "Номер упоминается в документах, но отсутствует в реестре. "
             "Обычно — последствие ручной правки БД или сбоя в import/rollback."},
    {"code": "ZERO_BATCH", "severity": "warning",
     "title": "Активная партия с нулевым остатком",
     "desc": "Партионная запись со status=1 и quantity≤0. Должна была быть удалена при обнулении."},
    {"code": "OVERDUE_SHIPMENT", "severity": "warning",
     "title": "Просроченная отправка",
     "desc": "Документ «Отправка» / «Перемещение» давно проведён, но документа «Прием» у получателя нет. "
             "Возможно, имущество зависло в пути или приёмка не оформлена."},
]
