import os
import base64
import qrcode
from io import BytesIO
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML, CSS

from app.models import User, MeterReading, Tariff, BillingPeriod

# Папки
# Определяем пути относительно текущего файла
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__)) # app/services
APP_DIR = os.path.dirname(CURRENT_DIR) # app
BASE_DIR = os.path.dirname(APP_DIR) # корень проекта (где main.py или выше)

# Путь к шаблонам. Если templates лежит рядом с app или внутри app, путь может отличаться.
# Предполагаем, что templates лежит в корне контейнера /app/templates
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

# Папка по умолчанию (если не указана иная)
DEFAULT_PDF_DIR = "/tmp/receipts"
os.makedirs(DEFAULT_PDF_DIR, exist_ok=True)

# Инициализация Jinja2
# Добавляем проверку существования папки, чтобы не падать с ошибкой, если её нет
if os.path.exists(TEMPLATE_DIR):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
else:
    # Фолбек на случай, если templates внутри папки app
    env = Environment(loader=FileSystemLoader(os.path.join(APP_DIR, "templates")))

# =====================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================

ORG_DETAILS = {
    "name": 'ФГКУ "ЦСООР "Лидер"',
    "inn": "5003008102",
    "kpp": "775101001",
    "rs": "03100643000000017300",
    "bank": 'УФК по г. Москве (ФГКУ "ЦСООР "Лидер")',
    "bik": "004525988",
    "oktmo": "45953000"
}

KBC_RENT = "17711301991010300130"
KBC_UTILS = "17711302061017000130"


def _generate_qr_base64(kbc, total_sum, user, purpose_text):
    """Генерирует QR-код и возвращает его как base64 строку для вставки в HTML"""

    fio = user.username.split()
    last = fio[0] if len(fio) > 0 else ""
    first = fio[1] if len(fio) > 1 else ""
    middle = fio[2] if len(fio) > 2 else ""

    qr_data = (
        f"ST00012|"
        f"Name={ORG_DETAILS['name']}|"
        f"PersonalAcc={ORG_DETAILS['rs']}|"
        f"BankName={ORG_DETAILS['bank']}|"
        f"BIC={ORG_DETAILS['bik']}|"
        f"PayeeINN={ORG_DETAILS['inn']}|"
        f"KPP={ORG_DETAILS['kpp']}|"
        f"Sum={int(total_sum * 100)}|"
        f"Purpose={purpose_text} л/с {user.id}|"
        f"lastName={last}|"
        f"firstName={first}|"
        f"middleName={middle}|"
        f"payerAddress={user.dormitory or 'Не указан'}|"
        f"CBC={kbc}|"
        f"OKTMO={ORG_DETAILS['oktmo']}"
    )

    qr = qrcode.make(qr_data)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return img_str


# =====================================================
# ОСНОВНАЯ ФУНКЦИЯ
# =====================================================

def generate_receipt_pdf(
        user: User,
        reading: MeterReading,
        period: BillingPeriod,
        tariff: Tariff,
        prev_reading: MeterReading,
        output_dir: str = None  # <--- ДОБАВЛЕН АРГУМЕНТ
) -> str:
    """
    Генерирует PDF используя HTML шаблон и WeasyPrint.
    :param output_dir: Папка для сохранения. Если None, используется /tmp/receipts
    :return: Абсолютный путь к созданному файлу
    """

    # 1. Считаем объемы потребления
    prev_hot = prev_reading.hot_water if prev_reading else 0.0
    prev_cold = prev_reading.cold_water if prev_reading else 0.0
    prev_elect = prev_reading.electricity if prev_reading else 0.0

    vol_hot = max(0, reading.hot_water - prev_hot - reading.hot_correction)
    vol_cold = max(0, reading.cold_water - prev_cold - reading.cold_correction)

    if tariff.electricity_rate > 0:
        vol_elect = reading.cost_electricity / tariff.electricity_rate
    else:
        vol_elect = 0

    vol_sewage = vol_hot + vol_cold - reading.sewage_correction

    # 2. Генерируем QR коды
    total_rent = reading.cost_social_rent
    total_utils = reading.total_cost - total_rent

    qr_rent_b64 = _generate_qr_base64(KBC_RENT, total_rent, user, "Plata za naem")
    qr_utils_b64 = _generate_qr_base64(KBC_UTILS, total_utils, user, "Plata za KU")

    # 3. Подготовка контекста для шаблона
    context = {
        "user": user,
        "reading": reading,
        "period_name": period.name,
        "tariff": tariff,

        "vol_hot": vol_hot,
        "vol_cold": vol_cold,
        "vol_sewage": vol_sewage,
        "vol_elect": vol_elect,

        "qr_rent_b64": qr_rent_b64,
        "qr_utils_b64": qr_utils_b64
    }

    # 4. Рендеринг HTML
    template = env.get_template("receipt.html")
    html_content = template.render(context)

    # 5. Генерация PDF
    # Определяем целевую папку
    target_dir = output_dir if output_dir else DEFAULT_PDF_DIR
    os.makedirs(target_dir, exist_ok=True) # Гарантируем, что папка существует

    filename = f"receipt_{user.id}_{period.id}.pdf"
    filepath = os.path.join(target_dir, filename)

    # WeasyPrint делает магию
    HTML(string=html_content).write_pdf(filepath)

    return filepath