import secrets
import string
from passlib.context import CryptContext
from fastapi import APIRouter, Depends, HTTPException, status, Request
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

# Импорт подключения к БД (убедитесь, что get_gsm_db добавлено в database.py)
from app.database import get_gsm_db
from app.config import settings

# Импорт моделей ГСМ
from app.gsm.models import (
    GsmAccountingObject,
    GsmNomenclature,
    GsmDocument,
    GsmDocumentItem,
    FuelRegistry,
    GsmUser
)

# Импорт бизнес-логики ГСМ
from app.gsm.services import GsmService

# Импорт схем ГСМ (которые мы написали в предыдущем шаге)
from app.gsm.schemas import (
    ObjCreate,
    NomenclatureCreate,
    DocCreate
)

# ======================================================
# НАСТРОЙКА ХЕШИРОВАНИЯ (Argon2)
# ======================================================
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# ======================================================
# РОУТЕР
# ======================================================
router = APIRouter(prefix="/api/gsm", tags=["STROB GSM"])


# ======================================================
# АВТОРИЗАЦИЯ ТОЛЬКО ДЛЯ ГСМ (Изолированная)
# ======================================================
async def get_current_gsm_user(
        request: Request,
        db: AsyncSession = Depends(get_gsm_db)
):
    """Проверка токена и поиск пользователя именно в базе ГСМ"""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Неверный токен")
    except JWTError:
        raise HTTPException(status_code=401, detail="Ошибка валидации токена")

    # Ищем пользователя в базе ГСМ
    result = await db.execute(select(GsmUser).where(GsmUser.username == username))
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=401, detail="Пользователь ГСМ не найден")

    return user


# ======================================================
# 1. ОБЪЕКТЫ ИНФРАСТРУКТУРЫ (Склады, Резервуары, АТЗ)
# ======================================================

@router.get("/objects")
async def get_objects(
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Получить список всех объектов инфраструктуры ГСМ"""
    result = await db.execute(
        select(GsmAccountingObject).order_by(GsmAccountingObject.name)
    )
    return result.scalars().all()


@router.post("/objects")
async def create_object(
        data: ObjCreate,
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Создать новый объект и АВТОМАТИЧЕСКИ сгенерировать доступы для начальника склада"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Только администратор может создавать новые объекты инфраструктуры"
        )

    existing = await db.execute(
        select(GsmAccountingObject).where(GsmAccountingObject.name == data.name)
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Объект с таким именем уже существует"
        )

    # 1. Создаем объект
    obj = GsmAccountingObject(**data.dict())
    db.add(obj)
    await db.flush()

    # 2. Генерируем учетные данные для начальника резервуара/склада
    new_username = f"storage_{obj.id}"

    alphabet = string.ascii_letters + string.digits
    new_password = ''.join(secrets.choice(alphabet) for _ in range(8))
    hashed_pw = pwd_context.hash(new_password)

    # 3. Создаем пользователя ГСМ
    new_user = GsmUser(
        username=new_username,
        hashed_password=hashed_pw,
        role="storage_head",  # Роль начальника склада ГСМ
        object_id=obj.id
    )
    db.add(new_user)

    await db.commit()
    await db.refresh(obj)

    return {
        "id": obj.id,
        "name": obj.name,
        "obj_type": obj.obj_type,
        "credentials": {
            "username": new_username,
            "password": new_password
        }
    }


@router.delete("/objects/{obj_id}")
async def delete_object(
        obj_id: int,
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Удалить объект ГСМ"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может удалять объекты")

    obj = await db.get(GsmAccountingObject, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Объект не найден")

    await db.delete(obj)
    await db.commit()
    return {"status": "deleted"}


# ======================================================
# 2. НОМЕНКЛАТУРА (Справочник топлива и масел)
# ======================================================

@router.get("/nomenclature")
async def get_nomenclature(
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Получить справочник марок ГСМ"""
    result = await db.execute(
        select(GsmNomenclature).order_by(GsmNomenclature.name)
    )
    return result.scalars().all()


@router.post("/nomenclature")
async def create_nomenclature(
        data: NomenclatureCreate,
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Добавить новую марку ГСМ"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может добавлять номенклатуру")

    existing = await db.execute(
        select(GsmNomenclature).where(GsmNomenclature.name == data.name)
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Марка ГСМ с таким наименованием уже существует"
        )

    # Транслируем is_numbered из JS в is_packaged для БД ГСМ
    new_item = GsmNomenclature(
        code=data.code,
        name=data.name,
        category=data.category,
        is_packaged=data.is_numbered
    )
    db.add(new_item)
    await db.commit()
    await db.refresh(new_item)
    return new_item


# ======================================================
# 3. ДОКУМЕНТЫ (Накладные, Акты приема-передачи)
# ======================================================

@router.get("/documents")
async def get_documents(
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Журнал документов ГСМ"""
    stmt = (
        select(GsmDocument)
        .options(
            selectinload(GsmDocument.source),
            selectinload(GsmDocument.target)
        )
        .order_by(
            GsmDocument.operation_date.desc(),
            GsmDocument.created_at.desc()
        )
    )

    # Начальник склада видит только накладные своего резервуара/склада
    if current_user.role == "storage_head":
        stmt = stmt.where(
            (GsmDocument.source_id == current_user.object_id) |
            (GsmDocument.target_id == current_user.object_id)
        )

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
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Подробная информация о накладной ГСМ"""
    stmt = (
        select(GsmDocument)
        .where(GsmDocument.id == doc_id)
        .options(
            selectinload(GsmDocument.source),
            selectinload(GsmDocument.target),
            selectinload(GsmDocument.items)
            .selectinload(GsmDocumentItem.nomenclature)
        )
    )
    doc = (await db.execute(stmt)).scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    if current_user.role == "storage_head":
        if doc.source_id != current_user.object_id and doc.target_id != current_user.object_id:
            raise HTTPException(status_code=403, detail="Этот документ не относится к вашему объекту.")

    # Собираем JSON вручную, чтобы перевести batch_number в serial_number для JS
    response = {
        "id": doc.id,
        "doc_number": doc.doc_number,
        "operation_date": doc.operation_date.isoformat(),
        "operation_type": doc.operation_type,
        "source": {"name": doc.source.name} if doc.source else None,
        "target": {"name": doc.target.name} if doc.target else None,
        "items": []
    }

    for item in doc.items:
        response["items"].append({
            "nomenclature": {
                "name": item.nomenclature.name,
                "code": item.nomenclature.code
            },
            "serial_number": item.batch_number,  # Адаптация для фронтенда
            "quantity": item.quantity
        })

    return response


@router.post("/documents")
async def create_document(
        data: DocCreate,
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Проведение накладной и изменение остатков топлива"""
    if current_user.role == "storage_head":
        if data.operation_type in ["Отправка", "Перемещение", "Списание"]:
            if data.source_id != current_user.object_id:
                raise HTTPException(status_code=403, detail="Вы можете списывать топливо только со своего объекта!")

        if data.operation_type in ["Первичный ввод", "Прием"]:
            if data.target_id != current_user.object_id:
                raise HTTPException(status_code=403, detail="Вы можете принимать топливо только на свой объект!")

    try:
        new_doc = await GsmService.process_document(db, data, data.items)
        return {"status": "created", "id": new_doc.id}
    except HTTPException as he:
        raise he
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Ошибка проведения документа: {str(e)}")


@router.delete("/documents/{doc_id}")
async def delete_document(
        doc_id: int,
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может удалять документы")

    doc = await db.get(GsmDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    await db.delete(doc)
    await db.commit()
    return {"status": "deleted"}


# ======================================================
# 4. ОСТАТКИ ГСМ (РЕЗЕРВУАРЫ)
# ======================================================

@router.get("/balance/{obj_id}")
async def get_object_balance(
        obj_id: int,
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Получить текущие остатки ГСМ в резервуаре/на складе"""
    if current_user.role == "storage_head" and obj_id != current_user.object_id:
        raise HTTPException(status_code=403, detail="Вы можете просматривать остатки только своего объекта")

    stmt = (
        select(FuelRegistry)
        .join(GsmNomenclature)
        .options(selectinload(FuelRegistry.nomenclature))
        .where(
            FuelRegistry.current_object_id == obj_id,
            FuelRegistry.status == 1
        )
        .order_by(GsmNomenclature.name, FuelRegistry.batch_number)
    )

    fuels = (await db.execute(stmt)).scalars().all()

    balance = []
    for fuel in fuels:
        balance.append({
            "nomenclature": fuel.nomenclature.name,
            "code": fuel.nomenclature.code,
            "serial_number": fuel.batch_number,  # Адаптация для JS
            "quantity": fuel.quantity,
            "is_numbered": fuel.nomenclature.is_packaged
        })

    return balance


# ======================================================
# 5. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (Для Админа)
# ======================================================

@router.get("/users")
async def get_users(
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Получить список пользователей ГСМ"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    stmt = select(GsmUser).options(selectinload(GsmUser.accounting_object)).order_by(GsmUser.id)
    result = await db.execute(stmt)
    users = result.scalars().all()

    response = []
    for u in users:
        response.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "object_name": u.accounting_object.name if u.accounting_object else "Центральное управление",
            "created_at": u.created_at.strftime("%d.%m.%Y")
        })
    return response


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
        user_id: int,
        db: AsyncSession = Depends(get_gsm_db),
        current_user: GsmUser = Depends(get_current_gsm_user)
):
    """Сброс пароля (Генерирует новый)"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    user = await db.get(GsmUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    alphabet = string.ascii_letters + string.digits
    new_password = ''.join(secrets.choice(alphabet) for _ in range(8))

    user.hashed_password = pwd_context.hash(new_password)
    db.add(user)
    await db.commit()

    return {
        "message": "Пароль успешно сброшен",
        "username": user.username,
        "new_password": new_password
    }