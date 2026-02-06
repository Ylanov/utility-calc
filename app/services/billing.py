from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime
from decimal import Decimal
import logging

from app.models import User, MeterReading, BillingPeriod, Tariff
from app.services.calculations import calculate_utilities, D

logger = logging.getLogger("billing_service")


# --- ЛОГИКА ЗАКРЫТИЯ ПЕРИОДА ---
async def close_current_period(db: AsyncSession, admin_user_id: int):
    """
    Закрывает текущий расчетный период в рамках одной атомарной транзакции.
    1. Находит текущий активный период.
    2. Генерирует показания 'по среднему' для тех, кто не сдал.
    3. Утверждает все зависшие черновики.
    4. Делает период неактивным.
    Если любая из этих операций завершится с ошибкой, все изменения будут отменены.
    """

    # --- НАЧАЛО АТОМАРНОЙ ТРАНЗАКЦИИ ---
    async with db.begin() as transaction:
        # 1. Получаем активный период
        res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
        active_period = res_period.scalars().first()

        if not active_period:
            raise ValueError("Нет активного периода для закрытия.")

        # 2. Получаем тарифы
        res_tariff = await db.execute(select(Tariff).where(Tariff.id == 1))
        tariff = res_tariff.scalars().first()
        if not tariff:
            raise ValueError("Тарифы не найдены в системе. Невозможно выполнить расчет.")

        # 3. Ищем пользователей БЕЗ показаний
        res_users = await db.execute(select(User).where(User.role == "user"))
        all_users = res_users.scalars().all()

        res_readings = await db.execute(
            select(MeterReading.user_id)
            .where(MeterReading.period_id == active_period.id)
        )
        users_with_readings_ids = set(res_readings.scalars().all())

        generated_count = 0
        zero = Decimal("0.000")

        for user in all_users:
            if user.id in users_with_readings_ids:
                continue  # У пользователя уже есть показание, пропускаем

            # --- Генерация по среднему для должника ---
            last_readings = await db.execute(
                select(MeterReading)
                .where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
                .order_by(MeterReading.created_at.desc())
                .limit(3)
            )
            history = last_readings.scalars().all()

            # Расчет среднего прироста
            if len(history) >= 2:
                deltas_hot = []
                deltas_cold = []
                deltas_elect = []

                for i in range(len(history) - 1):
                    curr = history[i]
                    prev = history[i + 1]
                    d_hot = max(zero, D(curr.hot_water) - D(prev.hot_water))
                    d_cold = max(zero, D(curr.cold_water) - D(prev.cold_water))
                    d_elect = max(zero, D(curr.electricity) - D(prev.electricity))
                    deltas_hot.append(d_hot)
                    deltas_cold.append(d_cold)
                    deltas_elect.append(d_elect)

                count = len(deltas_hot)
                avg_hot = sum(deltas_hot) / count if deltas_hot else zero
                avg_cold = sum(deltas_cold) / count if deltas_cold else zero
                avg_elect = sum(deltas_elect) / count if deltas_elect else zero

                last_reading = history[0]
                new_hot = D(last_reading.hot_water) + avg_hot
                new_cold = D(last_reading.cold_water) + avg_cold
                new_elect = D(last_reading.electricity) + avg_elect

            elif len(history) == 1:
                new_hot = D(history[0].hot_water)
                new_cold = D(history[0].cold_water)
                new_elect = D(history[0].electricity)
            else:
                new_hot, new_cold, new_elect = zero, zero, zero

            # Расчет объемов и стоимости
            last_hot_val = D(history[0].hot_water) if history else zero
            last_cold_val = D(history[0].cold_water) if history else zero
            last_elect_val = D(history[0].electricity) if history else zero

            vol_hot = max(zero, new_hot - last_hot_val)
            vol_cold = max(zero, new_cold - last_cold_val)

            residents = Decimal(user.residents_count)
            total_res_val = user.total_room_residents if user.total_room_residents > 0 else 1
            total_residents = Decimal(total_res_val)
            d_elect_total = new_elect - last_elect_val
            user_share_kwh = (residents / total_residents) * d_elect_total

            vol_sewage = vol_hot + vol_cold

            costs = calculate_utilities(
                user=user,
                tariff=tariff,
                volume_hot=vol_hot,
                volume_cold=vol_cold,
                volume_sewage=vol_sewage,
                volume_electricity_share=max(zero, user_share_kwh)
            )

            auto_reading = MeterReading(
                user_id=user.id,
                period_id=active_period.id,
                hot_water=new_hot,
                cold_water=new_cold,
                electricity=new_elect,
                is_approved=True,
                anomaly_flags="AUTO_GENERATED",
                total_cost=costs["total_cost"],
                cost_hot_water=costs["cost_hot_water"],
                cost_cold_water=costs["cost_cold_water"],
                cost_sewage=costs["cost_sewage"],
                cost_electricity=costs["cost_electricity"],
                cost_maintenance=costs["cost_maintenance"],
                cost_social_rent=costs["cost_social_rent"],
                cost_waste=costs["cost_waste"],
                cost_fixed_part=costs["cost_fixed_part"],
                created_at=datetime.utcnow()
            )
            db.add(auto_reading)
            generated_count += 1

        # Утверждаем все висящие черновики в текущем периоде
        pending_drafts = await db.execute(
            select(MeterReading).where(MeterReading.period_id == active_period.id, MeterReading.is_approved == False)
        )
        for draft in pending_drafts.scalars().all():
            draft.is_approved = True

        # Делаем период неактивным
        active_period.is_active = False

    # --- КОНЕЦ АТОМАРНОЙ ТРАНЗАКЦИИ ---
    # Если код дошел до этой точки без ошибок, SQLAlchemy автоматически выполнит COMMIT.
    # В случае любой ошибки выше, будет выполнен ROLLBACK.

    logger.info(f"Period '{active_period.name}' closed successfully. Auto-generated readings: {generated_count}")

    return {
        "status": "closed",
        "closed_period": active_period.name,
        "auto_generated": generated_count
    }


# --- ЛОГИКА ОТКРЫТИЯ ПЕРИОДА ---
async def open_new_period(db: AsyncSession, new_name: str):
    """Открывает новый расчетный период."""

    # Эта операция достаточно проста, но использование транзакции
    # является хорошей практикой для единообразия.
    async with db.begin() as transaction:
        # Проверяем, нет ли уже активного периода
        res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
        if res.scalars().first():
            raise ValueError("Сначала закройте текущий активный месяц!")

        # Проверяем имя на уникальность
        res_exist = await db.execute(select(BillingPeriod).where(BillingPeriod.name == new_name))
        if res_exist.scalars().first():
            raise ValueError(f"Период с именем '{new_name}' уже существует!")

        new_p = BillingPeriod(name=new_name, is_active=True)
        db.add(new_p)

    logger.info(f"New period '{new_name}' opened.")
    # Объект new_p будет доступен и после коммита
    return new_p