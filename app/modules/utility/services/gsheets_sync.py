# app/modules/utility/services/gsheets_sync.py
"""
Синхронизация показаний из Google Sheets.

Таблица имеет колонки:
  A: timestamp (dd.mm.yyyy HH:MM:SS)
  B: ФИО жильца
  C: общежитие (свободный текст, игнорируем)
  D: номер комнаты
  E: ГВС (м³)
  F: ХВС (м³)

Стратегия:
  1. Читаем таблицу публичным CSV-экспортом (не требует OAuth/service-account,
     таблица должна быть доступна «по ссылке — все, у кого есть ссылка»).
  2. Для каждой строки считаем row_hash и добавляем в gsheets_import_rows
     ON CONFLICT DO NOTHING — идемпотентно.
  3. Fuzzy-матчим ФИО (rapidfuzz token_sort_ratio).
  4. Сверяем номер комнаты. Если не совпал — status=conflict.
  5. Если score ≥ 95 и комната совпала — status=auto_approved (админ сразу
     увидит их в «автоодобренных» и при желании откатит).
"""
import csv
import hashlib
import io
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.modules.utility.models import GSheetsImportRow, Room, User

logger = logging.getLogger(__name__)


# =======================================================================
# Константы
# =======================================================================
# URL публичного CSV-экспорта (работает если таблица открыта «по ссылке»).
#   https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}
GSHEETS_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    "?format=csv&gid={gid}"
)

FUZZY_THRESHOLD = 78   # Минимум для «подозрительного» матча (pending)
AUTO_APPROVE_THRESHOLD = 95  # ФИО почти точное + комната совпала → auto_approved


# =======================================================================
# Парсеры
# =======================================================================

def parse_timestamp(raw: str) -> Optional[datetime]:
    """Google Sheets отдаёт дату в разных форматах. Пробуем несколько."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_decimal(raw: str) -> Optional[Decimal]:
    """
    Показания приходят в разных форматах:
      "7890", "00039", "91,778", "1085.07", "  421 ", "50 039"
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Удаляем пробелы/nbsp внутри числа
    s = s.replace(" ", "").replace("\xa0", "")
    # Запятая → точка (decimal separator в RU-локали)
    s = s.replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def normalize_fio(fio: str) -> str:
    """Приводит ФИО к единому виду для fuzzy-match."""
    if not fio:
        return ""
    # Убираем точки, множественные пробелы, приводим к lower
    s = str(fio).lower()
    s = re.sub(r"[.,]", " ", s)
    s = " ".join(s.split())
    return s


def parse_room_number(raw: str) -> Optional[str]:
    """
    Номер комнаты может быть "414", "00016", "2/4". Нормализуем:
    убираем ведущие нули (но оставляем оригинал, если это строка).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Если это чисто число с ведущими нулями — убираем их
    if re.fullmatch(r"0*\d+", s):
        s = s.lstrip("0") or "0"
    return s


# =======================================================================
# Хэш строки для идемпотентного импорта
# =======================================================================

def compute_row_hash(ts: Optional[datetime], fio: str, room: str,
                     hot: str, cold: str) -> str:
    """
    Хэш должен быть стабильным между запусками. На один и тот же исходный
    ряд всегда выдаёт один и тот же MD5, поэтому дубли не создаются при
    повторных импортах.
    """
    ts_str = ts.isoformat() if ts else ""
    raw = f"{ts_str}|{fio.strip()}|{room.strip()}|{hot.strip()}|{cold.strip()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# =======================================================================
# Fuzzy-matcher
# =======================================================================

def build_users_index(db: Session) -> tuple[dict[str, dict], list[str]]:
    """
    Строит индекс users_map: normalized_fio -> {id, username, room_id, room_number}.
    Возвращает (map, list_of_keys) для rapidfuzz.extractOne.
    """
    users = db.execute(
        select(User, Room)
        .outerjoin(Room, User.room_id == Room.id)
        .where(User.is_deleted.is_(False))
    ).all()

    index: dict[str, dict] = {}
    for user, room in users:
        key = normalize_fio(user.username)
        if not key:
            continue
        index[key] = {
            "id": user.id,
            "username": user.username,
            "room_id": room.id if room else None,
            "room_number": room.room_number if room else None,
        }
    return index, list(index.keys())


def match_user(
    raw_fio: str,
    raw_room: Optional[str],
    users_map: dict[str, dict],
    users_keys: list[str],
) -> tuple[Optional[dict], int, Optional[str]]:
    """
    Возвращает (user_info | None, score 0..100, conflict_reason | None).

    - Сначала пытаемся token_sort_ratio (устойчив к перестановке слов "Иванов Иван" vs "Иван Иванов").
    - Если score ≥ FUZZY_THRESHOLD, проверяем совпадение комнаты.
    """
    norm = normalize_fio(raw_fio)
    if not norm:
        return None, 0, None

    # Точное совпадение
    if norm in users_map:
        user = users_map[norm]
        conflict = _check_room_conflict(user, raw_room)
        return user, 100, conflict

    # Fuzzy
    match = process.extractOne(norm, users_keys, scorer=fuzz.token_sort_ratio)
    if not match:
        return None, 0, None

    best_name, score, _idx = match
    if score < FUZZY_THRESHOLD:
        return None, int(score), None

    user = users_map[best_name]
    conflict = _check_room_conflict(user, raw_room)
    return user, int(score), conflict


def _check_room_conflict(user: dict, raw_room: Optional[str]) -> Optional[str]:
    """Возвращает описание конфликта или None если комната совпала."""
    sheet_room = parse_room_number(raw_room or "")
    user_room = parse_room_number(user.get("room_number") or "")
    if not sheet_room:
        return None  # В таблице не указана комната — сверять нечем
    if not user_room:
        return f"Жилец не привязан к помещению (в таблице: {sheet_room})"
    if sheet_room != user_room:
        return f"Комната не совпадает: в таблице {sheet_room}, у жильца {user_room}"
    return None


# =======================================================================
# Чтение таблицы
# =======================================================================

def fetch_csv(sheet_id: str, gid: str = "0", timeout: int = 30) -> str:
    """HTTP GET CSV-экспорта Google Sheets (синхронный — для Celery)."""
    url = GSHEETS_EXPORT_URL.format(sheet_id=sheet_id, gid=gid)
    # follow_redirects=True — Google может редиректить на auth-required страницу,
    # если таблица приватна. Таким образом мы не упадём с RedirectError и увидим HTML.
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "text/csv" not in content_type and "text/plain" not in content_type:
        raise RuntimeError(
            f"Google Sheets вернул не CSV ({content_type}). "
            "Проверьте, что таблица доступна «по ссылке — все, у кого есть ссылка»."
        )
    return resp.text


def parse_csv_rows(csv_text: str) -> list[dict]:
    """Парсит CSV в список словарей со структурой колонок A-F."""
    reader = csv.reader(io.StringIO(csv_text))
    rows = []
    header_seen = False
    for idx, row in enumerate(reader):
        if not row or not any(cell.strip() for cell in row):
            continue
        # Первая строка — заголовок, пропускаем
        if not header_seen:
            header_seen = True
            # Если первая ячейка выглядит как timestamp — это не хедер
            if not parse_timestamp(row[0]):
                continue
        # Защита от коротких строк
        row = row + [""] * (6 - len(row))
        rows.append({
            "_index": idx,
            "timestamp": row[0],
            "fio": row[1],
            "dormitory": row[2],
            "room": row[3],
            "hot": row[4],
            "cold": row[5],
        })
    return rows


# =======================================================================
# Основная функция синхронизации
# =======================================================================

def sync_gsheets(
    db: Session,
    sheet_id: str,
    gid: str = "0",
    limit: Optional[int] = None,
) -> dict:
    """
    Полный цикл: скачать CSV → распарсить → сопоставить → вставить в БД.

    Возвращает статистику:
        inserted  — новых строк добавлено
        duplicate — пропущено (row_hash уже есть)
        matched   — из inserted: fuzzy matched с юзером
        unmatched — из inserted: юзер не найден
        conflicts — из inserted: комната не совпала
        auto_approved — из inserted: score ≥95 и комната ок
        errors    — количество ошибок парсинга
    """
    logger.info(f"[GSHEETS] Starting sync from sheet {sheet_id}")

    csv_text = fetch_csv(sheet_id, gid)
    raw_rows = parse_csv_rows(csv_text)

    if limit:
        raw_rows = raw_rows[:limit]

    users_map, users_keys = build_users_index(db)

    stats = {
        "total_rows": len(raw_rows),
        "inserted": 0, "duplicate": 0,
        "matched": 0, "unmatched": 0,
        "conflicts": 0, "auto_approved": 0,
        "errors": 0,
    }

    for row in raw_rows:
        try:
            ts = parse_timestamp(row["timestamp"])
            hot = parse_decimal(row["hot"])
            cold = parse_decimal(row["cold"])

            row_hash = compute_row_hash(
                ts, row["fio"], row["room"] or "",
                row["hot"] or "", row["cold"] or "",
            )

            user_info, score, conflict = match_user(
                row["fio"], row["room"], users_map, users_keys,
            )

            # Определяем статус
            if user_info is None:
                status = "unmatched"
            elif conflict:
                status = "conflict"
            elif score >= AUTO_APPROVE_THRESHOLD:
                status = "auto_approved"
            else:
                status = "pending"

            # UPSERT по row_hash — идемпотентно
            stmt = pg_insert(GSheetsImportRow).values(
                sheet_timestamp=ts,
                raw_fio=row["fio"] or "",
                raw_dormitory=row["dormitory"],
                raw_room_number=row["room"],
                raw_hot_water=row["hot"],
                raw_cold_water=row["cold"],
                hot_water=hot,
                cold_water=cold,
                matched_user_id=user_info["id"] if user_info else None,
                matched_room_id=user_info["room_id"] if user_info else None,
                match_score=score,
                status=status,
                conflict_reason=conflict,
                row_hash=row_hash,
            ).on_conflict_do_nothing(index_elements=["row_hash"])

            result = db.execute(stmt)
            # В SQLAlchemy rowcount=0 означает «конфликт → ничего не сделали»
            if result.rowcount and result.rowcount > 0:
                stats["inserted"] += 1
                if status == "unmatched":
                    stats["unmatched"] += 1
                elif status == "conflict":
                    stats["conflicts"] += 1
                elif status == "auto_approved":
                    stats["auto_approved"] += 1
                    stats["matched"] += 1
                else:
                    stats["matched"] += 1
            else:
                stats["duplicate"] += 1

        except Exception as e:
            logger.warning(f"[GSHEETS] Row {row.get('_index')} failed: {e}")
            stats["errors"] += 1

    db.commit()
    logger.info(f"[GSHEETS] Sync finished: {stats}")
    return stats


# =======================================================================
# Извлечение sheet_id из URL (удобство для админа)
# =======================================================================

def extract_sheet_id(url_or_id: str) -> str:
    """Из полного URL Google Sheets или голого ID вытаскивает только ID."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()
