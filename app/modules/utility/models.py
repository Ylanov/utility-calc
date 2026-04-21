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

    # Тариф конкретного помещения. Опциональный.
    # Приоритет матча: Room.tariff_id → User.tariff_id → default (id=1).
    # Это позволяет сценарий «всё общежитие № 5 на тарифе А, а № 7 на тарифе Б»
    # массово, без обновления каждого жильца. Меняем тариф у комнаты — для всех её
    # жильцов он применится автоматически (через get_effective_tariff в tariff_cache).
    tariff_id = Column(Integer, ForeignKey("tariffs.id"), nullable=True, index=True)
    tariff = relationship("Tariff", foreign_keys=[tariff_id])

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

    # Тип жильца — определяет, как считаются коммуналка.
    #   'family' — семья (1 или несколько человек, residents_count >= 1).
    #              Платит по СЧЁТЧИКАМ (ГВС/ХВС/электр) + фикс. часть (содержание/наём/ТКО/отопление).
    #   'single' — холостяк (одиночное проживание, обычно с другими холостяками
    #              в одной комнате). Платит за КОЙКО-МЕСТО (фиксированная сумма из тарифа,
    #              независимо от показаний счётчиков).
    # По умолчанию 'family' — это сохраняет старое поведение для существующих жильцов.
    resident_type = Column(String(16), default="family", nullable=False, index=True)

    # Режим начисления — техническое поле, которое следует из resident_type, но
    # сделано отдельным чтобы:
    #   1) можно было исключения (например семья, которой временно начисляют per_capita);
    #   2) не ломать существующий код, который рассчитывает по billing_mode явно.
    # Допустимые значения: 'by_meter' | 'per_capita'.
    billing_mode = Column(String(16), default="by_meter", nullable=False, index=True)

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

    # Фиксированная сумма за «койко-место» — для холостяков, которые живут
    # вместе в одной квартире и каждый платит сам за себя.
    # При billing_mode='per_capita' эта сумма становится итогом квитанции
    # (счётчики ХВС/ГВС/Электр в этом случае НЕ учитываются индивидуально).
    # Если 0 — холостяки в этом тарифе не используются.
    per_capita_amount = Column(Numeric(10, 2), default=0.00, nullable=False)

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


# ======================================================
# GSHEETS ALIAS — запомненные соответствия «стороннее ФИО → реальный жилец»
# ======================================================
# Жильцы часто подают показания за родственников (жёны за мужей, дети за
# родителей). В базе числится только один зарегистрированный жилец, но ФИО
# в Google Sheets может быть супруга/родственника. Fuzzy-match такие случаи
# не ловит — ФИО совсем другое.
#
# Админ в UI подтверждает «да, это подача Иванова И.И. (его супругой)» →
# создаётся запись в gsheets_aliases. При следующем импорте эта запись
# подцепится АВТОМАТИЧЕСКИ без участия админа.
#
# Ключ alias_fio_normalized — ФИО в lower-case с коллапсом пробелов. Чтобы
# «Иванова Мария Петровна» и «ИВАНОВА  МАРИЯ ПЕТРОВНА» матчили одно и то же.
class GSheetsAlias(Base):
    __tablename__ = "gsheets_aliases"

    id = Column(Integer, primary_key=True, index=True)
    alias_fio = Column(String, nullable=False)  # оригинальное написание — для UI
    alias_fio_normalized = Column(String, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # 'manual'   — админ вручную переназначил в UI,
    # 'relative' — подтвердил подсказку «возможно, это супруга/родственник».
    kind = Column(String, default="manual", nullable=False)
    note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=_utcnow, index=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        # Одно ФИО → один жилец. Если админ хочет перепривязать — сначала
        # удалит старый алиас, иначе 409 Conflict. Это сознательное ограничение:
        # двусмысленные ФИО должны всплывать как проблема, а не тихо матчиться
        # «то в одно, то в другое» в зависимости от того, какой алиас создан первым.
        Index("uq_gsheets_alias_fio", "alias_fio_normalized", unique=True),
    )


# ======================================================
# ANALYZER SETTINGS — единое место для всех порогов анализаторов
# ======================================================
# До этой таблицы пороги были разбросаны по коду:
#   FUZZY_THRESHOLD = 78           в gsheets_sync.py
#   AUTO_APPROVE_THRESHOLD = 95    там же
#   anomaly_score < 80             в admin_readings_approve.py
#   MAD multiplier 4               в anomaly_detector.py
#   и т.д. — менять можно только релизом.
#
# Теперь все они хранятся как key/value в БД, кешируются на 60 секунд
# в analyzer_config.py, редактируются админом через /admin/analyzer/settings.
class AnalyzerSetting(Base):
    __tablename__ = "analyzer_settings"

    key = Column(String(64), primary_key=True)
    value = Column(String, nullable=False)            # храним как str, парсим по value_type
    value_type = Column(String(16), nullable=False)   # 'int' | 'float' | 'bool' | 'str'
    category = Column(String(32), nullable=False)     # 'gsheets' | 'anomaly' | 'approve' | ...
    description = Column(Text, nullable=True)         # человекочитаемое описание
    min_value = Column(String, nullable=True)         # для UI-валидации
    max_value = Column(String, nullable=True)
    is_enabled = Column(Boolean, default=True, nullable=False)  # для on/off правил-флагов

    updated_at = Column(DateTime, nullable=True, onupdate=_utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)


# ======================================================
# ANOMALY DISMISSAL — self-learning: «это НЕ аномалия для этого жильца»
# ======================================================
# Пример: жилец каждый месяц подаёт ровно 5.000 ХВС — потому что у него
# счётчик действительно так показывает (бывает). Анализатор флагует FLAT_COLD,
# админ устаёт от false-positive. Здесь админ помечает: «для user=42 правило
# FLAT_COLD не применяется». В будущем check_reading_for_anomalies этот флаг
# не выставит для этого жильца.
#
# Запись с user_id=NULL — глобальное отключение правила для всех (мягкий
# аналог is_enabled=false в analyzer_settings, но удобный для UI «по флагу»).
class AnomalyDismissal(Base):
    __tablename__ = "anomaly_dismissals"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    flag_code = Column(String(48), nullable=False, index=True)  # напр. 'FLAT_COLD', 'ROUND_NUMBER_HOT'
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        Index("uq_anomaly_dismissal", "user_id", "flag_code", unique=True),
    )


# ======================================================
# ROOM ASSIGNMENT — история проживания (где жил жилец)
# ======================================================
# Жильцы переезжают между комнатами и общежитиями: уволился — выехал, появился
# новый — заехал в свободное место. До этой таблицы при изменении User.room_id
# мы теряли информацию «когда уехал, куда». Это ломало:
#   * квитанции за прошлые периоды (на чьё имя выставлять);
#   * аналитику «текучка по общежитию»;
#   * расчёт частичного месяца при переезде в середине периода.
#
# Заводится одна запись при каждом переезде: открытая (moved_out_at IS NULL) =
# текущее место, закрытая (moved_out_at = дата выселения) = архив.
# Для каждого жильца в любой момент времени активна РОВНО ОДНА открытая запись
# (либо ни одной — если он уволен / не назначен в комнату).
class RoomAssignment(Base):
    __tablename__ = "room_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    moved_in_at = Column(DateTime, nullable=False, default=_utcnow)
    moved_out_at = Column(DateTime, nullable=True)  # NULL = до сих пор живёт
    note = Column(Text, nullable=True)              # «уволен», «переезд», «в декрете»

    user = relationship("User", foreign_keys=[user_id])
    room = relationship("Room", foreign_keys=[room_id])

    __table_args__ = (
        # Поиск «активного» назначения жильца: WHERE user_id=X AND moved_out_at IS NULL.
        Index("idx_assignment_user_active", "user_id", "moved_out_at"),
        # «Кто жил в комнате X в период Y»: фильтр по room_id + dates.
        Index("idx_assignment_room_dates", "room_id", "moved_in_at", "moved_out_at"),
    )


# ======================================================
# APP RELEASE — версии мобильного приложения
# ======================================================
# Хранит метаданные APK-релизов, выложенных через админку.
# Сами файлы лежат в static/apps/<filename> — отдаются nginx'ом напрямую.
#
# Модель публикации:
# - Админ загружает APK с указанием версии и release notes.
# - is_published управляется админом — недопубликованные не видны клиенту.
# - "Текущая" версия для клиента — последний is_published=True по version_code DESC.
# - Если задано min_required_version_code, и у клиента version_code меньше —
#   приложение покажет force-update диалог без возможности отложить.
# ======================================================
class AppRelease(Base):
    __tablename__ = "app_releases"

    id = Column(Integer, primary_key=True, index=True)

    # Семантическая версия (отображаемая) — "1.2.3"
    version = Column(String, nullable=False)
    # Числовое представление для сравнения — 10203 (1*10000+2*100+3)
    # Удобно использовать как Android versionCode.
    version_code = Column(Integer, nullable=False)

    # Если у клиента version_code < этой — force-update.
    min_required_version_code = Column(Integer, nullable=True)

    # Файл
    platform = Column(String, default="android", nullable=False, index=True)
    file_name = Column(String, nullable=False)        # имя файла в static/apps/
    file_size = Column(Integer, nullable=False)       # байты
    file_hash = Column(String, nullable=True)         # SHA-256 для проверки целостности

    release_notes = Column(Text, nullable=True)
    is_published = Column(Boolean, default=False, nullable=False, index=True)

    created_at = Column(DateTime, default=_utcnow, index=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        Index("idx_app_release_platform_published", "platform", "is_published", "version_code"),
    )