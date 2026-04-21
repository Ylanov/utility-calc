"""apk_meta.py — извлечение versionName и versionCode из APK без сторонних библиотек.

Зачем: пользователь жаловался, что в админке указывал "3.5.5", а приложение
после установки сообщало "1.2.0" и снова просило обновиться. Корень: версия
приложения зашита в APK на этапе сборки (pubspec.yaml → AndroidManifest.xml),
а текстовое поле в админке — просто метка в БД, никак не меняющая APK.

Решение: при загрузке APK сервер сам читает реальные значения из встроенного
AndroidManifest.xml и игнорирует то, что прислал админ. Так рассинхронизация
становится физически невозможной.

Парсер написан с нуля под формат Android Binary XML (AXML). Это намеренно:
библиотеки типа `androguard` или `pyaxmlparser` тянут за собой lxml/cffi
и весят десятки мегабайт ради задачи, которая решается в ~150 строках.

Документация формата: https://justanapplication.wordpress.com/category/android/
android-binary-xml/  и исходники Android: frameworks/base/include/androidfw/
ResourceTypes.h
"""
from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass
from typing import BinaryIO, Optional, Union

# Типы chunks (из ResourceTypes.h)
_RES_STRING_POOL_TYPE = 0x0001
_RES_XML_TYPE = 0x0003
_RES_XML_START_ELEMENT_TYPE = 0x0102

# Флаги string pool
_UTF8_FLAG = 0x00000100

# Типы значений атрибутов (Res_value::dataType)
_TYPE_STRING = 0x03
_TYPE_INT_DEC = 0x10
_TYPE_INT_HEX = 0x11
_TYPE_INT_BOOLEAN = 0x12


class ApkParseError(Exception):
    """Не удалось разобрать APK или AndroidManifest."""


@dataclass
class ApkMetadata:
    version_name: str       # например "3.5.5"
    version_code: int       # например 30505
    package_name: Optional[str] = None


def extract_apk_metadata(source: Union[str, BinaryIO]) -> ApkMetadata:
    """Извлекает версию и пакет из APK. Принимает путь либо file-like.

    Бросает ApkParseError если файл не APK или AndroidManifest повреждён.
    """
    try:
        with zipfile.ZipFile(source, "r") as zf:
            try:
                manifest_bytes = zf.read("AndroidManifest.xml")
            except KeyError:
                raise ApkParseError("В APK нет AndroidManifest.xml")
    except zipfile.BadZipFile as e:
        raise ApkParseError(f"Файл не является корректным APK/zip: {e}")

    return _parse_axml(manifest_bytes)


# ---------------------------------------------------------------------------
# AXML PARSER
# ---------------------------------------------------------------------------
def _parse_axml(data: bytes) -> ApkMetadata:
    if len(data) < 8:
        raise ApkParseError("AndroidManifest слишком маленький")

    chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, 0)
    if chunk_type != _RES_XML_TYPE:
        raise ApkParseError(f"Не AXML chunk (type={chunk_type:#x})")
    if chunk_size > len(data):
        raise ApkParseError("AXML заявляет размер больше файла")

    strings: list[str] = []
    pos = header_size  # пропускаем заголовок XML chunk

    found = ApkMetadata(version_name="", version_code=0)

    while pos + 8 <= chunk_size:
        sub_type, sub_header, sub_size = struct.unpack_from("<HHI", data, pos)
        if sub_size == 0 or pos + sub_size > chunk_size:
            break

        if sub_type == _RES_STRING_POOL_TYPE:
            strings = _read_string_pool(data, pos)
        elif sub_type == _RES_XML_START_ELEMENT_TYPE and strings:
            elem = _read_start_element(data, pos, sub_header, sub_size, strings)
            if elem is not None:
                tag, attrs = elem
                if tag == "manifest":
                    if "versionName" in attrs and not found.version_name:
                        found.version_name = str(attrs["versionName"])
                    if "versionCode" in attrs and not found.version_code:
                        try:
                            found.version_code = int(attrs["versionCode"])
                        except (TypeError, ValueError):
                            pass
                    if "package" in attrs and not found.package_name:
                        found.package_name = str(attrs["package"])
                    # versionCode и versionName — оба в <manifest>, после него
                    # уже неинтересно. Обрываем — экономим CPU на больших манифестах.
                    if found.version_name and found.version_code:
                        return found

        pos += sub_size

    if not found.version_name or not found.version_code:
        raise ApkParseError(
            f"Не нашли versionName/versionCode в манифесте "
            f"(version_name={found.version_name!r}, version_code={found.version_code})"
        )
    return found


def _read_string_pool(data: bytes, start: int) -> list[str]:
    """Читает ResStringPool, возвращает список декодированных строк."""
    # ResStringPool_header (28 bytes total: 8 chunk header + 20 fields)
    (
        _type, header_size, _chunk_size,
        string_count, _style_count, flags,
        strings_offset, _styles_offset,
    ) = struct.unpack_from("<HHIIIIII", data, start)

    is_utf8 = bool(flags & _UTF8_FLAG)
    offsets_start = start + header_size  # обычно start+28
    pool_strings_start = start + strings_offset

    strings: list[str] = []
    for i in range(string_count):
        off_pos = offsets_start + i * 4
        (rel_offset,) = struct.unpack_from("<I", data, off_pos)
        sp = pool_strings_start + rel_offset

        if is_utf8:
            # UTF-8: u8 utf16Len, u8 u8Len, [bytes], 0x00
            # если старший бит длины — длина закодирована в 2 байтах
            u16len = data[sp]
            sp += 1
            if u16len & 0x80:
                u16len = ((u16len & 0x7F) << 8) | data[sp]
                sp += 1
            u8len = data[sp]
            sp += 1
            if u8len & 0x80:
                u8len = ((u8len & 0x7F) << 8) | data[sp]
                sp += 1
            try:
                strings.append(data[sp: sp + u8len].decode("utf-8", errors="replace"))
            except Exception:
                strings.append("")
        else:
            # UTF-16 LE: u16 length, [length*2 bytes], 0x0000
            (length,) = struct.unpack_from("<H", data, sp)
            sp += 2
            if length & 0x8000:
                (low,) = struct.unpack_from("<H", data, sp)
                sp += 2
                length = ((length & 0x7FFF) << 16) | low
            try:
                strings.append(data[sp: sp + length * 2].decode("utf-16-le", errors="replace"))
            except Exception:
                strings.append("")
    return strings


def _read_start_element(
    data: bytes, start: int, header_size: int, chunk_size: int, strings: list[str],
):
    """Возвращает (tag_name, {attr_name: value}) либо None если не разобрался."""
    # XML_START_ELEMENT chunk:
    #   ResChunk_header (8) + uint32 lineNumber + uint32 commentRef = 16 (header_size)
    # затем ResXMLTree_attrExt:
    #   uint32 ns + uint32 name + 8 bytes (attrStart, attrSize, attrCount, ids...)
    body = start + header_size
    if body + 20 > start + chunk_size:
        return None

    name_idx = struct.unpack_from("<I", data, body + 4)[0]
    attr_start = struct.unpack_from("<H", data, body + 8)[0]
    attr_size = struct.unpack_from("<H", data, body + 10)[0]
    attr_count = struct.unpack_from("<H", data, body + 12)[0]

    if name_idx >= len(strings):
        return None
    tag_name = strings[name_idx]

    # attribute_start считается от начала attrExt-структуры (т.е. от `body`).
    # На практике attr_start всегда == 0x14 (20 bytes — размер attrExt-заголовка),
    # и attrs идут сразу после attrExt.
    attrs_pos = body + attr_start
    attrs: dict[str, object] = {}
    for i in range(attr_count):
        ap = attrs_pos + i * attr_size
        if ap + 20 > start + chunk_size:
            break
        # ns(4) name(4) rawValue(4) Res_value{size:2,res0:1,dataType:1,data:4} = 20
        a_name_idx = struct.unpack_from("<I", data, ap + 4)[0]
        a_raw_idx = struct.unpack_from("<I", data, ap + 8)[0]
        a_data_type = data[ap + 15]
        a_data = struct.unpack_from("<I", data, ap + 16)[0]

        if a_name_idx >= len(strings):
            continue
        attr_name = strings[a_name_idx]

        if a_data_type == _TYPE_STRING:
            if a_data < len(strings):
                attrs[attr_name] = strings[a_data]
            elif a_raw_idx != 0xFFFFFFFF and a_raw_idx < len(strings):
                attrs[attr_name] = strings[a_raw_idx]
        elif a_data_type in (_TYPE_INT_DEC, _TYPE_INT_HEX):
            attrs[attr_name] = a_data
        elif a_data_type == _TYPE_INT_BOOLEAN:
            attrs[attr_name] = bool(a_data)
        else:
            # Прочее (resource references и т.д.) — нам неинтересно для версии
            if a_raw_idx != 0xFFFFFFFF and a_raw_idx < len(strings):
                attrs[attr_name] = strings[a_raw_idx]

    return tag_name, attrs
