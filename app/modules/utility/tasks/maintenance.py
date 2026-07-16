# Чистки/ретеншны/сторожа (хвост монолита, beat-задачи): очистка старых
# gsheets-строк, автозачистка outlier-показаний, скан проблем жильцов,
# авто-перерасчёт drift, наём домам при открытии периода, QR-переписки,
# сторож здоровья системы. Вербатим-перенос из tasks.py (строки 1343-1783), 1:1.

import shutil
import asyncio
from datetime import datetime

from app.core.time_utils import utcnow
from app.worker import celery
from app.core.config import settings
from app.modules.utility.models import MeterReading

from ._shared import logger, sync_db_session


# ==========================================================================
# GSHEETS CLEANUP — автоочистка старых завершённых строк импорта
# ==========================================================================
# Gsheets-буфер за годы может набрать сотни тысяч строк (у нас 2000+ жильцов
# каждый месяц подаёт показание — это ~24k строк в год). Хранить всю историю
# бесконечно нет смысла: как только строка ушла в approved/auto_approved —
# у неё есть ссылка reading_id на MeterReading, где лежат реальные данные.
# rejected-строки нужны лишь для кратковременного разбора «почему отклонил»
# и через N месяцев их тоже можно удалять.
#
# pending / conflict / unmatched НИКОГДА не удаляем автоматически — это
# строки, которые ждут решения админа.
# ==========================================================================


def _cleanup_gsheets_rows(retention_days: int) -> dict:
    """Удаляет завершённые строки старше retention_days. Батчами по 1000.

    Возвращает {'deleted': N, 'cutoff': 'ISO datetime'}.
    """
    from datetime import timedelta as _td
    from app.modules.utility.models import GSheetsImportRow

    if retention_days <= 0:
        logger.info("[GSHEETS-CLEANUP] retention_days<=0, задача пропущена")
        return {"deleted": 0, "cutoff": None, "skipped": True}

    cutoff = utcnow() - _td(days=retention_days)
    # superseded — автопогашенные (месяц решён другим путём), терминальны как rejected.
    terminal_statuses = ("approved", "auto_approved", "rejected", "superseded")

    CHUNK = 1000
    total_deleted = 0

    with sync_db_session() as db:
        while True:
            # Выбираем id-батч в пределах CHUNK. Идём по (status, created_at)
            # — попадаем в индекс idx_gsheets_status_created.
            ids = [
                r[0] for r in db.query(GSheetsImportRow.id)
                .filter(
                    GSheetsImportRow.created_at < cutoff,
                    GSheetsImportRow.status.in_(terminal_statuses),
                )
                .order_by(GSheetsImportRow.id)
                .limit(CHUNK)
                .all()
            ]
            if not ids:
                break

            deleted = (
                db.query(GSheetsImportRow)
                .filter(GSheetsImportRow.id.in_(ids))
                .delete(synchronize_session=False)
            )
            db.commit()
            total_deleted += deleted

            # Если пришло меньше чем CHUNK — значит больше удалять нечего.
            if deleted < CHUNK:
                break

    logger.info(
        f"[GSHEETS-CLEANUP] удалено {total_deleted} строк "
        f"(старше {retention_days} дней, cutoff={cutoff.isoformat()})"
    )
    return {
        "deleted": total_deleted,
        "cutoff": cutoff.isoformat(),
        "retention_days": retention_days,
    }


@celery.task(name="cleanup_gsheets_old_rows_task")
def cleanup_gsheets_old_rows_task(retention_days: int | None = None):
    """Ежедневная автоочистка старых импортов из Google Sheets.

    Без параметра — берёт settings.GSHEETS_CLEANUP_DAYS (дефолт 365).
    Админ может вызвать вручную с параметром через /admin/analyzer/gsheets/cleanup-now.
    """
    days = retention_days if retention_days is not None else settings.GSHEETS_CLEANUP_DAYS
    return _cleanup_gsheets_rows(days)


# =====================================================================
# OUTLIER READINGS AUTO-CLEANUP
#
# Sync-эквивалент app/scripts/cleanup_anomaly_readings.py (которые админ
# запускал вручную) — теперь раз в сутки автоматически через celery beat.
# Помечает readings с нереалистичными значениями как DATA_OVERFLOW_RESET
# (is_approved=False, total=0, anomaly_score=100) — admin_notifications.py
# их подсчитывает в категорию data_overflow_resets, админ видит в bell.
# =====================================================================

def _cleanup_outlier_readings_run() -> dict:
    """Sync-версия для celery worker (без asyncio)."""
    from decimal import Decimal as _D
    from sqlalchemy import or_, update as _upd
    from app.modules.utility.services.reading_validators import (
        MAX_WATER_METER_VALUE,
        MAX_ELECTRICITY_METER_VALUE,
        MAX_TOTAL_COST_PER_READING,
    )
    from app.modules.utility.models import GSheetsImportRow as _GR

    with sync_db_session() as db:
        # Только approved (черновики и так требуют разбора админа).
        # Раньше отчищенные DATA_OVERFLOW_RESET — уже не approved → не попадут.
        outliers = db.query(MeterReading).filter(
            MeterReading.is_approved.is_(True),
            or_(
                MeterReading.hot_water > MAX_WATER_METER_VALUE,
                MeterReading.cold_water > MAX_WATER_METER_VALUE,
                MeterReading.electricity > MAX_ELECTRICITY_METER_VALUE,
                MeterReading.total_cost > MAX_TOTAL_COST_PER_READING,
            ),
        ).all()

        if not outliers:
            return {"status": "ok", "readings_reset": 0, "sheet_rows_reopened": 0}

        ids = [r.id for r in outliers]
        zero = _D("0.00")
        for r in outliers:
            r.total_cost = zero
            r.total_209 = zero
            r.total_205 = zero
            r.is_approved = False
            r.anomaly_flags = "DATA_OVERFLOW_RESET"
            r.anomaly_score = 100
            db.add(r)

        # GSheets-строки, привязанные к этим readings — возвращаем в conflict.
        sheet_res = db.execute(
            _upd(_GR)
            .where(_GR.reading_id.in_(ids))
            .values(
                reading_id=None,
                status="conflict",
                processed_at=None,
                conflict_reason=(
                    "auto_cleanup_data_overflow: показания или итог превысили "
                    "санитарные пороги. Проверьте формат — возможно пропущена "
                    "десятичная точка в показании счётчика."
                ),
            )
        )
        sheet_count = sheet_res.rowcount or 0
        db.commit()

        logger.warning(
            "[CLEANUP-OUTLIER] reset=%d sheet_rows_reopened=%d readings_ids=%s",
            len(outliers), sheet_count, ids[:20],
        )
        return {
            "status": "ok",
            "readings_reset": len(outliers),
            "sheet_rows_reopened": sheet_count,
            "reading_ids": ids[:50],
        }


@celery.task(name="cleanup_outlier_readings_task")
def cleanup_outlier_readings_task():
    """Ежедневная автозачистка outlier readings.

    Запускается раз в сутки (см. worker.beat_schedule). Помечает readings с
    нереалистичными значениями (вода >MAX_WATER_METER_VALUE, электричество >
    MAX_ELECTRICITY_METER_VALUE, итог > MAX_TOTAL_COST_PER_READING) как
    DATA_OVERFLOW_RESET — админ потом разберёт через bell-уведомления.

    Связано с фиксом sanity-check в save-точках (admin_readings_*, gsheets_sync,
    tasks._recalc_compute_one) — там новые подачи блокируются сразу, а эта
    задача чистит уже сохранённые «исторические» outliers.
    """
    return _cleanup_outlier_readings_run()


@celery.task(name="scan_resident_problems_task")
def scan_resident_problems_task():
    """Фоновый скан реальных проблем жильцов → таблица resident_problems.

    Запускается по расписанию (см. worker.beat_schedule). Прогоняет детекторы
    (не подаёт / долг растёт / битый формат / замер счётчика и т.д.), upsert'ит
    персистентные сигналы, авто-закрывает исчезнувшие. Результат видят админы
    в колокольчике / Inbox / дневном брифинге.
    """
    async def _run():
        # Свой engine на вызов (как auto_fill_missing_readings_task): asyncio.run
        # создаёт новый event loop каждый запуск, а asyncpg-коннекты привязаны
        # к loop'у создания. Модульный AsyncSessionLocal на QueuePool дал бы
        # «Future attached to a different loop» при USE_PGBOUNCER=False.
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession as _AS
        from sqlalchemy.orm import sessionmaker as _smaker
        from app.modules.utility.services.resident_problem_scanner import (
            scan_resident_problems,
        )
        _engine = create_async_engine(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0,
                          "statement_cache_size": 0, "command_timeout": 60},
        )
        _mk = _smaker(bind=_engine, class_=_AS, expire_on_commit=False, autoflush=False)
        try:
            async with _mk() as db:
                return await scan_resident_problems(db)
        finally:
            await _engine.dispose()

    try:
        result = asyncio.run(_run())
        logger.info("[scan_resident_problems_task] %s", result)
        return result
    except Exception as e:
        logger.exception("[scan_resident_problems_task] crashed")
        return {"crashed": True, "error": str(e)}


@celery.task(name="auto_recalc_drift_task")
def auto_recalc_drift_task():
    """Авто-перерасчёт расхождений активного периода: безопасные drift фиксит,
    опасные/повторные — сигналит в Монитор проблем (RECALC_DRIFT)."""
    async def _run():
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession as _AS
        from sqlalchemy.orm import sessionmaker as _smaker
        from sqlalchemy import select as _select
        from app.modules.utility.models import BillingPeriod as _BP
        from app.modules.utility.services.auto_recalc_drift import auto_recalc_drift
        _engine = create_async_engine(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0,
                          "statement_cache_size": 0, "command_timeout": 120},
        )
        _mk = _smaker(bind=_engine, class_=_AS, expire_on_commit=False, autoflush=False)
        try:
            async with _mk() as db:
                period = (await db.execute(
                    _select(_BP).where(_BP.is_active.is_(True))
                )).scalars().first()
                if not period:
                    return {"skipped": "no_active_period"}
                return await auto_recalc_drift(db, period.id)
        finally:
            await _engine.dispose()

    try:
        result = asyncio.run(_run())
        logger.info("[auto_recalc_drift_task] %s", result)
        return result
    except Exception as e:
        logger.exception("[auto_recalc_drift_task] crashed")
        return {"crashed": True, "error": str(e)}


@celery.task(name="charge_houses_rent_task")
def charge_houses_rent_task(period_id: int | None = None):
    """Авто-начисление статичного наёма (205) жильцам ДОМОВ (place_type=house)
    при открытии периода. Вызывается из check_auto_period_task после
    авто-открытия. Изолирован: ошибка НЕ ломает beat-задачу. Идемпотентно
    (жильцы с reading в периоде пропускаются)."""
    async def _run():
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession as _AS
        from sqlalchemy.orm import sessionmaker as _smaker
        from sqlalchemy import select as _select
        from app.modules.utility.models import BillingPeriod as _BP
        from app.modules.utility.services.billing import charge_static_rent_for_houses
        _engine = create_async_engine(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0,
                          "statement_cache_size": 0, "command_timeout": 120},
        )
        _mk = _smaker(bind=_engine, class_=_AS, expire_on_commit=False, autoflush=False)
        try:
            async with _mk() as db:
                pid = period_id
                if pid is None:
                    period = (await db.execute(
                        _select(_BP).where(_BP.is_active.is_(True))
                    )).scalars().first()
                    if not period:
                        return {"skipped": "no_active_period"}
                    pid = period.id
                return await charge_static_rent_for_houses(db, pid)
        finally:
            await _engine.dispose()

    try:
        result = asyncio.run(_run())
        logger.info("[charge_houses_rent_task] %s", result)
        return result
    except Exception as e:
        logger.exception("[charge_houses_rent_task] crashed")
        return {"crashed": True, "error": str(e)}


@celery.task(name="cleanup_qr_tickets_task")
def cleanup_qr_tickets_task(retention_days: int = 5):
    """Авто-удаление переписок с админом, начатых с QR-портала, старше N дней
    (по умолчанию 5). Privacy: эти данные долго не храним. Раз в сутки (beat).
    Маркер — subject «Обращение с QR-портала» (public_portal.QR_TICKET_SUBJECT)."""
    from datetime import datetime, timezone, timedelta
    from app.modules.utility.models import SupportTicket
    QR_SUBJECT = "Обращение с QR-портала"
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)
    with sync_db_session() as db:
        deleted = db.query(SupportTicket).filter(
            SupportTicket.subject == QR_SUBJECT,
            SupportTicket.created_at < cutoff,
        ).delete(synchronize_session=False)
        db.commit()
    logger.info("[cleanup_qr_tickets] удалено %d QR-переписок старше %d дн.", deleted, retention_days)
    return {"deleted": deleted, "retention_days": retention_days}


@celery.task(name="system_health_task")
def system_health_task() -> dict:
    """Сторож здоровья системы (аудит 2026-07-14): диск / релей ГИС /
    авто-цикл 1С / зависшие очереди. Пишет сводку в SystemSetting
    'system_health'; дашборд показывает красный/жёлтый баннер.

    Сам факт свежей записи = «beat жив»: эндпоинт /api/admin/system-health
    считает запись старше 30 мин признаком мёртвых фоновых задач (классика
    watchdog: задача пишет отметку времени, веб судит о свежести).
    """
    async def _run():
        import json as _json
        from sqlalchemy import select as _select, text as _text
        from sqlalchemy.ext.asyncio import create_async_engine as _cae, AsyncSession as _AS
        from sqlalchemy.orm import sessionmaker as _smaker
        from app.modules.utility.models import SystemSetting as _SS

        alerts: list[dict] = []

        def _alert(level: str, code: str, message: str):
            alerts.append({"level": level, "code": code, "message": message})

        # 1. Диск (внутри контейнера overlay отражает корневой диск ВМ —
        #    инциденты 2026-06-27/07-14: полный диск = краш воркеров и
        #    «пустой Chromium» у релея).
        try:
            du = shutil.disk_usage("/")
            pct = du.used / du.total * 100
            if pct >= 93:
                _alert("crit", "disk", f"Диск заполнен на {pct:.0f}% — воркеры и релей скоро встанут. Чистить немедленно.")
            elif pct >= 85:
                _alert("warn", "disk", f"Диск заполнен на {pct:.0f}% — запланируйте чистку (journald/docker-логи).")
        except Exception:
            logger.exception("[health] disk check failed")

        _engine = _cae(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0,
                          "statement_cache_size": 0, "command_timeout": 60},
        )
        _mk = _smaker(bind=_engine, class_=_AS, expire_on_commit=False, autoflush=False)
        try:
            async with _mk() as db:
                now = utcnow()

                def _age_min(ts):
                    try:
                        return (now - datetime.fromisoformat(ts)).total_seconds() / 60
                    except Exception:
                        return None

                async def _cfg(key):
                    row = (await db.execute(_select(_SS).where(_SS.key == key))).scalars().first()
                    try:
                        return _json.loads(row.value) if row and row.value else {}
                    except Exception:
                        return {}

                # 2. Релей ГИС ГМП: офлайн / сбор падает.
                gis = await _cfg("gisgmp_relay")
                if gis.get("enabled"):
                    a = _age_min(gis.get("last_poll_at"))
                    if a is None or a > 10:
                        _alert("crit", "gis_relay_offline",
                               "Релей ГИС ГМП не опрашивает сервер (>10 мин) — демон на ВМ упал или сеть.")
                    if gis.get("last_status") == "error":
                        _alert("warn", "gis_run_error",
                               f"Последний сбор ГИС упал: {(gis.get('last_message') or '')[:120]}")

                # 3. Очередь актуализации: повторные перевыдачи/застревание.
                act = await _cfg("gisgmp_actualize")
                if act.get("uuids"):
                    a = _age_min(act.get("last_at") or act.get("started_at"))
                    if int(act.get("restarted") or 0) >= 2:
                        _alert("warn", "gis_actualize_restarts",
                               f"Актуализация ГИС перезапускалась {act.get('restarted')} раз — проверьте релей/учётку.")
                    elif act.get("running") and a is not None and a > 45:
                        _alert("warn", "gis_actualize_stale",
                               "Актуализация ГИС молчит >45 мин — прогон перевыдастся автоматически.")

                # 4. 1С: сбор падает / предохранитель / залежавшиеся черновики.
                onec = await _cfg("onec_relay")
                if onec.get("enabled"):
                    if onec.get("last_status") == "error":
                        _alert("warn", "onec_run_error",
                               f"Последний сбор 1С упал: {(onec.get('last_message') or '')[:120]}")
                    ap = onec.get("last_autopublish") or {}
                    if ap.get("status") == "guard_tripped":
                        _alert("crit", "onec_guard",
                               "ПРЕДОХРАНИТЕЛЬ остановил авто-выгрузку долгов 1С — сбор выглядит битым. "
                               "Черновик ждёт ручной проверки в «Долги 1С».")

                stale = (await db.execute(_text(
                    "SELECT count(*) FROM debt_import_logs "
                    "WHERE status = 'staged' AND started_at < now() - interval '40 hours'"
                ))).scalar() or 0
                if stale:
                    _alert("warn", "onec_stale_drafts",
                           f"Черновики долгов 1С не выгружены >40ч ({stale} шт.) — авто-выгрузка не доехала.")

                # 5. Контроль 1С↔ГИС (снапшот пишут сбор ГИС и выгрузка 1С).
                ctl = await _cfg("gis1c_control")
                if gis.get("enabled") and ctl:
                    a = _age_min(ctl.get("ts"))
                    if a is not None and a > 48 * 60:
                        _alert("warn", "gis1c_control_stale",
                               "Сверка 1С↔ГИС не обновлялась >48ч — сборы/выгрузки не пересчитывают контроль.")
                    flags = ctl.get("flags") or {}
                    gis_over = int(flags.get("gis_more") or 0) + int(flags.get("only_gis") or 0)
                    if gis_over >= 30:
                        _alert("warn", "gis1c_gis_overstated",
                               f"ГИС завышает долг у {gis_over} жильцов (реестр показывает больше, чем 1С) — "
                               "запустите «Актуализацию расхождений» в «Долги 1С».")

                # 6. Осиротевшие показания (инцидент Безродний 2026-07-16):
                # активный месяц в чужой комнате → пустые дельты + baseline
                # с нулевым расходом. Страховка на случаи любого происхождения.
                try:
                    from app.modules.utility.services.stranded_readings import (
                        count_stranded_global,
                    )
                    _stranded = await count_stranded_global(db)
                    if _stranded:
                        _alert("warn", "stranded_readings",
                               f"У {_stranded} жильцов подача текущего месяца — в ПРЕЖНЕЙ "
                               "комнате, а в текущей показаний нет (сломанные дельты, риск "
                               "нулевого расхода). Исправляли привязку → откройте карточку "
                               "жильца (пере-сохраните комнату) и подтвердите перенос; "
                               "реальный переезд → показание должно остаться, скройте "
                               "уведомление (✕).")
                except Exception:
                    logger.exception("[health] stranded_readings check failed")

                # Запись сводки.
                row = (await db.execute(_select(_SS).where(_SS.key == "system_health"))).scalars().first()
                if row is None:
                    row = _SS(key="system_health", value="{}",
                              description="Сводка здоровья системы (system_health_task)")
                    db.add(row)
                row.value = _json.dumps(
                    {"checked_at": now.isoformat(), "alerts": alerts},
                    ensure_ascii=False,
                )
                await db.commit()
        finally:
            await _engine.dispose()
        return {"alerts": len(alerts)}

    try:
        result = asyncio.run(_run())
        if result.get("alerts"):
            logger.warning("[system_health_task] alerts=%s", result["alerts"])
        return result
    except Exception as e:
        logger.exception("[system_health_task] crashed")
        return {"crashed": True, "error": str(e)}
