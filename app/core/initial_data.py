import asyncio
import logging
from decimal import Decimal
from sqlalchemy.future import select
from app.core.database import AsyncSessionLocal, ArsenalSessionLocal
from app.modules.utility.models import User, Tariff, BillingPeriod
from app.modules.arsenal.models import ArsenalUser
from app.core.auth import get_password_hash
from app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def seed_data():
    """
    Создает начальные данные в БД (ЖКХ и Арсенал), если их нет.
    """
    if settings.ENVIRONMENT != "development":
        logger.info("Skipping data seeding in non-development environment.")
        return

    logger.info("Starting initial data seeding...")

    # 1. Сидинг базы ЖКХ (Utility DB)
    async with AsyncSessionLocal() as db:
        try:
            # Администратор ЖКХ
            admin_result = await db.execute(select(User).where(User.username == "admin"))
            if not admin_result.scalars().first():
                admin = User(
                    username="admin",
                    hashed_password=get_password_hash("admin"),
                    role="accountant"
                )
                db.add(admin)
                logger.info("Utility DB: 'admin' user created.")

            # Тариф
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
                logger.info("Utility DB: Default tariff created.")

            # Период
            period_result = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active.is_(True)))
            if not period_result.scalars().first():
                period = BillingPeriod(
                    name="Начальный период",
                    is_active=True
                )
                db.add(period)
                logger.info("Utility DB: Default billing period created.")

            await db.commit()
        except Exception as e:
            logger.error(f"Utility DB seeding error: {e}")
            await db.rollback()

    # 2. Сидинг базы Арсенал (Arsenal DB)
    async with ArsenalSessionLocal() as arsenal_db:
        try:
            # Администратор Арсенала
            arsenal_admin = await arsenal_db.execute(select(ArsenalUser).where(ArsenalUser.username == "admin"))
            if not arsenal_admin.scalars().first():
                new_admin = ArsenalUser(
                    username="admin",
                    hashed_password=get_password_hash("admin")
                )
                arsenal_db.add(new_admin)
                logger.info("Arsenal DB: 'admin' user created.")

            await arsenal_db.commit()
        except Exception as e:
            logger.error(f"Arsenal DB seeding error: {e}")
            await arsenal_db.rollback()

    logger.info("Initial data seeding finished.")


async def main():
    await asyncio.sleep(2)
    await seed_data()


if __name__ == "__main__":
    asyncio.run(main())