"""analyzer_config.py — единая точка чтения настроек ВСЕХ анализаторов.

Зачем: до этой штуки пороги были захардкожены в коде. Теперь они в таблице
`analyzer_settings`, редактируются админом, кешируются на 60 секунд (чтобы
горячий путь анализа не делал SELECT на каждый reading).

Использование:
    from app.modules.utility.services.analyzer_config import config
    threshold = config.get_int("gsheets.fuzzy_threshold", default=78)
    if config.is_rule_enabled("rule.round_number"):
        ...

Cache TTL — 60 секунд: компромисс между «изменения видны быстро» и «не дёргать
БД на каждое показание». При горячих fixes можно вызвать `config.invalidate()`.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

# Импорт sync-сессии только при использовании, чтобы модуль грузился без БД
# (нужно для тестов и alembic-миграций).
_CACHE_TTL_SECONDS = 60


class AnalyzerConfig:
    def __init__(self):
        self._cache: dict[str, str] = {}
        self._enabled_cache: dict[str, bool] = {}
        self._loaded_at: float = 0.0
        self._lock = threading.RLock()

    # --- Загрузка ---
    def _ensure_loaded(self) -> None:
        if time.time() - self._loaded_at < _CACHE_TTL_SECONDS and self._cache:
            return
        with self._lock:
            # Double-check внутри лока
            if time.time() - self._loaded_at < _CACHE_TTL_SECONDS and self._cache:
                return
            try:
                from app.core.database import sync_db_session
                from app.modules.utility.models import AnalyzerSetting
                with sync_db_session() as db:
                    rows = db.query(AnalyzerSetting).all()
                    self._cache = {r.key: r.value for r in rows}
                    self._enabled_cache = {r.key: bool(r.is_enabled) for r in rows}
                    self._loaded_at = time.time()
            except Exception:
                # Если БД недоступна / таблицы ещё нет (до миграции) —
                # оставляем пустой кеш, методы вернут default.
                self._loaded_at = time.time()

    def invalidate(self) -> None:
        """Сбросить кеш — для UI «Применить сразу»."""
        with self._lock:
            self._cache.clear()
            self._enabled_cache.clear()
            self._loaded_at = 0.0

    # --- Геттеры с типобезопасностью и default ---
    def get_str(self, key: str, default: str = "") -> str:
        self._ensure_loaded()
        return self._cache.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        self._ensure_loaded()
        v = self._cache.get(key)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        self._ensure_loaded()
        v = self._cache.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        self._ensure_loaded()
        v = self._cache.get(key)
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def is_rule_enabled(self, key: str, default: bool = True) -> bool:
        """Для правил `rule.*` — комбинация значения 'true' И is_enabled.
        Удобно: одна точка проверки в коде анализатора.
        """
        self._ensure_loaded()
        if not self.get_bool(key, default=default):
            return False
        return self._enabled_cache.get(key, True)


# Глобальный singleton — потокобезопасный, кеш TTL=60s.
config = AnalyzerConfig()


# --- Self-learning: dismissed flags ---
class DismissalChecker:
    """Кеш набора (user_id, flag) которые админ пометил как «не аномалия».
    Используется в anomaly_detector для отсева false-positive после
    помеченной аномалии.

    Глобальные dismissals (user_id IS NULL) — отключают правило для всех.
    """
    def __init__(self):
        self._user_flags: set[tuple[int, str]] = set()
        self._global_flags: set[str] = set()
        self._loaded_at: float = 0.0
        self._lock = threading.RLock()

    def _ensure_loaded(self) -> None:
        if time.time() - self._loaded_at < _CACHE_TTL_SECONDS and (
            self._user_flags or self._global_flags or self._loaded_at
        ):
            return
        with self._lock:
            if time.time() - self._loaded_at < _CACHE_TTL_SECONDS and (
                self._user_flags or self._global_flags or self._loaded_at
            ):
                return
            try:
                from app.core.database import sync_db_session
                from app.modules.utility.models import AnomalyDismissal
                with sync_db_session() as db:
                    rows = db.query(AnomalyDismissal).all()
                    self._user_flags = set()
                    self._global_flags = set()
                    for r in rows:
                        if r.user_id is None:
                            self._global_flags.add(r.flag_code)
                        else:
                            self._user_flags.add((r.user_id, r.flag_code))
                    self._loaded_at = time.time()
            except Exception:
                self._loaded_at = time.time()

    def invalidate(self) -> None:
        with self._lock:
            self._user_flags.clear()
            self._global_flags.clear()
            self._loaded_at = 0.0

    def is_dismissed(self, user_id: Optional[int], flag_code: str) -> bool:
        self._ensure_loaded()
        if flag_code in self._global_flags:
            return True
        if user_id is None:
            return False
        return (user_id, flag_code) in self._user_flags


dismissals = DismissalChecker()
