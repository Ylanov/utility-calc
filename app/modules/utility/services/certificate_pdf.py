# app/modules/utility/services/certificate_pdf.py
"""Генераторы PDF для справок.

Сейчас поддерживается один тип — `flc` (заявление на выписку из
финансово-лицевого счёта по договору найма жилого помещения).
Структура — 1-в-1 со шаблоном, который предоставил заказчик:

    Заместителю начальника Центра по тылу
    Колесникову А.Н.
    от <должность>
       <Ф.И.О. заявителя>

    Заявление

    Прошу Вас дать указание на выдачу мне, выписки из финансово-лицевого
    счета по договору найма жилого помещения в общежитии от <дата>
    № <№> за период <период>.
    Документ необходим для предоставления в <куда>.

    Приложения:
    1. Копии документов, удостоверяющих личность всех членов семьи, ...
    2. Копия договора найма жилого помещения в общежитии.

    <Ф.И.О.>                          <подпись>
    <дд.мм.гггг> г.

Используем reportlab + Canvas для точного контроля координат.
Шрифт — DejaVu (кириллица). Если шрифт не найден — fallback на Helvetica
(английская транслитерация не поддерживается, это крайний случай).
"""
from __future__ import annotations

import io
import os
from datetime import date
from typing import List, Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas


# =========================================================================
# ШРИФТЫ
# =========================================================================
# DejaVu — штатный кириллический шрифт в большинстве linux-контейнеров.
# В Docker базовый debian-образ кладёт его по пути
# /usr/share/fonts/truetype/dejavu/. Если нет — ставится `fonts-dejavu-core`.
_FONT_REG = "DejaVuSans"
_FONT_BOLD = "DejaVuSans-Bold"
_FONT_ITALIC = "DejaVuSans-Oblique"


def _ensure_fonts() -> None:
    """Регистрирует DejaVu один раз при первом вызове.
    Идемпотентно: повторная регистрация не падает."""
    if _FONT_REG in pdfmetrics.getRegisteredFontNames():
        return

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        # Windows — для локальной разработки
        "C:/Windows/Fonts/DejaVuSans.ttf",
        os.path.join(os.path.dirname(__file__), "..", "fonts", "DejaVuSans.ttf"),
    ]
    regular = next((p for p in candidates if os.path.isfile(p)), None)
    if not regular:
        # Fallback на Arial/Helvetica. Для кириллицы это плохо, но лучше
        # чем exception. В prod-окружении должно быть `apt install fonts-dejavu-core`.
        return

    pdfmetrics.registerFont(TTFont(_FONT_REG, regular))
    bold = regular.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    if os.path.isfile(bold):
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, bold))
    italic = regular.replace("DejaVuSans.ttf", "DejaVuSans-Oblique.ttf")
    if os.path.isfile(italic):
        pdfmetrics.registerFont(TTFont(_FONT_ITALIC, italic))


def _font(name: str) -> str:
    """Возвращает имя зарегистрированного шрифта или fallback."""
    return name if name in pdfmetrics.getRegisteredFontNames() else "Helvetica"


# =========================================================================
# ВСПОМОГАТЕЛЬНЫЕ
# =========================================================================

def _fmt_date(d: Optional[date]) -> str:
    if not d:
        return "__.__.______"
    return d.strftime("%d.%m.%Y")


def _fmt_date_period(period_from: Optional[date], period_to: Optional[date]) -> str:
    if period_from and period_to:
        return f"с {_fmt_date(period_from)} по {_fmt_date(period_to)}"
    if period_from:
        return f"с {_fmt_date(period_from)}"
    if period_to:
        return f"по {_fmt_date(period_to)}"
    return "___________________"


def _resident_fullname(user) -> str:
    """ФИО жильца — приоритет full_name, fallback на username (который
    в системе часто используется как ФИО + лицевой счёт)."""
    return (getattr(user, "full_name", None) or user.username or "").strip()


def _draw_field_line(
    c: rl_canvas.Canvas, x: float, y: float, width: float,
    value: str, label: str, font_size: int = 10,
):
    """Рисует линию-подчёркивание с текстом сверху и лейблом снизу (как в оригинале)."""
    # текст по линии
    c.setFont(_font(_FONT_REG), font_size)
    c.drawString(x, y + 2, value)
    # сама линия
    c.setLineWidth(0.4)
    c.line(x, y, x + width, y)
    # подпись под линией
    c.setFont(_font(_FONT_ITALIC), 8)
    label_width = c.stringWidth(label, _font(_FONT_ITALIC), 8)
    c.drawString(x + (width - label_width) / 2, y - 10, label)


# =========================================================================
# PDF: ЗАЯВЛЕНИЕ НА ВЫПИСКУ ИЗ ФЛС
# =========================================================================

def generate_flc_pdf(
    user,
    family: List = None,
    contract=None,
    period_from: Optional[date] = None,
    period_to: Optional[date] = None,
    purpose: str = "",
) -> bytes:
    """Собирает PDF заявления 1-в-1 с шаблоном заказчика.

    Возвращает bytes PDF, готовые к сохранению в S3.
    """
    _ensure_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    # Координатная сетка: y растёт снизу вверх. Сверху оставляем отступ 2 см.
    margin_left = 2.0 * cm
    margin_right = 1.5 * cm
    top = height - 2.0 * cm

    # ---------- 1. Шапка «Заместителю начальника Центра по тылу ...»  ----------
    # В оригинале шапка прижата к правому краю, ~65% ширины листа.
    header_x = 10 * cm
    header_lines = [
        "Заместителю начальника Центра по тылу",
        "Колесникову А.Н.",
    ]
    c.setFont(_font(_FONT_REG), 11)
    y = top
    for line in header_lines:
        c.drawString(header_x, y, line)
        y -= 14

    # ---------- 2. «от <должность> / <ФИО заявителя>» ----------
    y -= 8  # небольшой отступ
    # Ширина поля — до правого края страницы
    field_width = width - margin_right - header_x - 0.5 * cm
    # «от» слева от линии
    c.setFont(_font(_FONT_REG), 11)
    c.drawString(header_x, y, "от")
    _draw_field_line(
        c, header_x + 0.8 * cm, y, field_width - 0.8 * cm,
        getattr(user, "position", "") or "", "(должность)",
    )
    y -= 22
    _draw_field_line(
        c, header_x, y, field_width,
        _resident_fullname(user), "(Ф.И.О. заявителя)",
    )

    # ---------- 3. Заголовок «Заявление» ----------
    y -= 55
    c.setFont(_font(_FONT_REG), 14)
    title = "Заявление"
    c.drawString((width - c.stringWidth(title, _font(_FONT_REG), 14)) / 2, y, title)

    # ---------- 4. Основной текст с полями ----------
    y -= 35
    c.setFont(_font(_FONT_REG), 11)

    contract_date = _fmt_date(contract.signed_date) if contract and contract.signed_date else "__________"
    contract_num = contract.number if contract and contract.number else "______"
    period_str = _fmt_date_period(period_from, period_to)

    # Текст разбит на логические куски чтобы подставить значения в правильных местах.
    # Для аккуратности — разные строки, как в оригинале.
    body_lines = [
        "    Прошу Вас дать указание на выдачу мне, выписки из финансово-лицевого",
        f"счета по договору найма жилого помещения в общежитии от {contract_date}",
        f"№ {contract_num} за период {period_str}.",
        f"    Документ необходим для предоставления в {purpose or '___________________'}.",
    ]
    for line in body_lines:
        c.drawString(margin_left, y, line)
        y -= 16

    # ---------- 5. Приложения ----------
    y -= 10
    c.setFont(_font(_FONT_REG), 11)
    c.drawString(margin_left, y, "Приложения:")
    y -= 16
    annex = [
        "1. Копии документов, удостоверяющих личность всех членов семьи, с отметкой",
        "   о регистрации по месту жительства;",
        "2. Копия договора найма жилого помещения в общежитии.",
    ]
    for line in annex:
        c.drawString(margin_left, y, line)
        y -= 14

    # ---------- 6. Члены семьи (дополнительный блок — оригинала нет,
    #              но полезно: в справочные органы приятнее когда указан состав) ----------
    if family:
        y -= 12
        c.setFont(_font(_FONT_ITALIC), 9)
        c.drawString(margin_left, y, "Состав семьи:")
        y -= 12
        c.setFont(_font(_FONT_REG), 9)
        role_label = {"spouse": "супруг(а)", "child": "ребёнок", "parent": "родитель", "other": "член семьи"}
        for m in family:
            rl = role_label.get(m.role, m.role)
            bd = _fmt_date(m.birth_date)
            c.drawString(margin_left + 10, y, f"• {rl} — {m.full_name}, {bd} г.р.")
            y -= 11

    # ---------- 7. Подпись + ФИО + дата ----------
    y -= 40
    # Линия ФИО (слева, ~55% ширины) и линия подписи (справа)
    name_w = 9 * cm
    sig_w = 4.5 * cm
    name_x = margin_left
    sig_x = width - margin_right - sig_w

    _draw_field_line(
        c, name_x, y, name_w,
        _resident_fullname(user), "(фамилия, имя, отчество)",
    )
    _draw_field_line(
        c, sig_x, y, sig_w,
        "", "(подпись)",
    )

    # ---------- 8. Дата «дд.мм.гггг г.» ----------
    y -= 32
    c.setFont(_font(_FONT_REG), 11)
    today = date.today()
    day = f"{today.day:02d}"
    month = f"{today.month:02d}"
    year = str(today.year)
    # Рисуем 3 коротких линии с цифрами над ними — визуально максимально близко к шаблону.
    dx = margin_left
    for val, w in ((day, 1.0 * cm), (month, 1.0 * cm), (year, 1.8 * cm)):
        c.drawString(dx + 2, y + 2, val)
        c.setLineWidth(0.4)
        c.line(dx, y, dx + w, y)
        dx += w + 0.2 * cm
    c.drawString(dx + 0.1 * cm, y + 2, "г.")

    c.showPage()
    c.save()
    return buf.getvalue()
