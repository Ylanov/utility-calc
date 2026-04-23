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
    """Приводит ФИО к единому виду для fuzzy-match.

    — lowercase
    — убираем точки/запятые (чтобы «И.И.» и «И. И.» совпали)
    — ё → е (частый источник расхождений)
    — коллапсируем пробелы
    """
    if not fio:
        return ""
    s = str(fio).lower().replace("ё", "е")
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

def build_users_index(db: Session) -> tuple[dict[str, dict], list[str], dict[int, dict]]:
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
        if short and short != full and short not in by_name:
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

    # Точное совпадение по нормализованной строке ИЛИ canonical form
    for key in (norm, canon):
        if key and key in users_map:
            user = users_map[key]
            return user, 100, _check_room_conflict(user, raw_room)

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
                users_by_id=users_by_id, aliases_map=aliases_map,
            )

            # Определяем статус (порог из конфига).
            if user_info is None:
                status = "unmatched"
            elif conflict:
                status = "conflict"
            elif score >= _auto_approve_threshold():
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

def promote_auto_approved_rows(db: Session) -> dict:
    """Продвигает GSheetsImportRow со статусом auto_approved в MeterReading.

    Возвращает {'created': N, 'skipped': M}.
    """
    from decimal import Decimal as _Dec
    from datetime import datetime as _dt
    from app.modules.utility.models import (
        BillingPeriod, MeterReading, GSheetsImportRow,
    )

    # Активный период — показания привязываются к нему.
    active_period = db.query(BillingPeriod).filter(
        BillingPeriod.is_active.is_(True)
    ).first()
    if not active_period:
        return {"created": 0, "skipped": 0, "reason": "no_active_period"}

    # Все auto_approved без reading_id
    rows = db.query(GSheetsImportRow).filter(
        GSheetsImportRow.status == "auto_approved",
        GSheetsImportRow.reading_id.is_(None),
        GSheetsImportRow.matched_user_id.is_not(None),
        GSheetsImportRow.hot_water.is_not(None),
        GSheetsImportRow.cold_water.is_not(None),
    ).all()

    if not rows:
        return {"created": 0, "skipped": 0}

    created = 0
    skipped = 0

    for row in rows:
        user = db.query(User).filter(User.id == row.matched_user_id).first()
        if not user or user.is_deleted or not user.room_id:
            skipped += 1
            continue

        # Холостяк (per_capita) не подаёт показания — пропускаем.
        if getattr(user, "billing_mode", "by_meter") == "per_capita":
            skipped += 1
            continue

        # Защита от дублей: в этом периоде уже может быть утверждённое.
        dup = db.query(MeterReading).filter(
            MeterReading.user_id == user.id,
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(True),
        ).first()
        if dup:
            # Прикрепим row к существующему reading и закроем строку
            row.reading_id = dup.id
            row.processed_at = _dt.utcnow()
            skipped += 1
            continue

        # Последнее показание электричества по жильцу (в gsheets нет колонки
        # электричества — повторяем предыдущее значение, «расход 0»).
        prev_elect = db.query(MeterReading.electricity).filter(
            MeterReading.user_id == user.id,
            MeterReading.is_approved.is_(True),
        ).order_by(MeterReading.created_at.desc()).first()
        electricity_value = (
            prev_elect[0] if prev_elect and prev_elect[0] is not None else _Dec("0")
        )

        reading = MeterReading(
            user_id=user.id,
            room_id=user.room_id,
            period_id=active_period.id,
            hot_water=row.hot_water,
            cold_water=row.cold_water,
            electricity=electricity_value,
            is_approved=True,
            anomaly_flags="GSHEETS_AUTO",
            anomaly_score=0,
            total_cost=_Dec("0"),
            total_209=_Dec("0"),
            total_205=_Dec("0"),
        )
        db.add(reading)
        db.flush()

        row.reading_id = reading.id
        row.processed_at = _dt.utcnow()
        created += 1

    db.commit()
    logger.info(f"[GSHEETS-PROMOTE] created={created}, skipped={skipped}")
    return {"created": created, "skipped": skipped}


# =======================================================================
# Извлечение sheet_id из URL (удобство для админа)
# =======================================================================

def extract_sheet_id(url_or_id: str) -> str:
    """Из полного URL Google Sheets или голого ID вытаскивает только ID."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()
