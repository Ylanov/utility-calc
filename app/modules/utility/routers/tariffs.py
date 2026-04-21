# app/modules/utility/routers/tariffs.py

import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, update, text
from sqlalchemy.orm import selectinload
from fastapi_cache.decorator import cache
from fastapi_cache import FastAPICache

from app.core.database import get_db
from app.modules.utility.models import User, Tariff, Room
from app.modules.utility.schemas import TariffSchema
from app.core.dependencies import get_current_user, RoleChecker

# ИМПОРТ ДЛЯ ЖУРНАЛА ДЕЙСТВИЙ
from app.modules.utility.routers.admin_dashboard import write_audit_log
from app.modules.utility.services.tariff_cache import tariff_cache
from app.modules.utility.services.calculations import calculate_utilities

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tariffs", tags=["Tariffs"])
allow_management_roles = RoleChecker(["accountant", "admin", "financier"])


# =====================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: Безопасная очистка кэша
# =====================================================
async def _safe_clear_cache(namespace: str = "tariffs"):
    """
    Очищает оба кэша без выброса исключения:
      * FastAPICache (Redis) для HTTP-ответов /api/tariffs;
      * tariff_cache (in-memory) для расчётов approve/billing.
    Если Redis недоступен — логируем и продолжаем (in-memory всё равно сбросится).
    """
    try:
        await FastAPICache.clear(namespace=namespace)
    except Exception as e:
        logger.warning(f"Не удалось очистить FastAPICache '{namespace}': {e}")
    # Локальный in-memory сбросится в той же fastapi-worker-процессе.
    # Другие worker'ы дождутся истечения TTL (10 мин) — это допустимо для тарифов.
    tariff_cache.invalidate()


# =====================================================
# GET /api/tariffs — Список активных тарифов (кэшированный)
# =====================================================
@router.get("", response_model=List[TariffSchema])
@cache(expire=3600, namespace="tariffs")
async def get_tariffs(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """Получить список всех активных тарифов, отсортированных по ID."""
    result = await db.execute(
        select(Tariff).where(Tariff.is_active).order_by(Tariff.id)
    )
    return result.scalars().all()


# =====================================================
# GET /api/tariffs/with-stats — Тарифы + кол-во жильцов
# =====================================================
@router.get("/with-stats", summary="Тарифы с количеством привязанных жильцов")
async def get_tariffs_with_stats(
        current_user: User = Depends(allow_management_roles),
        db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список активных тарифов с количеством привязанных пользователей.
    Позволяет администратору видеть сколько жильцов используют каждый тариф,
    что критично перед удалением или изменением.
    """
    tariffs_result = await db.execute(
        select(Tariff).where(Tariff.is_active).order_by(Tariff.id)
    )
    tariffs = tariffs_result.scalars().all()

    # Считаем пользователей по каждому тарифу одним запросом
    user_counts_result = await db.execute(
        select(
            User.tariff_id,
            func.count(User.id).label("user_count")
        )
        .where(User.is_deleted.is_(False))
        .group_by(User.tariff_id)
    )
    user_counts_map = {row[0]: row[1] for row in user_counts_result.all()}

    # Считаем пользователей без тарифа (tariff_id IS NULL) — они на дефолтном
    null_count = user_counts_map.get(None, 0)

    result = []
    for t in tariffs:
        direct_count = user_counts_map.get(t.id, 0)
        # Если это базовый тариф (id=1), добавляем пользователей без привязки
        effective_count = direct_count + (null_count if t.id == 1 else 0)

        result.append({
            "id": t.id,
            "name": t.name,
            "is_active": t.is_active,
            "user_count": effective_count,
            "effective_from": t.effective_from.strftime("%Y-%m-%dT%H:%M") if t.effective_from else None,
            "maintenance_repair": t.maintenance_repair,
            "social_rent": t.social_rent,
            "heating": t.heating,
            "water_heating": t.water_heating,
            "water_supply": t.water_supply,
            "sewage": t.sewage,
            "waste_disposal": t.waste_disposal,
            "electricity_per_sqm": t.electricity_per_sqm,
            "electricity_rate": t.electricity_rate,
            "per_capita_amount": t.per_capita_amount,
        })

    return result


# =====================================================
# GET /api/tariffs/scheduled — Запланированные тарифы
# =====================================================
@router.get("/scheduled", summary="Запланированные (ещё не активные) тарифы")
async def get_scheduled_tariffs(
        current_user: User = Depends(allow_management_roles),
        db: AsyncSession = Depends(get_db)
):
    """Тарифы с будущей датой вступления в силу (is_active=False, effective_from задан)."""
    result = await db.execute(
        select(Tariff).where(
            Tariff.is_active.is_(False),
            Tariff.effective_from.is_not(None)
        ).order_by(Tariff.effective_from)
    )
    tariffs = result.scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "effective_from": t.effective_from.strftime("%Y-%m-%dT%H:%M") if t.effective_from else None,
            "maintenance_repair": t.maintenance_repair,
            "social_rent": t.social_rent,
            "heating": t.heating,
            "water_heating": t.water_heating,
            "water_supply": t.water_supply,
            "sewage": t.sewage,
            "waste_disposal": t.waste_disposal,
            "electricity_per_sqm": t.electricity_per_sqm,
            "electricity_rate": t.electricity_rate,
            "per_capita_amount": t.per_capita_amount,
        }
        for t in tariffs
    ]


# =====================================================
# POST /api/tariffs — Создание / обновление тарифа
# =====================================================
@router.post("", response_model=TariffSchema)
async def create_or_update_tariff(
        data: TariffSchema,
        current_user: User = Depends(allow_management_roles),
        db: AsyncSession = Depends(get_db)
):
    """
    Создать новый тарифный профиль или обновить существующий.
    Если в `data` передан `id`, происходит обновление.
    Если `id` отсутствует, создается новая запись.

    ИСПРАВЛЕНИЕ: Добавлен try/except + rollback. И синхронизация Sequence.
    """
    try:
        if data.id:
            # Режим обновления
            tariff = await db.get(Tariff, data.id)
            if not tariff:
                raise HTTPException(status_code=404, detail="Тарифный профиль не найден")

            if not tariff.is_active:
                raise HTTPException(
                    status_code=400,
                    detail="Нельзя редактировать деактивированный тариф. Создайте новый."
                )
        else:
            # Синхронизируем счетчик БД перед созданием,
            # чтобы избежать UniqueViolationError при первой записи через API.
            try:
                await db.execute(text("SELECT setval('tariffs_id_seq', COALESCE((SELECT MAX(id) FROM tariffs), 1))"))
            except Exception as seq_err:
                logger.warning(f"Не удалось обновить секвенцию тарифов: {seq_err}")

            # Режим создания
            tariff = Tariff()
            db.add(tariff)

        # Обновляем все поля из пришедшей схемы, кроме ID и системных
        # Поддержка разных версий Pydantic
        if hasattr(data, "model_dump"):
            update_data = data.model_dump(exclude={"id", "is_active"})
        else:
            update_data = data.dict(exclude={"id", "is_active"})

        for key, value in update_data.items():
            if hasattr(tariff, key) and key != "effective_from":
                setattr(tariff, key, value)

        # Обрабатываем effective_from отдельно: если задана будущая дата → тариф "запланирован"
        if data.effective_from:
            now = datetime.utcnow()
            tariff.effective_from = data.effective_from.replace(tzinfo=None) if data.effective_from.tzinfo else data.effective_from
            if tariff.effective_from > now:
                tariff.is_active = False  # Запланирован, ещё не активен
            else:
                tariff.is_active = True   # Дата уже прошла — активируем сразу
        else:
            tariff.effective_from = None  # Очищаем, если не указана

        # Для новых тарифов без effective_from — активны сразу
        if not data.id and not data.effective_from:
            tariff.is_active = True

        # ЗАПИСЬ В ЖУРНАЛ: Создание/Обновление тарифа
        action_type = "update" if data.id else "create"
        status_detail = "scheduled" if (tariff.effective_from and not tariff.is_active) else "active"
        await write_audit_log(
            db, current_user.id, current_user.username,
            action=action_type, entity_type="tariff", entity_id=tariff.id,
            details={"tariff_name": tariff.name, "status": status_detail,
                     "effective_from": str(tariff.effective_from) if tariff.effective_from else None}
        )

        await db.commit()
        await db.refresh(tariff)

    except HTTPException:
        # Штатные HTTP-ошибки (404, 400) пробрасываем как есть
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Ошибка при сохранении тарифа: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при сохранении тарифа. Обратитесь к администратору."
        )

    # Сбрасываем кэш ПОСЛЕ успешного коммита, безопасно.
    await _safe_clear_cache("tariffs")

    return tariff


# =====================================================
# DELETE /api/tariffs/{tariff_id} — Мягкое удаление
# =====================================================
@router.delete("/{tariff_id}", status_code=204)
async def delete_tariff(
        tariff_id: int,
        current_user: User = Depends(allow_management_roles),
        db: AsyncSession = Depends(get_db)
):
    """
    Мягкое удаление тарифа (пометка неактивным) с пересадкой жильцов на базовый.

    При удалении тарифа все привязанные к нему жильцы автоматически переводятся
    на базовый тариф (tariff_id = NULL → fallback id=1).
    """
    if tariff_id == 1:
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить базовый тарифный профиль (ID=1)"
        )

    tariff = await db.get(Tariff, tariff_id)

    if not tariff:
        raise HTTPException(status_code=404, detail="Тарифный профиль не найден")

    if not tariff.is_active:
        # Уже удалён — идемпотентность
        return None

    try:
        # Считаем сколько жильцов на этом тарифе
        affected_count_result = await db.execute(
            select(func.count(User.id)).where(
                User.tariff_id == tariff_id,
                User.is_deleted.is_(False)
            )
        )
        affected_count = affected_count_result.scalar_one()

        # Пересаживаем жильцов на базовый тариф (NULL = fallback на id=1)
        if affected_count > 0:
            await db.execute(
                update(User)
                .where(User.tariff_id == tariff_id, User.is_deleted.is_(False))
                .values(tariff_id=None)
            )

        # Мягкое удаление
        tariff.is_active = False

        # ЗАПИСЬ В ЖУРНАЛ: Удаление тарифа
        await write_audit_log(
            db, current_user.id, current_user.username,
            action="delete", entity_type="tariff", entity_id=tariff_id,
            details={"tariff_name": tariff.name, "users_reassigned": affected_count}
        )

        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Ошибка при удалении тарифа {tariff_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при удалении тарифа. Обратитесь к администратору."
        )

    # Безопасная очистка кэша после удаления
    await _safe_clear_cache("tariffs")

    return None


# =====================================================
# PREVIEW — калькулятор «сколько получится при таких объёмах»
# =====================================================
class PreviewRequest(BaseModel):
    """Тариф можно прислать целиком (live-превью при редактировании формы)
    либо по id (превью существующего тарифа — например для сравнения)."""
    tariff_id: Optional[int] = None
    tariff_data: Optional[dict] = None  # маппинг полей TariffSchema, без id

    # Параметры жилья
    apartment_area: Decimal = Field(default=Decimal("18.0"), gt=0)
    residents_count: int = Field(default=1, ge=1, le=20)
    total_room_residents: int = Field(default=1, ge=1, le=20)

    # Объёмы потребления за месяц
    volume_hot: Decimal = Field(default=Decimal("3.0"), ge=0)
    volume_cold: Decimal = Field(default=Decimal("5.0"), ge=0)
    volume_electricity: Decimal = Field(default=Decimal("100.0"), ge=0)


@router.post("/preview", summary="Калькулятор начисления по тарифу")
async def preview_calculation(
    data: PreviewRequest,
    current_user: User = Depends(allow_management_roles),
    db: AsyncSession = Depends(get_db),
):
    """
    Не пишет в БД ничего. Считает, сколько вышло бы при заданных объёмах
    и тарифе. Используется в UI редактирования тарифов: бухгалтер вводит
    цены — снизу сразу видит, сколько это будет «в живых деньгах» для
    типового жильца. Поможет ловить опечатку в копейках.
    """
    # Получаем тариф: либо из формы, либо из БД
    if data.tariff_data:
        # Пробуем сконвертить в pydantic schema, чтобы не пропустить мусор
        # Все Decimal-поля защищены: пустые строки / None заменяем на 0.
        cleaned = {}
        for k, v in data.tariff_data.items():
            if v is None or v == "":
                cleaned[k] = Decimal("0")
            else:
                try:
                    cleaned[k] = Decimal(str(v).replace(",", "."))
                except Exception:
                    cleaned[k] = Decimal("0")
        # Создаём временный объект Tariff для расчёта (без сохранения)
        tariff = Tariff(
            name="__preview__",
            maintenance_repair=cleaned.get("maintenance_repair", Decimal("0")),
            social_rent=cleaned.get("social_rent", Decimal("0")),
            heating=cleaned.get("heating", Decimal("0")),
            water_heating=cleaned.get("water_heating", Decimal("0")),
            water_supply=cleaned.get("water_supply", Decimal("0")),
            sewage=cleaned.get("sewage", Decimal("0")),
            waste_disposal=cleaned.get("waste_disposal", Decimal("0")),
            electricity_per_sqm=cleaned.get("electricity_per_sqm", Decimal("0")),
            electricity_rate=cleaned.get("electricity_rate", Decimal("0")),
            per_capita_amount=cleaned.get("per_capita_amount", Decimal("0")),
        )
    elif data.tariff_id:
        tariff = await db.get(Tariff, data.tariff_id)
        if not tariff:
            raise HTTPException(404, "Тариф не найден")
    else:
        raise HTTPException(400, "Передайте либо tariff_id, либо tariff_data")

    # Лёгкие fake-объекты под сигнатуру calculate_utilities()
    fake_user = type("U", (), {"residents_count": data.residents_count})()
    fake_room = type("R", (), {
        "apartment_area": data.apartment_area,
        "total_room_residents": data.total_room_residents,
    })()
    # Доля электричества — как в реальном расчёте
    elect_share = (
        Decimal(str(data.residents_count)) / Decimal(str(data.total_room_residents))
        * data.volume_electricity
    )
    costs = calculate_utilities(
        user=fake_user, room=fake_room, tariff=tariff,
        volume_hot=data.volume_hot,
        volume_cold=data.volume_cold,
        volume_sewage=data.volume_hot + data.volume_cold,
        volume_electricity_share=elect_share,
    )
    # Раскладываем по 209/205 как в реальном approve
    cost_205 = costs.get("cost_social_rent", Decimal("0"))
    cost_209 = costs["total_cost"] - cost_205
    return {
        "input": {
            "apartment_area": float(data.apartment_area),
            "residents_count": data.residents_count,
            "volume_hot": float(data.volume_hot),
            "volume_cold": float(data.volume_cold),
            "volume_electricity_share": float(elect_share),
        },
        "breakdown": {k: float(v) for k, v in costs.items()},
        "total_209": float(cost_209),
        "total_205": float(cost_205),
        "total_cost": float(costs["total_cost"]),
    }


# =====================================================
# USAGE — где и сколько применяется тариф
# =====================================================
@router.get("/{tariff_id}/usage", summary="Кто использует этот тариф")
async def tariff_usage(
    tariff_id: int,
    current_user: User = Depends(allow_management_roles),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает разложение «где применяется» для тарифа:
      * комнаты с прямой привязкой Room.tariff_id;
      * жильцы с прямой привязкой User.tariff_id;
      * (для базового id=1) сколько жильцов на нём по умолчанию (без привязки).
    Это нужно для понимания «что будет, если я поменяю/удалю».
    """
    tariff = await db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(404, "Тариф не найден")

    # Комнаты с прямой привязкой → их жильцы попадают на этот тариф автоматически.
    rooms = (await db.execute(
        select(Room).where(Room.tariff_id == tariff_id)
        .order_by(Room.dormitory_name, Room.room_number)
    )).scalars().all()

    # Сколько жильцов в этих комнатах
    room_ids = [r.id for r in rooms]
    users_in_rooms_count = 0
    if room_ids:
        users_in_rooms_count = (await db.execute(
            select(func.count(User.id)).where(
                User.room_id.in_(room_ids),
                User.is_deleted.is_(False),
            )
        )).scalar_one()

    # Жильцы с индивидуальной привязкой User.tariff_id (НЕ дублируются с теми,
    # у кого Room.tariff_id уже стоит — Room имеет приоритет, но User.tariff_id
    # всё равно показываем как «зачем-то навешенный персональный тариф»).
    users_direct = (await db.execute(
        select(User).options(selectinload(User.room))
        .where(User.tariff_id == tariff_id, User.is_deleted.is_(False))
    )).scalars().all()

    # Для базового тарифа (id=1) — все, у кого вообще нет привязки.
    null_users_count = 0
    if tariff_id == 1:
        null_users_count = (await db.execute(
            select(func.count(User.id))
            .outerjoin(Room, User.room_id == Room.id)
            .where(
                User.is_deleted.is_(False),
                User.tariff_id.is_(None),
                ((Room.tariff_id.is_(None)) | (User.room_id.is_(None))),
            )
        )).scalar_one()

    # Раскладка по общежитиям (для удобной массовой смены)
    by_dorm: dict[str, int] = {}
    for r in rooms:
        d = r.dormitory_name or "Без общежития"
        by_dorm[d] = by_dorm.get(d, 0) + 1

    return {
        "tariff": {"id": tariff.id, "name": tariff.name, "is_active": tariff.is_active},
        "by_room": {
            "rooms_count": len(rooms),
            "users_in_rooms": users_in_rooms_count,
            "by_dormitory": [
                {"dormitory": d, "rooms_count": c} for d, c in sorted(by_dorm.items())
            ],
            "rooms": [
                {
                    "id": r.id,
                    "dormitory": r.dormitory_name,
                    "number": r.room_number,
                }
                for r in rooms[:50]  # для UI хватит — превью; полный список не нужен
            ],
        },
        "by_user_direct": {
            "count": len(users_direct),
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "room": (
                        f"{u.room.dormitory_name}, ком. {u.room.room_number}"
                        if u.room else None
                    ),
                }
                for u in users_direct[:50]
            ],
        },
        "fallback_default_users": null_users_count,  # только для id=1
        "total_effective": users_in_rooms_count + len(users_direct) + null_users_count,
    }


# =====================================================
# ASSIGN-TO-DORMITORY — массовая привязка тарифа к общежитию
# =====================================================
class AssignToDormRequest(BaseModel):
    dormitory_name: str
    tariff_id: Optional[int] = None  # None = снять привязку (вернуться к default)


@router.post("/assign-to-dormitory", summary="Привязать тариф ко всему общежитию")
async def assign_tariff_to_dormitory(
    data: AssignToDormRequest,
    current_user: User = Depends(allow_management_roles),
    db: AsyncSession = Depends(get_db),
):
    """
    Массово проставляет Room.tariff_id всем комнатам указанного общежития.
    Это то самое «у общежития № 5 свой тариф» одной кнопкой.

    Передать tariff_id=null — снять привязку с комнат (вернутся на User.tariff_id
    или default).
    """
    if data.tariff_id is not None:
        tariff = await db.get(Tariff, data.tariff_id)
        if not tariff or not tariff.is_active:
            raise HTTPException(404, "Активный тариф с таким id не найден")

    # Считаем сколько комнат затронем — для аудита и подтверждения в UI
    affected = (await db.execute(
        select(func.count(Room.id)).where(Room.dormitory_name == data.dormitory_name)
    )).scalar_one()
    if not affected:
        raise HTTPException(404, f"Общежитие «{data.dormitory_name}» не найдено")

    await db.execute(
        update(Room)
        .where(Room.dormitory_name == data.dormitory_name)
        .values(tariff_id=data.tariff_id)
    )

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="tariff_assign_dorm", entity_type="tariff",
        entity_id=data.tariff_id,
        details={
            "dormitory": data.dormitory_name,
            "rooms_affected": affected,
            "tariff_id": data.tariff_id,
        },
    )
    await db.commit()
    await _safe_clear_cache("tariffs")  # пересчёты будут использовать новый тариф

    return {"status": "ok", "rooms_affected": affected}


# =====================================================
# CACHE STATS — диагностика для UI «эффективность кеша»
# =====================================================
@router.get("/cache/stats", summary="Состояние in-memory кеша тарифов")
async def cache_stats(current_user: User = Depends(allow_management_roles)):
    return tariff_cache.stats()


@router.post("/cache/invalidate", summary="Сбросить in-memory кеш тарифов")
async def cache_invalidate(current_user: User = Depends(allow_management_roles)):
    tariff_cache.invalidate()
    return {"status": "ok"}