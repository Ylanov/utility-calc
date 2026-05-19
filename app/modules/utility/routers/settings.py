# app/modules/utility/routers/settings.py

import logging
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.modules.utility.models import User, SystemSetting
from app.core.dependencies import RoleChecker, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["System Settings"])

allow_accountant_or_admin = RoleChecker(["accountant", "admin", "financier"])

# Допустимые значения формата ввода показаний счётчиков. Сделано
# отдельным enum'ом, чтобы UI мог их перечислить и подсветить пример.
#   "5_no_decimal"   — пишите ТОЛЬКО первые 5 целых цифр (рекомендуется)
#   "5_with_decimal" — 5 целых . 3 дробных (полное показание счётчика)
#   "any"            — любой формат, валидация только на максимум
MeterFormatHint = Literal["5_no_decimal", "5_with_decimal", "any"]
DEFAULT_METER_FORMAT_HINT: MeterFormatHint = "5_no_decimal"


class SubmissionPeriodSchema(BaseModel):
    start_day: int = Field(..., ge=1, le=28)
    end_day: int = Field(..., ge=1, le=28)


class MeterFormatSchema(BaseModel):
    """Подсказка жильцу: сколько цифр счётчика ему вводить.

    На счётчиках воды разный формат шкалы — где-то 5+3 цифр, где-то
    8 цифр без точки. Жилец может записать «01427.957» как «01427957»
    и парсер получит 1 427 957 м³ (это и был баг 1.48 млрд ₽). Админ
    указывает один общий формат для всего общежития, мобильное
    приложение и web-форма показывают конкретный пример.
    """
    format: MeterFormatHint
    example_hot: str = Field(
        ...,
        description="Пример валидного значения для жильца — будет показан "
                    "под полем ввода в мобилке/портале.",
    )
    instructions: str = Field(
        ...,
        description="Текст-объяснение жильцу. Можно настроить в админке.",
    )


@router.get("/submission-period", response_model=SubmissionPeriodSchema)
async def get_submission_period(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Получить дни подачи показаний.
    Доступно всем авторизованным пользователям.
    """
    start = await db.get(SystemSetting, "submission_start_day")
    end = await db.get(SystemSetting, "submission_end_day")

    def safe_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return SubmissionPeriodSchema(
        start_day=safe_int(start.value if start else None, 20),
        end_day=safe_int(end.value if end else None, 25)
    )


@router.post("/submission-period")
async def update_submission_period(
    data: SubmissionPeriodSchema,
    current_user: User = Depends(allow_accountant_or_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Обновить дни подачи показаний.
    Доступно бухгалтеру, финансисту и админу.

    ИСПРАВЛЕНИЕ: Добавлен try/except + rollback.
    Ранее если commit падал (например, конкурентная запись или constraint),
    клиент получал голый 500 без логирования.
    """
    if data.start_day >= data.end_day:
        raise HTTPException(
            status_code=400,
            detail="День начала должен быть раньше дня окончания"
        )

    try:
        async def upsert(key: str, val: int, desc: str):
            item = await db.get(SystemSetting, key)
            if item:
                item.value = str(val)
            else:
                db.add(SystemSetting(
                    key=key,
                    value=str(val),
                    description=desc
                ))

        await upsert("submission_start_day", data.start_day, "День начала приема показаний")
        await upsert("submission_end_day", data.end_day, "День окончания приема показаний")

        await db.commit()

    except Exception as e:
        await db.rollback()
        logger.error(f"Ошибка при обновлении графика подачи показаний: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при сохранении графика. Обратитесь к администратору."
        )

    return {
        "status": "success",
        "message": "График успешно обновлен"
    }


# =====================================================================
# METER FORMAT HINT — единый формат ввода показаний счётчиков
# =====================================================================
# Хранится в SystemSetting с тремя ключами:
#   meter_format_hint     — "5_no_decimal" / "5_with_decimal" / "any"
#   meter_example_hot     — пример валидного hot-значения (UI hint)
#   meter_instructions    — длинный текст-инструкция жильцу

_DEFAULT_HINTS = {
    "5_no_decimal": {
        "example": "01433",
        "instructions": (
            "Запишите только ПЕРВЫЕ 5 цифр счётчика (целая часть). "
            "Дробные цифры после точки — НЕ нужны. "
            "Пример: на счётчике «01433.887» вводите 01433 или 1433."
        ),
    },
    "5_with_decimal": {
        "example": "01433.887",
        "instructions": (
            "Введите все цифры счётчика, разделяя точкой целую и дробную "
            "часть. Пример: «01433.887»."
        ),
    },
    "any": {
        "example": "1433",
        "instructions": "Введите показание счётчика как видите на табло.",
    },
}


@router.get("/meter-format", response_model=MeterFormatSchema)
async def get_meter_format(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает текущий настроенный формат ввода показаний.
    Доступен всем авторизованным (мобилке + портал жильца).
    """
    hint_row = await db.get(SystemSetting, "meter_format_hint")
    hint = hint_row.value if hint_row else DEFAULT_METER_FORMAT_HINT
    if hint not in _DEFAULT_HINTS:
        hint = DEFAULT_METER_FORMAT_HINT  # защита от мусора в БД

    example_row = await db.get(SystemSetting, "meter_example_hot")
    instr_row = await db.get(SystemSetting, "meter_instructions")

    return MeterFormatSchema(
        format=hint,
        example_hot=(example_row.value if example_row else _DEFAULT_HINTS[hint]["example"]),
        instructions=(instr_row.value if instr_row else _DEFAULT_HINTS[hint]["instructions"]),
    )


@router.post("/meter-format")
async def update_meter_format(
    data: MeterFormatSchema,
    current_user: User = Depends(allow_accountant_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Обновить общесистемный формат ввода показаний счётчиков.
    Доступно admin/accountant/financier."""
    try:
        async def upsert(key: str, val: str, desc: str):
            item = await db.get(SystemSetting, key)
            if item:
                item.value = val
            else:
                db.add(SystemSetting(key=key, value=val, description=desc))

        await upsert("meter_format_hint", data.format, "Формат ввода счётчиков (жильцу)")
        await upsert("meter_example_hot", data.example_hot, "Пример hot для жильца")
        await upsert("meter_instructions", data.instructions, "Текст-инструкция жильцу")
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"meter-format update failed: {e}", exc_info=True)
        raise HTTPException(500, "Не удалось сохранить формат счётчиков")

    return {"status": "success", "message": "Формат счётчиков обновлён"}


# ======================================================================
# ЮРИДИЧЕСКИЕ РЕКВИЗИТЫ ОПЕРАТОРА (для политики 152-ФЗ).
# Подставляются в /privacy.html и в footer всех страниц.
# Хранятся в system_settings (key-value). Публичный GET без авторизации —
# чтобы neavtorizovannyy жилец на login.html видел контакт оператора.
# ======================================================================
# Список ключей, чтобы один источник правды — и в схеме, и в endpoint'ах,
# и (потенциально) в миграциях seed-данных.
_OPERATOR_KEYS = (
    ("operator_name",                       "Полное наименование организации",       ""),
    ("operator_inn",                        "ИНН",                                   ""),
    ("operator_ogrn",                       "ОГРН",                                  ""),
    ("operator_legal_address",              "Юридический адрес",                     ""),
    ("operator_postal_address",             "Почтовый адрес для корреспонденции",    ""),
    ("operator_email",                      "Электронная почта (для запросов по ПД)", "privacy@asy-tk.ru"),
    ("operator_phone",                      "Контактный телефон",                    ""),
    # Дополнения по результатам юр-аудита 152-ФЗ (май 2026).
    # Без этих полей политика обработки ПД формально неполная.
    ("operator_rkn_registry_number",        "Регистрационный номер в Реестре операторов РКН (ст. 22)", ""),
    ("operator_responsible_name",           "ФИО ответственного за организацию обработки ПД (ст. 22.1)", ""),
    ("operator_responsible_position",       "Должность ответственного за обработку ПД", ""),
    ("operator_responsible_email",          "Электронная почта ответственного за обработку ПД", ""),
    ("operator_infosystem_security_level",  "Уровень защищённости ИС (УЗ-1..4, ПП РФ № 1119)", ""),
)


class OperatorInfoSchema(BaseModel):
    """Реквизиты оператора персональных данных.

    Все поля опциональные на уровне схемы (admin может оставить пустыми
    на старте), но privacy.html подсвечивает плейсхолдером пустые.
    """
    operator_name: Optional[str] = Field(None, max_length=300)
    operator_inn: Optional[str] = Field(None, max_length=20)
    operator_ogrn: Optional[str] = Field(None, max_length=20)
    operator_legal_address: Optional[str] = Field(None, max_length=500)
    operator_postal_address: Optional[str] = Field(None, max_length=500)
    operator_email: Optional[str] = Field(None, max_length=200)
    operator_phone: Optional[str] = Field(None, max_length=50)
    # Поля 152-ФЗ.
    operator_rkn_registry_number: Optional[str] = Field(None, max_length=50)
    operator_responsible_name: Optional[str] = Field(None, max_length=200)
    operator_responsible_position: Optional[str] = Field(None, max_length=200)
    operator_responsible_email: Optional[str] = Field(None, max_length=200)
    operator_infosystem_security_level: Optional[str] = Field(None, max_length=50)


async def _load_operator_info(db: AsyncSession) -> dict:
    """Достаёт все operator_* ключи из system_settings одним запросом."""
    keys = [k for k, _desc, _default in _OPERATOR_KEYS]
    rows = (await db.execute(
        select(SystemSetting).where(SystemSetting.key.in_(keys))
    )).scalars().all()
    by_key = {r.key: r.value for r in rows}
    defaults = {k: default for k, _desc, default in _OPERATOR_KEYS}
    return {k: by_key.get(k, defaults.get(k, "")) for k in defaults}


@router.get("/operator-info", response_model=OperatorInfoSchema)
async def get_operator_info_public(db: AsyncSession = Depends(get_db)):
    """ПУБЛИЧНЫЙ endpoint (без авторизации).

    Используется в:
      - /privacy.html — подставить реквизиты в текст политики;
      - footer всех страниц — email/телефон для связи с оператором.
    Это юридически открытая информация (по 152-ФЗ оператор обязан её
    публиковать), не считается приватной.
    """
    info = await _load_operator_info(db)
    return OperatorInfoSchema(**info)


@router.put("/operator-info")
async def update_operator_info(
    data: OperatorInfoSchema,
    current_user: User = Depends(allow_accountant_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """Обновить реквизиты оператора. Только для admin/accountant/financier.

    Изменение этих полей мгновенно отражается в /privacy.html и footer
    (через клиентский GET /api/settings/operator-info).
    """
    try:
        async def upsert(key: str, val: str, desc: str):
            item = await db.get(SystemSetting, key)
            if item:
                item.value = val
            else:
                db.add(SystemSetting(key=key, value=val, description=desc))

        updates = data.dict(exclude_unset=False)
        for key, desc, _default in _OPERATOR_KEYS:
            await upsert(key, updates.get(key) or "", desc)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"operator-info update failed: {e}", exc_info=True)
        raise HTTPException(500, "Не удалось сохранить реквизиты оператора")

    return {"status": "success", "message": "Реквизиты оператора обновлены"}
