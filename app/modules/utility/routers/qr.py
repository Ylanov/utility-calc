# app/modules/utility/routers/qr.py
"""
Публичный эндпоинт для генерации QR-кодов на стороне сервера.

Используется на portal.html и в личном кабинете жильца для отображения
QR со ссылкой на скачивание APK. Раньше QR подгружался с quickchart.io,
что:
  - Нарушало CSP (img-src 'self' data: blob:)
  - Создавало внешнюю зависимость (если quickchart упал — QR не работает)
  - Леало через open internet с потенциальной утечкой содержимого URL

Теперь — собственная генерация через Python-библиотеку `qrcode` (уже есть
в зависимостях для 2FA). Кэшируется на 1 час браузером + 24 часа nginx-ом.
"""
from __future__ import annotations

import io
import re

import qrcode
from fastapi import APIRouter, HTTPException, Query, Response

router = APIRouter(tags=["QR"])

# Лимиты для предотвращения DoS / abuse
MAX_TEXT_LEN = 2048
MIN_BOX_SIZE = 4
MAX_BOX_SIZE = 20


@router.get("/api/qr")
async def generate_qr(
    text: str = Query(..., min_length=1, max_length=MAX_TEXT_LEN),
    box_size: int = Query(8, ge=MIN_BOX_SIZE, le=MAX_BOX_SIZE),
    border: int = Query(2, ge=0, le=10),
):
    """
    Возвращает PNG с QR-кодом.

    Параметры:
        text — содержимое QR (URL, любая строка ≤2048 символов)
        box_size — размер одного "пикселя" QR в выходном PNG (4..20)
        border — отступ в "квадратах" QR (0..10)

    Контент-тип `image/png`, кэш-заголовки на 1 час.
    """
    # Защита: только разумные тексты — никаких control-characters,
    # которые могут заломать picture/PIL.
    if "\x00" in text or any(ord(c) < 0x20 and c not in "\r\n\t" for c in text):
        raise HTTPException(400, "Недопустимые символы в тексте QR")

    qr = qrcode.QRCode(
        version=None,                # автоподбор версии
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(text)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            # 1 час браузер, 24 часа CDN/nginx — содержимое QR детерминировано
            "Cache-Control": "public, max-age=3600, s-maxage=86400, immutable",
        },
    )
