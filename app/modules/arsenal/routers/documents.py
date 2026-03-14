import json
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_arsenal_db
from app.modules.arsenal.models import Document, DocumentItem, ArsenalUser
from app.modules.arsenal.schemas import DocCreate
from app.modules.arsenal.deps import get_current_arsenal_user
from app.modules.arsenal.services.weapon_service import WeaponService
from app.modules.utility.services.s3_client import s3_service

router = APIRouter(tags=["Arsenal Documents"])


@router.get("/documents")
async def get_documents(
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user),
        skip: int = Query(0, ge=0, description="Сколько записей пропустить"),
        limit: int = Query(50, ge=1, le=500, description="Максимальное количество возвращаемых записей")
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

    # 🔥 ОПТИМИЗАЦИЯ: Применяем пагинацию (Limit / Offset) на уровне БД
    # Это предотвратит выгрузку 1 миллиона записей в оперативную память.
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
            "target": d.target.name if d.target else "-"
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
        # Получаем расширение файла (по умолчанию .bin, если нет точки)
        file_ext = file.filename.split('.')[-1] if '.' in file.filename else 'bin'
        # Генерируем уникальное имя файла: arsenal_docs/uuid_filename.ext
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
            attached_file_path=file_path
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
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может удалять документы")

    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    # Удаляем документ (строки удалятся каскадно, если настроено, но сами остатки на складе не откатятся автоматически).
    # *Примечание: в текущей логике реализовано удаление документа, но не откат регистра WeaponRegistry.
    # В будущем здесь можно добавить логику отката.
    await db.delete(doc)
    await db.commit()
    return {"status": "deleted"}