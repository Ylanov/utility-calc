import asyncio
import logging
from decimal import Decimal
from sqlalchemy.future import select
from app.core.database import AsyncSessionLocal, ArsenalSessionLocal
from app.modules.utility.models import User, Tariff, BillingPeriod
from app.modules.arsenal.models import ArsenalUser
from app.core.auth import get_password_hash
from app.core.config import settings
from sqlalchemy import text

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
    # ВАЖНО: автоматический seed `admin/admin` отключён в production!
    # Раньше скрипт при каждом старте СБРАСЫВАЛ пароль админа на 'admin' —
    # это означало что после рестарта контейнера любой кто знал URL
    # /login.html мог войти с admin/admin (если хотя бы один раз
    # запускался initial_data в prod). Теперь:
    #   - в dev (ENVIRONMENT != production): создаём admin/admin при отсутствии
    #     И сбрасываем пароль (как было) — удобно для разработки
    #   - в production: создаём admin/admin ТОЛЬКО если юзера нет вообще,
    #     НИКОГДА не сбрасываем пароль существующего админа
    from app.core.config import settings as _s
    is_dev = _s.ENVIRONMENT != "production"

    async with AsyncSessionLocal() as db:
        try:
            # Администратор ЖКХ
            admin_result = await db.execute(select(User).where(User.username == "admin"))
            admin = admin_result.scalars().first()

            if not admin:
                # Если нет - создаём (только в dev! В prod создание тоже
                # допустимо как bootstrap первого админа, но логируем громко).
                admin = User(
                    username="admin",
                    hashed_password=get_password_hash("admin"),
                    role="admin",  # упрощено в roles_001 (раньше было 'accountant')
                    is_deleted=False,
                    is_initial_setup_done=False  # модалка попросит сменить пароль
                )
                db.add(admin)
                if is_dev:
                    logger.info("Utility DB: 'admin' user created (dev).")
                else:
                    logger.warning(
                        "Utility DB: 'admin' user created with DEFAULT password 'admin' "
                        "— СМЕНИТЕ НЕМЕДЛЕННО через /me/setup или CLI."
                    )
            elif is_dev:
                # Только в dev — сбрасываем пароль на 'admin' (удобно для тестов).
                # В prod НИКОГДА не трогаем существующий пароль.
                admin.hashed_password = get_password_hash("admin")
                admin.is_deleted = False
                logger.info("Utility DB: 'admin' password reset to default (dev).")
            # В production: existing admin не трогаем — это критично.

            # Тариф (Создаем базовый профиль)
            tariff_result = await db.execute(select(Tariff).where(Tariff.id == 1))
            if not tariff_result.scalars().first():
                tariff = Tariff(
                    id=1,
                    name="Базовый тариф",
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

                # ВАЖНО: Делаем промежуточный коммит, чтобы тариф с id=1 записался в БД
                await db.commit()

                # ИСПРАВЛЕНИЕ: Синхронизируем sequence в PostgreSQL.
                # Так как мы жестко задали id=1, счетчик БД не сдвинулся, что вызывает ошибку UniqueViolation
                # при создании следующих тарифов через админку. Эта команда сдвигает счетчик.
                await db.execute(text("SELECT setval('tariffs_id_seq', (SELECT MAX(id) FROM tariffs))"))

                logger.info("Utility DB: Default tariff profile created and sequence updated.")

            # Период
            period_result = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active.is_(True)))
            if not period_result.scalars().first():
                period = BillingPeriod(
                    name="Начальный период",
                    is_active=True
                )
                db.add(period)
                logger.info("Utility DB: Default billing period created.")

            # Финальный коммит для пользователя и периода
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
