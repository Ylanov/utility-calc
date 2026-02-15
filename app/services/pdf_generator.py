import os
import base64
import qrcode
from uuid import uuid4
from io import BytesIO
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional, List

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from app.models import User, MeterReading, Tariff, BillingPeriod, Adjustment


BASE_DIR = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = BASE_DIR / "templates"
DEFAULT_PDF_DIR = "/app/static/generated_files"

os.makedirs(DEFAULT_PDF_DIR, exist_ok=True)

env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True
)

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


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def D(value) -> Decimal:
    if value is None:
        return Decimal("0.000")
    return Decimal(str(value))


def generate_qr_base64(kbk: str, total_sum: Decimal, user: User, purpose: str) -> str:
    if total_sum <= 0:
        total_sum = Decimal("0.00")

    total_sum = quantize_money(total_sum)
    sum_kopecks = int(total_sum * 100)

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
        f"Sum={sum_kopecks}|"
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


def generate_receipt_pdf(
    user: User,
    reading: MeterReading,
    period: BillingPeriod,
    tariff: Tariff,
    prev_reading: Optional[MeterReading] = None,
    adjustments: Optional[List[Adjustment]] = None,
    output_dir: Optional[str] = None
) -> str:
    if adjustments is None:
        adjustments = []

    # Финансы
    debt = Decimal(reading.initial_debt or 0)
    overpay = Decimal(reading.initial_overpayment or 0)
    recalc = sum((adj.amount for adj in adjustments), Decimal("0.00"))

    rent_sum = Decimal(reading.cost_social_rent or 0)
    total_sum = Decimal(reading.total_cost or 0)
    utils_sum = total_sum - rent_sum

    # --- РАСЧЕТ ОБЪЕМОВ (ДЛЯ ШАБЛОНА) ---
    # 1. Текущие показания
    cur_hot = D(reading.hot_water)
    cur_cold = D(reading.cold_water)
    cur_elect = D(reading.electricity)

    # 2. Предыдущие показания
    if prev_reading:
        prev_hot = D(prev_reading.hot_water)
        prev_cold = D(prev_reading.cold_water)
        prev_elect = D(prev_reading.electricity)
    else:
        prev_hot = Decimal("0.000")
        prev_cold = Decimal("0.000")
        prev_elect = Decimal("0.000")

    # 3. Дельта (Расход)
    raw_delta_hot = cur_hot - prev_hot
    raw_delta_cold = cur_cold - prev_cold
    raw_delta_elect = cur_elect - prev_elect

    # 4. Коррекции (вычитаем их из расхода, так как это поправка объема)
    corr_hot = D(reading.hot_correction)
    corr_cold = D(reading.cold_correction)
    corr_elect = D(reading.electricity_correction)
    corr_sewage = D(reading.sewage_correction)

    # 5. Итоговые объемы к оплате
    vol_hot = max(Decimal("0"), raw_delta_hot - corr_hot)
    vol_cold = max(Decimal("0"), raw_delta_cold - corr_cold)

    # Расчет доли электричества
    residents = D(user.residents_count)
    total_residents = D(user.total_room_residents) if user.total_room_residents > 0 else Decimal("1")
    share_elect = (residents / total_residents) * raw_delta_elect
    vol_elect = max(Decimal("0"), share_elect - corr_elect)

    # Водоотведение (ГВС + ХВС - Коррекция)
    vol_sewage = max(Decimal("0"), (vol_hot + vol_cold) - corr_sewage)

    # QR коды
    qr_rent = generate_qr_base64(KBC_RENT, rent_sum, user, "Плата за наем")
    qr_utils = generate_qr_base64(KBC_UTILS, utils_sum, user, "Коммунальные услуги")

    context = {
        "user": user,
        "period": period,
        "tariff": tariff,
        "reading": reading,
        "prev_reading": prev_reading,
        "adjustments": adjustments,

        # Финансы
        "rent_sum": rent_sum,
        "utils_sum": utils_sum,
        "total_sum": total_sum,
        "debt": debt,
        "overpay": overpay,
        "recalc": recalc,

        # Объемы (Критические переменные для шаблона!)
        "vol_hot": vol_hot,
        "vol_cold": vol_cold,
        "vol_elect": vol_elect,
        "vol_sewage": vol_sewage,

        "org": ORG_DETAILS,
        "kbk_rent": KBC_RENT,
        "kbk_utils": KBC_UTILS,
        "qr_rent": qr_rent,
        "qr_utils": qr_utils
    }

    template = env.get_template("receipt.html")
    html = template.render(context)

    target_dir = output_dir or DEFAULT_PDF_DIR
    os.makedirs(target_dir, exist_ok=True)

    filename = f"receipt_{user.id}_{period.id}_{uuid4().hex}.pdf"
    filepath = os.path.join(target_dir, filename)

    try:
        HTML(string=html).write_pdf(filepath)
    except Exception as e:
        raise RuntimeError(f"Ошибка генерации PDF: {str(e)}")

    return filepath