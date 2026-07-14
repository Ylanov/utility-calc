# app/modules/utility/routers/client_readings.py

import logging
from decimal import Decimal
from app.core.time_utils import utcnow

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import func

from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment, Room
from app.modules.utility.schemas import ReadingSchema
from app.modules.utility.services.calculations import calculate_utilities
from app.modules.utility.tasks import detect_anomalies_task

logger = logging.getLogger(__name__)


async def _is_submission_day_open(db: AsyncSession) -> tuple[bool, int, int, int]:
    """Возвращает (is_open, today_day, start_day, end_day).

    Окно подачи показаний — это диапазон дней месяца, заданный в
    SystemSetting (submission_start_day / submission_end_day). По
    умолчанию 20-25 (стандарт РФ). Если today.day НЕ в [start, end] —
    подача закрыта, даже если BillingPeriod.is_active=True.

    Bug 29.05.2026: раньше проверялся только `is_active` периода, без
    окна дней. Жильцы могли подавать в любой день месяца. Юзер настроил
    окно 1-28, но 29-го система всё равно писала «приём открыт» и
    принимала подачи через мобильное приложение. Фикс: добавлена эта
    функция и вызвана в /readings/state + /api/calculate.
    """
    from app.modules.utility.models import SystemSetting
    from datetime import date as _date

    start_row = await db.get(SystemSetting, "submission_start_day")
    end_row = await db.get(SystemSetting, "submission_end_day")

    def _safe_int(v, default: int) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    # Дефолт — московский стандарт: с 15 числа по 3 число СЛЕДУЮЩЕГО месяца.
    start_day = _safe_int(start_row.value if start_row else None, 15)
    end_day = _safe_int(end_row.value if end_row else None, 3)
    today_day = _date.today().day
    if start_day <= end_day:
        # Обычное окно внутри одного месяца (напр. 20–25).
        is_open = start_day <= today_day <= end_day
    else:
        # Окно ПЕРЕХОДИТ через границу месяца (напр. 15 → 3 следующего):
        # открыто с start_day до конца месяца И с 1-го по end_day.
        is_open = today_day >= start_day or today_day <= end_day
    return is_open, today_day, start_day, end_day


# =========================
# SERVICE LAYER
# =========================
class ReadingService:

    @staticmethod
    def parse_input(data: ReadingSchema):
        # None пропускаем как есть: поле может отсутствовать у комнаты без
        # этого счётчика (has_*_meter=False) — обязательность проверяет
        # perform_reading_submission по флагам комнаты.
        def _cv(v):
            return None if v is None else Decimal(str(v))
        try:
            return _cv(data.hot_water), _cv(data.cold_water), _cv(data.electricity)
        except Exception:
            raise HTTPException(400, "Некорректный формат данных")

    @staticmethod
    def calculate_costs(
        user: User, room: Room, tariff: Tariff,
        hot, cold, elect, p_hot, p_cold, p_elect,
        heating_season_active: bool = True,
        hot_water_heating_active: bool = True,
    ):
        d_hot = hot - p_hot
        d_cold = cold - p_cold
        d_elect = elect - p_elect
        sewage = d_hot + d_cold

        from app.modules.utility.services.calculations import paying_residents
        residents = Decimal(paying_residents(user, room))
        total = Decimal(room.total_room_residents or 1)
        if total == 0:
            total = Decimal("1")
        elect_share = (residents / total) * d_elect

        return calculate_utilities(
            user=user,
            room=room,
            tariff=tariff,
            volume_hot=d_hot,
            volume_cold=d_cold,
            volume_sewage=sewage,
            volume_electricity_share=elect_share,
            heating_season_active=heating_season_active,
            hot_water_heating_active=hot_water_heating_active,
        )


async def perform_reading_submission(
        db: AsyncSession,
        user_id: int,
        data: ReadingSchema,
) -> dict:
    """ЯДРО подачи показаний (биллинг-критично). Единый источник правды для
    резидентской ручки /api/calculate И анонимного QR-портала.

    user_id — чей лицевой счёт ведёт подачу. Для QR-портала это
    «представитель комнаты» (детерминированный активный жилец). Показания
    привязаны к КОМНАТЕ (room_id); для холостяцких квартир
    (is_singles_apartment) подача тиражируется на всех жильцов (SINGLES_SHARED).
    """
    hot, cold, elect = ReadingService.parse_input(data)

    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == user_id)
    )).scalars().first()

    # housing_001/E2-B: жильцы домов (place_type='house') не имеют
    # счётчиков. Защита на API-уровне дублирует UI (мобильный/веб-клиент
    # не должны показывать кнопку «Подать показания» для дома), но
    # отдельно блокирует случаи когда клиент устарел или запрос пришёл
    # напрямую (curl).
    from app.modules.utility.services.room_validators import (
        require_room_has_meters,
    )
    if user and user.room:
        require_room_has_meters(user.room)

    # Холостяк (per_capita) платит фикс. сумму, счётчики не передаются — отвергаем POST
    # с понятным сообщением. Иначе клиент будет «отправлять впустую» — данные не сохранятся.
    if user and getattr(user, "billing_mode", "by_meter") == "per_capita":
        raise HTTPException(
            status_code=400,
            detail=(
                "Вы оформлены на «койко-место»: показания счётчиков не подаются. "
                "Сумма к оплате фиксированная, см. в личном кабинете."
            ),
        )
    # Bug AT этап 4: если тариф жильца с charge_*-meter=False — счётчиков
    # в нём нет, POST подачи запрещён. Защита от клиента, который ещё не
    # обновился под submission_required.
    if user and user.room_id:
        from app.modules.utility.services.tariff_cache import tariff_cache
        _eff_t = tariff_cache.get_effective_tariff(user=user, room=user.room)
        if _eff_t:
            _meter_charges = any([
                bool(getattr(_eff_t, "charge_hot_water", True)),
                bool(getattr(_eff_t, "charge_cold_water", True)),
                bool(getattr(_eff_t, "charge_electricity", True)),
            ])
            if not _meter_charges:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "На вашем тарифе подача показаний счётчиков не требуется. "
                        "Сумма к оплате фиксированная (см. квитанцию)."
                    ),
                )

    if not user or not user.room_id:
        raise HTTPException(status_code=400, detail="Вы не привязаны к помещению для подачи показаний.")

    # Какие счётчики у комнаты есть физически (приоритет комнаты, fallback
    # на жильца — та же логика, что _has_meter в calculate_utilities).
    # Отсутствующий счётчик НЕ требуем и НЕ валидируем: его значение ниже
    # подставится = prev (расход 0). Биллинг при has_*=False всё равно
    # считает объём по нормативу тарифа, а реальные цифры вносит
    # электрик/админ вручную через админку. Кейс: дом «только вода» —
    # QR-портал спрашивает 2 счётчика, электричество не требует.
    def _need_meter(attr: str) -> bool:
        rv = getattr(user.room, attr, None) if user.room else None
        return bool(rv) if rv is not None else bool(getattr(user, attr, True))
    need = {
        "hot_water": _need_meter("has_hw_meter"),
        "cold_water": _need_meter("has_cw_meter"),
        "electricity": _need_meter("has_el_meter"),
    }

    # Проверка raw-формата (если включён 5_3_strict — жёсткий 5+3).
    # Делается ДО parse_input, чтобы вернуть жильцу конкретную ошибку
    # «не 8 цифр» вместо «некорректный формат данных». Только для
    # счётчиков, которые у комнаты есть.
    from app.modules.utility.models import SystemSetting
    from app.modules.utility.services.reading_validators import validate_raw_format
    fmt_row = await db.get(SystemSetting, "meter_format_hint")
    fmt = (fmt_row.value if fmt_row else "5_3_strict")
    if fmt == "5_3_strict":
        for name, raw in [
            ("hot_water", data.hot_water),
            ("cold_water", data.cold_water),
            ("electricity", data.electricity),
        ]:
            if not need[name]:
                continue
            # raw_input приходит как Pydantic-validated number или строка;
            # приводим к str для проверки на pattern.
            err = validate_raw_format(str(raw) if raw is not None else None, fmt)
            if err:
                raise HTTPException(400, f"{name}: {err}")

    room = user.room

    # 1. ЗАПРОСЫ. Тариф берём из in-memory кеша по правильному приоритету
    # (Room.tariff_id → User.tariff_id → default), без обращения к БД.
    from app.modules.utility.services.tariff_cache import tariff_cache
    period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()
    if not period:
        raise HTTPException(400, "Расчетный период закрыт")

    # Окно подачи показаний (бухгалтерская настройка submission_start_day /
    # submission_end_day). Если сегодня вне окна — отказываем с понятным
    # сообщением. Bug 29.05.2026: ранее проверки не было, жильцы подавали
    # 29-30 числа когда окно уже закрыто (1-28).
    _day_open, _today_day, _start_day, _end_day = await _is_submission_day_open(db)
    if not _day_open:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Приём показаний за этот период закрыт. "
                f"Сегодня {_today_day} число, окно подачи: с {_start_day} "
                f"по {_end_day} число месяца. Подайте показания в "
                f"следующем расчётном периоде."
            ),
        )
    tariff = tariff_cache.get_effective_tariff(user=user, room=room)
    if not tariff:
        # Кеш пуст / БД ещё не сидирована — fallback на любой активный тариф.
        tariff = (await db.execute(
            select(Tariff).where(Tariff.is_active)
        )).scalars().first()
    if not tariff:
        raise HTTPException(500, "Тариф не найден")

    # 2. ИСПРАВЛЕНИЕ race condition: используем SELECT FOR UPDATE чтобы заблокировать
    # черновики на время транзакции. Два соседа не смогут одновременно создать дубль.
    draft_rows = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.room_id == user.room_id,
            MeterReading.period_id == period.id,
            MeterReading.is_approved.is_(False)
        )
        .with_for_update()  # блокировка строк на время транзакции
    )).scalars().all()

    # Свой черновик (вкл. debt-черновик 1С без показаний) — обновим его.
    draft = next((d for d in draft_rows if d.user_id == user.id), None)

    # Чужой черновик блокирует подачу ТОЛЬКО если это реальная подача соседа
    # (есть показания). Debt-черновик 1С (все счётчики NULL) «Выгрузить долги»
    # создаёт на ЛЮБОГО жильца комнаты из выгрузки — он носит только сальдо
    # и подачу представителя комнаты блокировать не должен (fix 2026-07-14;
    # раньше .first() мог вернуть его → ложное «уже переданы другим жильцом»).
    foreign_real = next(
        (d for d in draft_rows
         if d.user_id != user.id
         and (d.hot_water is not None or d.cold_water is not None
              or d.electricity is not None)),
        None,
    )
    if foreign_real is not None:
        raise HTTPException(
            status_code=400,
            detail="Показания для вашей комнаты уже переданы другим жильцом."
        )

    # Дедуп (аудит 2026-07-14, инцидент «Мороз»): если в периоде УЖЕ ЕСТЬ
    # утверждённое НАСТОЯЩЕЕ показание комнаты — подача закрыта. Раньше это
    # проверял только фронт портала (state.approved), а сам эндпоинт молча
    # создавал ВТОРОЙ reading → два утверждённых на период → нулевые дельты.
    # «Настоящее» = с показаниями и не служебное (замена счётчика
    # METER_CLOSED/METER_REPLACEMENT, разовое начисление ONE_TIME_CHARGE,
    # квитанция-сальдо MANUAL_RECEIPT — они живут рядом с подачей законно).
    _approved_rows = (await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == user.room_id,
            MeterReading.period_id == period.id,
            MeterReading.is_approved.is_(True),
        )
    )).scalars().all()

    def _real_approved(r) -> bool:
        has_meters = (r.hot_water is not None or r.cold_water is not None
                      or r.electricity is not None)
        fl = r.anomaly_flags or ""
        return has_meters and not any(
            m in fl for m in ("METER_", "ONE_TIME_CHARGE", "MANUAL_RECEIPT")
        )

    if any(_real_approved(r) for r in _approved_rows):
        raise HTTPException(
            status_code=400,
            detail=(
                "Показания за этот период уже проверены и приняты бухгалтерией. "
                "Изменить их может только администратор."
            ),
        )

    # 3. Корректировки периода (долги/скидки).
    adj_res = await db.execute(
        select(Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == user.id, Adjustment.period_id == period.id)
        .group_by(Adjustment.account_type)
    )

    adj_map = {a[0]: (a[1] or Decimal("0.00")) for a in adj_res.all()}

    # 4. Предыдущие реальные показания — ЕДИНЫЙ канонический выбор
    # (аудит 2026-07-14): find_prev_reading — хронология по ИМЕНИ периода
    # (ретроактивные периоды не ломают дельту), is_meaningful_prev,
    # приоритет METER_REPLACEMENT текущего месяца (замена счётчика).
    from app.modules.utility.services.reading_calculator import find_prev_reading
    prev_latest, prev_any, _hist = await find_prev_reading(
        db, user_id=user.id, room_id=user.room_id,
        target_period_name=period.name,
    )

    zero = Decimal("0.000")

    p_hot = prev_latest.hot_water if prev_latest else zero
    p_cold = prev_latest.cold_water if prev_latest else zero
    p_elect = prev_latest.electricity if prev_latest else zero

    # Отсутствующие у комнаты счётчики: значение = prev (дельта 0, монотонность
    # не ломается; объём биллинг и так берёт по нормативу). Требуемые без
    # значения — понятная 400 (а не TypeError в расчёте).
    if not need["hot_water"]:
        hot = p_hot or zero
    if not need["cold_water"]:
        cold = p_cold or zero
    if not need["electricity"]:
        elect = p_elect or zero
    for _name, _val in [("hot_water", hot), ("cold_water", cold), ("electricity", elect)]:
        if need[_name] and _val is None:
            raise HTTPException(400, f"{_name}: значение не задано")

    # synth-baseline: meaningful prev отсутствует, но какая-то AUTO_GENERATED
    # запись была. Тогда дельту от 0 проверяем строже, чтобы не пропустить
    # кейс Пегарькова (значения 161/340 поверх AUTO_GENERATED 0/0/0).
    _prev_is_synth = (prev_latest is None) and (prev_any is not None)
    if _prev_is_synth:
        _val_prev_hot, _val_prev_cold, _val_prev_elect = (
            prev_any.hot_water or zero, prev_any.cold_water or zero, prev_any.electricity or zero,
        )
    elif prev_latest is not None:
        _val_prev_hot, _val_prev_cold, _val_prev_elect = p_hot, p_cold, p_elect
    else:
        _val_prev_hot = _val_prev_cold = _val_prev_elect = None

    # Единая валидация (см. reading_validators.py). Раньше тут была только
    # проверка монотонности — этого недостаточно: жилец мог ввести 99 999
    # м³ воды и оно проходило, calculate_utilities дисциплинированно
    # умножал на тариф и получал миллионы. Теперь валидатор ловит overflow,
    # отрицательные значения, и аномально большие месячные дельты.
    from app.modules.utility.services.reading_validators import validate_meter_reading
    is_baseline = prev_latest is None and not _prev_is_synth
    vresult = validate_meter_reading(
        hot=hot, cold=cold, elect=elect,
        prev_hot=_val_prev_hot, prev_cold=_val_prev_cold, prev_elect=_val_prev_elect,
        is_baseline=is_baseline,
        prev_is_synth=_prev_is_synth,
    )
    if not vresult.ok:
        raise HTTPException(400, "; ".join(vresult.errors))

    # 5. Расчёт стоимостей.
    # BASELINE: если у комнаты НЕТ ни одного утверждённого показания раньше —
    # это первая в жизни подача. Счётчики уже могут быть «накрученные» за годы
    # (45000 ГВС и т.п.), считать дельту от нуля нельзя: получится квитанция
    # на сотни тысяч. Поэтому первую подачу регистрируем как baseline —
    # все cost_* = 0, дельт нет; реальные расчёты пойдут со следующего месяца.
    # Идентично логике approve_single / bulk_approve_drafts / _recalc_compute_one.
    # is_baseline уже посчитан выше (в блоке валидации).
    ZERO_MONEY = Decimal("0.00")
    if is_baseline:
        # Bug L: area-based начисления (содержание/найм/ТКО/отопление)
        # платятся ВСЕГДА, даже при первой подаче. Вместо zero_costs
        # вызываем calculate_utilities с volume_*=0 — water/sewage/elect
        # будут 0, а area-based корректно начислятся.
        from app.modules.utility.routers.settings import _load_seasonal
        from app.modules.utility.services.calculations import (
            calculate_utilities as _calc_baseline,
            CalculationError as _CE_baseline,
        )
        try:
            _seasonal_b = await _load_seasonal(db)
            costs = _calc_baseline(
                user=user, room=room, tariff=tariff,
                volume_hot=ZERO_MONEY, volume_cold=ZERO_MONEY,
                volume_sewage=ZERO_MONEY, volume_electricity_share=ZERO_MONEY,
                heating_season_active=(_seasonal_b.heating_season_active and tariff.is_heating_active_now()),
                hot_water_heating_active=(_seasonal_b.hot_water_heating_active and tariff.is_hw_heating_active_now()),
            )
        except _CE_baseline:
            costs = {
                "cost_hot_water": ZERO_MONEY, "cost_cold_water": ZERO_MONEY,
                "cost_sewage": ZERO_MONEY, "cost_electricity": ZERO_MONEY,
                "cost_maintenance": ZERO_MONEY, "cost_social_rent": ZERO_MONEY,
                "cost_waste": ZERO_MONEY, "cost_fixed_part": ZERO_MONEY,
                "total_cost": ZERO_MONEY,
            }
    else:
        # Сезонные флаги. Двухуровневая логика (с tariffs_seasonal_002):
        #   1. Глобальный SystemSetting — emergency «stop». Если false,
        #      отключает статью у всех тарифов.
        #   2. Per-tariff поля (heating_active + heating_season_start/end).
        #      Тариф сам решает, активна ли статья сегодня.
        # Реально активно = global AND tariff.is_*_now().
        from app.modules.utility.routers.settings import _load_seasonal
        seasonal = await _load_seasonal(db)
        heating_now = (
            seasonal.heating_season_active and tariff.is_heating_active_now()
        )
        hw_heating_now = (
            seasonal.hot_water_heating_active and tariff.is_hw_heating_active_now()
        )
        costs = ReadingService.calculate_costs(
            user, room, tariff, hot, cold, elect, p_hot, p_cold, p_elect,
            heating_season_active=heating_now,
            hot_water_heating_active=hw_heating_now,
        )

    # 6. Итоги. ВАЖНО (30.05.2026): долг/переплата 1С НЕ суммируются в ИТОГО.
    # ИТОГО = начисление за месяц + корректировки. Долг/переплата хранятся
    # отдельно (reading.debt_*/overpayment_* из импорта 1С), показываются
    # справкой в квитанции и агрегируются отдельно для отчётности/баланса.
    cost_rent = costs['cost_social_rent']
    cost_utils = costs['total_cost'] - cost_rent

    total_209 = cost_utils + adj_map.get('209', Decimal("0.00"))
    total_205 = cost_rent + adj_map.get('205', Decimal("0.00"))
    grand_total = total_209 + total_205
    # Пометка флага, чтобы в реестре/админке было понятно — это baseline,
    # ноль намеренно, а не ошибка расчёта.
    baseline_flag = "BASELINE" if is_baseline else "PENDING"

    # 7. СОХРАНЕНИЕ
    if draft:
        if draft.is_approved:
            raise HTTPException(400, "Ваши показания уже проверены и приняты бухгалтерией. Изменение невозможно.")

        old_record = {
            "hot": str(draft.hot_water),
            "cold": str(draft.cold_water),
            "elect": str(draft.electricity),
            "date": utcnow().strftime("%d.%m.%Y %H:%M")
        }
        history_list = draft.edit_history if draft.edit_history else []
        draft.edit_history = history_list + [old_record]
        draft.edit_count = (draft.edit_count or 0) + 1

        draft.hot_water, draft.cold_water, draft.electricity = hot, cold, elect
        draft.total_209, draft.total_205, draft.total_cost = total_209, total_205, grand_total
        draft.anomaly_flags, draft.anomaly_score = baseline_flag, 0

        # costs_for_model_fields фильтрует sanity_warning и total_cost
        # — последний устанавливаем выше (grand_total с долгами/коррект.,
        # а не «чистый» total_cost из calculate_utilities).
        from app.modules.utility.services.calculations import costs_for_model_fields
        for key, value in costs_for_model_fields(costs).items():
            setattr(draft, key, value)

        db.add(draft)
        await db.flush()
        reading_id_for_celery = draft.id

    else:
        from app.modules.utility.services.calculations import costs_for_model_fields
        costs_for_create = costs_for_model_fields(costs)

        new_draft = MeterReading(
            user_id=user.id,
            room_id=user.room_id,
            period_id=period.id,
            hot_water=hot,
            cold_water=cold,
            electricity=elect,
            debt_209=Decimal("0.00"),
            overpayment_209=Decimal("0.00"),
            debt_205=Decimal("0.00"),
            overpayment_205=Decimal("0.00"),
            total_209=total_209,
            total_205=total_205,
            total_cost=grand_total,
            is_approved=False,
            anomaly_flags=baseline_flag,
            anomaly_score=0,
            edit_count=1,
            edit_history=[],
            **costs_for_create
        )
        db.add(new_draft)
        await db.flush()
        reading_id_for_celery = new_draft.id

    # Bug 29.05.2026 (Коммит 22 — revert Коммита 16): триггер изменён.
    # Раньше клонирование шло когда tariff.tariff_type='singles'. После
    # уточнения архитектуры — теперь триггер `room.is_singles_apartment`.
    # Один тариф на всех; статус «холостяцкая квартира» — атрибут комнаты.
    # Family-комнаты НЕ затрагиваются.
    is_singles_apt = bool(getattr(user.room, "is_singles_apartment", False))
    if is_singles_apt:
        # Все другие жильцы той же комнаты, активные.
        other_residents = (await db.execute(
            select(User).where(
                User.room_id == user.room_id,
                User.is_deleted.is_(False),
                User.role == "user",
                User.id != user.id,
            )
        )).scalars().all()

        # Для каждого другого жильца — создать draft с теми же значениями
        # ИЛИ обновить существующий draft если он есть.
        current_reading_data = {
            "hot_water": hot, "cold_water": cold, "electricity": elect,
            "total_209": total_209, "total_205": total_205,
            "total_cost": grand_total,
            "anomaly_flags": (baseline_flag or "") + "|SINGLES_SHARED",
            "anomaly_score": 0,
        }
        # Добавляем cost_* из costs_for_model_fields
        from app.modules.utility.services.calculations import (
            costs_for_model_fields as _cfmf,
        )
        current_reading_data.update(_cfmf(costs))

        for other_user in other_residents:
            existing_draft = (await db.execute(
                select(MeterReading).where(
                    MeterReading.user_id == other_user.id,
                    MeterReading.period_id == period.id,
                    MeterReading.is_approved.is_(False),
                )
            )).scalars().first()

            if existing_draft:
                for k, v in current_reading_data.items():
                    setattr(existing_draft, k, v)
                existing_draft.edit_count = (existing_draft.edit_count or 0) + 1
                db.add(existing_draft)
            else:
                clone = MeterReading(
                    user_id=other_user.id,
                    room_id=user.room_id,
                    period_id=period.id,
                    debt_209=Decimal("0.00"),
                    overpayment_209=Decimal("0.00"),
                    debt_205=Decimal("0.00"),
                    overpayment_205=Decimal("0.00"),
                    is_approved=False,
                    edit_count=1,
                    edit_history=[],
                    **current_reading_data,
                )
                db.add(clone)

        await db.flush()
        logger.info(
            "[CALC] singles-tariff: подача user=%s клонирована на %d других "
            "жильцов комнаты room=%s",
            user.id, len(other_residents), user.room_id,
        )

    await db.commit()

    # 8. Запускаем асинхронную проверку на аномалии
    detect_anomalies_task.delay(reading_id_for_celery)

    return {"status": "success", "total_cost": grand_total, "total_209": total_209, "total_205": total_205}


# =========================
# RECEIPT
# =========================


async def _build_receipt_context(reading: MeterReading, db: AsyncSession):
    """Тариф / предыдущее показание / корректировки для PDF — БЕЗ проверки
    доступа (её делает вызывающий). Переиспользуется резидентским скачиванием
    И анонимным QR-порталом (там доступ = сам токен квартиры).

    Эффективный тариф (Room.tariff_id → User.tariff_id → default id=1) — тот же,
    что billing при расчёте. РАНЬШЕ брался «первый активный по valid_from», что
    мог вернуть пустой тариф → в PDF все ставки 0 при верных cost_*.
    """
    from app.modules.utility.services.tariff_cache import tariff_cache
    tariff = tariff_cache.get_effective_tariff(user=reading.user, room=reading.room)
    if tariff is None:
        tariff = (await db.execute(
            select(Tariff).where(Tariff.is_active).order_by(Tariff.id)
        )).scalars().first()
    if not tariff:
        raise HTTPException(500, "Тариф не найден")

    # prev — по БИЛЛИНГОВОЙ хронологии (строго более ранний месяц), а не по
    # created_at. По created_at ломалось дважды: (а) ввод прошлых месяцев
    # задним числом → prev не находился; (б) вторая запись ТОГО ЖЕ периода
    # (debt-черновик 1С / повторная подача) становилась prev с теми же
    # цифрами → в PDF «Объём 0.00» при верных суммах (инцидент Мороз).
    # Зеркалит выбор prev в save_manual_entry: жилец+комната, meaningful,
    # период строго раньше. Фолбэк — по комнате (показания комнатные).
    from app.modules.utility.services.period_helpers import period_chron_key
    from app.modules.utility.services.reading_calculator import is_meaningful_prev

    prev = None
    cur_period = await db.get(BillingPeriod, reading.period_id) if reading.period_id else None
    if cur_period is not None:
        cur_key = period_chron_key(cur_period.name)
        cand_rows = (await db.execute(
            select(MeterReading, BillingPeriod)
            .join(BillingPeriod, BillingPeriod.id == MeterReading.period_id)
            .where(
                MeterReading.room_id == reading.room_id,
                MeterReading.is_approved.is_(True),
                MeterReading.id != reading.id,
            )
        )).all()

        def _pick(rows):
            earlier = [(period_chron_key(bp.name), mr) for mr, bp in rows
                       if period_chron_key(bp.name) < cur_key and is_meaningful_prev(mr)]
            earlier.sort(key=lambda x: x[0])
            return earlier[-1][1] if earlier else None

        # Сначала — свои показания жильца (как в расчёте суммы), затем комнатные.
        prev = _pick([rw for rw in cand_rows if rw[0].user_id == reading.user_id]) or _pick(cand_rows)
    if prev is None and cur_period is None:
        # Показание без периода (legacy) — прежнее поведение по created_at.
        prev = (await db.execute(
            select(MeterReading)
            .where(
                MeterReading.room_id == reading.room_id,
                MeterReading.is_approved.is_(True),
                MeterReading.created_at < reading.created_at,
            )
            .order_by(MeterReading.created_at.desc())
            .limit(1)
        )).scalars().first()

    adjustments = (await db.execute(
        select(Adjustment).where(
            Adjustment.user_id == reading.user_id,
            Adjustment.period_id == reading.period_id
        )
    )).scalars().all()

    return tariff, prev, adjustments


