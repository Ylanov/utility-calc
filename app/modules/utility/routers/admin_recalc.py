# app/modules/utility/routers/admin_recalc.py
"""
Полный перерасчёт периода — fire-and-forget через Celery.

Жизненный цикл:
    POST /api/admin/periods/{period_id}/recalc/start
        → создаёт RecalcJob(status=preview_pending)
        → Celery task recalc_period_preview_task собирает diff_summary
        → status=preview_ready

    GET /api/admin/recalc-jobs/{job_id}
        → {status, progress, diff_summary, error}  (polled из UI)

    POST /api/admin/recalc-jobs/{job_id}/apply
        → только если status=preview_ready
        → Celery task recalc_period_apply_task обновляет MeterReading
        → status=done

    POST /api/admin/recalc-jobs/{job_id}/cancel
        → пометить job cancelled; Celery-задача проверяет в каждой итерации

    GET /api/admin/periods/{period_id}/recalc-jobs
        → история перерасчётов по периоду
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc

from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import (
    RecalcJob, BillingPeriod, MeterReading, User,
)
from app.modules.utility.routers.admin_dashboard import write_audit_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["Admin Recalc"])
allow_admin = RoleChecker(["admin"])
allow_management = RoleChecker(["accountant", "admin"])


def _job_to_dict(job: RecalcJob) -> dict:
    return {
        "id": job.id,
        "period_id": job.period_id,
        "status": job.status,
        "progress": job.progress,
        "total_readings": job.total_readings,
        "processed": job.processed,
        "diff_summary": job.diff_summary,
        "started_by_username": job.started_by_username,
        "celery_task_id": job.celery_task_id,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "applied_at": job.applied_at.isoformat() if job.applied_at else None,
    }


@router.post("/periods/{period_id}/recalc/start", dependencies=[Depends(allow_management)])
async def start_recalc(
    period_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(RoleChecker(["accountant", "admin"])),
):
    """Запускает preview-прогон пересчёта. Показания не меняются.

    Блокируем запуск нового превью, если есть незавершённое задание по этому
    периоду — иначе админ может по ошибке развести несколько задач параллельно.
    """
    period = await db.get(BillingPeriod, period_id)
    if not period:
        raise HTTPException(404, "Период не найден")

    # Проверяем незавершённые задания (preview/apply в процессе)
    busy = (await db.execute(
        select(RecalcJob).where(
            RecalcJob.period_id == period_id,
            RecalcJob.status.in_(["preview_pending", "apply_pending"]),
        )
    )).scalars().first()
    if busy:
        raise HTTPException(
            409,
            f"По этому периоду уже идёт задача id={busy.id} (status={busy.status}). "
            "Дождитесь завершения или отмените её.",
        )

    job = RecalcJob(
        period_id=period_id,
        status="preview_pending",
        started_by_id=current_user.id,
        started_by_username=current_user.username,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Импорт здесь, чтобы Celery-app не тянулся в момент импорта роутера
    from app.modules.utility.tasks import recalc_period_preview_task

    async_result = recalc_period_preview_task.delay(job.id)
    job.celery_task_id = async_result.id
    await db.commit()

    await write_audit_log(
        db=db, user_id=current_user.id, username=current_user.username,
        action="recalc_start", entity_type="period", entity_id=period.id,
        details={"job_id": job.id, "period_name": period.name},
    )
    await db.commit()

    return _job_to_dict(job)


@router.get("/recalc-jobs/{job_id}", dependencies=[Depends(allow_management)])
async def get_recalc_job(job_id: int, db: AsyncSession = Depends(get_db)):
    job = await db.get(RecalcJob, job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return _job_to_dict(job)


@router.post("/recalc-jobs/{job_id}/apply")
async def apply_recalc(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    # Применение разрешено и бухгалтеру, и админу — симметрично
    # со start/cancel. Отдельный RoleChecker в сигнатуре (а не только
    # в dependencies) нужен ради current_user для audit_log.
    current_user: User = Depends(allow_management),
):
    """Применяет результаты preview к БД. Разрешено accountant и admin."""
    job = await db.get(RecalcJob, job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    if job.status != "preview_ready":
        raise HTTPException(
            400,
            f"Нельзя применить задачу в статусе «{job.status}». "
            "Дождитесь preview_ready или запустите новый preview.",
        )

    job.status = "apply_pending"
    await db.commit()

    from app.modules.utility.tasks import recalc_period_apply_task

    async_result = recalc_period_apply_task.delay(job.id)
    job.celery_task_id = async_result.id
    await db.commit()

    await write_audit_log(
        db=db, user_id=current_user.id, username=current_user.username,
        action="recalc_apply", entity_type="period", entity_id=job.period_id,
        details={"job_id": job.id, "diff_summary": job.diff_summary},
    )
    await db.commit()

    return _job_to_dict(job)


@router.post("/recalc-jobs/{job_id}/cancel", dependencies=[Depends(allow_management)])
async def cancel_recalc(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(RoleChecker(["accountant", "admin"])),
):
    """Помечает задачу cancelled. Celery task проверяет флаг в каждой итерации
    и выходит, откатывая апдейты. Если задача уже done/failed — 400."""
    job = await db.get(RecalcJob, job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    if job.status in ("done", "failed", "cancelled"):
        raise HTTPException(400, f"Задача уже завершена (статус «{job.status}»)")

    job.status = "cancelled"
    await db.commit()

    await write_audit_log(
        db=db, user_id=current_user.id, username=current_user.username,
        action="recalc_cancel", entity_type="period", entity_id=job.period_id,
        details={"job_id": job.id},
    )
    await db.commit()

    return _job_to_dict(job)


@router.get("/periods/{period_id}/recalc-jobs", dependencies=[Depends(allow_management)])
async def list_recalc_jobs(period_id: int, db: AsyncSession = Depends(get_db)):
    """История перерасчётов по периоду (последние 20)."""
    jobs = (await db.execute(
        select(RecalcJob)
        .where(RecalcJob.period_id == period_id)
        .order_by(desc(RecalcJob.created_at))
        .limit(20)
    )).scalars().all()
    return [_job_to_dict(j) for j in jobs]


@router.get("/recalc/periods", dependencies=[Depends(allow_management)])
async def list_periods_for_recalc(db: AsyncSession = Depends(get_db)):
    """Список периодов для селектора в UI перерасчёта.

    Отдельный путь `/recalc/periods` (а не `/periods`), чтобы не конфликтовать
    с admin_periods.py, где уже есть /active, /history, /open, /close etc."""
    from sqlalchemy import func as _func

    # counts — сколько approved readings в каждом периоде (чтобы UI показал
    # «пересчёт затронет N жильцов» ещё до запуска preview).
    periods = (await db.execute(
        select(BillingPeriod).order_by(desc(BillingPeriod.created_at))
    )).scalars().all()

    counts_rows = (await db.execute(
        select(MeterReading.period_id, _func.count(MeterReading.id))
        .where(MeterReading.is_approved.is_(True))
        .group_by(MeterReading.period_id)
    )).all()
    counts_map = {pid: cnt for pid, cnt in counts_rows}

    return [
        {
            "id": p.id,
            "name": p.name,
            "is_active": p.is_active,
            "approved_readings": int(counts_map.get(p.id, 0)),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in periods
    ]
