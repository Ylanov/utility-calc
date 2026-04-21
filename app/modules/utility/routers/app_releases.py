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
from app.modules.utility.utils.apk_meta import (
    ApkParseError, extract_apk_metadata,
)

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
        # 500 здесь неверный код — сервис жив, это ресурса нет на диске.
        # Клиент (мобилка) на 500 показывает «ошибка сервера» и ретраит,
        # а должен сразу понимать «этой сборки нет». Отдаём 404.
        # Запись в БД об этом релизе остаётся — админ увидит её в админке
        # и сможет перезалить файл.
        logger.error(
            f"[APP] APK file missing on disk: {full_path} "
            f"(release_id={release.id}, file_name={safe}). "
            "Перезалейте APK через админку или снимите is_published."
        )
        raise HTTPException(404, "Файл этой версии не найден — перезалейте APK в админке")

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
    # ВАЖНО: version и version_code теперь ОПЦИОНАЛЬНЫ. Сервер сам прочитает
    # их из AndroidManifest.xml внутри APK. Это убирает источник проблем
    # «в форме указал 3.5.5, а APK на самом деле 1.2.0 → пользователи в цикле
    # обновлений» — невозможно по построению.
    # Если поля переданы — служат как контроль (если расходятся, 400).
    version: Optional[str] = Form(None),
    version_code: Optional[int] = Form(None),
    platform: str = Form("android"),
    release_notes: Optional[str] = Form(None),
    min_required_version_code: Optional[int] = Form(None),
    is_published: bool = Form(True),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Загружает новый APK и создаёт запись AppRelease.

    Версия и version_code определяются АВТОМАТИЧЕСКИ из APK (AndroidManifest.xml).
    Поля формы `version` и `version_code` — опциональны и используются только
    как контроль: если переданы и не совпадают с APK, возвращается 400.
    """
    _require_admin(current_user)

    if platform not in ("android",):
        raise HTTPException(400, f"Платформа {platform} пока не поддерживается")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Допустимы только: {', '.join(ALLOWED_EXTENSIONS)}")

    # Сохраняем файл во временное место — нам нужно сначала прочитать
    # AndroidManifest, и только потом понять какое имя дать файлу
    # (jkh-lider-android-<version>.apk берёт версию из APK, не из формы).
    os.makedirs(APPS_DIR, exist_ok=True)
    tmp_path = os.path.join(APPS_DIR, f".upload-{current_user.id}-{datetime.utcnow().timestamp()}.tmp")

    sha = hashlib.sha256()
    written = 0
    try:
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_APK_SIZE:
                    f.close()
                    os.remove(tmp_path)
                    raise HTTPException(
                        413,
                        f"Файл слишком большой (>{MAX_APK_SIZE // 1024 // 1024} MB)",
                    )
                sha.update(chunk)
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise HTTPException(500, f"Ошибка записи файла: {e}")

    file_hash = sha.hexdigest()

    # APK signature: первые 4 байта должны быть PK\x03\x04 (zip)
    with open(tmp_path, "rb") as f:
        if f.read(4) != b"PK\x03\x04":
            os.remove(tmp_path)
            raise HTTPException(400, "Файл не похож на корректный APK")

    # === Извлечение метаданных из APK ===
    try:
        apk_meta = extract_apk_metadata(tmp_path)
    except ApkParseError as e:
        os.remove(tmp_path)
        raise HTTPException(
            400,
            f"Не удалось прочитать AndroidManifest.xml: {e}. "
            "Убедитесь, что загружаете релизный APK, а не AAB или поврежденный файл.",
        )

    real_version = apk_meta.version_name
    real_version_code = apk_meta.version_code
    logger.info(
        "APK upload: file=%s package=%s version=%s versionCode=%s",
        file.filename, apk_meta.package_name, real_version, real_version_code,
    )

    if not re.fullmatch(r"[0-9]+(\.[0-9]+)+", real_version):
        os.remove(tmp_path)
        raise HTTPException(
            400,
            f"versionName из APK ({real_version!r}) не выглядит как semver. "
            "Проверьте pubspec.yaml — должно быть, например, version: 3.5.5+30505",
        )
    if real_version_code < 1:
        os.remove(tmp_path)
        raise HTTPException(400, f"versionCode из APK ({real_version_code}) некорректен")

    # Если админ что-то ввёл вручную — это контроль, расхождение → 400
    # с понятным объяснением, ЧТО реально внутри APK.
    if version and version != real_version:
        os.remove(tmp_path)
        raise HTTPException(
            400,
            f"Несоответствие: в форме указана версия «{version}», "
            f"а APK собран как «{real_version}». Поправьте поле «Версия» "
            f"либо пересоберите APK с pubspec.yaml version: {version}+{real_version_code}.",
        )
    if version_code is not None and version_code != real_version_code:
        os.remove(tmp_path)
        raise HTTPException(
            400,
            f"Несоответствие: в форме указан version_code={version_code}, "
            f"а APK собран с {real_version_code}. Используйте {real_version_code} "
            "либо пересоберите APK с нужным значением в pubspec.yaml.",
        )

    # Используем версии ИЗ APK (надёжный источник).
    version = real_version
    version_code = real_version_code

    # Проверка дубля версии
    existing = (await db.execute(
        select(AppRelease).where(
            AppRelease.platform == platform,
            AppRelease.version_code == version_code,
        )
    )).scalars().first()
    if existing:
        os.remove(tmp_path)
        raise HTTPException(
            409,
            f"Версия {version} (code {version_code}) уже загружена. "
            "Удалите старую запись или соберите APK с другим versionCode.",
        )

    # Финальное имя файла — на основе версии из APK.
    safe_version = _slug_version(version)
    file_name = f"jkh-lider-{platform}-{safe_version}.apk"
    full_path = os.path.join(APPS_DIR, file_name)

    # Если файл с таким именем уже есть (предыдущая попытка с тем же version) —
    # перезаписываем. БД-проверка дубля по version_code уже прошла выше.
    if os.path.exists(full_path):
        os.remove(full_path)
    os.rename(tmp_path, full_path)

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
