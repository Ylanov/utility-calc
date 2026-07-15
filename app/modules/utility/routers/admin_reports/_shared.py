# Общее ядро пакета admin_reports: единый APIRouter, logger и хелперы,
# используемые несколькими модулями пакета. Перенесено из монолитного
# routers/admin_reports.py механически (распил на пакет), поведение 1:1.
# ВАЖНО: этот модуль не должен импортировать модули-роуты пакета (цикл).

import logging
from decimal import Decimal

from fastapi import APIRouter

# Имя логгера оставляем историческим ("…routers.admin_reports", как у бывшего
# модуля-монолита), чтобы настройка логирования по имени продолжала работать.
logger = logging.getLogger(__name__.rsplit(".", 1)[0])

router = APIRouter(tags=["Admin Reports"])
ZERO = Decimal("0.00")


def _report_group(room) -> str:
    """Имя блока в финотчёте. Общага → dormitory_name. ДОМ (place_type='house')
    → здание «ул. X, д. Y»: все квартиры одного дома собираются в ОТДЕЛЬНЫЙ блок,
    как комнаты общежития (2026-06-18). Раньше все дома сваливались в общий
    «Без общежития»."""
    if getattr(room, "place_type", None) == "house":
        parts = []
        if getattr(room, "street", None):
            parts.append(f"ул. {room.street}")
        if getattr(room, "house_number", None):
            parts.append(f"д. {room.house_number}")
        return ", ".join(parts) if parts else "Дома"
    return room.dormitory_name or "Без общежития"


def _unit_label(room) -> str:
    """Подпись комнаты/квартиры в строке отчёта: общага → room_number,
    дом → «кв. N» (у дома room_number=NULL, номер живёт в apartment_number)."""
    if getattr(room, "place_type", None) == "house":
        return f"кв. {room.apartment_number}" if getattr(room, "apartment_number", None) else "—"
    return room.room_number or "—"
