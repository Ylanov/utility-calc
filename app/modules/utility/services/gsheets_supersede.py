"""Автопогашение устаревших строк буфера GSheets.

Проблема (жалоба 2026-07-15, кейс Хайбуллина): жилец подал через гугл-форму,
строка легла в буфер (pending/conflict), а админ решил месяц ДРУГИМ путём —
ручным вводом, утверждением QR-черновика или Excel. Строка буфера при этом
висела «Конфликтом» в реестре навсегда: её погашали только собственные
approve/reject.

Правило (ужесточено ревью 2026-07-15, 6 находок):
строка ПЕРЕКРЫТА и гасится (status='superseded'), только если ВСЁ разом:
  1) сопоставление ДОВЕРЕННОЕ — match_score >= 95 (порог auto-approve):
     pending/conflict с фаззи-скором ниже могут указывать на ЧУЖОГО жильца
     (Иванова Е.А. ↔ Иванова Е.В.) — их гасить нельзя, пусть решает админ;
  2) у сопоставленного жильца есть УТВЕРЖДЁННОЕ показание за месяц подачи
     (sheet_timestamp → период «Месяц ГГГГ», как в _resolve_period_for_row);
  3) подача была РАНЬШЕ решения месяца (sheet_timestamp < created_at
     утверждённого показания) — уточнение, поданное ПОСЛЕ утверждения,
     не гасим: для него есть штатный 409-диалог «Заменить».
     (sheet_timestamp в MSK, created_at в UTC — сдвиг 3ч работает в
     безопасную сторону: сомнительное НЕ гасится.)

Погашение обратимо: delete_reading при удалении последнего утверждённого
показания месяца возвращает погашенные строки жильца в pending.

status='superseded' — терминальный (выпадает из ACTIVE_STATUSES, реестра и
retention чистит его как rejected). Уведомление жильцу НЕ шлётся: его
показания за месяц есть. Запись — гардированным UPDATE (status ещё
нетерминальный И reading_id IS NULL), чтобы не перетереть параллельный
approve/reject другим админом.

Вызывается self-heal'ом из unified_registry (отдельная сессия).
Коммит — на вызывающем.
"""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_utils import utcnow
from app.modules.utility.models import BillingPeriod, MeterReading, GSheetsImportRow
from app.modules.utility.services.period_helpers import month_period_name

# Нетерминальные статусы буфера (совпадает с выборкой unified_registry).
_UNRESOLVED = ("pending", "conflict", "unmatched", "auto_approved")

# Порог доверия сопоставлению — как у auto-approve в gsheets-импорте.
_TRUSTED_SCORE = 95


async def retire_superseded_rows(
    db: AsyncSession, rows: list[GSheetsImportRow],
) -> int:
    """Гасит перекрытые строки из переданного набора. Возвращает число погашенных.

    rows — уже загруженные кандидаты (обычно выборка реестра: reading_id IS
    NULL + нетерминальный статус); фильтры доверия/времени — здесь. Периоды
    НЕ создаются (в отличие от _resolve_period_for_row): нет периода — нет и
    утверждённого показания за него, гасить нечего.
    """
    candidates = [
        r for r in rows
        if r.matched_user_id
        and r.reading_id is None
        and r.status in _UNRESOLVED
        and r.sheet_timestamp is not None          # без даты месяц не определить
        and (r.match_score or 0) >= _TRUSTED_SCORE  # фаззи ниже — решает админ
    ]
    if not candidates:
        return 0

    # Целевой месяц каждой строки → существующие периоды по имени.
    names = {month_period_name(r.sheet_timestamp) for r in candidates}
    period_by_name: dict[str, BillingPeriod] = {}
    for p in (await db.execute(
        select(BillingPeriod).where(BillingPeriod.name.in_(names))
    )).scalars().all():
        period_by_name[p.name] = p
    if not period_by_name:
        return 0

    # Одним запросом: самое СВЕЖЕЕ утверждённое показание каждой пары
    # (жилец, период) — для проверки «подача была до решения месяца».
    user_ids = {r.matched_user_id for r in candidates}
    period_ids = {p.id for p in period_by_name.values()}
    latest_approved: dict[tuple, object] = dict(
        (((uid, pid), mx) for uid, pid, mx in (await db.execute(
            select(MeterReading.user_id, MeterReading.period_id,
                   func.max(MeterReading.created_at))
            .where(
                MeterReading.user_id.in_(user_ids),
                MeterReading.period_id.in_(period_ids),
                MeterReading.is_approved.is_(True),
            )
            .group_by(MeterReading.user_id, MeterReading.period_id)
        )).all())
    )

    # Группируем погашаемых по периоду — общий текст причины на группу.
    by_period: dict[str, list[int]] = {}
    for r in candidates:
        pname = month_period_name(r.sheet_timestamp)
        p = period_by_name.get(pname)
        if p is None:
            continue
        approved_at = latest_approved.get((r.matched_user_id, p.id))
        if approved_at is None or r.sheet_timestamp >= approved_at:
            continue   # месяц не решён ИЛИ подача новее решения — не гасим
        by_period.setdefault(pname, []).append(r.id)

    retired = 0
    now = utcnow()
    for pname, ids in by_period.items():
        # Гард от гонки с параллельным approve/reject/promote: гасим только
        # если строка ВСЁ ЕЩЁ нетерминальная и без reading_id на момент записи.
        res = await db.execute(
            update(GSheetsImportRow)
            .where(
                GSheetsImportRow.id.in_(ids),
                GSheetsImportRow.status.in_(_UNRESOLVED),
                GSheetsImportRow.reading_id.is_(None),
            )
            .values(
                status="superseded",
                processed_at=now,
                conflict_reason=(
                    f"Перекрыто: за «{pname}» уже есть утверждённое показание "
                    f"(месяц решён другим путём — ручной ввод/QR/Excel)"
                ),
            )
        )
        retired += res.rowcount or 0
    return retired
