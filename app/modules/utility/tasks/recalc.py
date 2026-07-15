# Полный перерасчёт периода текущим тарифом: preview/apply c progress в
# recalc_jobs. Вербатим-перенос из tasks.py (строки 887-1340), поведение 1:1.

from datetime import datetime, timezone

from app.worker import celery
from app.modules.utility.services.reading_calculator import is_meaningful_prev

from ._shared import logger, sync_db_session


# ==========================================================================
# ПОЛНЫЙ ПЕРЕРАСЧЁТ ПЕРИОДА
# ==========================================================================
# Контекст: показания утверждаются и сохраняют total_* значения, посчитанные
# с тарифом на момент утверждения. Если админ потом поменял тариф (или раньше
# его вообще не было), данные устарели — и 1С шлёт некорректные квитанции.
#
# Эта пара задач (_preview и _apply) пересчитывает ВСЕ approved MeterReading
# за данный period_id с текущим эффективным тарифом (Room → User → default).
# Работает поблочно по chunk_size — защита от OOM на 10k+ записях.
# Progress сохраняется в recalc_jobs.progress/processed, чтобы UI мог показывать
# живой progress-bar через polling.
# ==========================================================================

def _recalc_compute_one(db_session, reading, user, room, prev_reading, tariffs_by_active,
                        global_heating_on: bool = True,
                        global_hw_on: bool = True):
    """Пересчитать одно approved-показание с актуальным тарифом.

    Возвращает (new_totals_dict, new_costs_dict). НЕ пишет в БД.
    prev_reading — последнее утверждённое показание по комнате СТРОГО ДО текущего
    (для вычисления дельт; None если эта запись — первая по комнате).
    global_heating_on / global_hw_on — глобальные SystemSetting (emergency override).
    Per-tariff поля (heating_active, heating_season_start/end и т.п.) — берутся
    из выбранного tariff внутри функции через is_*_active_now().
    """
    from decimal import Decimal
    from app.modules.utility.services.tariff_cache import tariff_cache
    from app.modules.utility.services.calculations import calculate_utilities, D

    ZERO = Decimal("0.000")

    tariff = (
        tariff_cache.get_effective_tariff(user=user, room=room)
        or tariffs_by_active
    )

    # BASELINE: первая подача жильца — потребление = 0, но area-based
    # (содержание/найм/ТКО/отопление) ПЛАТЯТСЯ ВСЕГДА. Bug L (фикс
    # may 2026): раньше тут возвращались сплошные нули — area-based
    # начисления ~5000-7000 ₽/мес теряли все жильцы с AUTO_GENERATED
    # baseline. Теперь вызываем calculate_utilities с volume_*=0:
    # water/sewage = 0 (правильно), area-based = area × tariff.
    if prev_reading is None:
        from app.modules.utility.services.calculations import CalculationError as _CE
        try:
            baseline = calculate_utilities(
                user=user, room=room, tariff=tariff,
                volume_hot=ZERO, volume_cold=ZERO,
                volume_sewage=ZERO, volume_electricity_share=ZERO,
                heating_season_active=(global_heating_on and tariff.is_heating_active_now()),
                hot_water_heating_active=(global_hw_on and tariff.is_hw_heating_active_now()),
            )
            base_total = Decimal(str(baseline.get("total_cost") or 0))
            base_205 = Decimal(str(baseline.get("cost_social_rent") or 0))
            base_209 = base_total - base_205
        except _CE as _exc:
            logger.warning(
                "[recalc] baseline calc_utilities failed reading_id=%s: %s",
                reading.id, _exc,
            )
            baseline = {
                "cost_hot_water": ZERO, "cost_cold_water": ZERO, "cost_sewage": ZERO,
                "cost_electricity": ZERO, "cost_maintenance": ZERO, "cost_social_rent": ZERO,
                "cost_waste": ZERO, "cost_fixed_part": ZERO, "total_cost": ZERO,
            }
            base_total = base_205 = base_209 = ZERO

        # Долг/переплата 1С НЕ в ИТОГО (30.05.2026) — только начисление.
        total_209_b = base_209
        total_205_b = base_205
        return {
            "total_209": total_209_b,
            "total_205": total_205_b,
            "total_cost": total_209_b + total_205_b,
            "cost_hot_water": Decimal(str(baseline.get("cost_hot_water") or 0)),
            "cost_cold_water": Decimal(str(baseline.get("cost_cold_water") or 0)),
            "cost_sewage": Decimal(str(baseline.get("cost_sewage") or 0)),
            "cost_electricity": Decimal(str(baseline.get("cost_electricity") or 0)),
            "cost_maintenance": Decimal(str(baseline.get("cost_maintenance") or 0)),
            "cost_social_rent": Decimal(str(baseline.get("cost_social_rent") or 0)),
            "cost_waste": Decimal(str(baseline.get("cost_waste") or 0)),
            "cost_fixed_part": Decimal(str(baseline.get("cost_fixed_part") or 0)),
        }

    p_hot = D(prev_reading.hot_water)
    p_cold = D(prev_reading.cold_water)
    p_elect = D(prev_reading.electricity)

    hot_corr = D(reading.hot_correction or 0)
    cold_corr = D(reading.cold_correction or 0)
    elect_corr = D(reading.electricity_correction or 0)
    sewage_corr = D(reading.sewage_correction or 0)

    d_hot = max(ZERO, (D(reading.hot_water) - p_hot) - hot_corr)
    d_cold = max(ZERO, (D(reading.cold_water) - p_cold) - cold_corr)

    from app.modules.utility.services.calculations import paying_residents
    residents = Decimal(paying_residents(user, room))
    total_room = Decimal(room.total_room_residents if room.total_room_residents and room.total_room_residents > 0 else 1)
    d_elect = max(ZERO, ((residents / total_room) * (D(reading.electricity) - p_elect)) - elect_corr)

    # global flags AND per-tariff (heating_active, season_start/end в самом tariff)
    _heating = global_heating_on and tariff.is_heating_active_now()
    _hw = global_hw_on and tariff.is_hw_heating_active_now()
    costs = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=d_hot, volume_cold=d_cold,
        volume_sewage=d_hot + d_cold,
        volume_electricity_share=d_elect,
        heating_season_active=_heating,
        hot_water_heating_active=_hw,
        # корректировку водоотведения — явным параметром (ревизия регрессии #4)
        sewage_correction=sewage_corr,
    )

    cost_205 = costs["cost_social_rent"]
    cost_209 = costs["total_cost"] - cost_205

    # Санитарный потолок: если пересчёт даёт нереалистичную сумму
    # (> MAX_TOTAL_COST_PER_READING, обычно 100k ₽/период) — НЕ обновляем,
    # возвращаем исходные значения и логируем. Это страховка от bug-инцидентов
    # (см. валидатор reading_validators.py — там 1.48 млрд ₽-инцидент).
    from app.modules.utility.services.reading_validators import validate_total_cost
    _sanity = validate_total_cost(costs["total_cost"])
    if not _sanity.ok:
        logger.warning(
            "[recalc] reading_id=%s skipped: %s (computed total=%s, kept old)",
            reading.id, "; ".join(_sanity.errors), costs["total_cost"],
        )
        return {
            "total_209": reading.total_209 or Decimal("0"),
            "total_205": reading.total_205 or Decimal("0"),
            "total_cost": reading.total_cost or Decimal("0"),
        }

    # При пересчёте debt_209/205 и overpayment_209/205 НЕ трогаем —
    # они пришли из предыдущего периода и не зависят от текущего тарифа.
    # Adjustments тоже не учитываем в total — они применяются в момент
    # первичного approve. Если админ хочет «чистый» пересчёт по тарифу —
    # ему важны именно cost_* поля и total_cost без корректировок долга.
    # Долг/переплата 1С НЕ в ИТОГО (30.05.2026) — только начисление.
    total_209 = cost_209
    total_205 = cost_205

    # Whitelist полей которые реально есть в MeterReading. calculate_utilities
    # возвращает helper-поля типа sanity_warning (для UI), которые нельзя
    # передавать в update().values() — SQLAlchemy ругается Unconsumed column.
    # Раньше bulk_update_mappings молча игнорировал лишние ключи — после
    # перехода на explicit update() пришлось делать whitelist явно.
    _COST_KEYS = (
        "cost_hot_water", "cost_cold_water", "cost_sewage", "cost_electricity",
        "cost_maintenance", "cost_social_rent", "cost_waste", "cost_fixed_part",
    )
    new_fields = {
        "total_209": total_209,
        "total_205": total_205,
        "total_cost": total_209 + total_205,
    }
    for k in _COST_KEYS:
        if k in costs:
            new_fields[k] = costs[k]
    return new_fields


def _recalc_run(job_id: int, apply: bool):
    """Общая логика для preview и apply. Разница — пишем ли результаты в БД.

    Идея реализации:
      * один проход по всем approved readings периода, батчами по 500;
      * для каждой записи считаем новые поля, сравниваем total_cost;
      * собираем агрегат: increased/decreased/unchanged + топ-30 по |delta|;
      * при apply=True — обновляем записи чанком через bulk_update_mappings.
    """
    from decimal import Decimal
    from sqlalchemy.orm import selectinload
    from app.modules.utility.models import RecalcJob, MeterReading, BillingPeriod, Tariff, User

    CHUNK = 500

    with sync_db_session() as db:
        job = db.query(RecalcJob).filter(RecalcJob.id == job_id).first()
        if not job:
            logger.error(f"[RECALC] job_id={job_id} not found")
            return {"status": "error", "error": "job_not_found"}

        if job.status == "cancelled":
            logger.info(f"[RECALC] job {job_id} cancelled before start")
            return {"status": "cancelled"}

        try:
            # Фиксируем запущенный статус
            job.status = "apply_pending" if apply else "preview_pending"
            job.progress = 0
            job.processed = 0
            db.commit()

            period = db.query(BillingPeriod).filter(BillingPeriod.id == job.period_id).first()
            if not period:
                raise ValueError(f"Период id={job.period_id} не найден")

            # Берём любой активный тариф как fallback — вдруг ни user, ни room
            # не указывают эффективный тариф.
            fallback_tariff = (
                db.query(Tariff).filter(Tariff.is_active).order_by(Tariff.id).first()
            )
            if not fallback_tariff:
                raise ValueError("Нет ни одного активного тарифа — пересчёт невозможен")

            total_q = db.query(MeterReading).filter(
                MeterReading.period_id == period.id,
                MeterReading.is_approved.is_(True),
            )
            total = total_q.count()
            job.total_readings = total
            db.commit()

            if total == 0:
                job.status = "preview_ready" if not apply else "done"
                job.progress = 100
                job.diff_summary = {
                    "total": 0, "unchanged": 0, "increased": 0, "decreased": 0,
                    "sum_old": "0.00", "sum_new": "0.00", "delta": "0.00", "top": [],
                }
                if apply:
                    job.applied_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()
                return {"status": job.status, "total": 0}

            # Сезонные флаги читаем ОДИН раз перед обходом всех reading'ов.
            # При перерасчёте 5000 квитанций без этого было бы 5000 SELECT'ов
            # за SystemSetting. compute использует тот же набор флагов что
            # и /api/calculate, иначе recalc находил бы ложный «дрейф».
            from app.modules.utility.routers.settings import load_seasonal_sync
            _seasonal = load_seasonal_sync(db)

            unchanged = increased = decreased = 0
            sum_old = Decimal("0")
            sum_new = Decimal("0")
            top_diffs = []  # [(abs_delta, dict_item)]

            offset = 0
            while offset < total:
                # Важно: readings — это ORM-объекты, user+room подгружаем eager
                # чтобы внутри чанка не было N+1.
                chunk = (
                    db.query(MeterReading)
                    .options(
                        selectinload(MeterReading.user).selectinload(User.room),
                    )
                    .filter(
                        MeterReading.period_id == period.id,
                        MeterReading.is_approved.is_(True),
                    )
                    .order_by(MeterReading.id)
                    .offset(offset)
                    .limit(CHUNK)
                    .all()
                )
                if not chunk:
                    break

                # ОПТИМИЗАЦИЯ N+1 (apr 2026): раньше для каждой записи в chunk
                # делался отдельный SELECT на prev MeterReading по паре
                # (user_id, room_id). На 5000 readings = 5000 round-trip'ов до БД.
                # Теперь один запрос за весь chunk, in-memory поиск prev.
                #
                # ДЕТЕРМИНИЗМ (may 2026): сортировка ИСКЛЮЧИТЕЛЬНО по
                # period_id + created_at + id (стабильный порядок). Раньше
                # сортировка была только по created_at — при readings с
                # одинаковым timestamp порядок плыл между вызовами,
                # «Перерасчёт» давал разные суммы при одном и том же тарифе.
                chunk_user_ids = list({r.user_id for r in chunk if r.user_id})
                chunk_room_ids = list({r.room_id for r in chunk if r.room_id})

                prev_by_pair: dict[tuple[int, int], list] = {}
                if chunk_user_ids and chunk_room_ids:
                    for mr in db.query(MeterReading).filter(
                        MeterReading.user_id.in_(chunk_user_ids),
                        MeterReading.room_id.in_(chunk_room_ids),
                        MeterReading.is_approved.is_(True),
                    ).order_by(
                        MeterReading.user_id,
                        MeterReading.room_id,
                        MeterReading.period_id,
                        MeterReading.created_at,
                        MeterReading.id,
                    ).all():
                        prev_by_pair.setdefault((mr.user_id, mr.room_id), []).append(mr)

                updates = []
                for r in chunk:
                    user = r.user
                    room = user.room if user else None
                    if not user or not room:
                        # ломаные данные — пропускаем
                        continue

                    # ХОЛОСТЯЦКИЕ комнаты НЕ пересчитываем поштучно: их счёт
                    # делится ПОРОВНУ отдельным singles-выравниванием
                    # (equalize_singles_room / эндпоинт fix-singles). Поштучный
                    # пересчёт по ЛИЧНОМУ prev откатил бы соседа без истории в
                    # baseline (Миронов 389 вместо 1333). 2026-06-18.
                    if bool(getattr(room, "is_singles_apartment", False)):
                        unchanged += 1
                        continue

                    # prev ищется ПО ПАРЕ (user_id, room_id), по period_id (а не
                    # created_at — иначе recalc недетерминирован). Пропускаем
                    # synth-reading'и (AUTO_GENERATED/DATA_OVERFLOW_RESET/MANUAL_RECEIPT)
                    # — их обнулённые значения дают фантастическую дельту при
                    # следующей реальной подаче. См. is_meaningful_prev.
                    prev = None
                    r_pid = r.period_id or 0
                    for cand in reversed(prev_by_pair.get((r.user_id, r.room_id), [])):
                        if (cand.period_id or 0) >= r_pid:
                            continue
                        if not is_meaningful_prev(cand):
                            continue
                        prev = cand
                        break

                    # Per-tariff внутри _recalc_compute_one — там tariff
                    # выбирается через tariff_cache для каждой строки,
                    # поэтому seasonal-логику применяем там же.
                    new_fields = _recalc_compute_one(
                        db, r, user, room, prev, fallback_tariff,
                        global_heating_on=_seasonal.heating_season_active,
                        global_hw_on=_seasonal.hot_water_heating_active,
                    )

                    old_total = Decimal(str(r.total_cost or 0))
                    new_total = Decimal(str(new_fields["total_cost"] or 0))
                    delta = new_total - old_total
                    sum_old += old_total
                    sum_new += new_total

                    if delta == 0:
                        unchanged += 1
                    elif delta > 0:
                        increased += 1
                    else:
                        decreased += 1

                    # Поддерживаем отсортированный топ по |delta|, размер <=30
                    if delta != 0:
                        item = {
                            "reading_id": r.id,
                            "user_id": user.id,
                            "username": user.username,
                            "room": room.format_address if room else "",
                            "old_total": str(old_total),
                            "new_total": str(new_total),
                            "delta": str(delta),
                        }
                        top_diffs.append((abs(delta), item))
                        # Каждые 100 сравнений уменьшаем хвост — экономия памяти.
                        if len(top_diffs) > 200:
                            top_diffs.sort(key=lambda x: x[0], reverse=True)
                            top_diffs = top_diffs[:30]

                    if apply:
                        updates.append({"id": r.id, "created_at": r.created_at, **{k: v for k, v in new_fields.items()}})

                if apply and updates:
                    # ИСПРАВЛЕНИЕ (may 2026): раньше использовался
                    # db.bulk_update_mappings(MeterReading, updates) с
                    # передачей составного PK (id, created_at). Но
                    # MeterReading партиционирована по created_at, и
                    # bulk_update тихо возвращал rowcount=0 — admin
                    # жал «Перерасчёт» 5 раз и каждый раз видел те же
                    # 29 изменений (apply не писал, повторный preview
                    # снова обнаруживал расхождение).
                    #
                    # Now: explicit per-row UPDATE по id (SERIAL уникален
                    # сам по себе, без created_at). Чуть медленнее (500
                    # round-trips на chunk), но apply делается раз в
                    # сутки админом — нагрузка приемлема. И главное —
                    # ТОЧНО пишет, плюс логируем rowcount для отладки.
                    from sqlalchemy import update as _sa_update
                    total_affected = 0
                    for upd in updates:
                        rid = upd["id"]
                        values = {
                            k: v for k, v in upd.items()
                            if k not in ("id", "created_at")
                        }
                        res = db.execute(
                            _sa_update(MeterReading)
                            .where(MeterReading.id == rid)
                            .values(**values)
                        )
                        total_affected += res.rowcount or 0
                    logger.info(
                        "[RECALC] apply chunk: requested=%d affected=%d job=%d",
                        len(updates), total_affected, job_id,
                    )

                offset += CHUNK
                job.processed = min(offset, total)
                job.progress = int(job.processed / total * 100) if total else 100
                db.commit()

                # Повторная проверка: админ мог отменить
                db.refresh(job)
                if job.status == "cancelled":
                    logger.info(f"[RECALC] job {job_id} cancelled mid-run")
                    db.rollback()
                    return {"status": "cancelled"}

            top_diffs.sort(key=lambda x: x[0], reverse=True)
            top_items = [item for _, item in top_diffs[:30]]

            job.diff_summary = {
                "total": total,
                "unchanged": unchanged,
                "increased": increased,
                "decreased": decreased,
                "sum_old": str(sum_old.quantize(Decimal("0.01"))),
                "sum_new": str(sum_new.quantize(Decimal("0.01"))),
                "delta": str((sum_new - sum_old).quantize(Decimal("0.01"))),
                "top": top_items,
            }
            if apply:
                job.status = "done"
                job.applied_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                job.status = "preview_ready"
            job.progress = 100
            db.commit()
            logger.info(f"[RECALC] job {job_id} finished (apply={apply}) — {total} readings")
            return {"status": job.status, "total": total}

        except Exception as exc:
            db.rollback()
            logger.exception(f"[RECALC] job {job_id} failed")
            job2 = db.query(RecalcJob).filter(RecalcJob.id == job_id).first()
            if job2:
                job2.status = "failed"
                job2.error = str(exc)[:2000]
                db.commit()
            return {"status": "failed", "error": str(exc)}


@celery.task(name="recalc_period_preview_task")
def recalc_period_preview_task(job_id: int):
    """Read-only прогон: собирает diff_summary без апдейтов MeterReading."""
    return _recalc_run(job_id, apply=False)


@celery.task(name="recalc_period_apply_task")
def recalc_period_apply_task(job_id: int):
    """Применяет пересчитанные значения к БД (bulk_update)."""
    return _recalc_run(job_id, apply=True)
