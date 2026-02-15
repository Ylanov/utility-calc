# app/initial_data.py
import asyncio
import logging
from decimal import Decimal

from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.models import User, Tariff, BillingPeriod
from app.auth import get_password_hash
from app.config import settings

# Настраиваем логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def seed_data():
    """
    Создает начальные данные в БД, если их нет.
    Эта функция выполняется один раз перед запуском Gunicorn.
    """
    # Выполняем сидинг только в development-режиме
    if settings.ENVIRONMENT != "development":
        logger.info("Skipping data seeding in non-development environment.")
        return

    logger.info("Starting initial data seeding for development...")

    async with AsyncSessionLocal() as db:
        try:
            # ---- 1. Администратор ----
            admin_result = await db.execute(select(User).where(User.username == "admin"))
            if not admin_result.scalars().first():
                admin = User(
                    username="admin",
                    hashed_password=get_password_hash("admin"),
                    role="accountant"
                )
                db.add(admin)
                logger.info("Default 'admin' user will be created.")

            # ---- 2. Тариф по умолчанию ----
            tariff_result = await db.execute(select(Tariff).where(Tariff.id == 1))
            if not tariff_result.scalars().first():
                tariff = Tariff(
                    id=1,
                    is_active=True,
                    electricity_rate=Decimal("5.0"),
                    maintenance_repair=Decimal("0.0"),
                    social_rent=Decimal("0.0"),
                    heating=Decimal("0.0"),
                    water_heating=Decimal("0.0"),
                    water_supply=Decimal("0.0"),
                    sewage=Decimal("0.0"),
                    waste_disposal=Decimal("0.0"),
                    electricity_per_sqm=Decimal("0.0")
                )
                db.add(tariff)
                logger.info("Default tariff (ID=1) will be created.")

            # ---- 3. Расчетный период по умолчанию ----
            period_result = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active.is_(True)))
            if not period_result.scalars().first():
                period = BillingPeriod(
                    name="Начальный период",
                    is_active=True
                )
                db.add(period)
                logger.info("Default active billing period will be created.")

            # Пытаемся сохранить все изменения
            await db.commit()
            logger.info("Initial data seeding finished successfully.")

        except IntegrityError:
            # Эта ошибка может возникнуть, если два процесса все же попытались создать одно и то же.
            # Мы просто откатываем транзакцию и считаем, что данные уже есть.
            logger.warning("Data already exists, rolling back transaction.")
            await db.rollback()
        except Exception as e:
            logger.error(f"An error occurred during data seeding: {e}", exc_info=True)
            await db.rollback()
            raise

async def main():
    # Небольшая задержка, чтобы дать БД полностью "проснуться"
    await asyncio.sleep(2)
    await seed_data()

if __name__ == "__main__":
    asyncio.run(main())