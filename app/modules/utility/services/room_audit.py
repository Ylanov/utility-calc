"""room_audit.py — аудит соответствия типа квартиры составу жильцов.

Семейная квартира (`Room.is_singles_apartment=False`) рассчитана на ОДИН
аккаунт-жилец (глава семьи, `residents_count` = размер семьи), который платит
полную сумму. Холостяцкая (`is_singles_apartment=True`) — несколько холостяков
(`resident_type='single'`), делящих счёт поровну (`/total_room_residents`).

Несоответствие = либо ошибка данных (дубль / призрак / не выехавший жилец),
либо неучтённое койко-место (надо пометить квартиру холостяцкой), либо неверный
тип жильца. Считаем по ПРИВЯЗКЕ к комнате (`User.room_id`) — ловит и тех, кто
не подаёт показания.

Единый источник правды: сигнал `ROOM_TYPE_MISMATCH` в Мониторе проблем
(resident_problem_scanner) и список в Центре анализов
(`GET /api/admin/analyzer/room-type-mismatches`) зовут одну эту функцию.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.utility.models import User

# kind → (severity, заголовок, рекомендация админу)
KIND_META = {
    "multi_family": (
        "high", "Несколько семей в одной квартире",
        "Семейная квартира рассчитана на один аккаунт. Проверьте: дубль, "
        "не выехавший жилец — или это койко-место, тогда пометьте квартиру "
        "холостяцкой в Жилфонде.",
    ),
    "mixed_types": (
        "high", "Смешанные типы жильцов в квартире",
        "В несхолостяцкой квартире есть и семейные, и холостяки. Приведите к "
        "одному типу или пометьте квартиру холостяцкой.",
    ),
    "unmarked_singles": (
        "high", "Холостяки без пометки квартиры",
        "Несколько холостяков в квартире, не помеченной холостяцкой — счёт не "
        "делится поровну. Включите «Холостяцкая квартира» в Жилфонде.",
    ),
    "singles_with_family": (
        "medium", "Семейный в холостяцкой квартире",
        "Квартира помечена холостяцкой, но есть семейный аккаунт — делёж счёта "
        "применится неверно. Проверьте тип жильца или флаг квартиры.",
    ),
}


def _classify(is_singles: bool, n_family: int, n_single: int):
    """Возвращает kind несоответствия или None, если состав корректен."""
    n = n_family + n_single
    if not is_singles:
        # Семейная квартира: ожидается РОВНО один аккаунт (любого типа).
        if n < 2:
            return None
        if n_single == 0:
            return "multi_family"      # 2+ семейных
        if n_family == 0:
            return "unmarked_singles"  # 2+ холостяка, но флаг не выставлен
        return "mixed_types"           # и семейные, и холостяки
    # Холостяцкая квартира: ожидаются только холостяки.
    if n_family >= 1:
        return "singles_with_family"
    return None


async def find_room_type_mismatches(db: AsyncSession) -> list[dict]:
    """Сканирует все комнаты с активными жильцами и возвращает несоответствия
    типа квартиры составу жильцов — по одной записи на проблемную комнату."""
    residents = (await db.execute(
        select(User)
        .options(selectinload(User.room))
        .where(
            User.is_deleted.is_(False),
            User.role == "user",
            User.room_id.isnot(None),
        )
    )).scalars().all()

    by_room: dict[int, list] = {}
    for u in residents:
        if u.room is None:
            continue
        by_room.setdefault(u.room_id, []).append(u)

    out: list[dict] = []
    for members in by_room.values():
        room = members[0].room
        is_singles = bool(room.is_singles_apartment)
        n_single = sum(1 for u in members if (u.resident_type or "family") == "single")
        n_family = len(members) - n_single
        kind = _classify(is_singles, n_family, n_single)
        if kind is None:
            continue
        severity, title, recommendation = KIND_META[kind]
        # Представитель для сигнала в Мониторе — минимальный user_id.
        rep = min(members, key=lambda u: u.id)
        out.append({
            "room_id": room.id,
            "address": room.format_address or (room.room_number or "—"),
            "dormitory_name": room.dormitory_name,
            "room_number": room.room_number,
            "place_type": getattr(room, "place_type", None),
            "is_singles_apartment": is_singles,
            "kind": kind,
            "severity": severity,
            "title": title,
            "recommendation": recommendation,
            "n_residents": len(members),
            "n_family": n_family,
            "n_single": n_single,
            "representative_user_id": rep.id,
            "residents": sorted(
                [
                    {
                        "user_id": u.id,
                        "username": u.username,
                        "full_name": getattr(u, "full_name", None),
                        "resident_type": u.resident_type or "family",
                        "residents_count": u.residents_count or 1,
                    }
                    for u in members
                ],
                key=lambda r: (r["resident_type"], (r["username"] or "")),
            ),
        })

    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    out.sort(key=lambda r: (sev_rank.get(r["severity"], 9), -r["n_residents"]))
    return out


__all__ = ["find_room_type_mismatches", "KIND_META"]
