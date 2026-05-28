"""room_validators.py — общие проверки помещения перед операциями.

После housing_001 (place_type='dormitory' | 'house') у нас два типа
помещений. Дома (house) физически не имеют счётчиков ГВС/ХВС/электр —
им начисляется только найм (charge_social_rent=True, остальные
charge_*=False). Поэтому ЛЮБАЯ попытка подать/утвердить MeterReading
с реальными значениями для такого помещения = ошибка процесса:
- жилец дома не должен видеть кнопку «Подать показания» в мобильном
  (но даже если как-то нажал — API отдаёт 400);
- админ не должен вводить за него руками (UI скрывает форму, но API
  отдаёт 400 даже если запрос пришёл напрямую);
- GSheets-импорт fuzzy-матча, который попал на дом, помечает строку
  как conflict (не raise, потому что batch).

E2-B (28.05.2026): сначала через жалобы пользователей выясняется,
что иногда reading создаётся «бесшумно» для дома (например при
старых mobile-клиентах). Этот модуль — единая точка истины.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.modules.utility.models import PlaceType, Room


_REASON_HOUSE_NO_METERS = (
    "Дома и частные квартиры не подают показания счётчиков — "
    "им начисляется только найм. Если это ошибка типа помещения, "
    "поправьте его в Жилфонде."
)


def is_house(room: Optional[Room]) -> bool:
    """True если room — дом (place_type='house'). None трактуется как False."""
    if room is None:
        return False
    return room.place_type == PlaceType.HOUSE.value


def require_room_has_meters(room: Optional[Room]) -> None:
    """Поднимает HTTPException(400) если room=house. Используется в
    эндпоинтах создания/редактирования MeterReading (mobile, admin-manual).
    """
    if is_house(room):
        raise HTTPException(status_code=400, detail=_REASON_HOUSE_NO_METERS)


def house_conflict_reason(room: Optional[Room]) -> Optional[str]:
    """Возвращает короткий машинно-читаемый conflict_reason если room=house.

    Используется в gsheets_sync для пометки import-row как conflict
    вместо raise (batch-обработка не должна прерываться на одной
    проблемной строке). Сообщение начинается с префикса
    'house_place_type_no_meters' — UI может узнать по нему этот случай
    и показать админу кнопку «Этот жилец живёт в доме — не нужно
    утверждать» вместо обычного «Утвердить».
    """
    if is_house(room):
        return (
            "house_place_type_no_meters: жилец живёт в доме/квартире, "
            "счётчиков нет — подача показаний не требуется. "
            "Отклоните эту строку либо переназначьте на жильца общежития."
        )
    return None


__all__ = [
    "is_house",
    "require_room_has_meters",
    "house_conflict_reason",
]
