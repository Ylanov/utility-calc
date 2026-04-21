"""tariff_cache.py — единый кеш и единая точка получения тарифа.

Зачем:
  1) Тарифы меняются редко (раз в полгода-год), а расчёты делаются часто.
     При approve / billing на каждый MeterReading раньше шёл SELECT FROM tariffs.
     На 1000+ жильцов это сотни запросов. Тут — один SELECT на 10 минут,
     остальное из in-memory dict.

  2) Раньше логика «какой тариф у этого жильца» была размазана по нескольким
     местам:
        tariffs_map.get(user.tariff_id) if user.tariff_id else default
     В таком виде Room.tariff_id не учитывался. Теперь единая функция
     `get_effective_tariff()`:
        Room.tariff_id → User.tariff_id → default (id=1)

Кеш потокобезопасный, TTL 600 секунд. Инвалидируется явно при изменении тарифа.
Для Celery-воркеров кеш отдельный per-process — это нормально, расчёты
идемпотентны и допускают рассинхронизацию на минуты.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # Только для тайп-чекеров (mypy / ruff). Реальный импорт лениво в _ensure_loaded
    # чтобы избежать циклов и дать модулю грузиться без БД.
    from app.modules.utility.models import Tariff


_CACHE_TTL_SECONDS = 600  # 10 минут — баланс между «свежо» и «не дёргать БД»


class TariffCache:
    def __init__(self):
        self._tariffs: dict[int, "Tariff"] = {}
        self._default_id: Optional[int] = None
        self._loaded_at: float = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Загрузка / инвалидация
    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if time.time() - self._loaded_at < _CACHE_TTL_SECONDS and self._tariffs:
            return
        with self._lock:
            if time.time() - self._loaded_at < _CACHE_TTL_SECONDS and self._tariffs:
                return
            try:
                from app.core.database import sync_db_session
                from app.modules.utility.models import Tariff
                with sync_db_session() as db:
                    rows = db.query(Tariff).filter(Tariff.is_active.is_(True)).all()
                    self._tariffs = {t.id: t for t in rows}
                    # default = id=1 если есть, иначе любой первый активный
                    self._default_id = 1 if 1 in self._tariffs else (
                        next(iter(self._tariffs)) if self._tariffs else None
                    )
                    self._loaded_at = time.time()
            except Exception:
                # БД ещё не доступна (тесты / миграция) — оставляем пустой кеш.
                self._loaded_at = time.time()

    def invalidate(self) -> None:
        """Сбросить кеш — вызывать после PATCH/POST/DELETE тарифов или Room.tariff_id."""
        with self._lock:
            self._tariffs.clear()
            self._default_id = None
            self._loaded_at = 0.0

    # ------------------------------------------------------------------
    # Геттеры
    # ------------------------------------------------------------------
    def get_by_id(self, tariff_id: Optional[int]):
        """Возвращает Tariff из кеша, либо None если такого нет (или неактивен)."""
        if tariff_id is None:
            return None
        self._ensure_loaded()
        return self._tariffs.get(tariff_id)

    def get_default(self):
        """Возвращает дефолтный тариф (id=1 или первый активный)."""
        self._ensure_loaded()
        return self._tariffs.get(self._default_id) if self._default_id else None

    def get_effective_tariff(self, *, user=None, room=None):
        """Главная функция: какой тариф РЕАЛЬНО применяется для пары (user, room).

        Приоритет (от сильного к слабому):
          1. room.tariff_id        — комнатный тариф (часто общеобщежитский)
          2. user.tariff_id        — индивидуальный тариф жильца
          3. default               — fallback (id=1)

        Это позволяет:
          * массово сменить тариф для общежития: проставить room.tariff_id у всех
            комнат этого общежития (есть отдельный endpoint assign-to-dormitory);
          * для исключительных жильцов оставить персональный User.tariff_id;
          * при удалении тарифа все автоматически падают на default.
        """
        self._ensure_loaded()

        if room is not None:
            rt_id = getattr(room, "tariff_id", None)
            if rt_id is not None and rt_id in self._tariffs:
                return self._tariffs[rt_id]

        if user is not None:
            ut_id = getattr(user, "tariff_id", None)
            if ut_id is not None and ut_id in self._tariffs:
                return self._tariffs[ut_id]

        return self.get_default()

    def get_all_active(self) -> dict[int, "Tariff"]:
        """Снимок всех активных тарифов из кеша. Для bulk-операций."""
        self._ensure_loaded()
        return dict(self._tariffs)

    # ------------------------------------------------------------------
    # Вспомогательное: сколько раз за час кеш реально использовался
    # (для отладки / KPI «эффективность кеша»)
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "loaded_at": self._loaded_at,
            "ttl_seconds": _CACHE_TTL_SECONDS,
            "active_tariffs_count": len(self._tariffs),
            "default_tariff_id": self._default_id,
            "stale_in_seconds": max(
                0, int(_CACHE_TTL_SECONDS - (time.time() - self._loaded_at))
            ) if self._loaded_at else 0,
        }


# Глобальный singleton — потокобезопасный, ленивый.
tariff_cache = TariffCache()
