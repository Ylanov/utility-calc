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
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.core.time_utils import utcnow

import httpx
from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.modules.utility.models import GSheetsImportRow, Room, User
# Пороги вынесены в reading_validators.py — единый источник правды для
# всех 4 точек входа MeterReading (mobile/gsheets/manual/approve).
from app.modules.utility.services.reading_validators import (
    MAX_WATER_METER_VALUE,
)

logger = logging.getLogger(__name__)


# =======================================================================
# Константы
# =======================================================================
# Два URL для CSV-экспорта Google Sheets:
#
# 1) gviz endpoint (предпочтительный): не редиректит, не требует cookies,
#    стабильно работает для публичных таблиц и таблиц "по ссылке".
#    Минус — может слегка иначе кодировать спецсимволы.
#
# 2) export endpoint (fallback): официальный, но возвращает 307 → cookie-сессия
#    → google ругается 400 без правильных headers. Поэтому пытаемся вторым.
GSHEETS_GVIZ_URL = (
    "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
    "?tqx=out:csv&gid={gid}"
)
GSHEETS_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    "?format=csv&gid={gid}"
)

# User-Agent — без него Google регулярно отвечает 400 на анонимные httpx-запросы.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# DEFAULT-значения. Реально используются геттеры _fuzzy_threshold() и
# _auto_approve_threshold() — они читают из таблицы analyzer_settings,
# админ может крутить ползунки в UI «Центр анализа» без релиза.
FUZZY_THRESHOLD = 78   # Минимум для «подозрительного» матча (pending)
AUTO_APPROVE_THRESHOLD = 95  # ФИО почти точное + комната совпала → auto_approved


def _fuzzy_threshold() -> int:
    from app.modules.utility.services.analyzer_config import config
    return config.get_int("gsheets.fuzzy_threshold", FUZZY_THRESHOLD)


def _auto_approve_threshold() -> int:
    from app.modules.utility.services.analyzer_config import config
    return config.get_int("gsheets.auto_approve_threshold", AUTO_APPROVE_THRESHOLD)


def _ambiguity_band() -> int:
    from app.modules.utility.services.analyzer_config import config
    return config.get_int("gsheets.ambiguity_band", 2)


# Максимальный возраст gsheets-строки, которую имеет смысл импортировать.
# Жильцы оставляют в таблице многолетнюю историю — без этого фильтра sync
# тащит подачи 2023-2024 годов, забивает gsheets_import_rows, и админ
# видит сотни «зависших» строк за давно закрытые периоды. По умолчанию
# 90 дней (3 месяца — текущий + предыдущий + запас на поздние подачи).
# Меняется через analyzer_config: ключ "gsheets.max_age_days".
DEFAULT_GSHEETS_MAX_AGE_DAYS = 90


def _max_age_days() -> int:
    from app.modules.utility.services.analyzer_config import config
    return config.get_int("gsheets.max_age_days", DEFAULT_GSHEETS_MAX_AGE_DAYS)


def _cutoff_date() -> Optional[datetime]:
    """Фиксированная нижняя граница sync: подачи раньше этой даты НЕ
    импортируются, даже если попадают в окно max_age_days.

    Параметр: `gsheets.cutoff_date` в analyzer_settings, ISO-формат
    `YYYY-MM-DD` (например `2026-01-15`). Если не задан или невалиден —
    возвращает None, фильтр работает только по max_age_days.

    Зачем: max_age_days считается относительно «сегодня» и каждый день
    сдвигается. Если админ хочет «никогда не подгружать подачи раньше
    15 января» — нужна абсолютная дата. Случилось 29.05.2026 когда
    почистили старые подачи 2025 года и не хотим чтобы они возвращались
    через 2 месяца когда max_age_days передвинется.
    """
    from app.modules.utility.services.analyzer_config import config
    raw = config.get_str("gsheets.cutoff_date", "")
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    logger.warning(
        "[GSHEETS-SYNC] gsheets.cutoff_date=%r не распарсился (ожидаем "
        "YYYY-MM-DD или DD.MM.YYYY). Игнорируем настройку.", raw,
    )
    return None


def _effective_age_cutoff() -> datetime:
    """Эффективная нижняя граница sync: максимальная из двух —
    `today - max_age_days` (relative) и `cutoff_date` (absolute).

    Если cutoff_date не задан — только max_age_days.
    Если cutoff_date в будущем относительно max-границы (т.е. админ
    хочет ещё более позднюю границу) — используется cutoff_date.
    Если cutoff_date в прошлом — игнорируется (max_age_days уже жёстче).
    """
    from datetime import timedelta as _td
    relative_cutoff = utcnow() - _td(days=_max_age_days())
    absolute_cutoff = _cutoff_date()
    if absolute_cutoff is None:
        return relative_cutoff
    return max(relative_cutoff, absolute_cutoff)


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


# Невидимые символы — частый мусор из копипаста 1С/ГИС/PDF/Excel: zero-width
# space/non-joiner/joiner, word-joiner, BOM, мягкий перенос. Удаляем (translate→None).
_FIO_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD], None)

# Латиница-гомоглифы → кириллица. ФИО жильцов всегда кириллические, поэтому
# латинская буква внутри ФИО — это «порча» (раскладка/копипаст из 1С/ГИС): на вид
# «Агаметов» один-в-один, но с латинской 'а' по байтам другой → не матчился.
# Для чисто-кириллических ФИО это no-op (латиницы нет → нечего сворачивать).
_FIO_HOMOGLYPH = {
    ord(lat): cyr for lat, cyr in {
        "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
        "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
    }.items()
}

# Варианты тире/дефиса → обычный «-» (двойные фамилии: en/em-dash, minus,
# fullwidth — на вид один-в-один с дефисом, по байтам разные).
_FIO_DASHES = {ord(d): "-" for d in "‐‑‒–—―−﹘﹣－"}


def normalize_fio(fio: str) -> str:
    """Приводит ФИО к единому виду для сопоставления 1С ↔ ГИС ГМП ↔ база.

    Гасит НЕвидимые различия (на вид «один в один», по байтам разные — из-за
    них идентичные ФИО не матчились и показывались «не обнаружен»):
      — Unicode NFC (й/ё из «буква + комбинируемый знак» → единый кодпоинт);
      — невидимые символы (zero-width, BOM, мягкий перенос);
      — латиница-гомоглифы (а е о с р х у к м т н в) → кириллица;
      — варианты тире → обычный дефис;
    и видимые: lowercase, ё→е, точки/запятые → пробел, коллапс пробелов.
    """
    if not fio:
        return ""
    s = unicodedata.normalize("NFC", str(fio))
    s = s.translate(_FIO_INVISIBLE)
    s = s.lower().replace("ё", "е")
    s = s.translate(_FIO_HOMOGLYPH)
    s = s.translate(_FIO_DASHES)
    s = re.sub(r"[.,]", " ", s)
    s = " ".join(s.split())
    return s


def canonical_initials(fio: str) -> str:
    """Канонический вид «фамилия + инициалы» — для match'a
    разных форматов одного и того же человека.

    «Иванов Иван Иванович» → «иванов и и»
    «Иванов И.И.»          → «иванов и и»
    «Иванов И. И.»         → «иванов и и»
    «Иванов  И.И.»         → «иванов и и»

    Используется как ДОПОЛНИТЕЛЬНЫЙ ключ индекса: админы в Google Sheets
    пишут короткие формы (ФИ или Ф.И.И.), а в БД username хранится как
    полное ФИО. Без этого пересчёта fuzzy-матч слабый.
    """
    norm = normalize_fio(fio)
    parts = [p for p in norm.split() if p]
    if not parts:
        return ""
    # Фамилия + первые буквы всех остальных слов
    return parts[0] + "".join(" " + p[0] for p in parts[1:])


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
    # Если это чисто число с ведущими нулями — убираем их.
    # str.isdigit() заменил re.fullmatch(r"0*\d+", s): семантически
    # эквивалентно для практических входов (номера комнат — ASCII), но
    # без regex-движка → нет даже теоретического risk на ReDoS.
    if s.isdigit():
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
    # usedforsecurity=False — явно говорим Python (3.9+) что MD5 здесь
    # используется НЕ для крипто, а для идемпотентного dedup-ключа: это
    # снимает Sonar/Bandit warning про слабую крипту и не меняет хеш —
    # обратная совместимость с уже сохранёнными в БД row_hash сохраняется.
    return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


# =======================================================================
# Fuzzy-matcher
# =======================================================================

def build_users_index(db: Session, with_initials: bool = True) -> tuple[dict[str, dict], list[str], dict[int, dict]]:
    """
    Строит индексы для матчинга:
      - by_name: normalized_fio -> {id, username, room_id, room_number} (для fuzzy)
      - keys:    list[normalized_fio] (для rapidfuzz.extract)
      - by_id:   user_id -> {...} (для резолва alias→user_info)

    ВАЖНО: в by_name кладём ДВА ключа на одного юзера:
      1) полная нормализация:  «иванов иван иванович»
      2) канонический инициальный вид: «иванов и и»
    Так матчится и полное ФИО в подаче, и короткий формат «Иванов И.И.» —
    оба дадут попадание без fuzzy.

    with_initials=False — кладём ТОЛЬКО полное ФИО (без инициального ключа).
    Для строгого режима «точь-в-точь» (импорт 1С / сверка ГИС ГМП), где
    «Иванов И.П.» НЕ должен совпасть с «Иванов Иван Петрович».
    """
    users = db.execute(
        select(User, Room)
        .outerjoin(Room, User.room_id == Room.id)
        .where(User.is_deleted.is_(False))
    ).all()

    by_name: dict[str, dict] = {}
    by_id: dict[int, dict] = {}
    for user, room in users:
        info = {
            "id": user.id,
            "username": user.username,
            "room_id": room.id if room else None,
            "room_number": room.room_number if room else None,
            # housing_001/E2-B: фронт sync помечает совпавшие строки
            # с домом-помещением как conflict — гасит лишний import.
            "place_type": (room.place_type if room else None),
        }
        by_id[user.id] = info
        full = normalize_fio(user.username)
        short = canonical_initials(user.username)
        # Длинная форма приоритетна — если два разных юзера дают одинаковый
        # short («Иванов Иван Иванович» и «Иванов Иннокентий Иванович»),
        # short-ключ схлопнется и match_user пометит как conflict через fuzzy.
        if full:
            by_name[full] = info
        # short добавляем только если отличается — иначе шум в keys-списке.
        # И только если его ещё нет — полное имя в конфликте не перезапишет short
        # другого юзера.
        if with_initials and short and short != full and short not in by_name:
            by_name[short] = info
    return by_name, list(by_name.keys()), by_id


def build_aliases_index(db: Session) -> dict[str, int]:
    """Загружает все алиасы: normalized_fio -> user_id.
    Используется sync для мгновенного матча подач от родственников
    без обращения к fuzzy-логике.

    При лукапе в match_user пробуем И нормализованное полное ФИО,
    И canonical_initials — так старые алиасы (сохранённые до унификации
    с точками «иванов и.и.») продолжат работать, а новые сохраняются
    в чистом виде без точек.
    """
    from app.modules.utility.models import GSheetsAlias
    rows = db.execute(select(GSheetsAlias.alias_fio_normalized, GSheetsAlias.user_id)).all()
    aliases: dict[str, int] = {}
    for norm, uid in rows:
        if not norm:
            continue
        aliases[norm] = uid
        # Страховка: перенормализуем старую запись на свежую нормализацию
        # (на случай когда в БД лежит «иванов и.и.» с точками) и добавим
        # дополнительный ключ. canonical_initials тоже.
        re_norm = normalize_fio(norm)
        if re_norm and re_norm != norm:
            aliases.setdefault(re_norm, uid)
        canon = canonical_initials(norm)
        if canon:
            aliases.setdefault(canon, uid)
    return aliases


def match_user(
    raw_fio: str,
    raw_room: Optional[str],
    users_map: dict[str, dict],
    users_keys: list[str],
    users_by_id: Optional[dict[int, dict]] = None,
    aliases_map: Optional[dict[str, int]] = None,
    fuzzy: bool = True,
) -> tuple[Optional[dict], int, Optional[str]]:
    """
    Возвращает (user_info | None, score 0..100, conflict_reason | None).

    Порядок попыток:
    0) Если ФИО есть в `aliases_map` — берём оттуда (это запомненная админом
       связка «жена X подаёт за мужа X»). Score=100, conflict не проверяем
       по комнате — алиас намеренно нарушает соответствие комнаты.
    1) Точное совпадение нормализованного ФИО.
    2) Fuzzy token_sort_ratio (устойчив к перестановке слов).
    3) Если несколько кандидатов с почти равным максимальным score (≥95) —
       conflict, админ выбирает вручную.
    """
    norm = normalize_fio(raw_fio)
    if not norm:
        return None, 0, None
    # Canonical (фамилия + инициалы) — ДОПОЛНИТЕЛЬНЫЙ ключ, матчит разные
    # форматы одного человека (полный vs инициальный).
    canon = canonical_initials(raw_fio)

    # 0. Запомненный alias (родственник). Самый сильный сигнал.
    # Пробуем ОБА вида ключа: полную нормализацию и canonical. Так старые
    # записи алиасов (сохранённые до унификации normalize — с точками)
    # продолжат работать, а новые сохраняются в canonical форме.
    if aliases_map and users_by_id:
        for key in (norm, canon):
            if key and key in aliases_map:
                uid = aliases_map[key]
                info = users_by_id.get(uid)
                if info:
                    return info, 100, None

    # Точное совпадение. Строгий режим (fuzzy=False) — ТОЛЬКО полное ФИО, без
    # canonical-инициалов: «Иванов И.П.» не совпадёт с «Иванов Иван Петрович».
    exact_keys = (norm,) if not fuzzy else (norm, canon)
    for key in exact_keys:
        if key and key in users_map:
            user = users_map[key]
            return user, 100, _check_room_conflict(user, raw_room)

    # Строгий режим: без нечёткого матчинга — не нашли точно → not_found.
    if not fuzzy:
        return None, 0, None

    # Fuzzy: extract топ-5 кандидатов
    candidates = process.extract(
        norm, users_keys,
        scorer=fuzz.token_sort_ratio,
        limit=5,
    )
    if not candidates:
        return None, 0, None

    best_name, best_score, _ = candidates[0]
    best_score = int(best_score)

    if best_score < _fuzzy_threshold():
        return None, best_score, None

    # Проверяем: есть ли ДРУГИЕ кандидаты с тем же или почти тем же score?
    # Пороги читаются из конфига админа.
    auto_thr = _auto_approve_threshold()
    band = _ambiguity_band()
    near_top = [
        (name, int(s)) for name, s, _ in candidates
        if int(s) >= auto_thr and int(s) >= best_score - band
    ]

    if len(near_top) >= 2:
        # Несколько одинаково "хороших" вариантов → conflict
        names_preview = ", ".join(
            users_map[n]["username"] for n, _ in near_top[:3]
        )
        conflict = (
            f"Найдено несколько похожих жильцов ({len(near_top)}): {names_preview}. "
            "Выберите нужного через «Переназначить»."
        )
        # Возвращаем первого как "догадку", но статус conflict.
        return users_map[best_name], best_score, conflict

    user = users_map[best_name]
    conflict = _check_room_conflict(user, raw_room)
    return user, best_score, conflict


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
    """
    Скачивает CSV из Google Sheets (синхронно, для Celery).

    Стратегия:
      1. Пытаемся через /gviz/tq?tqx=out:csv — стабильный endpoint без редиректов,
         работает для всех публичных таблиц.
      2. Если упало — fallback на /export?format=csv с правильным User-Agent.

    Без этого порядка получаем 307 → 400 от googleusercontent.com.
    """
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/csv,text/plain,*/*",
    }

    urls = [
        ("gviz", GSHEETS_GVIZ_URL.format(sheet_id=sheet_id, gid=gid)),
        ("export", GSHEETS_EXPORT_URL.format(sheet_id=sheet_id, gid=gid)),
    ]

    last_err: Optional[Exception] = None
    for label, url in urls:
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()

            content_type = (resp.headers.get("content-type") or "").lower()
            text = resp.text

            # Если в ответе HTML — это страница логина Google (таблица закрыта).
            if "html" in content_type or text.lstrip().lower().startswith("<!doctype"):
                raise RuntimeError(
                    "Google Sheets вернул HTML вместо CSV — скорее всего таблица "
                    "закрыта от публики. Откройте «Поделиться → Все, у кого есть "
                    "ссылка → Читатель»."
                )

            # gviz endpoint иногда возвращает application/octet-stream — это нормально.
            # Главное чтобы не HTML.
            logger.info(f"[GSHEETS] Successfully fetched via {label} endpoint")
            return text

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.warning(f"[GSHEETS] {label} endpoint failed: {e}")
            last_err = e
            continue

    raise RuntimeError(
        f"Не удалось скачать таблицу ни через один endpoint. Последняя ошибка: {last_err}"
    )


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

    users_map, users_keys, users_by_id = build_users_index(db)
    aliases_map = build_aliases_index(db)
    logger.info(
        f"[GSHEETS] Index ready: {len(users_map)} users, {len(aliases_map)} aliases"
    )

    stats = {
        "total_rows": len(raw_rows),
        "inserted": 0, "duplicate": 0,
        "matched": 0, "unmatched": 0,
        "conflicts": 0, "auto_approved": 0,
        # Раньше `pending` сидел внутри `matched` без отдельного счётчика —
        # админ видел «матч: 10», и не понимал, что 3 из них требуют ручной
        # проверки. После инцидента с Левшиным (его подача висела как
        # pending и не доехала до MeterReading) выделяем явно.
        "pending": 0,
        "errors": 0,
        "skipped_too_old": 0,
    }

    # Cutoff для отсечения исторических подач: жилец мог оставлять
    # показания в гугл-таблице 2-3 года, а нам нужны только последние
    # ~3 месяца (текущий + предыдущий + запас на поздние подачи).
    # Без этого фильтра gsheets_import_rows растёт неограниченно, и
    # админ видит сотни «зависших» подач за давно закрытые периоды.
    # Граница = max(today - max_age_days, cutoff_date) — см.
    # _effective_age_cutoff(). cutoff_date — фиксированная дата
    # (не сдвигается каждый день).
    age_cutoff = _effective_age_cutoff()

    # ОПТИМИЗАЦИЯ N+1 (apr 2026): раньше для каждой строки делали отдельный
    # pg_insert(...).on_conflict_do_nothing() — на 1000+ строк это 1000 round-trip
    # до Postgres внутри одной задачи (Sentry ловит как N+1). Теперь сначала
    # парсим/матчим всё в памяти, потом батчами по 500 делаем один INSERT
    # с values=[...] и RETURNING row_hash чтобы понять inserted vs duplicate.
    auto_approve_thr = _auto_approve_threshold()

    records: list[dict] = []
    for row in raw_rows:
        try:
            ts = parse_timestamp(row["timestamp"])

            # Отсекаем старые подачи ДО любого парсинга и матчинга —
            # чтобы не тратить CPU на rapidfuzz и не засорять БД.
            # Если timestamp не распарсился (None) — пропускаем фильтр,
            # пусть строка залетает с status='conflict' (без даты её
            # сложно валидно обработать дальше).
            if ts is not None and ts < age_cutoff:
                stats["skipped_too_old"] += 1
                continue

            hot = parse_decimal(row["hot"])
            cold = parse_decimal(row["cold"])

            row_hash = compute_row_hash(
                ts, row["fio"], row["room"] or "",
                row["hot"] or "", row["cold"] or "",
            )

            user_info, score, conflict = match_user(
                row["fio"], row["room"], users_map, users_keys,
                users_by_id=users_by_id, aliases_map=aliases_map,
            )

            # Sanity-проверка значений ДО присвоения статуса. Жилец, скорее
            # всего, записал «01427.957» без точки, и парсер дал 1 427 957 м³.
            # Auto-approve таких показаний — катастрофа: расчёт корректно
            # умножит на тариф и выдаст счёт в миллионы.
            value_overflow: list[str] = []
            if hot is not None and hot > MAX_WATER_METER_VALUE:
                value_overflow.append(f"hot={hot}>{MAX_WATER_METER_VALUE}")
            if cold is not None and cold > MAX_WATER_METER_VALUE:
                value_overflow.append(f"cold={cold}>{MAX_WATER_METER_VALUE}")

            # housing_001/E2-B: если матченный жилец живёт в доме
            # (place_type='house'), счётчиков у него нет — любая
            # gsheets-подача от его имени должна быть помечена как
            # conflict с понятным reason, а не идти в auto_approved.
            # Это защита от ситуации «жилец дома по ошибке заполнил
            # таблицу как общажный».
            if user_info is not None and user_info.get("place_type") == "house":
                conflict = (
                    "house_place_type_no_meters: жилец живёт в "
                    "доме/квартире, счётчиков нет — подача показаний "
                    "не требуется. Отклоните эту строку либо "
                    "переназначьте на жильца общежития."
                )

            # Определяем статус (порог из конфига).
            if user_info is None:
                status = "unmatched"
            elif conflict:
                status = "conflict"
            elif value_overflow:
                # Подача матчится с жильцом и комнатой, но значения мусорные —
                # отправляем в conflict с понятной причиной, чтобы админ
                # увидел в админке и попросил жильца переподать.
                status = "conflict"
                conflict = (
                    f"value_too_large: {', '.join(value_overflow)}. "
                    f"Скорее всего пропущена десятичная точка."
                )
            elif score >= auto_approve_thr:
                status = "auto_approved"
            else:
                status = "pending"

            records.append({
                "sheet_timestamp": ts,
                "raw_fio": row["fio"] or "",
                "raw_dormitory": row["dormitory"],
                "raw_room_number": row["room"],
                "raw_hot_water": row["hot"],
                "raw_cold_water": row["cold"],
                "hot_water": hot,
                "cold_water": cold,
                "matched_user_id": user_info["id"] if user_info else None,
                "matched_room_id": user_info["room_id"] if user_info else None,
                "match_score": score,
                "status": status,
                "conflict_reason": conflict,
                "row_hash": row_hash,
            })

        except Exception as e:
            logger.warning(f"[GSHEETS] Row {row.get('_index')} failed: {e}")
            stats["errors"] += 1

    # Дедуп внутри батча: если в CSV одна и та же строка встречается дважды,
    # оба попадут в records, но в pg_insert.values(...) duplicate row_hash
    # внутри одного INSERT даст constraint error. Оставляем первое вхождение.
    seen_hashes: set[str] = set()
    deduped: list[dict] = []
    for r in records:
        if r["row_hash"] in seen_hashes:
            stats["duplicate"] += 1
            continue
        seen_hashes.add(r["row_hash"])
        deduped.append(r)

    CHUNK = 500
    inserted_hashes: set[str] = set()
    for i in range(0, len(deduped), CHUNK):
        batch = deduped[i:i + CHUNK]
        stmt = (
            pg_insert(GSheetsImportRow)
            .values(batch)
            .on_conflict_do_nothing(index_elements=["row_hash"])
            .returning(GSheetsImportRow.row_hash)
        )
        for (h,) in db.execute(stmt):
            inserted_hashes.add(h)

    # Агрегируем статистику по in-memory данным.
    for r in deduped:
        if r["row_hash"] in inserted_hashes:
            stats["inserted"] += 1
            status = r["status"]
            if status == "unmatched":
                stats["unmatched"] += 1
            elif status == "conflict":
                stats["conflicts"] += 1
            elif status == "auto_approved":
                stats["auto_approved"] += 1
                stats["matched"] += 1
            else:
                # pending — матч есть, но score между fuzzy_threshold и auto_approve.
                # Требует ручного «утвердить» в админке. Отдельный счётчик чтобы
                # админ сразу видел «3 на проверке» в тоасте sync.
                stats["pending"] += 1
                stats["matched"] += 1
        else:
            stats["duplicate"] += 1

    db.commit()

    # Сразу после импорта продвигаем auto_approved → MeterReading.
    # Без этого 1000+ строк висят в статусе "автоутверждено" и не попадают
    # в сводку / расчёты, потому что фактического MeterReading нет.
    try:
        promoted = promote_auto_approved_rows(db)
        stats["promoted_readings"] = promoted.get("created", 0)
        stats["promote_skipped"]   = promoted.get("skipped", 0)
    except Exception as e:
        logger.warning(f"[GSHEETS] promote_auto_approved_rows failed: {e}")
        stats["promoted_readings"] = 0

    logger.info(f"[GSHEETS] Sync finished: {stats}")
    return stats


# =======================================================================
# PROMOTE AUTO_APPROVED → MeterReading
# =======================================================================
# Gsheets помечает строки status="auto_approved", но сам по себе этот
# статус ничего не создаёт в таблице readings. Раньше админ должен был
# кликать «утвердить» вручную, и тысячи строк просто лежали невидимыми
# для отчётности. Эта функция обходит все auto_approved с reading_id=NULL
# и создаёт под них MeterReading (минимальный, total=0 — расчёт подхватит
# потом пересчёт периода). Идемпотентно по (user_id, period_id).

_MONTH_NAMES_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def _ensure_active_period(db: Session):
    """Возвращает активный BillingPeriod, создавая его при необходимости.

    Раньше promote_auto_approved_rows возвращал no_active_period и тысячи
    auto_approved строк висели без MeterReading — невидимые в финотчёте.
    Теперь если активного периода нет — создаём по текущему месяцу
    («Апрель 2026»). Идемпотентно: если период с таким именем уже есть,
    активируем его.
    """
    from app.modules.utility.models import BillingPeriod
    from datetime import date as _date

    active = db.query(BillingPeriod).filter(BillingPeriod.is_active.is_(True)).first()
    if active:
        return active

    today = _date.today()
    name = f"{_MONTH_NAMES_RU[today.month]} {today.year}"
    existing = db.query(BillingPeriod).filter(BillingPeriod.name == name).first()
    if existing:
        existing.is_active = True
        db.flush()
        logger.info(f"[GSHEETS-PROMOTE] reactivated existing period '{name}'")
        return existing

    period = BillingPeriod(name=name, is_active=True)
    db.add(period)
    db.flush()
    logger.info(f"[GSHEETS-PROMOTE] auto-created active period '{name}'")
    return period


def promote_auto_approved_rows(db: Session, target_period=None) -> dict:
    """Продвигает GSheetsImportRow со статусом auto_approved в MeterReading.

    Логика:
      1. Группируем все auto_approved строки без reading_id по жильцу.
      2. Для каждого жильца берём ОДНУ строку с максимальным sheet_timestamp
         (последняя поданная подача — самые свежие показания счётчика).
         Раньше использовалась произвольная «первая попавшаяся» из .all() —
         иногда подавалось старое значение, а более свежая запись в gsheets
         висела без MeterReading.
      3. Создаём MeterReading на активный период; остальные сестринские строки
         того же жильца биндим к тому же reading_id (закрываем как обработанные).

    Параметр `target_period` (Bug 29.05.2026, Коммит 17):
      Если None — используется текущий активный BillingPeriod (как было).
      Если задан — создаём reading'и в этом периоде, и фильтруем rows ТОЛЬКО
      теми у которых sheet_timestamp попадает в календарный месяц этого периода.
      Это нужно для исторического promote (Январь/Февраль/Март/Апрель 2026):
      53 подачи зависли auto_approved потому что они от прошлых месяцев, а
      promote по умолчанию обрабатывает только активный.

    Возвращает {'created': N, 'skipped': M, 'bound': K, 'errors': [...]}.
    """
    from decimal import Decimal as _Dec
    from datetime import datetime as _dt
    from app.modules.utility.models import (
        MeterReading, GSheetsImportRow,
    )
    from app.modules.utility.services.period_helpers import period_chron_key

    active_period = target_period if target_period is not None else _ensure_active_period(db)

    # КРИТИЧНО (фикс инцидента may 2026 — «Пегарьков А.В.»):
    # Promote НЕ фильтровал по sheet_timestamp. Строки от 2023 года, давно
    # лежавшие в gsheets_import_rows как auto_approved + reading_id=NULL
    # (так бывает после рестартов, миграций, отключенного бота), при
    # очередном запуске promote создавали MeterReading в АКТИВНОМ периоде
    # с values из 2023. Затем следующий месяц считал дельту от этих
    # значений → счёт +73 699 ₽ за «выросший на 111 кубов» расход.
    #
    # Now: пропускаем строки старше gsheets.max_age_days (дефолт 90).
    # Это тот же порог что используется в sync для новых строк. Старые
    # «застрявшие» строки помечаем как rejected отдельным механизмом —
    # здесь только filter, чтобы они не превращались в фейковые reading'и.
    # Граница та же что и при sync (см. _effective_age_cutoff).
    from sqlalchemy import or_ as _or
    cutoff = _effective_age_cutoff()

    rows = db.query(GSheetsImportRow).filter(
        GSheetsImportRow.status == "auto_approved",
        GSheetsImportRow.reading_id.is_(None),
        GSheetsImportRow.matched_user_id.is_not(None),
        GSheetsImportRow.hot_water.is_not(None),
        GSheetsImportRow.cold_water.is_not(None),
        # Только относительно свежие подачи. NULL-timestamp допускаем
        # (legacy-данные до введения parse_timestamp), но логируем —
        # сейчас будет видно сколько таких в WARNING'е ниже.
        _or(
            GSheetsImportRow.sheet_timestamp.is_(None),
            GSheetsImportRow.sheet_timestamp >= cutoff,
        ),
    ).all()

    # Bug 29.05.2026 (Коммит 17): если target_period передан, фильтруем
    # rows ТОЛЬКО теми что в его календарном месяце. Иначе historical
    # promote для Февраль создал бы reading'и для всех (включая Май)
    # в Февральском периоде — это double-charging.
    if target_period is not None:
        target_chron = period_chron_key(target_period.name)
        if target_chron != (0, 0):
            rows = [
                r for r in rows
                if r.sheet_timestamp is not None
                and (r.sheet_timestamp.year, r.sheet_timestamp.month) == target_chron
            ]
            logger.info(
                "[GSHEETS-PROMOTE] target_period='%s' (chron=%s) — фильтр оставил %d rows",
                target_period.name, target_chron, len(rows),
            )

    # Disgnостика: сколько promote'нутых строк имели NULL timestamp и
    # сколько было «слишком старых» — для отслеживания на проде.
    stale_count = db.query(GSheetsImportRow).filter(
        GSheetsImportRow.status == "auto_approved",
        GSheetsImportRow.reading_id.is_(None),
        GSheetsImportRow.matched_user_id.is_not(None),
        GSheetsImportRow.hot_water.is_not(None),
        GSheetsImportRow.cold_water.is_not(None),
        GSheetsImportRow.sheet_timestamp.is_not(None),
        GSheetsImportRow.sheet_timestamp < cutoff,
    ).count()
    if stale_count > 0:
        logger.warning(
            "[GSHEETS-PROMOTE] %d auto_approved rows SKIPPED as too old "
            "(sheet_timestamp < cutoff=%s). Admin should review and reject "
            "them — иначе они навсегда останутся в auto_approved без reading.",
            stale_count, cutoff.isoformat(),
        )

    if not rows:
        # Раньше тут не было period_name в payload — caller-скрипты ловили
        # KeyError. Возвращаем стабильный shape всегда.
        return {
            "created": 0, "skipped": 0, "bound": 0, "errors": [],
            "period_name": active_period.name,
        }

    # Группируем по жильцу + выбираем самую свежую подачу как «основную».
    by_user: dict[int, list] = {}
    for r in rows:
        by_user.setdefault(r.matched_user_id, []).append(r)
    for uid, lst in by_user.items():
        lst.sort(
            key=lambda r: (r.sheet_timestamp or _dt.min, r.id),
            reverse=True,  # самые свежие первыми
        )

    # ОПТИМИЗАЦИЯ N+1 (apr 2026): раньше внутри цикла на каждого жильца
    # делалось 3 отдельных запроса (User, dup MeterReading, prev electricity).
    # Теперь — 3 batch-запроса до цикла по всем user_id сразу.
    from sqlalchemy import func as _sa_func
    user_ids = list(by_user.keys())

    users_by_id_local: dict[int, User] = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(user_ids)).all()
    }

    # Ищем ВСЕ существующие reading'и (approved И draft) в активном периоде.
    # Раньше смотрели только approved — но если у жильца висит draft (например
    # admin создал заготовку через manual receipt, или два sync'а гонялись
    # параллельно и оставили дубль), promote всё равно делал INSERT и падал
    # на UNIQUE constraint (или silent rollback всей транзакции). После
    # инцидента с Левшиным (24 жильца в мае стояли с processed_at=NULL)
    # переходим на UPSERT: approved → bind, draft → update + approve, нет
    # ничего → insert.
    existing_by_user: dict[int, list[MeterReading]] = {}
    for mr in db.query(MeterReading).filter(
        MeterReading.user_id.in_(user_ids),
        MeterReading.period_id == active_period.id,
    ).all():
        existing_by_user.setdefault(mr.user_id, []).append(mr)
    # approved-кэш для быстрого bind-case
    existing_dup_by_user: dict[int, MeterReading] = {
        uid: next((mr for mr in lst if mr.is_approved), None)
        for uid, lst in existing_by_user.items()
    }
    existing_dup_by_user = {k: v for k, v in existing_dup_by_user.items() if v is not None}

    # Последнее утверждённое показание по каждому жильцу — для дельт при
    # расчёте. Раньше брали только electricity (gsheets его не передаёт),
    # но без hot/cold tariff×volume=0 не получалось бы посчитать. Теперь
    # одним subquery достаём всё нужное (DISTINCT ON через row_number).
    #
    # КРИТИЧНО: order_by period_id.desc(), а НЕ created_at.desc(). Жильцы
    # импортируют исторические подачи задним числом, и created_at не
    # отражает биллинговую хронологию. Берём period_id < active_period.id —
    # т.е. строго ПРОШЛЫЕ периоды, и среди них самый свежий.
    prev_subq = (
        db.query(
            MeterReading.user_id.label("uid"),
            MeterReading.id.label("mr_id"),
            MeterReading.hot_water.label("hot"),
            MeterReading.cold_water.label("cold"),
            MeterReading.electricity.label("elect"),
            _sa_func.row_number().over(
                partition_by=MeterReading.user_id,
                order_by=MeterReading.period_id.desc(),
            ).label("rn"),
        )
        .filter(
            MeterReading.user_id.in_(user_ids),
            MeterReading.is_approved.is_(True),
            MeterReading.period_id < active_period.id,
        )
        .subquery()
    )
    prev_by_user: dict[int, MeterReading] = {}
    prev_elect_by_user: dict[int, _Dec] = {}
    for uid, mr_id, hot, cold, elect, rn in db.query(
        prev_subq.c.uid, prev_subq.c.mr_id,
        prev_subq.c.hot, prev_subq.c.cold, prev_subq.c.elect, prev_subq.c.rn,
    ).filter(prev_subq.c.rn == 1).all():
        # Подгружаем сам MeterReading (не только id) — нужен compute_reading_breakdown.
        mr = db.query(MeterReading).filter(MeterReading.id == mr_id).first()
        if mr:
            prev_by_user[uid] = mr
        if elect is not None:
            prev_elect_by_user[uid] = elect

    # Тарифный кеш и helper расчёта — импорт здесь чтобы избежать
    # циклических импортов при загрузке модуля.
    from app.modules.utility.services.tariff_cache import tariff_cache
    from app.modules.utility.services.reading_calculator import (
        compute_reading_breakdown, CalculationError,
    )
    from app.modules.utility.services.calculations import (
        costs_for_model_fields,
    )
    # Сезонные флаги — читаем один раз перед циклом, иначе по запросу
    # на каждую строку gsheets под нагрузкой.
    from app.modules.utility.routers.settings import load_seasonal_sync
    _seasonal = load_seasonal_sync(db)

    created = 0
    skipped = 0
    bound = 0
    errors: list[dict] = []

    # Helper: однотипно регистрируем skip-причину (с WARN-логом, чтобы
    # видеть в worker logs, а не только в payload errors[]).
    def _skip(uid, reason, user_rows, **extra):
        nonlocal skipped
        skipped += len(user_rows)
        payload = {"user_id": uid, "reason": reason,
                   "rows": [r.id for r in user_rows], **extra}
        errors.append(payload)
        logger.warning("[GSHEETS-PROMOTE] skip user=%s rows=%s reason=%s extra=%s",
                       uid, [r.id for r in user_rows], reason, extra)

    for uid, user_rows in by_user.items():
        user = users_by_id_local.get(uid)
        if not user or user.is_deleted or not user.room_id:
            _skip(uid, "user_missing_or_no_room", user_rows)
            continue

        # housing_001/E2-B: дом → счётчиков нет → reading не создаём.
        # Помечаем все строки этого жильца как обработанные с понятным
        # reason. Без этого auto_approved-строки висели бы вечно (promote
        # пытается их продвинуть, но reading не имеет смысла).
        _user_room = db.query(Room).filter(Room.id == user.room_id).first()
        if _user_room and _user_room.place_type == "house":
            from sqlalchemy import update as _sa_update_house
            from app.modules.utility.models import GSheetsImportRow as _GR_house
            _reason = (
                "house_place_type_no_meters: жилец живёт в доме/квартире, "
                "счётчиков нет — подача показаний не требуется."
            )
            db.execute(
                _sa_update_house(_GR_house)
                .where(_GR_house.id.in_([r.id for r in user_rows]))
                .values(status="rejected", conflict_reason=_reason,
                        processed_at=utcnow())
            )
            _skip(uid, _reason, user_rows)
            continue

        # Холостяк (per_capita) не подаёт показания счётчика — все его строки
        # помечаем как обработанные (без reading), чтобы не висели в auto_approved.
        if getattr(user, "billing_mode", "by_meter") == "per_capita":
            _skip(uid, "per_capita_no_meter", user_rows)
            continue

        # Если в текущем периоде уже есть утверждённый MeterReading — биндим
        # ВСЕ строки жильца к нему (закрываем «висящие» auto_approved).
        dup = existing_dup_by_user.get(uid)
        if dup:
            for r in user_rows:
                r.reading_id = dup.id
                r.processed_at = utcnow()
            bound += len(user_rows)
            continue

        # Берём САМУЮ СВЕЖУЮ подачу (первая в отсортированном списке).
        primary = user_rows[0]

        # Финальный sanity-check (defence in depth): даже если строка как-то
        # прошла валидацию на этапе sync (старая запись, ручная вставка,
        # будущее изменение порога) — не создаём reading с гарантированно
        # бракованными значениями. Скрипт audit_calculations покажет такие
        # строки с status=auto_approved + reading_id=NULL для разбора.
        if (primary.hot_water and primary.hot_water > MAX_WATER_METER_VALUE) or \
           (primary.cold_water and primary.cold_water > MAX_WATER_METER_VALUE):
            _skip(uid, "value_too_large_skipped", user_rows,
                  hot=str(primary.hot_water), cold=str(primary.cold_water))
            continue

        # Электричество в gsheets не передаётся — берём последнее известное.
        electricity_value = prev_elect_by_user.get(uid, _Dec("0"))

        # Расчёт суммы СРАЗУ при создании reading. Раньше сохранялось
        # total_cost=0 → жилец видел нулевую квитанцию при реальной
        # подаче, деньги физически не начислялись. См. инцидент may 2026.
        prev_reading = prev_by_user.get(uid)

        # Bug G: проверка delta ДО расчёта стоимости. prev_by_user уже
        # отфильтрован через is_meaningful_prev (см. вычисление выше),
        # т.е. если AUTO_GENERATED 0/0/0 — он сюда не попадёт. Нам нужен
        # сырой «есть ли что-то в истории» — берём из existing_by_user.
        _hist = existing_by_user.get(uid, [])
        _approved_hist = [r for r in _hist if r.is_approved]
        _approved_hist.sort(key=lambda r: r.created_at, reverse=True)
        _prev_any = _approved_hist[0] if _approved_hist else None
        _prev_is_synth = (prev_reading is None) and (_prev_any is not None)
        if _prev_is_synth:
            _val_prev_hot = _prev_any.hot_water or _Dec("0")
            _val_prev_cold = _prev_any.cold_water or _Dec("0")
            _val_prev_elect = _prev_any.electricity or _Dec("0")
        elif prev_reading is not None:
            _val_prev_hot = prev_reading.hot_water or _Dec("0")
            _val_prev_cold = prev_reading.cold_water or _Dec("0")
            _val_prev_elect = prev_reading.electricity or _Dec("0")
        else:
            _val_prev_hot = _val_prev_cold = _val_prev_elect = None

        from app.modules.utility.services.reading_validators import (
            validate_meter_reading as _validate_mr,
        )
        _vmr = _validate_mr(
            hot=primary.hot_water,
            cold=primary.cold_water,
            elect=electricity_value,
            prev_hot=_val_prev_hot,
            prev_cold=_val_prev_cold,
            prev_elect=_val_prev_elect,
            is_baseline=(prev_reading is None and not _prev_is_synth),
            prev_is_synth=_prev_is_synth,
        )
        if not _vmr.ok:
            from sqlalchemy import update as _sa_update_vmr
            from app.modules.utility.models import GSheetsImportRow as _GR_vmr
            _reason = "high_delta_or_baseline_overflow: " + "; ".join(_vmr.errors)
            db.execute(
                _sa_update_vmr(_GR_vmr)
                .where(_GR_vmr.id.in_([r.id for r in user_rows]))
                .values(status="conflict", conflict_reason=_reason)
            )
            _skip(uid, _reason, user_rows)
            logger.warning(
                "[GSHEETS-PROMOTE] HIGH_DELTA_OR_BASELINE user=%s prev_synth=%s errors=%s",
                uid, _prev_is_synth, _vmr.errors,
            )
            continue
        room_obj = db.query(Room).filter(Room.id == user.room_id).first()
        tariff = (
            tariff_cache.get_effective_tariff(user=user, room=room_obj)
            if room_obj else None
        )
        if tariff is None:
            _skip(uid, "no_active_tariff", user_rows)
            continue

        # Per-tariff (heating_active + даты) AND global emergency override.
        _heating = _seasonal.heating_season_active and tariff.is_heating_active_now()
        _hw = _seasonal.hot_water_heating_active and tariff.is_hw_heating_active_now()
        try:
            breakdown = compute_reading_breakdown(
                user=user, room=room_obj, tariff=tariff,
                current_hot=primary.hot_water,
                current_cold=primary.cold_water,
                current_elect=electricity_value,
                prev_reading=prev_reading,
                heating_season_active=_heating,
                hot_water_heating_active=_hw,
            )
        except CalculationError as e:
            _skip(uid, f"calculation_error: {e}", user_rows)
            continue

        # Финальный sanity на total_cost — защита от того что delta огромная
        # прошла valid + расчёт дал нереалистичный итог. Помечаем строки conflict
        # с понятным reason — админ разберётся вручную (может опечатка в счётчике).
        from app.modules.utility.services.reading_validators import validate_total_cost
        _tc = validate_total_cost(breakdown.get("total_cost"))
        if not _tc.ok:
            from sqlalchemy import update as _sa_update_tc
            from app.modules.utility.models import GSheetsImportRow as _GR_tc
            _reason = (
                f"total_cost_too_high: расчётный итог {breakdown.get('total_cost')} ₽ "
                f"превышает санитарный потолок. {'; '.join(_tc.errors)}"
            )
            db.execute(
                _sa_update_tc(_GR_tc)
                .where(_GR_tc.id.in_([r.id for r in user_rows]))
                .values(status="conflict", conflict_reason=_reason)
            )
            _skip(uid, _reason, user_rows)
            logger.warning(
                "[GSHEETS-PROMOTE] TOTAL_COST_TOO_HIGH user=%s total=%s",
                uid, breakdown.get("total_cost"),
            )
            continue

        # «Счётчик упал»: новое значение < предыдущего. Физически невозможно.
        # Возможные причины: смена счётчика без оформления, ошибка ввода
        # жильцом (написал текущие 0183 вместо 1830), смена жильца в комнате
        # с обнулением. Раньше тихо проглатывалось (max(0, cur-prev)) и
        # MeterReading создавался с total=0 — жилец видел «-213505 ₽ переплата»
        # из-за прошлых корректировок. После инцидента Шияна (май 2026):
        # не авто-апрувим, переводим строки в conflict + понятный reason.
        if breakdown.get("meter_decreased"):
            from sqlalchemy import update as _sa_update
            from app.modules.utility.models import GSheetsImportRow as _GR
            reason = (
                f"meter_decreased: счётчик 'упал' — "
                f"hot {prev_reading.hot_water}→{primary.hot_water}, "
                f"cold {prev_reading.cold_water}→{primary.cold_water}. "
                f"Возможные причины: смена счётчика без оформления, "
                f"ошибка ввода жильца, или сменился жилец в комнате. "
                f"Проверьте вручную (Замена счётчика / приёмка комнаты)."
            )
            db.execute(
                _sa_update(_GR)
                .where(_GR.id.in_([r.id for r in user_rows]))
                .values(status="conflict", conflict_reason=reason)
            )
            _skip(uid, reason, user_rows)
            logger.warning(
                "[GSHEETS-PROMOTE] METER_DECREASED user=%s prev=hot:%s,cold:%s "
                "current=hot:%s,cold:%s",
                uid, prev_reading.hot_water, prev_reading.cold_water,
                primary.hot_water, primary.cold_water,
            )
            continue

        # cost_* поля для setattr (без total_cost / sanity_warning).
        # is_baseline_flag отличает первую подачу от обычного auto-approve
        # (для UI и фильтров админа).
        is_baseline = breakdown["is_baseline"]
        anomaly_flag = "GSHEETS_AUTO_BASELINE" if is_baseline else "GSHEETS_AUTO"

        # UPSERT-логика. Раньше промоут делал ТОЛЬКО INSERT — если у жильца
        # уже висел draft (admin создал manual receipt, или какой-то sync
        # оставил дубль), новая вставка падала на UNIQUE-constraint и тихо
        # rollback'ила всю транзакцию. После инцидента с Левшиным (24
        # жильца стояли с processed_at=NULL, errors=24) делаем:
        #   1) если есть draft → берём свежайший, обновляем поля, approve;
        #   2) лишние draft'ы того же жильца удаляем (это были артефакты);
        #   3) если drafts нет → создаём новый reading.
        existing_lst = existing_by_user.get(uid, [])
        drafts = [r for r in existing_lst if not r.is_approved]
        reading = None
        if drafts:
            # Берём САМЫЙ свежий draft по created_at — туда писал последний sync.
            drafts.sort(key=lambda r: r.created_at, reverse=True)
            reading = drafts[0]
            # Остальные drafts удаляем — они артефакты дублирования.
            for stale in drafts[1:]:
                logger.warning(
                    "[GSHEETS-PROMOTE] user=%s removing stale draft reading id=%s",
                    uid, stale.id,
                )
                db.delete(stale)
            # Обновляем поля свежими значениями.
            reading.hot_water = primary.hot_water
            reading.cold_water = primary.cold_water
            reading.electricity = electricity_value
            reading.is_approved = True
            reading.anomaly_flags = anomaly_flag
            reading.anomaly_score = 0
            reading.total_cost = breakdown["total_cost"]
            reading.total_209 = breakdown["total_209"]
            reading.total_205 = breakdown["total_205"]
            for k, v in costs_for_model_fields(breakdown).items():
                setattr(reading, k, v)
        else:
            reading = MeterReading(
                user_id=user.id,
                room_id=user.room_id,
                period_id=active_period.id,
                hot_water=primary.hot_water,
                cold_water=primary.cold_water,
                electricity=electricity_value,
                is_approved=True,
                anomaly_flags=anomaly_flag,
                anomaly_score=0,
                total_cost=breakdown["total_cost"],
                total_209=breakdown["total_209"],
                total_205=breakdown["total_205"],
                **costs_for_model_fields(breakdown),
            )
            db.add(reading)

        if breakdown.get("sanity_warning"):
            logger.warning(
                "[GSHEETS-PROMOTE] user=%s sanity_warning: %s",
                uid, breakdown["sanity_warning"],
            )

        # flush в SAVEPOINT'е — если упадёт IntegrityError на UNIQUE/FK/чём-то
        # ещё, не валим ВСЮ транзакцию (24 других жильца). Просто скипаем
        # этого и идём дальше.
        from sqlalchemy.exc import IntegrityError
        try:
            with db.begin_nested():  # SAVEPOINT
                db.flush()
        except IntegrityError as e:
            _skip(uid, f"integrity_error: {e.orig}", user_rows)
            continue
        except Exception as e:
            _skip(uid, f"flush_error: {type(e).__name__}: {e}", user_rows)
            continue

        for r in user_rows:
            r.reading_id = reading.id
            r.processed_at = utcnow()
        created += 1
        bound += len(user_rows) - 1  # primary не считаем как «привязанный к чужому»

    db.commit()
    logger.info(
        f"[GSHEETS-PROMOTE] users={len(by_user)} created={created} "
        f"bound_extra={bound} skipped_rows={skipped} errors={len(errors)}"
    )
    return {
        "created": created,
        "skipped": skipped,
        "bound": bound,
        "errors": errors[:20],  # обрезаем чтобы не раздуть payload
        "period_name": active_period.name,
    }


# =======================================================================
# Извлечение sheet_id из URL (удобство для админа)
# =======================================================================

def extract_sheet_id(url_or_id: str) -> str:
    """Из полного URL Google Sheets или голого ID вытаскивает только ID."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()
