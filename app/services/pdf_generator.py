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

env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)

ORG_DETAILS = {
    "name": 'ФГКУ "ЦСООР "Лидер"',
    "inn": "5003008102", "kpp": "775101001",
    "account": "03100643000000017300",
    "bank": 'УФК по г. Москве (ФГКУ "ЦСООР "Лидер")',
    "bik": "004525988", "oktmo": "45953000"
}

KBC_RENT = "17711301991010300130"
KBC_UTILS = "17711302061017000130"


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def D(value) -> Decimal:
    if value is None: return Decimal("0.000")
    return Decimal(str(value))


def generate_qr_base64(kbk: str, total_sum: Decimal, user: User, purpose: str) -> str:
    total_sum = quantize_money(max(total_sum, Decimal("0.00")))
    sum_kopecks = int(total_sum * 100)
    fio = user.username.split()
    last, first, middle = (fio[0] if len(fio) > 0 else ""), (fio[1] if len(fio) > 1 else ""), (
        fio[2] if len(fio) > 2 else "")

    qr_data = (
        f"ST00012|Name={ORG_DETAILS['name']}|PersonalAcc={ORG_DETAILS['account']}|"
        f"BankName={ORG_DETAILS['bank']}|BIC={ORG_DETAILS['bik']}|PayeeINN={ORG_DETAILS['inn']}|"
        f"KPP={ORG_DETAILS['kpp']}|Sum={sum_kopecks}|Purpose={purpose} л/с {user.id}|"
        f"lastName={last}|firstName={first}|middleName={middle}|payerAddress={user.dormitory or 'Не указан'}|"
        f"CBC={kbk}|OKTMO={ORG_DETAILS['oktmo']}"
    )
    qr = qrcode.make(qr_data)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def generate_receipt_pdf(
        user: User, reading: MeterReading, period: BillingPeriod, tariff: Tariff,
        prev_reading: Optional[MeterReading] = None, adjustments: Optional[List[Adjustment]] = None,
        output_dir: Optional[str] = None
) -> str:
    if adjustments is None: adjustments = []

    # --- ИСПРАВЛЕНИЕ: Используем новые поля из модели ---
    # Общий долг = долг по коммуналке + долг по найму
    total_debt = (reading.debt_209 or Decimal(0)) + (reading.debt_205 or Decimal(0))
    # Общая переплата = переплата по коммуналке + переплата по найму
    total_overpayment = (reading.overpayment_209 or Decimal(0)) + (reading.overpayment_205 or Decimal(0))

    # Сумма всех корректировок
    recalc = sum((adj.amount for adj in adjustments), Decimal("0.00"))

    # Итоговые суммы к оплате по каждому счету
    total_209_due = reading.total_209 or Decimal(0)
    total_205_due = reading.total_205 or Decimal(0)
    grand_total = reading.total_cost or Decimal(0)
    # --------------------------------------------------------

    # Расчет объемов
    cur_hot, cur_cold, cur_elect = D(reading.hot_water), D(reading.cold_water), D(reading.electricity)
    prev_hot = D(prev_reading.hot_water) if prev_reading else D(0)
    prev_cold = D(prev_reading.cold_water) if prev_reading else D(0)
    prev_elect = D(prev_reading.electricity) if prev_reading else D(0)

    raw_delta_hot, raw_delta_cold, raw_delta_elect = cur_hot - prev_hot, cur_cold - prev_cold, cur_elect - prev_elect
    corr_hot, corr_cold, corr_elect, corr_sewage = D(reading.hot_correction), D(reading.cold_correction), D(
        reading.electricity_correction), D(reading.sewage_correction)

    vol_hot = max(D(0), raw_delta_hot - corr_hot)
    vol_cold = max(D(0), raw_delta_cold - corr_cold)

    residents = D(user.residents_count)
    total_residents = D(user.total_room_residents) if user.total_room_residents > 0 else D(1)
    share_elect = (residents / total_residents) * raw_delta_elect
    vol_elect = max(D(0), share_elect - corr_elect)
    vol_sewage = max(D(0), (vol_hot + vol_cold) - corr_sewage)

    # QR-коды генерируем на основе итоговых сумм к оплате по каждому счету
    qr_rent = generate_qr_base64(KBC_RENT, total_205_due, user, "Плата за наем")
    qr_utils = generate_qr_base64(KBC_UTILS, total_209_due, user, "Коммунальные услуги")

    context = {
        "user": user, "period": period, "tariff": tariff, "reading": reading,
        "prev_reading": prev_reading, "adjustments": adjustments,
        "total_209_due": total_209_due, "total_205_due": total_205_due,
        "grand_total": grand_total, "total_debt": total_debt, "total_overpayment": total_overpayment,
        "recalc": recalc, "vol_hot": vol_hot, "vol_cold": vol_cold, "vol_elect": vol_elect,
        "vol_sewage": vol_sewage, "org": ORG_DETAILS, "kbk_rent": KBC_RENT,
        "kbk_utils": KBC_UTILS, "qr_rent": qr_rent, "qr_utils": qr_utils
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