# app/modules/utility/routers/admin_readings.py

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.utility.models import User
from app.modules.utility.schemas import ApproveRequest, AdminManualReadingSchema
from app.core.dependencies import RoleChecker

from app.modules.utility.services import admin_readings_list
from app.modules.utility.services import admin_readings_approve
from app.modules.utility.services import admin_readings_manual

router = APIRouter(tags=["Admin Readings"])

allow_readings_view = RoleChecker(["accountant", "admin", "financier"])
allow_readings_manage = RoleChecker(["accountant", "admin"])


@router.get("/api/admin/readings")
async def get_admin_readings(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=1000),
        cursor_id: Optional[int] = Query(None, description="Keyset pagination cursor"),
        direction: str = Query("next", pattern="^(next|prev)$"),
        search: Optional[str] = Query(None),
        anomalies_only: bool = Query(False),
        sort_by: str = Query("created_at"),
        sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
        # Волна 1 — расширенные фильтры реестра
        period_id: Optional[int] = Query(None, description="ID периода (default — активный)"),
        risk_level: Optional[str] = Query(None, pattern="^(clean|suspicious|critical)$"),
        flag_code: Optional[str] = Query(None, description="SPIKE_HOT / ZERO_BILL / ..."),
        source: Optional[str] = Query(None, pattern="^(user|gsheets|auto|one_time|meter_replace)$"),
        current_user: User = Depends(allow_readings_view),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_list.get_paginated_readings(
        db, page, limit, cursor_id, direction, search, anomalies_only, sort_by, sort_dir,
        period_id=period_id, risk_level=risk_level, flag_code=flag_code, source=source,
    )


@router.get("/api/admin/readings/stats")
async def get_admin_readings_stats(
        period_id: Optional[int] = Query(None),
        current_user: User = Depends(allow_readings_view),
        db: AsyncSession = Depends(get_db)
):
    """KPI для шапки реестра показаний (волна 1)."""
    return await admin_readings_list.get_readings_stats(db, period_id)


@router.get("/api/admin/readings/{reading_id}/decision-context")
async def get_reading_decision_context(
        reading_id: int,
        current_user: User = Depends(allow_readings_view),
        db: AsyncSession = Depends(get_db)
):
    """Расширенный контекст для раскрывающейся панели реестра (волна 2):
    история 4-х предыдущих утверждений, соседи, флаги + рекомендация."""
    return await admin_readings_list.get_decision_context(db, reading_id)


@router.post("/api/admin/approve-bulk")
async def bulk_approve_readings(
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_approve.bulk_approve_drafts(db, current_user)


@router.post("/api/admin/approve/{reading_id}")
async def approve_reading(
        reading_id: int,
        correction_data: ApproveRequest,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_approve.approve_single(db, reading_id, correction_data, current_user)


@router.post("/api/admin/readings/{reading_id}/unapprove")
async def unapprove_reading(
        reading_id: int,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    """Отмена ошибочного утверждения — возврат показания в черновик с
    восстановлением room.last_* из предыдущего утверждённого. Используется
    кнопкой «Отменить» в мобильной сверке сразу после ошибочного approve."""
    return await admin_readings_approve.unapprove_single(db, reading_id, current_user)


@router.delete("/api/admin/readings/{reading_id}")
async def delete_reading_record(
        reading_id: int,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_manual.delete_reading(db, reading_id, actor=current_user)


@router.post("/api/admin/readings/{reading_id}/convert-to-baseline")
async def convert_reading_to_baseline_endpoint(
        reading_id: int,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    """Превратить аномальный reading в Начальный период (baseline) комнаты.

    Используется во вкладке «Аномальные дельты» Анализатора, когда жилец
    впервые подал реальные накопленные показания (например, ГВС=2186) поверх
    AUTO_GENERATED baseline 0/0/0. Endpoint:
      1) переносит значения reading'а в INITIAL_SETUP-запись комнаты
         (или создаёт её, если её не было);
      2) удаляет текущий аномальный reading;
      3) обновляет Room.last_* — следующая подача будет корректной.

    После операции рекомендуется запустить «Перерасчёт периода» — старые
    счета по этому жильцу пересчитаются с нуля.
    """
    return await admin_readings_manual.convert_reading_to_baseline(
        db, reading_id, actor=current_user
    )


@router.post("/api/admin/readings/convert-to-baseline-bulk")
async def bulk_convert_to_baseline_endpoint(
        reading_ids: list[int],
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    """Массовая операция convert-to-baseline для списка reading_ids.

    Используется во вкладке «Аномальные дельты» кнопкой «Превратить все
    с synth prev в baseline». Обрабатывает по очереди — если хоть одна
    падает, остальные продолжают обрабатываться (errors собираются в
    отдельный массив).
    """
    ok: list[dict] = []
    errors: list[dict] = []
    for rid in reading_ids:
        try:
            res = await admin_readings_manual.convert_reading_to_baseline(
                db, rid, actor=current_user
            )
            ok.append({"reading_id": rid, **res})
        except Exception as exc:
            errors.append({"reading_id": rid, "error": str(exc)})
    return {
        "processed": len(reading_ids),
        "ok_count": len(ok),
        "error_count": len(errors),
        "ok": ok,
        "errors": errors,
    }


@router.post("/api/admin/readings/manual-receipt/{user_id}")
async def create_manual_receipt_endpoint(
        user_id: int,
        period_id: int | None = None,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db),
):
    """Создать квитанцию для жильца без подачи показаний (только долги/
    переплаты + фикс-часть тарифа). Использовать в финансовой отчётности
    когда у жильца есть debt от импорта 1С, но показания ещё не подал.

    Если total_209+total_205 < 0 → у жильца переплата (вернуть деньги или
    зачесть в следующем периоде). UI должен показать это как «остаток».
    """
    return await admin_readings_manual.create_manual_receipt(db, user_id, period_id)


@router.post("/api/admin/readings/manual-receipt-bulk")
async def bulk_create_manual_receipts_endpoint(
        period_id: int | None = None,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db),
):
    """Массово создать квитанции для всех жильцов которые НЕ подали
    показания в целевом периоде. Использует ту же логику что
    /manual-receipt/{user_id} — только сальдо, без начислений.

    Use case: в конце периода многие жильцы не подают показания. Админ
    одной кнопкой формирует им квитанции с актуальным сальдо (долги/
    переплаты из импорта 1С), не трогая тех у кого квитанция уже есть.
    """
    return await admin_readings_manual.bulk_create_manual_receipts(db, period_id)


@router.post("/api/admin/billing/auto-fill-readings/{period_id}")
async def auto_fill_period_endpoint(
        period_id: int,
        dry_run: bool = False,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db),
):
    """Bug AN: добить пустые периоды auto-readings по правилам биллинга.

    Применяет ту же стратегию что и close_current_period, но к
    указанному period_id (не только активному):
      - AUTO_NORM_SANCTION × коэф — после 3 пропусков подряд (4-й = санкция)
      - AUTO_AVG — среднее по дельтам manual-подач
      - AUTO_AVG_FALLBACK — повтор последних показаний (расход 0)
      - AUTO_NO_HISTORY — только фикс-часть

    Создаёт reading только для жильцов БЕЗ существующего reading в этом
    периоде (любой статус). dry_run=true вернёт preview без записи в БД.
    """
    from app.modules.utility.services.billing import auto_fill_period_readings

    # dry_run — только preview, без записи → лок не нужен.
    if dry_run:
        try:
            return await auto_fill_period_readings(db, period_id, dry_run=True)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # Защита от ДВОЙНОГО начисления норматива. На readings нет UNIQUE(user_id,
    # period_id), а два ПАРАЛЛЕЛЬНЫХ вызова (двойной клик / кнопка vs beat-задача
    # vs второй бухгалтер) читают existing_user_ids до коммита друг друга и
    # вставляют по 2 AUTO_NORM каждому не сдавшему → счёт ×2. Сериализуем по
    # period_id атомарным SET NX EX (как close_period_task). Последовательный
    # повтор уже идемпотентен (existing_user_ids внутри auto_fill).
    import uuid
    from redis import asyncio as aioredis
    from app.core.config import settings
    redis_client = aioredis.from_url(settings.REDIS_URL)
    lock_key = f"lock:autofill:{period_id}"
    lock_value = f"u{getattr(current_user, 'id', 0)}-{uuid.uuid4().hex}"
    acquired = await redis_client.set(lock_key, lock_value, nx=True, ex=1800)
    if not acquired:
        try:
            await (getattr(redis_client, "aclose", None) or redis_client.close)()
        except Exception:
            pass
        raise HTTPException(
            409, "Начисление норматива по этому периоду уже выполняется — дождитесь завершения")
    try:
        return await auto_fill_period_readings(db, period_id, dry_run=False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        # Освобождаем лок только если он наш (Lua, атомарно).
        try:
            release_script = (
                "if redis.call('GET', KEYS[1]) == ARGV[1] "
                "then return redis.call('DEL', KEYS[1]) else return 0 end"
            )
            await redis_client.eval(release_script, 1, lock_key, lock_value)
        except Exception:
            pass
        try:
            await (getattr(redis_client, "aclose", None) or redis_client.close)()
        except Exception:
            pass


@router.post("/api/admin/billing/charge-rent-now/{period_id}")
async def charge_houses_rent_endpoint(
        period_id: int,
        dry_run: bool = False,
        recompute: bool = False,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db),
):
    """Начислить статичный наём (205) жильцам ДОМОВ (place_type='house') в
    периоде — ЧЕРНОВИКОМ, сразу, не дожидаясь закрытия (наём статичен:
    площадь × тариф). Идемпотентно (жильцы с reading в периоде пропускаются).
    dry_run=true → preview без записи. На закрытии черновики утвердятся штатно.
    """
    from app.modules.utility.services.billing import charge_static_rent_for_houses

    if dry_run:
        try:
            return await charge_static_rent_for_houses(db, period_id, dry_run=True, recompute=recompute)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # Тот же атомарный Redis-лок, что у auto-fill — защита от двойного начисления
    # (двойной клик / кнопка vs авто-хук при открытии периода).
    import uuid
    from redis import asyncio as aioredis
    from app.core.config import settings
    redis_client = aioredis.from_url(settings.REDIS_URL)
    lock_key = f"lock:charge-rent:{period_id}"
    lock_value = f"u{getattr(current_user, 'id', 0)}-{uuid.uuid4().hex}"
    acquired = await redis_client.set(lock_key, lock_value, nx=True, ex=1800)
    if not acquired:
        try:
            await (getattr(redis_client, "aclose", None) or redis_client.close)()
        except Exception:
            pass
        raise HTTPException(
            409, "Начисление наёма домам по этому периоду уже выполняется — дождитесь завершения")
    try:
        result = await charge_static_rent_for_houses(db, period_id, dry_run=False, recompute=recompute)
        try:
            from app.modules.utility.routers.admin_dashboard import write_audit_log
            await write_audit_log(
                db, current_user.id, current_user.username,
                action="charge_houses_rent", entity_type="period",
                entity_id=period_id, details={"created": result.get("created")},
            )
            await db.commit()
        except Exception:
            pass
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        try:
            release_script = (
                "if redis.call('GET', KEYS[1]) == ARGV[1] "
                "then return redis.call('DEL', KEYS[1]) else return 0 end"
            )
            await redis_client.eval(release_script, 1, lock_key, lock_value)
        except Exception:
            pass
        try:
            await (getattr(redis_client, "aclose", None) or redis_client.close)()
        except Exception:
            pass


@router.post("/api/admin/billing/charge-norm-now/{period_id}")
async def charge_unconditional_norm_endpoint(
        period_id: int,
        dry_run: bool = False,
        recompute: bool = False,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db),
):
    """Начислить по тарифу «БЕЗ УСЛОВИЙ» (норматив на квартиру) жильцам на таких
    тарифах в периоде — сразу, approved. Семья платит норму целиком, холостяки
    делят поровну. dry_run=true → preview. recompute=true → пересчёт существующих."""
    from app.modules.utility.services.billing import charge_unconditional_norm

    if dry_run:
        try:
            return await charge_unconditional_norm(db, period_id, dry_run=True, recompute=recompute)
        except ValueError as e:
            raise HTTPException(400, str(e))

    import uuid
    from redis import asyncio as aioredis
    from app.core.config import settings
    redis_client = aioredis.from_url(settings.REDIS_URL)
    lock_key = f"lock:charge-norm:{period_id}"
    lock_value = f"u{getattr(current_user, 'id', 0)}-{uuid.uuid4().hex}"
    acquired = await redis_client.set(lock_key, lock_value, nx=True, ex=1800)
    if not acquired:
        try:
            await (getattr(redis_client, "aclose", None) or redis_client.close)()
        except Exception:
            pass
        raise HTTPException(
            409, "Начисление по нормативу для этого периода уже выполняется — дождитесь завершения")
    try:
        result = await charge_unconditional_norm(db, period_id, dry_run=False, recompute=recompute)
        try:
            from app.modules.utility.routers.admin_dashboard import write_audit_log
            await write_audit_log(
                db, current_user.id, current_user.username,
                action="charge_unconditional_norm", entity_type="period",
                entity_id=period_id,
                details={"created": result.get("created"), "updated": result.get("updated")},
            )
            await db.commit()
        except Exception:
            pass
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        try:
            release_script = (
                "if redis.call('GET', KEYS[1]) == ARGV[1] "
                "then return redis.call('DEL', KEYS[1]) else return 0 end"
            )
            await redis_client.eval(release_script, 1, lock_key, lock_value)
        except Exception:
            pass
        try:
            await (getattr(redis_client, "aclose", None) or redis_client.close)()
        except Exception:
            pass


@router.get("/api/admin/readings/manual-state/{user_id}")
async def get_manual_reading_state(
        user_id: int,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_list.get_manual_state(db, user_id)


@router.get("/api/admin/readings/manual-grid-state/{user_id}")
async def get_manual_grid_state_route(
        user_id: int,
        period_ids: str = "",
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    """Состояние строчного мульти-месячного ввода. period_ids — список id
    периодов через запятую (выбранный + предыдущие)."""
    ids = [int(x) for x in period_ids.split(",") if x.strip().isdigit()]
    return await admin_readings_list.get_manual_grid_state(db, user_id, ids)


@router.post("/api/admin/readings/manual")
async def save_manual_reading(
        data: AdminManualReadingSchema,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_manual.save_manual_entry(db, data)

# Эндпоинт POST /api/admin/readings/one-time УДАЛЁН (аудит #21): схема
# OneTimeChargeSchema не совпадала с полями, которые читал сервис →
# гарантированный 500 с Initial commit; фронт его не вызывал, выселение/переезд
# идёт через POST /users/{id}/relocate → move_user_to_room. Мёртвый сервис-стаб
# create_one_time_charge оставлен недостижимым; при необходимости разовое
# пропорциональное начисление переписать заново корректно.
