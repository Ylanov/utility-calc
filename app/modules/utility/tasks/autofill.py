# Авто-добивка пропущенных показаний по нормативу (Bug AO, beat 03:45).
# Вербатим-перенос из tasks.py (строки 411-493), поведение 1:1.

from app.worker import celery
from app.core.config import settings

from ._shared import logger


@celery.task(name="auto_fill_missing_readings_task")
def auto_fill_missing_readings_task() -> dict:
    """Bug AO: дневная авто-добивка пропущенных reading'ов по нормативу.

    Каждый день в 03:45 (см. worker.py beat_schedule) проходит по
    периодам, для которых должна была быть подача (любой период старше
    чем `min_age_days`, кроме самого свежего активного — там жильцы
    ещё могут подать), и для каждого жильца БЕЗ reading'а в этом
    периоде создаёт reading через billing.auto_fill_period_readings:
      - AUTO_NORM_SANCTION × коэф — после 3 пропусков подряд
      - AUTO_AVG — среднее по дельтам manual-подач
      - AUTO_AVG_FALLBACK — повтор последних показаний
      - AUTO_NO_HISTORY — только фикс-часть тарифа

    Параметры через analyzer_settings:
      - billing.auto_fill_enabled (bool, default True) — глобальный switch
      - billing.auto_fill_min_age_days (int, default 0) — отсечка молодых
        периодов (0 = обрабатываем все, даже активные с прошедшим
        окном подачи; при 7 — только периоды старше недели)
      - billing.auto_fill_max_periods (int, default 12) — лимит на прогон

    Идемпотентна — повторный запуск не создаёт дубликатов
    (auto_fill_period_readings проверяет существующие reading'и).
    """
    import asyncio
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from app.modules.utility.models import BillingPeriod
    from app.modules.utility.services.analyzer_config import config
    from app.modules.utility.services.billing import auto_fill_period_readings

    # Двушаговая политика (июнь 2026): ночная авто-добивка нормативом по умолчанию
    # ВЫКЛЮЧЕНА — норматив начисляет админ вручную кнопкой «Начислить норматив»
    # после закрытия и проверки. Включить: billing.auto_fill_enabled=true.
    enabled = config.get_bool("billing.auto_fill_enabled", False)
    if not enabled:
        logger.info("[AUTO-FILL] disabled (billing.auto_fill_enabled=false), skipping")
        return {"status": "disabled"}

    min_age_days = config.get_int("billing.auto_fill_min_age_days", 0)
    max_periods = config.get_int("billing.auto_fill_max_periods", 12)

    async def _run():
        async_engine = create_async_engine(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0, "statement_cache_size": 0, "command_timeout": 60},
        )
        smaker = sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
        try:
            async with smaker() as db:
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=min_age_days)
                periods = (await db.execute(
                    select(BillingPeriod)
                    .where(BillingPeriod.created_at < cutoff)
                    .order_by(BillingPeriod.id.desc())
                    .limit(max_periods)
                )).scalars().all()
                results = []
                for p in periods:
                    try:
                        r = await auto_fill_period_readings(db, p.id, dry_run=False)
                        if r.get("created", 0) > 0:
                            logger.info(
                                "[AUTO-FILL] period=%s id=%s created=%d by=%s",
                                p.name, p.id, r["created"], r.get("by_strategy"),
                            )
                            results.append({"period_id": p.id, "name": p.name, "created": r["created"]})
                    except Exception as e:
                        logger.exception("[AUTO-FILL] period=%s failed: %s", p.name, e)
                return {
                    "status": "ok",
                    "periods_checked": len(periods),
                    "periods_filled": len(results),
                    "total_created": sum(r["created"] for r in results),
                    "details": results,
                }
        finally:
            await async_engine.dispose()

    return asyncio.run(_run())
