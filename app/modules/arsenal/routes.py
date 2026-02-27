import secrets
import string
from passlib.context import CryptContext
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from pydantic import BaseModel, validator
from datetime import datetime

# ======================================================
# ИМПОРТЫ ДЛЯ АУТЕНТИФИКАЦИИ И БД
# ======================================================
from app.core.database import get_arsenal_db
from app.core.config import settings

from app.modules.arsenal.models import (
    AccountingObject,
    Nomenclature,
    Document,
    DocumentItem,
    WeaponRegistry,
    ArsenalUser
)
from app.modules.arsenal.services import WeaponService

# ИМПОРТ НОВОГО СЕРВИСА ДЛЯ EXCEL
from app.modules.arsenal.services.excel_import import import_arsenal_from_excel

# ======================================================
# НАСТРОЙКА ХЕШИРОВАНИЯ (Argon2)
# ======================================================
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


# ======================================================
# АВТОРИЗАЦИЯ ТОЛЬКО ДЛЯ АРСЕНАЛА (Изолированная)
# ======================================================
async def get_current_arsenal_user(
        request: Request,
        db: AsyncSession = Depends(get_arsenal_db)
):
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

    result = await db.execute(select(ArsenalUser).where(ArsenalUser.username == username))
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=401, detail="Пользователь Арсенала не найден")

    return user


# ======================================================
# PYDANTIC СХЕМЫ (Валидация входящих данных)
# ======================================================

class ObjCreate(BaseModel):
    name: str
    obj_type: str
    parent_id: Optional[int] = None
    # 🔥 НОВОЕ: Материально-ответственное лицо
    mol_name: Optional[str] = None


class NomenclatureCreate(BaseModel):
    code: Optional[str] = None
    name: str
    category: Optional[str] = None
    is_numbered: bool = True
    # 🔥 НОВОЕ: Счет учета
    default_account: Optional[str] = None


class DocItemCreate(BaseModel):
    nomenclature_id: int
    serial_number: Optional[str] = None
    quantity: int = 1
    # 🔥 НОВЫЕ ПОЛЯ ИЗ TXT/EXCEL
    inventory_number: Optional[str] = None
    price: Optional[float] = None


class DocCreate(BaseModel):
    doc_number: Optional[str] = None
    operation_type: str
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    operation_date: Optional[datetime] = None
    items: List[DocItemCreate]

    @validator("operation_date", pre=True, always=True)
    def normalize_date(cls, value):
        if not value:
            return datetime.utcnow()
        if isinstance(value, str):
            if len(value) == 10:
                return datetime.strptime(value, "%Y-%m-%d")
            return datetime.fromisoformat(value)
        return value


# ======================================================
# РОУТЕР
# ======================================================

router = APIRouter(prefix="/api/arsenal", tags=["STROB Arsenal"])


# ======================================================
# 1. ОБЪЕКТЫ УЧЕТА (Склады, Подразделения)
# ======================================================

@router.get("/objects")
async def get_objects(
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Получить список всех объектов учета"""
    result = await db.execute(
        select(AccountingObject).order_by(AccountingObject.name)
    )
    # Возвращаем со всеми новыми полями (Алхимия сама сериализует)
    return result.scalars().all()


@router.post("/objects")
async def create_object(
        data: ObjCreate,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Создать новый объект учета и АВТОМАТИЧЕСКИ создать для него начальника"""

    if current_user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Только администратор может создавать новые объекты и структуры"
        )

    existing = await db.execute(
        select(AccountingObject).where(AccountingObject.name == data.name)
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Объект с таким именем уже существует"
        )

    # Создаем объект, mol_name автоматически подхватится из data.dict()
    obj = AccountingObject(**data.dict())
    db.add(obj)
    await db.flush()

    new_username = f"unit_{obj.id}"
    alphabet = string.ascii_letters + string.digits
    new_password = ''.join(secrets.choice(alphabet) for _ in range(8))
    hashed_pw = pwd_context.hash(new_password)

    new_user = ArsenalUser(
        username=new_username,
        hashed_password=hashed_pw,
        role="unit_head",
        object_id=obj.id
    )
    db.add(new_user)

    await db.commit()
    await db.refresh(obj)

    return {
        "id": obj.id,
        "name": obj.name,
        "obj_type": obj.obj_type,
        "mol_name": obj.mol_name,  # Отдаем на фронт
        "credentials": {
            "username": new_username,
            "password": new_password
        }
    }


@router.delete("/objects/{obj_id}")
async def delete_object(
        obj_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Удалить объект учета"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может удалять объекты")

    obj = await db.get(AccountingObject, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Объект не найден")

    await db.delete(obj)
    await db.commit()
    return {"status": "deleted"}


# ======================================================
# 2. НОМЕНКЛАТУРА (Справочник изделий)
# ======================================================

@router.get("/nomenclature")
async def get_nomenclature(
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Получить список номенклатуры"""
    result = await db.execute(
        select(Nomenclature).order_by(Nomenclature.name)
    )
    return result.scalars().all()


@router.post("/nomenclature")
async def create_nomenclature(
        data: NomenclatureCreate,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Добавить новый тип вооружения или боеприпасов"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может добавлять номенклатуру")

    existing = await db.execute(
        select(Nomenclature).where(Nomenclature.name == data.name)
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Изделие с таким наименованием уже существует"
        )

    new_item = Nomenclature(**data.dict())
    db.add(new_item)
    await db.commit()
    await db.refresh(new_item)
    return new_item


# ======================================================
# 3. ДОКУМЕНТЫ (Приход, Перемещение, Списание)
# ======================================================

@router.get("/documents")
async def get_documents(
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Получить журнал документов с учетом роли пользователя"""
    stmt = (
        select(Document)
        .options(
            selectinload(Document.source),
            selectinload(Document.target)
        )
        .order_by(
            Document.operation_date.desc(),
            Document.created_at.desc()
        )
    )

    if current_user.role == "unit_head":
        stmt = stmt.where(
            (Document.source_id == current_user.object_id) |
            (Document.target_id == current_user.object_id)
        )

    result = await db.execute(stmt)
    docs = result.scalars().all()

    response_data = []
    for d in docs:
        response_data.append({
            "id": d.id,
            "doc_number": d.doc_number,
            "date": d.operation_date.strftime("%d.%m.%Y")
            if d.operation_date else "-",
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
    """Получить подробную информацию о документе"""
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
            raise HTTPException(status_code=403,
                                detail="Отказано в доступе. Этот документ не принадлежит вашему подразделению.")

    return doc


@router.post("/documents")
async def create_document(
        data: DocCreate,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """
    Создать документ с автоматической проводкой по реестру оружия.
    Операция выполняется атомарно через WeaponService.
    """
    if current_user.role == "unit_head":
        if data.operation_type in ["Отправка", "Перемещение", "Списание"]:
            if data.source_id != current_user.object_id:
                raise HTTPException(
                    status_code=403,
                    detail="Вы можете списывать/отправлять имущество только со своего склада!"
                )

        if data.operation_type in ["Первичный ввод", "Прием"]:
            if data.target_id != current_user.object_id:
                raise HTTPException(
                    status_code=403,
                    detail="Вы можете принимать имущество только на свой склад!"
                )

    try:
        new_doc = await WeaponService.process_document(
            db,
            data,
            data.items
        )
        return {
            "status": "created",
            "id": new_doc.id
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Ошибка проведения документа: {str(e)}"
        )


@router.delete("/documents/{doc_id}")
async def delete_document(
        doc_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Удалить документ (Только админ)."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может удалять документы")

    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    await db.delete(doc)
    await db.commit()
    return {"status": "deleted"}


# ======================================================
# 4. ОСТАТКИ (РЕЕСТР)
# ======================================================

@router.get("/balance/{obj_id}")
async def get_object_balance(
        obj_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Получить текущие остатки по объекту."""
    if current_user.role == "unit_head" and obj_id != current_user.object_id:
        raise HTTPException(
            status_code=403,
            detail="Вы можете просматривать остатки только своего подразделения"
        )

    stmt = (
        select(WeaponRegistry)
        .join(Nomenclature)
        .options(selectinload(WeaponRegistry.nomenclature))
        .where(
            WeaponRegistry.current_object_id == obj_id,
            WeaponRegistry.status == 1
        )
        .order_by(Nomenclature.name, WeaponRegistry.serial_number)
    )

    weapons = (await db.execute(stmt)).scalars().all()

    balance = []
    for weapon in weapons:
        is_numbered = weapon.nomenclature.is_numbered
        display_serial = weapon.serial_number

        if not is_numbered:
            display_serial = f"Партия {weapon.serial_number}"

        # Определяем счет (если у конкретной единицы не задан - берем из номенклатуры)
        account = weapon.account_code or weapon.nomenclature.default_account or "Не указан"

        balance.append({
            "nomenclature": weapon.nomenclature.name,
            "code": weapon.nomenclature.code,
            "serial_number": display_serial,
            # 🔥 НОВЫЕ ПОЛЯ ОТПРАВЛЯЮТСЯ НА ФРОНТЕНД
            "inventory_number": weapon.inventory_number or "Б/Н",
            "price": float(weapon.price) if weapon.price else 0.0,
            "account": account,

            "quantity": weapon.quantity,
            "is_numbered": is_numbered
        })

    return balance


# ======================================================
# 5. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (Только для Админа)
# ======================================================

@router.get("/users")
async def get_users(
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Получить список всех пользователей (только для админа)"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    stmt = select(ArsenalUser).options(selectinload(ArsenalUser.accounting_object)).order_by(ArsenalUser.id)
    result = await db.execute(stmt)
    users = result.scalars().all()

    response = []
    for u in users:
        response.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "object_name": u.accounting_object.name if u.accounting_object else "Главное управление",
            "created_at": u.created_at.strftime("%d.%m.%Y")
        })
    return response


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
        user_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Сброс пароля пользователя (Генерирует новый)"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    user = await db.get(ArsenalUser, user_id)
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


# ======================================================
# 6. ИМПОРТ ИЗ EXCEL (Для Админа)
# ======================================================

@router.post("/import")
async def import_excel_data(
        file: UploadFile = File(...),
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Импорт остатков, складов и номенклатуры из Excel файла"""

    # ПРОВЕРКА РОЛИ: Только администратор может загружать начальные остатки
    if current_user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Только администратор может выполнять массовый импорт данных"
        )

    # Проверка формата файла
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Неверный формат. Пожалуйста, загрузите файл формата Excel (.xlsx или .xls)"
        )

    # Чтение байтов файла
    file_bytes = await file.read()

    # Запуск сервиса парсинга
    result = await import_arsenal_from_excel(file_bytes, db)

    # Обработка ошибки сервиса
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["message"])

    return result