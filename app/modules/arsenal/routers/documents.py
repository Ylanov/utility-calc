import json
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import or_

from app.core.database import get_arsenal_db
from app.modules.arsenal.models import Document, DocumentItem, ArsenalUser
from app.modules.arsenal.schemas import DocCreate
from app.modules.arsenal.deps import get_current_arsenal_user
from app.modules.arsenal.services.weapon_service import WeaponService
from app.modules.utility.services.s3_client import s3_service

router = APIRouter(tags=["Arsenal Documents"])


# =====================================================================
# ВАЛИДАЦИЯ ФАЙЛОВ — прикреплений к документам
# =====================================================================
# Подтверждения документов — это сканы/фото/PDF. Разрешаем только
# канонический набор: если админ прикрепляет .exe или .html — это либо
# ошибка, либо storage abuse (использование нашего MinIO как хостинга
# вредоносных файлов). Проверяем расширение, MIME и размер.

ALLOWED_DOC_EXT = {"pdf", "jpg", "jpeg", "png", "webp", "heic", "tif", "tiff"}
ALLOWED_DOC_MIME = {
    "application/pdf",
    "image/jpeg", "image/png", "image/webp",
    "image/heic", "image/heif", "image/tiff",
    # iOS иногда шлёт generic octet-stream — MIME без расширения не
    # принимаем, но octet-stream с валидным extension допустим.
    "application/octet-stream",
}
MAX_DOC_SIZE_BYTES = 15 * 1024 * 1024  # 15 МБ — скан договора/акта точно влезет


def _validate_upload(file: UploadFile) -> str:
    """Проверяет extension + MIME + размер. Возвращает нормализованное ext.

    Raises HTTPException(400) — всегда с конкретной причиной, чтобы админу
    было понятно (не сервис же прячет валидные ошибки).
    """
    if not file.filename:
        raise HTTPException(400, "У файла нет имени")
    if "." not in file.filename:
        raise HTTPException(400, "У файла нет расширения")

    ext = file.filename.rsplit(".", 1)[-1].lower().strip()
    if ext not in ALLOWED_DOC_EXT:
        raise HTTPException(
            400,
            f"Недопустимый формат .{ext}. Разрешены: "
            + ", ".join(sorted(ALLOWED_DOC_EXT)),
        )

    if file.content_type and file.content_type not in ALLOWED_DOC_MIME:
        raise HTTPException(
            400,
            f"Недопустимый MIME-тип: {file.content_type}",
        )

    # Размер: ленивая проверка через seek. UploadFile хранит SpooledTemporaryFile,
    # на котором seek/tell работают.
    try:
        file.file.seek(0, 2)  # 2 == os.SEEK_END
        size = file.file.tell()
        file.file.seek(0)
    except Exception:
        size = 0
    if size and size > MAX_DOC_SIZE_BYTES:
        raise HTTPException(
            413,
            f"Файл слишком большой: {size // 1024 // 1024} МБ. "
            f"Максимум {MAX_DOC_SIZE_BYTES // 1024 // 1024} МБ.",
        )

    return ext


@router.get("/documents")
async def get_documents(
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user),
        skip: int = Query(0, ge=0, description="Сколько записей пропустить"),
        limit: int = Query(50, ge=1, le=500, description="Максимальное количество возвращаемых записей"),
        q: Optional[str] = Query(None, description="Поиск по номеру документа, типу операции или объектам")
):
    stmt = (
        select(Document)
        .options(selectinload(Document.source), selectinload(Document.target))
        .order_by(Document.operation_date.desc(), Document.created_at.desc())
    )

    if current_user.role == "unit_head":
        stmt = stmt.where(
            (Document.source_id == current_user.object_id) |
            (Document.target_id == current_user.object_id)
        )

    if q:
        search_term = f"%{q}%"
        stmt = stmt.where(
            or_(
                Document.doc_number.ilike(search_term),
                Document.operation_type.ilike(search_term),
            )
        )

    # Применяем пагинацию на уровне БД
    stmt = stmt.offset(skip).limit(limit)

    result = await db.execute(stmt)
    docs = result.scalars().all()

    response_data = []
    for d in docs:
        response_data.append({
            "id": d.id,
            "doc_number": d.doc_number,
            "date": d.operation_date.strftime("%d.%m.%Y") if d.operation_date else "-",
            "type": d.operation_type,
            "source": d.source.name if d.source else "-",
            "target": d.target.name if d.target else "-",
            # Для UI-подсветки «отменён»/«отменяющий» — без этих полей
            # невозможно правильно показать статус документа в списке.
            "is_reversed": bool(d.is_reversed),
            "reversed_by_document_id": d.reversed_by_document_id,
            "reverses_document_id": d.reverses_document_id,
        })

    return response_data


@router.get("/documents/{doc_id}")
async def get_document_details(
        doc_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    stmt = (
        select(Document)
        .where(Document.id == doc_id)
        .options(
            selectinload(Document.source),
            selectinload(Document.target),
            selectinload(Document.items).selectinload(DocumentItem.nomenclature),
            selectinload(Document.items).selectinload(DocumentItem.weapon)
        )
    )
    doc = (await db.execute(stmt)).scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    if current_user.role == "unit_head":
        if doc.source_id != current_user.object_id and doc.target_id != current_user.object_id:
            raise HTTPException(status_code=403, detail="Доступ к документу запрещен")

    # 🔥 Генерируем временную ссылку на скачивание, если файл прикреплен
    presigned_url = None
    if doc.attached_file_path:
        presigned_url = s3_service.get_presigned_url(doc.attached_file_path, expiration=3600)

    # Преобразуем объект в словарь, чтобы добавить ссылку
    doc_dict = {
        "id": doc.id,
        "doc_number": doc.doc_number,
        "operation_date": doc.operation_date,
        "operation_type": doc.operation_type,
        "source": {"name": doc.source.name} if doc.source else None,
        "target": {"name": doc.target.name} if doc.target else None,
        "file_url": presigned_url,  # <-- ПЕРЕДАЕМ ССЫЛКУ ФРОНТУ
        "items": [
            {
                "nomenclature": {"name": item.nomenclature.name, "code": item.nomenclature.code},
                "serial_number": item.serial_number,
                "inventory_number": item.inventory_number,
                "price": float(item.price) if item.price else None,
                "quantity": item.quantity
            } for item in doc.items
        ]
    }
    return doc_dict


@router.post("/documents")
async def create_document(
        # Принимаем данные как строку (JSON) и файл (UploadFile)
        data: str = Form(...),
        file: Optional[UploadFile] = File(None),
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    # 1. Парсим JSON строку обратно в Pydantic схему DocCreate
    try:
        parsed_data = json.loads(data)
        doc_obj = DocCreate(**parsed_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Неверный формат данных документа (JSON)")

    # Проверка прав доступа
    if current_user.role == "unit_head":
        if doc_obj.operation_type in ["Отправка", "Перемещение", "Списание"]:
            if doc_obj.source_id != current_user.object_id:
                raise HTTPException(status_code=403, detail="Вы можете списывать только со своего склада!")

        if doc_obj.operation_type in ["Первичный ввод", "Прием"]:
            if doc_obj.target_id != current_user.object_id:
                raise HTTPException(status_code=403, detail="Вы можете принимать имущество только на свой склад!")

    # 2. Обработка файла (загрузка в MinIO)
    file_path = None
    if file:
        # Валидируем extension / MIME / размер ДО попытки upload — чтобы
        # не тратить квоту MinIO на заведомо мусорный файл и не давать
        # storage abuse (загрузка .exe, больших бинарей).
        file_ext = _validate_upload(file)
        unique_name = f"arsenal_docs/{uuid.uuid4().hex}.{file_ext}"

        is_uploaded = s3_service.upload_fileobj(file.file, unique_name)
        if not is_uploaded:
            raise HTTPException(status_code=500, detail="Ошибка загрузки файла в S3/MinIO хранилище")

        file_path = unique_name

    # 3. Проведение документа в БД
    try:
        new_doc = await WeaponService.process_document(
            db=db,
            doc_data=doc_obj,
            items_data=doc_obj.items,
            attached_file_path=file_path,
            author_id=current_user.id,  # теперь аудируемо: видно кто провёл
        )
        return {"status": "created", "id": new_doc.id}
    except HTTPException as he:
        raise he
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Ошибка проведения документа: {str(e)}")


@router.delete("/documents/{doc_id}")
async def delete_document(
        doc_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Физическое удаление документа.

    ВНИМАНИЕ: по умолчанию это запрещено, если документ УЖЕ ПРОВЁЛ ДВИЖЕНИЕ
    (есть DocumentItem-ы). Корректный путь — использовать POST .../rollback,
    который создаёт обратный документ и оставляет аудируемый след.

    Этот endpoint теперь разрешает только удаление документов-«заготовок»
    (без items) — чисто «ошибка ввода, не прошёл ещё».
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может удалять документы")

    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    # Блокируем удаление документов с движением — требуем rollback.
    items_count = (await db.execute(
        select(DocumentItem.id).where(DocumentItem.document_id == doc_id).limit(1)
    )).scalars().first()
    if items_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Документ #{doc.doc_number} уже провёл движение остатков. "
                "Для отмены используйте POST /arsenal/documents/{id}/rollback — "
                "создаётся обратный документ и остаётся аудируемый след."
            ),
        )

    await db.delete(doc)
    await db.commit()
    return {"status": "deleted"}


@router.post("/documents/{doc_id}/rollback")
async def rollback_document(
        doc_id: int,
        reason: Optional[str] = Form(None),
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """Создать обратный документ (reversal) по ранее проведённому.

    Исходный документ не удаляется — помечается `is_reversed=True` и
    получает ссылку на reversal (`reversed_by_document_id`). Новый reversal
    имеет ссылку `reverses_document_id` на оригинал. История полностью
    сохраняется, остатки на складе возвращаются в состояние «как до документа».
    """
    if current_user.role != "admin":
        raise HTTPException(403, "Только администратор может отменять документы")

    new_doc = await WeaponService.rollback_document(
        db, document_id=doc_id, author_id=current_user.id, reason=reason,
    )
    return {
        "status": "reversed",
        "reversal_document_id": new_doc.id,
        "reversal_doc_number": new_doc.doc_number,
    }
