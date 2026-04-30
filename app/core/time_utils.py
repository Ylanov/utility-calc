"""Единая точка получения «текущего UTC» в кодовой базе.

Зачем
=====
Python 3.13 пометил `datetime.utcnow()` как deprecated, в 3.14 будет
DeprecationWarning. Замена — `datetime.now(timezone.utc)`, но она возвращает
tz-aware datetime, а у нас все колонки `TIMESTAMP WITHOUT TIME ZONE`
(asyncpg строго требует naive datetime для них — иначе DataError).

Поэтому используем `datetime.now(timezone.utc).replace(tzinfo=None)` —
получаем naive UTC, совместимый с asyncpg.

Использование
=============
    from app.core.time_utils import utcnow

    created_at = utcnow()                          # для записи в БД
    if (utcnow() - row.created_at).days > 30:      # для арифметики
        ...

В моделях `Column(default=...)` SQLAlchemy ожидает callable, поэтому
передавать так:

    created_at = Column(DateTime, default=utcnow)
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Текущее UTC-время как naive datetime (без tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
