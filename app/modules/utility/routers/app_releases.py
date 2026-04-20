# app/modules/utility/routers/app_releases.py
"""
Управление релизами мобильного приложения.

Публичные эндпоинты (для самого приложения и страницы скачивания):
  GET  /api/app/latest?platform=android   — метаданные последней версии
  GET  /api/app/download/{filename}        — раздача .apk файла

Админские эндпоинты (только admin/accountant):
  GET    /api/admin/app/releases           — список всех релизов
  POST   /api/admin/app/releases           — multipart upload APK
  PATCH  /api/admin/app/releases/{id}      — изменить published / release_notes
  DELETE /api/admin/app/releases/{id}      — удалить релиз и файл

APK-файлы хранятся в `/app/static/apps/`. nginx раздаёт их напрямую через
location /static/apps/ — но публичный download идёт через /api/app/download
чтобы можно было считать загрузки и логировать.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.modules.utility.models import AppRelease, User
from app.modules.utility.routers.admin_dashboard import write_audit_log

router = APIRouter(tags=["App Releases"])
logger = logging.getLogger(__name__)

# Папка для хранения APK. Должна существовать и быть writable приложением.
# Также должна быть подмонтирована в nginx как static/apps/ — но запросы идут
# через FastAPI, чтобы считать downloads и логировать.
APPS_DIR = "/app/static/apps"
MAX_APK_SIZE = 200 * 1024 * 1024  # 200 MB — с большим запасом
ALLOWED_EXTENSIONS = {".apk"}     # iOS добавим позже как .ipa


# =====================================================================
# AUTH HELPERS
# =====================================================================
def _require_admin(user: User) -> None:
    if user.role not in ("admin", "accountant"):
        raise HTTPException(status_code=403, detail="Доступ запрещён")


def _slug_version(version: str) -> str:
    """1.2.3 → 1_2_3 — для безопасных имён файлов."""
    return re.sub(r"[^0-9a-zA-Z._-]", "_", version)


def _safe_filename(name: str) -> str:
    """Удаляем path-traversal элементы."""
    return os.path.basename(name).replace("..", "_")


# =====================================================================
# PYDANTIC SCHEMAS
# =====================================================================
class ReleaseInfo(BaseModel):
    id: int
    version: str
    version_code: int
    min_required_version_code: Optional[int]
    platform: str
    file_name: str
    file_size: int
    file_hash: Optional[str]
    release_notes: Optional[str]
    is_published: bool
    created_at: Optional[datetime]
    download_url: str

    class Config:
        from_attributes = True


class LatestVersionInfo(BaseModel):
    version: str
    version_code: int
    min_required_version_code: Optional[int]
    force_update: bool      # true если у клиента старее min_required
    download_url: str
    release_notes: Optional[str]
    file_size: int
    file_hash: Optional[str]


class UpdatePayload(BaseModel):
    is_published: Optional[bool] = None
    release_notes: Optional[str] = None
    min_required_version_code: Optional[int] = None


# =====================================================================
# ПУБЛИЧНЫЕ ЭНДПОИНТЫ
# =====================================================================
@router.get("/api/app/latest", response_model=LatestVersionInfo)
async def latest_version(
    platform: str = Query("android"),
    current_version_code: Optional[int] = Query(
        None,
        description="Текущая версия клиента — для определения force_update",
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Публичный эндпоинт. Мобильное приложение дёргает его при запуске
    и решает, показать ли диалог обновления.

    Если `current_version_code` передан и он меньше `min_required_version_code`
    последнего релиза, возвращаем `force_update=true`.
    """
    release = (await db.execute(
        select(AppRelease)
        .where(
            AppRelease.platform == platform,
            AppRelease.is_published.is_(True),
        )
        .order_by(desc(AppRelease.version_code))
        .limit(1)
    )).scalars().first()

    if not release:
        raise HTTPException(404, "Опубликованных версий ещё нет")

    force = False
    if (
        current_version_code is not None
        and release.min_required_version_code is not None
        and current_version_code < release.min_required_version_code
    ):
        force = True

    return LatestVersionInfo(
        version=release.version,
        version_code=release.version_code,
        min_required_version_code=release.min_required_version_code,
        force_update=force,
        download_url=f"/api/app/download/{release.file_name}",
        release_notes=release.release_notes,
        file_size=release.file_size,
        file_hash=release.file_hash,
    )


@router.get("/api/app/download/{filename}")
async def download_apk(filename: str, db: AsyncSession = Depends(get_db)):
    """
    Раздача APK. Без авторизации (приложение скачивает в т.ч. до логина),
    но имя файла строго проверяется и резолвится из БД — так нельзя
    скачать произвольный файл из APPS_DIR.
    """
    safe = _safe_filename(filename)
    if safe != filename:
        raise HTTPException(400, "Некорректное имя файла")

    # Проверяем что такая запись действительно опубликована — защита
    # от линкования на неопубликованную dev-сборку.
    release = (await db.execute(
        select(AppRelease).where(
            AppRelease.file_name == safe,
            AppRelease.is_published.is_(True),
        )
    )).scalars().first()
    if not release:
        raise HTTPException(404, "Версия не найдена или не опубликована")

    full_path = os.path.join(APPS_DIR, safe)
    if not os.path.isfile(full_path):
        logger.error(f"[APP] APK file missing on disk: {full_path}")
        raise HTTPException(500, "Файл недоступен на сервере")

    return FileResponse(
        path=full_path,
        media_type="application/vnd.android.package-archive",
        filename=safe,
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "Cache-Control": "public, max-age=3600",
        },
    )


# =====================================================================
# АДМИНСКИЕ ЭНДПОИНТЫ
# =====================================================================
@router.get("/api/admin/app/releases")
async def list_releases(
    platform: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    q = select(AppRelease).order_by(desc(AppRelease.version_code))
    if platform:
        q = q.where(AppRelease.platform == platform)
    rows = (await db.execute(q)).scalars().all()
    return [
        ReleaseInfo(
            id=r.id,
            version=r.version,
            version_code=r.version_code,
            min_required_version_code=r.min_required_version_code,
            platform=r.platform,
            file_name=r.file_name,
            file_size=r.file_size,
            file_hash=r.file_hash,
            release_notes=r.release_notes,
            is_published=r.is_published,
            created_at=r.created_at,
            download_url=f"/api/app/download/{r.file_name}",
        ).model_dump()
        for r in rows
    ]


@router.post("/api/admin/app/releases")
async def upload_release(
    version: str = Form(...),
    version_code: int = Form(...),
    platform: str = Form("android"),
    release_notes: Optional[str] = Form(None),
    min_required_version_code: Optional[int] = Form(None),
    is_published: bool = Form(True),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Загружает новый APK и создаёт запись AppRelease."""
    _require_admin(current_user)

    # Валидация
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise HTTPException(400, "Версия должна быть в формате semver (1.2.3)")

    if version_code < 1:
        raise HTTPException(400, "version_code должен быть положительным")

    if platform not in ("android",):
        raise HTTPException(400, f"Платформа {platform} пока не поддерживается")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Допустимы только: {', '.join(ALLOWED_EXTENSIONS)}")

    # Проверка дубля версии
    existing = (await db.execute(
        select(AppRelease).where(
            AppRelease.platform == platform,
            AppRelease.version_code == version_code,
        )
    )).scalars().first()
    if existing:
        raise HTTPException(409, f"Версия с кодом {version_code} уже загружена")

    # Сохранение файла
    os.makedirs(APPS_DIR, exist_ok=True)
    safe_version = _slug_version(version)
    file_name = f"jkh-lider-{platform}-{safe_version}.apk"
    full_path = os.path.join(APPS_DIR, file_name)

    # Поточная запись с подсчётом размера и хеша
    sha = hashlib.sha256()
    written = 0
    try:
        with open(full_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_APK_SIZE:
                    f.close()
                    os.remove(full_path)
                    raise HTTPException(
                        413,
                        f"Файл слишком большой (>{MAX_APK_SIZE // 1024 // 1024} MB)",
                    )
                sha.update(chunk)
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(full_path):
            os.remove(full_path)
        raise HTTPException(500, f"Ошибка записи файла: {e}")

    file_hash = sha.hexdigest()

    # APK signature: первые 4 байта должны быть PK\x03\x04 (zip)
    with open(full_path, "rb") as f:
        if f.read(4) != b"PK\x03\x04":
            os.remove(full_path)
            raise HTTPException(400, "Файл не похож на корректный APK")

    release = AppRelease(
        version=version,
        version_code=version_code,
        min_required_version_code=min_required_version_code,
        platform=platform,
        file_name=file_name,
        file_size=written,
        file_hash=file_hash,
        release_notes=release_notes,
        is_published=is_published,
        created_by_id=current_user.id,
    )
    db.add(release)
    await db.flush()

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="app_release_upload", entity_type="app_release",
        entity_id=release.id,
        details={
            "version": version, "version_code": version_code,
            "platform": platform, "file_size": written,
            "is_published": is_published,
        },
    )
    await db.commit()
    await db.refresh(release)

    return {
        "id": release.id,
        "version": release.version,
        "version_code": release.version_code,
        "file_name": release.file_name,
        "file_size": release.file_size,
        "file_hash": release.file_hash,
        "is_published": release.is_published,
        "download_url": f"/api/app/download/{release.file_name}",
    }


@router.patch("/api/admin/app/releases/{release_id}")
async def update_release(
    release_id: int,
    payload: UpdatePayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)

    release = await db.get(AppRelease, release_id)
    if not release:
        raise HTTPException(404, "Релиз не найден")

    changed = {}
    if payload.is_published is not None and release.is_published != payload.is_published:
        release.is_published = payload.is_published
        changed["is_published"] = payload.is_published
    if payload.release_notes is not None and release.release_notes != payload.release_notes:
        release.release_notes = payload.release_notes
        changed["release_notes"] = "updated"
    if payload.min_required_version_code is not None and \
            release.min_required_version_code != payload.min_required_version_code:
        release.min_required_version_code = payload.min_required_version_code
        changed["min_required_version_code"] = payload.min_required_version_code

    if changed:
        await write_audit_log(
            db, current_user.id, current_user.username,
            action="app_release_update", entity_type="app_release",
            entity_id=release.id, details=changed,
        )
        await db.commit()

    return {"status": "ok", "changed": changed}


@router.delete("/api/admin/app/releases/{release_id}")
async def delete_release(
    release_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)

    release = await db.get(AppRelease, release_id)
    if not release:
        raise HTTPException(404, "Релиз не найден")

    file_path = os.path.join(APPS_DIR, release.file_name)
    file_was = release.file_name

    await db.delete(release)
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="app_release_delete", entity_type="app_release",
        entity_id=release_id,
        details={"file_name": file_was},
    )
    await db.commit()

    # Удаляем файл с диска ПОСЛЕ commit'a — чтобы при ошибке БД не остаться без файла.
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.warning(f"[APP] Failed to delete APK file {file_path}: {e}")

    return {"status": "ok"}
