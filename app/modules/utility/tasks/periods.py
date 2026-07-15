# Жизненный цикл расчётного периода: закрытие (с Redis-lock), авто-открытие/
# закрытие по окну подачи, активация тарифов по effective_from.
# Вербатим-перенос из tasks.py (строки 561-783), поведение 1:1.

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from redis import asyncio as aioredis
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from redis import Redis

from app.worker import celery
from app.core.config import settings
from app.modules.utility.models import BillingPeriod, SystemSetting, User
from app.modules.utility.services.billing import close_current_period

from ._shared import logger, sync_db_session


async def run_async_close_period(admin_user_id: int):
    """Асинхронная обертка для логики закрытия периода."""
    try:
        redis = aioredis.from_url(settings.REDIS_URL, encoding="utf8", decode_responses=True)
        FastAPICache.init(RedisBackend(redis), prefix="fastapi-cache")
    except Exception as e:
        logger.warning(f"Could not init cache in worker: {e}")

    async_engine = create_async_engine(
        settings.DATABASE_URL_ASYNC,
        echo=False,
        future=True,
        pool_pre_ping=True,
        connect_args={"prepared_statement_cache_size": 0, "statement_cache_size": 0, "command_timeout": 60}
    )

    local_session_maker = sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    try:
        async with local_session_maker() as db:
            try:
                result = await close_current_period(db=db, admin_user_id=admin_user_id)
                await db.commit()
                await FastAPICache.clear(namespace="periods")
                return result
            except Exception as e:
                await db.rollback()
                raise e
    finally:
        await async_engine.dispose()


@celery.task(
    name="close_period_task",
    queue="heavy",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 2, "countdown": 60},
)
def close_period_task(admin_user_id: int):
    """
    Celery-задача закрытия расчётного периода с защитой от двойного запуска.

    ИСПРАВЛЕНИЯ:
    1. Раньше Redis lock НЕ ставился — был только delete() в finally. Это значит
       что два админа одновременно могли запустить close → двойная авто-генерация
       показаний → двойные суммы. Теперь используем SET NX EX (атомарно).
    2. Раньше при ошибке возвращали {"status": "error"} — Celery считал задачу
       успешной, retry не работал. Теперь raise — autoretry_for сделает 2 попытки.
    3. Lock освобождаем по тому же значению (атомарно через Lua-скрипт), чтобы
       ретрай не убил чужой lock.

    Сама `close_current_period` уже атомарна: вся работа в одной транзакции,
    при exception — rollback. Период «полузакрытым» не останется.
    """
    logger.info(f"Starting close_period_task for admin {admin_user_id}")
    redis_client = Redis.from_url(settings.REDIS_URL)
    lock_key = "lock:close_period"
    lock_value = f"task-{admin_user_id}-{datetime.now(timezone.utc).timestamp()}"
    lock_ttl = 1800  # 30 минут — заведомо больше любого реального close_period

    # Атомарный SET NX EX — если ключа нет, ставим и возвращаем True.
    acquired = redis_client.set(lock_key, lock_value, nx=True, ex=lock_ttl)
    if not acquired:
        logger.warning("[LOCK] close_period уже выполняется другой задачей — пропускаем")
        return {"status": "skipped", "reason": "already_running"}

    try:
        result = asyncio.run(run_async_close_period(admin_user_id))
        logger.info(f"[CLOSE_PERIOD] Success: {result}")
        return result
    except Exception:
        logger.exception("[CLOSE_PERIOD] Failed — будет retry автоматически")
        raise  # пусть Celery увидит ошибку и сделает retry
    finally:
        # Освобождаем lock только если он наш (Lua-скрипт для атомарности).
        # Иначе после retry мы могли бы стереть чужой lock.
        try:
            release_script = """
            if redis.call('GET', KEYS[1]) == ARGV[1] then
                return redis.call('DEL', KEYS[1])
            else
                return 0
            end
            """
            redis_client.eval(release_script, 1, lock_key, lock_value)
            logger.info("[LOCK] Released close_period lock")
        except Exception as e:
            logger.warning(f"[LOCK] Failed to release: {e}")


@celery.task(name="check_auto_period_task")
def check_auto_period_task():
    """Ежедневная задача (Beat): автоматическое управление периодами."""
    logger.info("[AUTO] Checking period automation...")
    try:
        with sync_db_session() as db:
            start_setting = db.query(SystemSetting).filter_by(key="submission_start_day").first()
            end_setting = db.query(SystemSetting).filter_by(key="submission_end_day").first()
            # Дефолт — московский стандарт 15 → 3 следующего месяца.
            start_day = int(start_setting.value) if start_setting else 15
            end_day = int(end_setting.value) if end_setting else 3
            today = datetime.now()
            current_day = today.day
            active = db.query(BillingPeriod).filter_by(is_active=True).first()

            month_names = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                           "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

            # Окно подачи может ПЕРЕХОДИТЬ через границу месяца (start > end,
            # напр. 15 → 3 следующего). in_window — открыт ли приём сегодня.
            if start_day <= end_day:
                in_window = start_day <= current_day <= end_day
            else:
                in_window = current_day >= start_day or current_day <= end_day

            if in_window:
                if not active:
                    # В «хвосте» wrap-окна (1..end_day) приём идёт за ПРОШЛЫЙ
                    # календарный месяц → период именуем прошлым месяцем.
                    if start_day > end_day and current_day <= end_day:
                        pm = today.month - 1 if today.month > 1 else 12
                        py = today.year if today.month > 1 else today.year - 1
                    else:
                        pm, py = today.month, today.year
                    period_name = f"{month_names[pm]} {py}"
                    exists = db.query(BillingPeriod).filter_by(name=period_name).first()
                    if not exists:
                        new_period = BillingPeriod(name=period_name, is_active=True)
                        db.add(new_period)
                        db.commit()
                        logger.info(f"[AUTO] Opened new period: {period_name}")
                        # Сразу начисляем статичный наём (205) домам — не ждём
                        # закрытия. Отдельной Celery-задачей, чтобы ошибка не
                        # сломала beat-цикл (изолировано). Наём — не норматив.
                        try:
                            # charge_houses_rent_task живёт в maintenance.py
                            # (распил на пакет) — импорт локальный, чтобы не
                            # менять порядок импорта модулей в __init__.
                            from .maintenance import charge_houses_rent_task
                            charge_houses_rent_task.delay(new_period.id)
                        except Exception:
                            logger.exception("[AUTO] enqueue charge_houses_rent_task failed")
            elif active:
                # Сегодня ВНЕ окна подачи, но период активен.
                # КРИТИЧНО (фикс июнь 2026): закрываем ТОЛЬКО когда окно уже
                # ЗАКОНЧИЛОСЬ (мы ПОСЛЕ end_day), а НЕ до его открытия. Для
                # невраппинг-окна (start<=end, напр. 15→28) дни 1..start-1 — это
                # ещё ДО начала приёма; закрытие там выставляло бы ВСЕМ 0 (никто
                # не подавал). Для враппинг-окна (start>end, 15→3) любой день вне
                # окна уже после end — там закрывать корректно.
                window_has_ended = (start_day > end_day) or (current_day > end_day)
                if not window_has_ended:
                    logger.info(
                        "[AUTO] Вне окна, но ДО открытия приёма (день %d < start %d) — "
                        "период '%s' НЕ закрываем (иначе всем 0).",
                        current_day, start_day, active.name)
                else:
                    # Авто-закрытие периода по умолчанию ВЫКЛЮЧЕНО (двушаговая
                    # политика, июнь 2026): период закрывает админ вручную, чтобы
                    # норматив не начислялся раньше, чем жилец успел подать.
                    # Вернуть авто-закрытие: billing.auto_close_enabled=true.
                    from app.modules.utility.services.analyzer_config import config
                    if not config.get_bool("billing.auto_close_enabled", False):
                        logger.info(
                            "[AUTO] Окно закрылось, но авто-закрытие выключено "
                            "(billing.auto_close_enabled=false) — период '%s' "
                            "закроет админ вручную.", active.name)
                    elif Redis.from_url(settings.REDIS_URL).get("lock:close_period"):
                        # read-only get; реальный atomic lock делает close_period_task.
                        logger.info("[AUTO] Close already running, skip duplicate")
                    else:
                        admin = db.query(User).filter_by(username="admin").first()
                        if admin:
                            close_period_task.delay(admin.id)
                            logger.info(f"[AUTO] Triggered closing task for period '{active.name}'")
    except Exception:
        logger.exception("[AUTO] Automation failed")


@celery.task(name="activate_scheduled_tariffs_task", queue="default")
def activate_scheduled_tariffs_task():
    """
    Ежедневная задача (Beat): автоматическая активация тарифов по дате вступления в силу.
    Находит тарифы с effective_from <= сейчас и is_active=False → устанавливает is_active=True.
    """
    logger.info("[TARIFF] Checking scheduled tariffs for activation...")
    try:
        with sync_db_session() as db:
            from app.modules.utility.models import Tariff
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            tariffs_to_activate = db.query(Tariff).filter(
                Tariff.effective_from.is_not(None),
                Tariff.effective_from <= now,
                Tariff.is_active.is_(False)
            ).all()

            if not tariffs_to_activate:
                logger.info("[TARIFF] No scheduled tariffs to activate.")
                return {"activated": 0}

            activated_ids = []
            for t in tariffs_to_activate:
                t.is_active = True
                activated_ids.append(t.id)
                logger.info(f"[TARIFF] Activated tariff '{t.name}' (id={t.id}), effective_from={t.effective_from}")

            db.commit()

            # ВАЖНО: без invalidate кеш tariff_cache на 10 мин продолжит
            # отдавать старые (is_active=False) тарифы как «несуществующие»,
            # и приоритет «Room.tariff_id» провалится на default. После этого
            # коммита нужно явно сбросить кеш, чтобы новые тарифы сразу
            # применялись в расчётах.
            try:
                from app.modules.utility.services.tariff_cache import tariff_cache
                tariff_cache.invalidate()
            except Exception:
                logger.warning("[TARIFF] Could not invalidate tariff_cache after activation")

            logger.info(f"[TARIFF] Activated {len(activated_ids)} tariff(s).")
            return {"activated": len(activated_ids), "ids": activated_ids}

    except Exception:
        logger.exception("[TARIFF] activate_scheduled_tariffs_task failed")
        return {"activated": 0, "error": "task failed"}
