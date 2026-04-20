# app/modules/utility/models.py

from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    Boolean,
    Index,
    DateTime,
    Text,
    func
)
from sqlalchemy.types import Numeric
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from app.core.database import Base
from sqlalchemy.dialects.postgresql import JSONB


def _utcnow():
    """Возвращает текущее время в UTC (naive).
    ИСПРАВЛЕНИЕ: Драйвер asyncpg строго требует naive datetime для колонок TIMESTAMP.
    Использование tz-aware времени приводило к 500 ошибке (DataError).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ======================================================
# ROOM (ОСНОВА СИСТЕМЫ)
# ======================================================
class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)

    dormitory_name = Column(String, index=True)
    room_number = Column(String, index=True)

    apartment_area = Column(Numeric(10, 2), default=0.00)
    total_room_residents = Column(Integer, default=1)

    hw_meter_serial = Column(String, nullable=True)
    cw_meter_serial = Column(String, nullable=True)
    el_meter_serial = Column(String, nullable=True)

    last_hot_water = Column(Numeric(12, 3), default=0.000)
    last_cold_water = Column(Numeric(12, 3), default=0.000)
    last_electricity = Column(Numeric(12, 3), default=0.000)

    # НОВОЕ: Отслеживание последнего изменения
    updated_at = Column(DateTime, nullable=True, onupdate=_utcnow)

    __table_args__ = (
        Index(
            "uq_room_dormitory_number",
            "dormitory_name",
            "room_number",
            unique=True
        ),
    )


# ======================================================
# USER (ЖИЛЕЦ)
# ======================================================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False)

    workplace = Column(String, nullable=True)
    residents_count = Column(Integer, default=1)

    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True)
    room = relationship("Room", backref="users")

    tariff_id = Column(Integer, ForeignKey("tariffs.id"), nullable=True)
    tariff = relationship("Tariff")

    totp_secret = Column(String, nullable=True)

    is_deleted = Column(Boolean, default=False, index=True)
    is_initial_setup_done = Column(Boolean, default=False)
    telegram_id = Column(String, unique=True, nullable=True, index=True)

    # Brute-force защита. Инкрементируется при неверном пароле;
    # при достижении порога (3) выставляется locked_until = now + 15 мин.
    # Сбрасывается на 0 при успешном входе.
    failed_login_count = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)

    # НОВОЕ: Отслеживание последнего изменения
    updated_at = Column(DateTime, nullable=True, onupdate=_utcnow)

    __table_args__ = (
        Index(
            "uq_user_username_lower",
            func.lower(username),
            unique=True
        ),
        Index(
            "idx_user_username_trgm",
            "username",
            postgresql_using="gin",
            postgresql_ops={"username": "gin_trgm_ops"}
        ),
    )


# ======================================================
# TARIFF
# ======================================================
class Tariff(Base):
    __tablename__ = "tariffs"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, default="Базовый тариф")
    is_active = Column(Boolean, default=True, index=True)
    valid_from = Column(DateTime, default=_utcnow)

    maintenance_repair = Column(Numeric(10, 4), default=0.0)
    social_rent = Column(Numeric(10, 4), default=0.0)
    heating = Column(Numeric(10, 4), default=0.0)
    water_heating = Column(Numeric(10, 4), default=0.0)
    water_supply = Column(Numeric(10, 4), default=0.0)
    sewage = Column(Numeric(10, 4), default=0.0)
    waste_disposal = Column(Numeric(10, 4), default=0.0)
    electricity_per_sqm = Column(Numeric(10, 4), default=0.0)
    electricity_rate = Column(Numeric(10, 4), default=5.0)

    # Дата вступления в силу. Если задана в будущем — тариф "запланирован" (is_active=False)
    # и автоматически активируется Celery-задачей в эту дату.
    effective_from = Column(DateTime, nullable=True, index=True)

    # Отслеживание последнего изменения
    updated_at = Column(DateTime, nullable=True, onupdate=_utcnow)


# ======================================================
# BILLING PERIOD
# ======================================================
class BillingPeriod(Base):
    __tablename__ = "periods"

    id = Column(Integer, primary_key=True, index=True)
    tariff_id = Column(Integer, ForeignKey("tariffs.id"), nullable=True)
    tariff = relationship("Tariff")

    name = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=_utcnow)


# ======================================================
# ADJUSTMENTS
# ======================================================
class Adjustment(Base):
    __tablename__ = "adjustments"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    period_id = Column(Integer, ForeignKey("periods.id"), nullable=False)

    amount = Column(Numeric(10, 2), nullable=False)
    description = Column(String, nullable=False)
    account_type = Column(String, default="209", nullable=False)

    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User")
    period = relationship("BillingPeriod")

    __table_args__ = (
        Index("idx_adj_user_period", "user_id", "period_id"),
    )


# ======================================================
# METER READING (ПРИВЯЗКА К КОМНАТЕ)
# ======================================================
class MeterReading(Base):
    __tablename__ = "readings"

    __table_args__ = (
        Index("idx_reading_room_period", "room_id", "period_id"),
        Index("idx_reading_approved_period", "is_approved", "period_id"),
        Index("idx_reading_room_approved", "room_id", "is_approved"),
        # Добавлено миграцией perf_001_scaling_indexes для масштаба 5-10к пользователей.
        Index("idx_reading_user_approved", "user_id", "is_approved"),
        Index("idx_reading_user_created", "user_id", "created_at"),
        Index("idx_reading_period_approved_created",
              "period_id", "is_approved", "created_at"),
        {
            "postgresql_partition_by": "RANGE (created_at)"
        }
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime, primary_key=True, default=_utcnow)

    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    period_id = Column(Integer, ForeignKey("periods.id"), nullable=True)

    room = relationship("Room")
    user = relationship("User")
    period = relationship("BillingPeriod")

    hot_water = Column(Numeric(12, 3))
    cold_water = Column(Numeric(12, 3))
    electricity = Column(Numeric(12, 3))

    debt_209 = Column(Numeric(12, 2), default=0.00)
    overpayment_209 = Column(Numeric(12, 2), default=0.00)
    debt_205 = Column(Numeric(12, 2), default=0.00)
    overpayment_205 = Column(Numeric(12, 2), default=0.00)

    hot_correction = Column(Numeric(12, 3), default=0.0)
    cold_correction = Column(Numeric(12, 3), default=0.0)
    electricity_correction = Column(Numeric(12, 3), default=0.0)
    sewage_correction = Column(Numeric(12, 3), default=0.0)

    total_209 = Column(Numeric(12, 2), default=0.00)
    total_205 = Column(Numeric(12, 2), default=0.00)
    total_cost = Column(Numeric(12, 2), default=0.00)

    cost_hot_water = Column(Numeric(12, 2), default=0.00)
    cost_cold_water = Column(Numeric(12, 2), default=0.00)
    cost_electricity = Column(Numeric(12, 2), default=0.00)
    cost_sewage = Column(Numeric(12, 2), default=0.00)
    cost_maintenance = Column(Numeric(12, 2), default=0.00)
    cost_social_rent = Column(Numeric(12, 2), default=0.00)
    cost_waste = Column(Numeric(12, 2), default=0.00)
    cost_fixed_part = Column(Numeric(12, 2), default=0.00)

    anomaly_flags = Column(String, nullable=True)
    anomaly_score = Column(Integer, default=0)

    is_approved = Column(Boolean, default=False)

    edit_count = Column(Integer, default=0)
    edit_history = Column(JSONB, nullable=True)


# ======================================================
# SYSTEM SETTINGS
# ======================================================
class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=False)
    description = Column(String, nullable=True)


# ======================================================
# DEVICE TOKENS (Push-уведомления)
# ======================================================
class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String, unique=True, nullable=False, index=True)
    device_type = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    user = relationship("User", backref="device_tokens")


# ======================================================
# AUDIT LOG (НОВОЕ: Журнал действий администратора)
# ======================================================
class AuditLog(Base):
    """
    Журнал действий администратора.
    Фиксирует кто, когда и что сделал в системе.
    Критично для бухгалтерских проверок и разрешения споров.
    """
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)

    # Кто совершил действие (nullable + SET NULL — сохраняем лог даже если пользователь удалён)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    username = Column(String, nullable=False)

    # Что сделал
    action = Column(String, nullable=False)  # create, update, delete, approve, close_period, etc.

    # Над чем (сущность)
    entity_type = Column(String, nullable=False)  # user, room, tariff, reading, period, adjustment
    entity_id = Column(Integer, nullable=True)  # ID объекта (может быть NULL для массовых операций)

    # Детали (произвольный JSON)
    details = Column(JSONB, nullable=True)

    # Когда
    created_at = Column(DateTime, default=_utcnow, index=True)

    user = relationship("User")

    __table_args__ = (
        Index("idx_audit_user_id", "user_id"),
        Index("idx_audit_entity", "entity_type", "entity_id"),
        Index("idx_audit_action", "action"),
        Index("idx_audit_created", "created_at"),
        # Добавлено миграцией perf_001_scaling_indexes: ускоряют фильтрацию
        # журнала по action/entity с сортировкой по created_at (самый частый запрос).
        Index("idx_audit_action_created", "action", "created_at"),
        Index("idx_audit_entity_created", "entity_type", "created_at"),
    )


# ======================================================
# GOOGLE SHEETS IMPORT (интеграция с внешней таблицей)
# ======================================================
# Показания подаются жильцами в стороннюю Google-таблицу с колонками:
#   A: timestamp (dd.mm.yyyy HH:MM:SS)
#   B: ФИО жильца (разные форматы: полное, сокращённое, с опечатками)
#   C: общежитие (свободный текст — "4", "Общежитие № 2", "УТК", "дмвл. 4, с. 15")
#   D: номер комнаты
#   E: ГВС (м³, decimal — "91,778" или "1085.07")
#   F: ХВС (м³)
#
# Наша фоновая задача читает CSV-экспорт таблицы раз в N минут и складывает
# каждую строку в этот импорт-буфер со статусом. Админ из UI решает:
# - pending → approved (создаётся MeterReading + привязывается к пользователю)
# - pending → rejected (строка отброшена, в БД показаний не появится)
# - conflict → resolved_reassign (админ переопределяет user_id/room_id)
#
# row_hash (unique): MD5 от кортежа (дата, ФИО, комната, ГВС, ХВС). Гарантирует
# идемпотентность импорта — повторная синхронизация не создаст дубль.
# ======================================================
class GSheetsImportRow(Base):
    __tablename__ = "gsheets_import_rows"

    id = Column(Integer, primary_key=True, index=True)

    # Сырые данные из таблицы
    sheet_timestamp = Column(DateTime, nullable=True, index=True)
    raw_fio = Column(String, nullable=False)
    raw_dormitory = Column(String, nullable=True)
    raw_room_number = Column(String, nullable=True)
    raw_hot_water = Column(String, nullable=True)
    raw_cold_water = Column(String, nullable=True)

    # Разобранные значения (после парсинга decimal и очистки)
    hot_water = Column(Numeric(12, 3), nullable=True)
    cold_water = Column(Numeric(12, 3), nullable=True)

    # Результат сопоставления с БД
    matched_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    matched_room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True)
    match_score = Column(Integer, default=0)  # 0..100 — уверенность fuzzy-матча

    # Статусы:
    # - pending: импортировано, ждёт решения админа (fuzzy matched, но не approved)
    # - unmatched: ФИО не найдено ни с каким жильцом
    # - conflict: ФИО нашлось, но номер комнаты не совпадает с привязанной
    # - approved: админ утвердил, создан MeterReading
    # - rejected: админ отклонил
    # - auto_approved: высокий score (≥95) + комната совпала — импорт без ручного утверждения
    status = Column(String, default="pending", index=True)
    conflict_reason = Column(Text, nullable=True)

    # Ссылка на созданный MeterReading (если approved)
    reading_id = Column(Integer, ForeignKey("readings.id"), nullable=True)

    # Уникальный хэш строки — защита от дублей при повторном импорте
    row_hash = Column(String(32), unique=True, nullable=False, index=True)

    # Метаданные
    created_at = Column(DateTime, default=_utcnow, index=True)
    processed_at = Column(DateTime, nullable=True)
    processed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)

    matched_user = relationship("User", foreign_keys=[matched_user_id])
    matched_room = relationship("Room", foreign_keys=[matched_room_id])
    processed_by = relationship("User", foreign_keys=[processed_by_id])

    __table_args__ = (
        Index("idx_gsheets_status_created", "status", "created_at"),
        Index("idx_gsheets_matched_user", "matched_user_id"),
        Index("idx_gsheets_timestamp", "sheet_timestamp"),
    )