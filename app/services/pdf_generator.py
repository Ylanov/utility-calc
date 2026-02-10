import os
import base64
import qrcode

from io import BytesIO
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

# ИЗМЕНЕНИЕ: Добавлен импорт Adjustment
from app.models import User, MeterReading, Tariff, BillingPeriod, Adjustment


# =====================================================
# ПУТИ ПРОЕКТА
# =====================================================

BASE_DIR = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"

# Папка для сохранения PDF (совпадает с volume nginx)
DEFAULT_PDF_DIR = "/app/static/generated_files"

os.makedirs(DEFAULT_PDF_DIR, exist_ok=True)


# =====================================================
# JINJA2
# =====================================================

env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True
)


# =====================================================
# РЕКВИЗИТЫ ОРГАНИЗАЦИИ
# =====================================================

ORG_DETAILS = {
    "name": 'ФГКУ "ЦСООР "Лидер"',
    "inn": "5003008102",
    "kpp": "775101001",
    "account": "03100643000000017300",
    "bank": 'УФК по г. Москве (ФГКУ "ЦСООР "Лидер")',
    "bik": "004525988",
    "oktmo": "45953000"
}

KBC_RENT = "17711301991010300130"
KBC_UTILS = "17711302061017000130"


# =====================================================
# QR-КОД
# =====================================================

def generate_qr_base64(
    kbk: str,
    total_sum: Decimal,
    user: User,
    purpose: str
) -> str:
    """
    Генерирует QR-код оплаты (ГОСТ)
    """

    fio = user.username.split()

    last = fio[0] if len(fio) > 0 else ""
    first = fio[1] if len(fio) > 1 else ""
    middle = fio[2] if len(fio) > 2 else ""

    qr_data = (
        f"ST00012|"
        f"Name={ORG_DETAILS['name']}|"
        f"PersonalAcc={ORG_DETAILS['account']}|"
        f"BankName={ORG_DETAILS['bank']}|"
        f"BIC={ORG_DETAILS['bik']}|"
        f"PayeeINN={ORG_DETAILS['inn']}|"
        f"KPP={ORG_DETAILS['kpp']}|"
        f"Sum={int(total_sum * 100)}|"
        f"Purpose={purpose} л/с {user.id}|"
        f"lastName={last}|"
        f"firstName={first}|"
        f"middleName={middle}|"
        f"payerAddress={user.dormitory or 'Не указан'}|"
        f"CBC={kbk}|"
        f"OKTMO={ORG_DETAILS['oktmo']}"
    )

    qr = qrcode.make(qr_data)

    buffer = BytesIO()
    qr.save(buffer, format="PNG")

    return base64.b64encode(buffer.getvalue()).decode()


# =====================================================
# ОСНОВНАЯ ФУНКЦИЯ
# =====================================================

def generate_receipt_pdf(
    user: User,
    reading: MeterReading,
    period: BillingPeriod,
    tariff: Tariff,
    prev_reading: MeterReading | None,
    adjustments: list = [],  # ИЗМЕНЕНИЕ: Добавлен аргумент для корректировок
    output_dir: str | None = None
) -> str:
    """
    Генерация PDF-квитанции
    """

    # =====================================================
    # 1. ОБЪЁМЫ (ТОЛЬКО DECIMAL)
    # =====================================================

    prev_hot = Decimal(prev_reading.hot_water) if prev_reading else Decimal("0")
    prev_cold = Decimal(prev_reading.cold_water) if prev_reading else Decimal("0")
    prev_elect = Decimal(prev_reading.electricity) if prev_reading else Decimal("0")

    # Приводим к Decimal, так как из базы могут прийти float или None
    hot_corr = Decimal(reading.hot_correction or "0")
    cold_corr = Decimal(reading.cold_correction or "0")
    elect_corr = Decimal(reading.electricity_correction or "0")
    sewage_corr = Decimal(reading.sewage_correction or "0")

    # Расчет чистых объемов потребления за период
    vol_hot = max(Decimal("0"), reading.hot_water - prev_hot - hot_corr)
    vol_cold = max(Decimal("0"), reading.cold_water - prev_cold - cold_corr)
    vol_elect = max(Decimal("0"), reading.electricity - prev_elect - elect_corr)
    vol_sewage = max(Decimal("0"), vol_hot + vol_cold - sewage_corr)


    # =====================================================
    # 2. СУММЫ
    # =====================================================

    rent_sum = Decimal(reading.cost_social_rent or "0")
    total_sum = Decimal(reading.total_cost or "0")
    utils_sum = total_sum - rent_sum

    debt = Decimal(getattr(reading, "debt", 0) or 0)
    overpay = Decimal(getattr(reading, "overpay", 0) or 0)
    recalc = Decimal(getattr(reading, "recalc", 0) or 0)


    # =====================================================
    # 3. QR
    # =====================================================

    qr_rent = generate_qr_base64(
        KBC_RENT,
        rent_sum,
        user,
        "Плата за наем"
    )

    qr_utils = generate_qr_base64(
        KBC_UTILS,
        utils_sum,
        user,
        "Коммунальные услуги"
    )


    # =====================================================
    # 4. КОНТЕКСТ
    # =====================================================

    context = {

        # Пользователь
        "user": user,

        # Период
        "period": period,

        # Тарифы
        "tariff": tariff,

        # Начисление
        "reading": reading,

        # Корректировки (ИЗМЕНЕНИЕ)
        "adjustments": adjustments,

        # Объёмы
        "vol_hot": vol_hot,
        "vol_cold": vol_cold,
        "vol_elect": vol_elect,
        "vol_sewage": vol_sewage,

        # Суммы
        "rent_sum": rent_sum,
        "utils_sum": utils_sum,
        "total_sum": total_sum,

        "debt": debt,
        "overpay": overpay,
        "recalc": recalc,

        # Организация
        "org": ORG_DETAILS,

        # КБК
        "kbk_rent": KBC_RENT,
        "kbk_utils": KBC_UTILS,

        # QR
        "qr_rent": qr_rent,
        "qr_utils": qr_utils
    }


    # =====================================================
    # 5. HTML
    # =====================================================

    template = env.get_template("receipt.html")
    html = template.render(context)


    # =====================================================
    # 6. PDF
    # =====================================================

    target_dir = output_dir or DEFAULT_PDF_DIR
    os.makedirs(target_dir, exist_ok=True)

    filename = f"receipt_{user.id}_{period.id}.pdf"
    filepath = os.path.join(target_dir, filename)

    HTML(string=html).write_pdf(filepath)

    return filepath