# app/modules/utility/models.py

from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    Boolean,
    Index,
    DateTime,
    Date,
    Text,
    Enum as SAEnum,
    func
)
from sqlalchemy.types import Numeric
from sqlalchemy.orm import relationship, validates
from datetime import datetime, timezone

from app.core.database import Base
from sqlalchemy.dialects.postgresql import JSONB
from enum import Enum as PyEnum


# =====================================================================
# Native PG ENUM-типы (см. миграцию housing_001_place_type).
#
# Использование Python Enum (а не голых строк) критично для корректного
# каста параметров в SQL. С чистым `SAEnum("dormitory", "house", ...)`
# asyncpg шлёт WHERE-параметр как VARCHAR без cast'а, и Postgres ругается
# `operator does not exist: place_type_enum = varchar` — это валило
# /api/rooms/dormitories и /api/rooms/streets на проде после E1-деплоя
# (28.05.2026).
#
# Python Enum-классы наследуют `str` чтобы значения сохранялись в
# человекочитаемом виде ('dormitory', 'house') как в БД, и литералы
# в коде (`Room.place_type == "dormitory"`) продолжали работать.
# =====================================================================

class PlaceType(str, PyEnum):
    DORMITORY = "dormitory"
    HOUSE = "house"


class TariffApplicableTo(str, PyEnum):
    DORMITORY = "dormitory"
    HOUSE = "house"
    BOTH = "both"


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

    # ─────────────────────────────────────────────────────────────────
    # Тип помещения. Решает форму адреса, правила начисления, наличие
    # счётчиков, разрешён ли per_capita-жилец и т.д.
    #   'dormitory' — общежитие (dormitory_name + room_number, счётчики,
    #                  семейные + холостяки).
    #   'house'     — дом / квартира (street + house_number + apartment_number,
    #                  без счётчиков, только семейные, тариф начисляет
    #                  обычно только найм через charge_* флаги).
    # Партиц-индекс уникальности (миграция housing_001_place_type)
    # обеспечивает что одинаковые адреса в рамках одного типа запрещены,
    # но «комната 101» в общаге и «квартира 101» в доме на одной улице
    # могут сосуществовать.
    # ─────────────────────────────────────────────────────────────────
    # Native PG ENUM (создаётся миграцией housing_001_place_type как
    # `CREATE TYPE place_type_enum AS ENUM ('dormitory', 'house')`).
    # ВАЖНО: используем Python Enum-класс (PlaceType), а не голые строки.
    # С голыми строками asyncpg шлёт параметр как VARCHAR без явного
    # cast'а, и PG падает с `operator does not exist: place_type_enum =
    # varchar` (инцидент на проде 28.05.2026 после E1-деплоя).
    # values_callable=lambda e: [v.value for v in e] — кладёт в БД
    # value ('dormitory'), а не name ('DORMITORY').
    place_type = Column(
        SAEnum(
            PlaceType,
            name="place_type_enum",
            native_enum=True,
            create_type=False,
            values_callable=lambda e: [v.value for v in e],
        ),
        default=PlaceType.DORMITORY, nullable=False,
        server_default=PlaceType.DORMITORY.value, index=True,
    )

    # Адресные поля «общажного» типа. Для place_type='house' — None.
    dormitory_name = Column(String, index=True, nullable=True)
    room_number = Column(String, index=True, nullable=True)

    # Адресные поля «домового» типа. Для place_type='dormitory' — None.
    # Обязательность тройки гарантирует CHECK-constraint
    # ck_rooms_address_matches_place_type (см. housing_001 миграцию).
    street = Column(String(200), nullable=True, index=True)
    house_number = Column(String(50), nullable=True)
    apartment_number = Column(String(50), nullable=True)

    apartment_area = Column(Numeric(10, 2), default=0.00)
    total_room_residents = Column(Integer, default=1)

    # Серийники счётчиков. Используются только для общаг (для домов —
    # начислений по счётчикам нет, серийники не задаются). UI скрывает
    # эти поля для place_type='house', schemas-валидатор игнорирует.
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

    # Bug X: статус «вакантная» — комната без жильцов. При переезде
    # последнего жильца автоматически ставится True. Комната НЕ удаляется
    # (история reading'ов остаётся, чтобы новый жилец видел показания
    # счётчика на момент въезда).
    is_vacant = Column(Boolean, default=False, nullable=False, index=True)

    # Bug AS: «холостяцкая» квартира — здесь живут несколько холостяков,
    # которые делят все коммунальные счета поровну. Каждый жилец получает
    # свою квитанцию = (расчёт по квартире) / total_room_residents.
    # Для семейных квартир флаг False — поведение прежнее (один user
    # с residents_count платит полную сумму).
    is_singles_apartment = Column(Boolean, default=False, nullable=False,
                                   server_default="false", index=True)
    # Максимальная вместимость (2-шка / 3-шка / 4-комнатная и т.д.).
    # Опциональное информационное поле — используется в UI и для будущей
    # валидации «нельзя зарегистрировать больше людей чем вмещает».
    max_capacity = Column(Integer, nullable=True)

    # Уникальность адресов внутри типа обеспечивается partial unique
    # индексами в миграции housing_001_place_type (uq_room_dorm_addr /
    # uq_room_house_addr) — здесь декларативно не описываем, потому что
    # SQLAlchemy partial-index с WHERE без хака труднее читается. Источник
    # правды — миграция.

    # =====================================================================
    # Адресные helper'ы (housing_001 / E2-A).
    #
    # До рефакторинга 16 мест в Python и 1 шаблон + JS склеивали адрес
    # вручную как f"{dormitory_name}, ком. {room_number}". С появлением
    # place_type='house' (адрес = street/house_number/apartment_number)
    # эти формулы стали выдавать None/пустоту для домов.
    #
    # Используй .format_address — это «канонический» долгий формат для
    # квитанций/PDF/отчётов. .short_address — для компактных списков
    # (без префикса корпуса). Логика инкапсулирована здесь, чтобы новые
    # типы помещений в будущем не требовали гонять find-and-replace.
    # =====================================================================
    @property
    def format_address(self) -> str:
        """Канонический полный адрес для отчётов/PDF/квитанций.

        dormitory → "<dorm>, ком. <room>" (исторический формат).
        house     → "ул. <street>, д. <house_number>, кв. <apartment_number>".
        Пустые поля корректно обходим (для legacy-данных без place_type
        тоже отработает — fallback на dormitory-формат).
        """
        if self.place_type == PlaceType.HOUSE.value:
            parts: list[str] = []
            if self.street:
                parts.append(f"ул. {self.street}")
            if self.house_number:
                parts.append(f"д. {self.house_number}")
            if self.apartment_number:
                parts.append(f"кв. {self.apartment_number}")
            return ", ".join(parts) if parts else "Адрес дома не указан"
        # dormitory (default)
        dorm = self.dormitory_name or "Общежитие не указано"
        room = self.room_number or "?"
        return f"{dorm}, ком. {room}"

    @property
    def short_address(self) -> str:
        """Короткий вариант адреса — без префикса дома/общаги.

        dormitory → "ком. <room>" (только номер комнаты, корпус опущен).
        house     → "кв. <apartment_number>" (только номер квартиры).
        Используется в местах где корпус/улица уже выведены отдельно.
        """
        if self.place_type == PlaceType.HOUSE.value:
            return f"кв. {self.apartment_number}" if self.apartment_number else "—"
        return f"ком. {self.room_number}" if self.room_number else "—"

    @property
    def address_dedup_key(self) -> str:
        """Стабильный ключ для словарей-дедупликаторов адресов.

        Раньше excel_service строил ключи вида
        `f"{r.dormitory_name}_{r.room_number}"` — для дома это давало
        "None_None". Этот property возвращает уникальный детерминированный
        ключ независимо от типа помещения.
        """
        if self.place_type == PlaceType.HOUSE.value:
            return f"H::{self.street}|{self.house_number}|{self.apartment_number}"
        return f"D::{self.dormitory_name}|{self.room_number}"


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

    # Флаги наличия счётчиков. Если у жильца НЕТ какого-то счётчика
    # (например, нет электросчётчика в общежитии-блоке), то:
    #  - анализатор не флагит «не подал X»
    #  - в calculate_utilities потребление считается как
    #    norm_per_capita × residents_count из тарифа (если norm > 0)
    # По умолчанию все True — старое поведение (счётчики у всех).
    has_hw_meter = Column(Boolean, default=True, nullable=False)
    has_cw_meter = Column(Boolean, default=True, nullable=False)
    has_el_meter = Column(Boolean, default=True, nullable=False)

    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True)
    room = relationship("Room", backref="users")

    tariff_id = Column(Integer, ForeignKey("tariffs.id"), nullable=True)
    tariff = relationship("Tariff")

    totp_secret = Column(String, nullable=True)

    is_deleted = Column(Boolean, default=False, index=True)
    is_initial_setup_done = Column(Boolean, default=False)

    # Brute-force защита. Инкрементируется при неверном пароле;
    # при достижении порога (3) выставляется locked_until = now + 15 мин.
    # Сбрасывается на 0 при успешном входе.
    failed_login_count = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)

    # НОВОЕ: Отслеживание последнего изменения
    updated_at = Column(DateTime, nullable=True, onupdate=_utcnow)

    # Согласие на обработку ПД (152-ФЗ). Фиксируем сам факт согласия —
    # когда, с какого IP и на какую версию политики. См. миграцию
    # pdn_001_consent и константу PDN_CURRENT_VERSION в services/pdn_consent.
    # NULL = согласие не давалось → при следующем входе попросим подписать.
    # При обновлении политики (bump версии) — попросим переподписать.
    pdn_consent_at = Column(DateTime, nullable=True)
    pdn_consent_ip = Column(String(45), nullable=True)
    pdn_consent_version = Column(String(10), nullable=True)

    # Версия токенов для отзыва сессий (см. миграцию token_001_version).
    # JWT содержит claim `tv: <значение>`. При decode сравниваем — если
    # не совпадает (потому что после выдачи токена админ сделал logout
    # или сменил пароль), 401. Старт с 0, инкрементируется при logout
    # / change-password / pdn-consent-revoke.
    token_version = Column(Integer, default=0, nullable=False, server_default="0")

    # ======================================================
    # Паспорт и личные данные — нужны для заказа справок
    # (выписка из ФЛС и др.). Все nullable — жилец заполняет
    # при первом заказе, админ может поправить.
    # ======================================================
    full_name = Column(String(255), nullable=True)
    position = Column(String(255), nullable=True)
    passport_series = Column(String(20), nullable=True)
    passport_number = Column(String(20), nullable=True)
    passport_issued_by = Column(String(500), nullable=True)
    passport_issued_at = Column(Date, nullable=True)
    registration_date = Column(Date, nullable=True)
    # Адрес прописки по паспорту — отдельно от адреса комнаты, т.к. они
    # могут не совпадать (многие прописаны по другому адресу, а в общежитии
    # проживают по договору найма). Нужен для справок.
    registration_address = Column(String(500), nullable=True)
    # «Проживаю один» — альтернатива обязательному списку членов семьи.
    # Если True — в справке будет только сам наниматель, без таблицы семьи.
    # По умолчанию False — старое поведение, семья опциональна (но при заказе
    # теперь проверяется: либо lives_alone, либо хотя бы один полностью
    # заполненный FamilyMember).
    lives_alone = Column(Boolean, default=False, nullable=False, server_default="false")

    # Bug BB: запрос на актуализацию данных. Админ выставляет
    # data_refresh_required=True (например после массового аудита). Жилец
    # при следующем входе в моб-приложение видит popup один раз, отвечает,
    # popup отправляет submission в data_refresh_submissions, флаг
    # сбрасывается. Подробности — миграция data_refresh_001.
    data_refresh_required = Column(Boolean, default=False, nullable=False,
                                     server_default="false", index=True)
    data_refresh_requested_at = Column(DateTime, nullable=True)

    # --------------------------------------------------------------
    # Валидаторы консистентности — срабатывают на setattr.
    # Защищают от ситуации когда поля рассыпаются («single» с 5 жильцами
    # в residents_count, или family на per_capita).
    # Тихие автокоррекции вместо exception — не ломаем старые скрипты
    # импорта, но приводим данные в корректный вид.
    # --------------------------------------------------------------
    @validates("residents_count")
    def _v_residents_count(self, key, value):
        # Одиночка = 1 человек по определению. Если кто-то пытается поставить 5,
        # молча исправляем до 1 и логируем (через print, логгер здесь не нужен,
        # SQLAlchemy ORM events не лучшее место для логирования).
        if getattr(self, "resident_type", None) == "single" and value not in (None, 1):
            return 1
        return value

    @validates("resident_type")
    def _v_resident_type(self, key, value):
        # При смене на single сбрасываем count до 1 и billing_mode на per_capita.
        if value == "single":
            if getattr(self, "residents_count", None) not in (None, 1):
                self.residents_count = 1
            if getattr(self, "billing_mode", None) != "per_capita":
                self.billing_mode = "per_capita"
        elif value == "family":
            # Смена на family: авто-установка by_meter (но только если был per_capita).
            if getattr(self, "billing_mode", None) == "per_capita":
                self.billing_mode = "by_meter"
        return value

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

    # Нормативы потребления на 1 человека в месяц для случаев когда у
    # жильца НЕТ счётчика (User.has_X_meter=False). Когда счётчик есть —
    # используются показания, эти поля игнорируются. Если норматив = 0,
    # потребление считается 0 (т.е. жильцу без счётчика ничего не
    # начисляется за этот ресурс).
    hw_norm_per_capita = Column(Numeric(10, 3), default=0.0, nullable=False)  # м³ ГВС/чел/мес
    cw_norm_per_capita = Column(Numeric(10, 3), default=0.0, nullable=False)  # м³ ХВС/чел/мес
    el_norm_per_capita = Column(Numeric(10, 3), default=0.0, nullable=False)  # кВт·ч/чел/мес
    # Коэффициент-множитель к нормативу для жильцов, не подающих показания
    # 4+ месяцев подряд (санкция за длительное игнорирование). См.
    # миграцию tariffs_norm_001_coefficient. Default 3.0 — типовая
    # санкция в РФ ЖКХ. На обычное начисление (has_X_meter=False) НЕ
    # влияет — используется только в billing.close_current_period
    # для long-term defaulters.
    norm_coefficient = Column(Numeric(5, 2), default=3.0, nullable=False)

    # Тип тарифа: 'family' (для семей и обычных жильцов с счётчиками) или
    # 'singles' (для коммунальных квартир с холостяками — billing_mode=per_capita).
    # Метка для UI (другой цвет в селекторе) и отчётов «Жильцы → Холостяки».
    # На расчёт НЕ влияет — расчёт смотрит на user.billing_mode + tariff.per_capita_amount.
    # См. миграцию tariffs_type_001_family_singles.
    tariff_type = Column(String(20), default="family", nullable=False, server_default="family")

    # Bug AS+: фильтрация тарифов по типу помещения.
    # 'dormitory' — только для Room.place_type='dormitory' (общаги).
    # 'house'     — только для Room.place_type='house' (дома/квартиры,
    #               обычно с charge_*=False за исключением social_rent).
    # 'both'      — обратная совместимость / универсальный тариф.
    # На начислении напрямую НЕ влияет (это делают charge_*-флаги),
    # фильтрует только селектор тарифа в UI Жилфонда. См. миграцию
    # housing_001_place_type.
    # Native PG ENUM (`tariff_applicable_to_enum`) — то же что и для
    # Room.place_type, см. подробный комментарий там. Python Enum-класс
    # необходим для корректного каста asyncpg-параметров.
    applicable_to = Column(
        SAEnum(
            TariffApplicableTo,
            name="tariff_applicable_to_enum",
            native_enum=True,
            create_type=False,
            values_callable=lambda e: [v.value for v in e],
        ),
        default=TariffApplicableTo.BOTH, nullable=False,
        server_default=TariffApplicableTo.BOTH.value, index=True,
    )

    # Дата вступления в силу. Если задана в будущем — тариф "запланирован" (is_active=False)
    # и автоматически активируется Celery-задачей в эту дату.
    effective_from = Column(DateTime, nullable=True, index=True)

    # =====================================================================
    # СЕЗОННОСТЬ (см. миграцию tariffs_seasonal_002_per_tariff).
    # Каждый тариф знает, активна ли статья «отопление» и «подогрев ГВС»
    # на сегодня. Раньше это были два глобальных SystemSetting на всю
    # систему — теперь per-tariff (разные общежития могут иметь разные
    # графики поставщиков тепла).
    #
    # heating_active=True + start=end=NULL → круглогодично (на всякий случай
    # можно вручную выключить через heating_active=False).
    # heating_active=True + start/end заданы → активен только когда
    # сегодняшняя MM-DD попадает в диапазон (year игнорируется).
    # heating_active=False → не начисляется никогда, что бы ни было в датах.
    #
    # Глобальные SystemSetting heating_season_active / hot_water_heating_active
    # сохраняются как EMERGENCY override: если они false, отключаются
    # все тарифы сразу. Это страховочный «stop» для админа.
    # =====================================================================
    # Bug AP: добавлен Python-default=True. server_default="true" работает
    # только при INSERT в БД; для in-memory объектов (preview-калькулятор,
    # тесты, временные расчёты) атрибут оставался None, что в
    # is_hw_heating_active_now() трактовалось как «выключено» → ГВС считался
    # без подогрева (3 × 63.83 вместо 3 × (63.83 + 244.14)).
    heating_active = Column(Boolean, nullable=False, default=True, server_default="true")
    heating_season_start = Column(Date, nullable=True)
    heating_season_end = Column(Date, nullable=True)
    hw_heating_active = Column(Boolean, nullable=False, default=True, server_default="true")
    hw_heating_season_start = Column(Date, nullable=True)
    hw_heating_season_end = Column(Date, nullable=True)

    # Bug AS: skip-флаги «какие компоненты НЕ начисляются для
    # холостяцких квартир» (room.is_singles_apartment=True). Default
    # False — никаких изменений для существующих тарифов. Админ
    # включает по необходимости в UI тарифа. Счётчики (ГВС/ХВС/электр)
    # СКЁТЧЕТЫВАЮТСЯ ВСЕГДА — это потребление, не статья.
    singles_skip_maintenance = Column(Boolean, nullable=False,
                                        default=False, server_default="false")
    singles_skip_social_rent = Column(Boolean, nullable=False,
                                        default=False, server_default="false")
    singles_skip_heating = Column(Boolean, nullable=False,
                                    default=False, server_default="false")
    singles_skip_waste = Column(Boolean, nullable=False,
                                  default=False, server_default="false")

    # Bug AT: положительные флаги «что начисляет этот тариф». Default
    # True (всё начисляется) — zero-impact на существующие тарифы.
    # Применимы ДЛЯ ВСЕХ жильцов на этом тарифе (не только холостяки).
    # Снять галочку = статья НЕ начисляется никому. Пресеты в UI:
    # «Лидер» (всё) / «Только наём» (только social_rent) /
    # «Без счётчиков» (все meter-флаги выключены).
    #
    # Если все 4 счётчика выключены — клиентский UI не показывает форму
    # подачи показаний (см. /api/readings/state.submission_required).
    charge_hot_water = Column(Boolean, nullable=False, default=True, server_default="true")
    charge_cold_water = Column(Boolean, nullable=False, default=True, server_default="true")
    charge_sewage = Column(Boolean, nullable=False, default=True, server_default="true")
    charge_electricity = Column(Boolean, nullable=False, default=True, server_default="true")
    charge_maintenance = Column(Boolean, nullable=False, default=True, server_default="true")
    charge_social_rent = Column(Boolean, nullable=False, default=True, server_default="true")
    charge_heating = Column(Boolean, nullable=False, default=True, server_default="true")
    charge_waste = Column(Boolean, nullable=False, default=True, server_default="true")

    # Отслеживание последнего изменения
    updated_at = Column(DateTime, nullable=True, onupdate=_utcnow)

    # ------------------------------------------------------------------
    # Хелперы для расчёта: «активна ли статья на сегодня?»
    # ------------------------------------------------------------------
    @staticmethod
    def _in_season(start, end, today) -> bool:
        """True если today.MM-DD попадает в [start.MM-DD, end.MM-DD].
        Год полностью игнорируется. Диапазон может переходить через
        Новый год (start > end по календарю): 15.10 → 15.04.
        """
        if start is None and end is None:
            return True
        if start is None or end is None:
            return True
        s = (start.month, start.day)
        e = (end.month, end.day)
        t = (today.month, today.day)
        if s <= e:
            return s <= t <= e
        # Перетёк через Новый год.
        return t >= s or t <= e

    def is_heating_active_now(self, today=None) -> bool:
        """Применяется ли статья «отопление» при расчёте на сегодня."""
        from datetime import date as _date
        if not self.heating_active:
            return False
        today = today or _date.today()
        return self._in_season(self.heating_season_start, self.heating_season_end, today)

    def is_hw_heating_active_now(self, today=None) -> bool:
        """Применяется ли статья «подогрев ГВС» на сегодня."""
        from datetime import date as _date
        if not self.hw_heating_active:
            return False
        today = today or _date.today()
        return self._in_season(self.hw_heating_season_start, self.hw_heating_season_end, today)


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

    # Bug V: обороты за период из ОСВ 1С — для показа «движения средств»
    # в админке. obor_debit = доначисления (новые суммы), obor_credit =
    # поступления (что заплатил жилец). Сальдо конец = start_d - start_c
    # + obor_d - obor_c, его храним в debt/overpayment_*. Эти 4 колонки —
    # для визуализации движения, не для расчётов.
    obor_debit_209 = Column(Numeric(12, 2), nullable=True)
    obor_credit_209 = Column(Numeric(12, 2), nullable=True)
    obor_debit_205 = Column(Numeric(12, 2), nullable=True)
    obor_credit_205 = Column(Numeric(12, 2), nullable=True)

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
# DATA REFRESH SUBMISSION (Bug BB)
# ======================================================
class DataRefreshSubmission(Base):
    """Ответ жильца на запрос актуализации данных.

    Сценарий: админ выставил User.data_refresh_required=True →
    жилец в моб-приложении видит popup и отправляет: общежитие, комната,
    кол-во проживающих. Сохраняем как submission (history) — НЕ применяем
    автоматически к User/Room (защита от ошибочных ответов).

    Админ смотрит submissions, сравнивает с системой и при расхождении
    правит вручную через стандартный flow редактирования жильца/комнаты.
    """
    __tablename__ = "data_refresh_submissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    # Когда админ запросил данные. NULL если жилец сам отправил без запроса.
    requested_at = Column(DateTime, nullable=True)
    # Что прислал жилец — свободные строки (мог переехать в другое общежитие).
    dorm_name = Column(String(200), nullable=False)
    room_number = Column(String(50), nullable=False)
    residents_count = Column(Integer, nullable=False)
    submitted_at = Column(DateTime, default=_utcnow, nullable=False, index=True)

    user = relationship("User", backref="data_refresh_submissions")


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

    # Ссылка на созданный MeterReading (если approved).
    # FK на readings нет на уровне БД (readings — партиционированная
    # таблица, PG это не любит), оставляем ORM-FK для удобства SQLAlchemy
    # и валидации в коде. См. миграцию gsheets_001_import_rows.
    # Отвязывание при удалении reading делается явно в коде —
    # admin_readings_manual.delete_reading.
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


# ======================================================
# RECALC JOB — полный перерасчёт показаний за период
# ======================================================
class RecalcJob(Base):
    """Задача «Перерасчёт периода» со статусом и агрегированным diff.

    Жизненный цикл:
        preview_pending → preview_ready → apply_pending → done
                                       ↘ cancelled
                                       ↘ failed

    diff_summary хранит JSON-структуру для UI-модалки:
        {
            "total": 1234,
            "unchanged": 40, "increased": 800, "decreased": 394,
            "sum_old": "120000.00", "sum_new": "135000.00",
            "delta": "+15000.00",
            "top": [{"reading_id":..., "username":..., "old_total":..., "new_total":..., "delta":...}, ...]
        }
    """
    __tablename__ = "recalc_jobs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    period_id = Column(Integer, ForeignKey("periods.id", ondelete="CASCADE"), nullable=False)
    period = relationship("BillingPeriod")

    # preview_pending | preview_ready | apply_pending | done | failed | cancelled
    status = Column(String(24), nullable=False, default="preview_pending")

    # 0-100
    progress = Column(Integer, nullable=False, default=0)
    total_readings = Column(Integer, nullable=False, default=0)
    processed = Column(Integer, nullable=False, default=0)

    diff_summary = Column(JSONB, nullable=True)

    started_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    started_by_username = Column(String(128), nullable=True)
    celery_task_id = Column(String(64), nullable=True)

    error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    applied_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_recalc_jobs_period_created", "period_id", "created_at"),
        Index("idx_recalc_jobs_status", "status"),
    )


# ======================================================
# DEBT IMPORT LOG — история импортов долгов из 1С
# ======================================================
class DebtImportLog(Base):
    """Запись о каждом импорте Excel-сальдо из 1С.

    Жизненный цикл:
        pending → completed → (опционально reverted)
               ↘ failed

    snapshot_data хранит предыдущие значения debt_*/overpayment_* ДО
    применения импорта — нужен для отката:
        {
            "<reading_id>": {
                "debt_209": "...", "overpayment_209": "...",
                "debt_205": "...", "overpayment_205": "..."
            },
            ...
        }
    not_found_users — JSON-массив строк ФИО, которые fuzzy-матчер не
    смог привязать к жильцу. Админ может вернуться к списку и сделать
    ручную привязку (reassign).
    """
    __tablename__ = "debt_import_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    # "209" | "205"
    account_type = Column(String(8), nullable=False)

    # Период, в который шла подача (FK nullable — если период удалили, лог остаётся)
    period_id = Column(Integer, ForeignKey("periods.id", ondelete="SET NULL"), nullable=True)

    file_name = Column(String(255), nullable=True)

    # pending | completed | failed | reverted
    status = Column(String(24), nullable=False, default="pending")

    started_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    started_by_username = Column(String(128), nullable=True)
    started_by = relationship("User", foreign_keys=[started_by_id])

    processed = Column(Integer, nullable=False, default=0)
    updated = Column(Integer, nullable=False, default=0)
    created = Column(Integer, nullable=False, default=0)
    not_found_count = Column(Integer, nullable=False, default=0)

    not_found_users = Column(JSONB, nullable=True)
    snapshot_data = Column(JSONB, nullable=True)

    error = Column(Text, nullable=True)

    started_at = Column(DateTime, default=_utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    reverted_at = Column(DateTime, nullable=True)

    # Постоянный архив оригинального xlsx — /app/data/debt_archives/{log_id}.xlsx.
    # До миграции debts_002 файл лежал в /static/temp_imports/{uuid}.xlsx и
    # мог теряться. Колонка nullable — у старых логов значения нет.
    archive_path = Column(String(512), nullable=True)
    # Индивидуальный retention в днях. None → берётся из analyzer_settings
    # (debt.archive_retention_days, default 730).
    retention_days = Column(Integer, nullable=True)
    # Группировка парных импортов: тот же UUID в обоих логах (205 + 209)
    # когда админ загрузил два файла одной операцией.
    batch_id = Column(String(36), nullable=True, index=True)
    # State ПОСЛЕ применения импорта — для быстрого diff между двумя
    # импортами того же account_type. Структура:
    #   {<room_id>: {
    #     "debt_209": "...", "overpayment_209": "...",
    #     "debt_205": "...", "overpayment_205": "...",
    #     "username": "...", "room_label": "общ./комн."
    #   }}
    # Хранится denormalized чтобы diff не делал JOIN на user/room.
    # snapshot_data — для undo (state ДО), applied_state — для analytics
    # (state ПОСЛЕ).
    applied_state = Column(JSONB, nullable=True)

    __table_args__ = (
        Index("idx_debt_import_logs_started_at", "started_at"),
        Index("idx_debt_import_logs_status", "status"),
        Index("idx_debt_import_logs_batch_id", "batch_id"),
    )


# ======================================================
# FAMILY MEMBER — члены семьи жильца
# ======================================================
class FamilyMember(Base):
    """Супруг(а), дети, другие родственники — прикрепляются к жильцу.

    Используется для генерации справок (выписка из ФЛС и др.),
    где требуется перечислить состав семьи. Свидетельства о рождении /
    паспорта можно хранить опционально (поля passport_* nullable) —
    в базовом сценарии жильцу хватит ФИО + дата рождения.
    """
    __tablename__ = "family_members"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # spouse | child | parent | other
    role = Column(String(20), nullable=False)
    full_name = Column(String(255), nullable=False)
    birth_date = Column(Date, nullable=True)
    passport_series = Column(String(20), nullable=True)
    passport_number = Column(String(20), nullable=True)
    registration_date = Column(Date, nullable=True)
    # Дата прибытия (вселения в общежитие) — попадает в таблицу проживающих
    # в справке-выписке. У нанимателя и членов семьи разные даты только
    # если они вселились в разное время.
    arrival_date = Column(Date, nullable=True)
    # Тип регистрации: permanent (по месту жительства) | temporary (по месту
    # пребывания). В справке выводится текстом «По месту жительства/пребывания».
    registration_type = Column(String(20), nullable=True)
    # Отношение к нанимателю — свободный текст («сын», «дочь», «жена»,
    # «мать»). role-поле слишком грубое (spouse/child/parent/other), а в
    # справке нужно точно как в домовой книге. Если не заполнено — при
    # генерации PDF берём расшифровку role.
    relation_to_head = Column(String(64), nullable=True)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, nullable=True, onupdate=_utcnow)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("idx_family_user", "user_id"),
    )


# ======================================================
# RENTAL CONTRACT — договор найма жилого помещения
# ======================================================
class RentalContract(Base):
    """Договор найма жилья (PDF-копия + метаданные).

    Подгружается админом или самим жильцом, хранится в MinIO по пути
    `rental_contracts/<user_id>/<uuid>.pdf`. При заказе справки
    автоматически подтягиваются поля «дата/№» из активного договора.
    Жилец может иметь несколько договоров (переезд между комнатами) —
    актуальный определяется по `is_active`.
    """
    __tablename__ = "rental_contracts"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    number = Column(String(64), nullable=True)
    signed_date = Column(Date, nullable=True)
    valid_until = Column(Date, nullable=True)

    file_s3_key = Column(String(500), nullable=True)
    file_name = Column(String(255), nullable=True)
    file_size = Column(Integer, nullable=True)

    note = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    uploaded_at = Column(DateTime, default=_utcnow, nullable=False)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (
        Index("idx_rental_user_active", "user_id", "is_active"),
    )


# ======================================================
# CERTIFICATE REQUEST — заявка на справку
# ======================================================
class CertificateRequest(Base):
    """Заявка жильца на справку (выписка из ФЛС и др.).

    Жизненный цикл: pending (заказал жилец) → generated (PDF готов) →
    delivered (админ выдал). rejected — отклонена админом.

    type сейчас поддерживает 'flc' (выписка из финансово-лицевого счёта).
    В будущем добавятся 'residency' (о проживании), 'composition' (о
    составе семьи) и т.д. — поле data (JSONB) хранит type-specific поля.
    """
    __tablename__ = "certificate_requests"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(32), nullable=False, default="flc")
    status = Column(String(16), nullable=False, default="pending")
    data = Column(JSONB, nullable=True)
    pdf_s3_key = Column(String(500), nullable=True)
    note = Column(Text, nullable=True)

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)
    processed_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    processed_by = relationship("User", foreign_keys=[processed_by_id])

    __table_args__ = (
        Index("idx_cert_user_created", "user_id", "created_at"),
        Index("idx_cert_status", "status"),
    )


# ======================================================
# SUPPORT TICKETS (обращения жильцов в техподдержку / админу).
# Простая 1-к-1 модель: один вопрос + один ответ (без многошаговых диалогов).
# При повторных вопросах жилец создаёт новый тикет. См. миграцию tickets_001.
# ======================================================
class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subject = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    # open | in_progress | answered | closed
    status = Column(String(20), nullable=False, default="open", index=True)
    admin_response = Column(Text, nullable=True)
    responded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    responded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    user = relationship("User", foreign_keys=[user_id])
    responded_by = relationship("User", foreign_keys=[responded_by_id])
