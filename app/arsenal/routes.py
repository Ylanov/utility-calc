import secrets
import string
from passlib.context import CryptContext
from fastapi import APIRouter, Depends, HTTPException, status, Request
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
from app.database import get_arsenal_db
from app.config import settings

from app.arsenal.models import (
    AccountingObject,
    Nomenclature,
    Document,
    DocumentItem,
    WeaponRegistry,
    ArsenalUser
)
from app.arsenal.services import WeaponService

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
    # Достаем токен из куки (как настроено в auth.py)
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

    # Ищем пользователя ИМЕННО в базе Арсенала
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


class NomenclatureCreate(BaseModel):
    code: Optional[str] = None
    name: str
    category: Optional[str] = None
    # Флаг: True - номерной учет (автоматы), False - партионный (патроны)
    is_numbered: bool = True


class DocItemCreate(BaseModel):
    nomenclature_id: int
    serial_number: Optional[str] = None
    quantity: int = 1


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
            # Если приходит только дата без времени
            if len(value) == 10:
                return datetime.strptime(value, "%Y-%m-%d")

            # Если ISO формат
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
    return result.scalars().all()


@router.post("/objects")
async def create_object(
        data: ObjCreate,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    """Создать новый объект учета и АВТОМАТИЧЕСКИ создать для него начальника"""

    # ПРОВЕРКА РОЛИ: Только admin может создавать объекты
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

    # 1. Создаем сам объект
    obj = AccountingObject(**data.dict())
    db.add(obj)
    await db.flush()  # Делаем flush, чтобы БД присвоила объекту ID (obj.id)

    # 2. Генерируем учетные данные для начальника склада
    new_username = f"unit_{obj.id}"

    # Генерируем случайный 8-значный пароль
    alphabet = string.ascii_letters + string.digits
    new_password = ''.join(secrets.choice(alphabet) for _ in range(8))

    # Хешируем пароль через Argon2
    hashed_pw = pwd_context.hash(new_password)

    # 3. Создаем учетную запись пользователя и привязываем к складу
    new_user = ArsenalUser(
        username=new_username,
        hashed_password=hashed_pw,
        role="unit_head",  # Роль начальника подразделения
        object_id=obj.id  # Привязка к созданному объекту
    )
    db.add(new_user)

    await db.commit()
    await db.refresh(obj)

    # Возвращаем данные объекта И сгенерированные доступы для вывода админу
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

    # ФИЛЬТРАЦИЯ ПО РОЛЯМ: Начальник склада видит только свои документы
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
            selectinload(Document.items)
            .selectinload(DocumentItem.nomenclature),
            selectinload(Document.items)
            .selectinload(DocumentItem.weapon)
        )
    )
    doc = (await db.execute(stmt)).scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    # Проверка прав: может ли начальник склада видеть этот конкретный документ
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

    # ПРОВЕРКА ПРАВ: Начальник склада может работать только со своим складом
    if current_user.role == "unit_head":
        # Если это расход/отправка - источником ОБЯЗАН быть его склад
        if data.operation_type in ["Отправка", "Перемещение", "Списание"]:
            if data.source_id != current_user.object_id:
                raise HTTPException(
                    status_code=403,
                    detail="Вы можете списывать/отправлять имущество только со своего склада!"
                )

        # Если это приход - получателем ОБЯЗАН быть его склад
        if data.operation_type in ["Первичный ввод", "Прием"]:
            if data.target_id != current_user.object_id:
                raise HTTPException(
                    status_code=403,
                    detail="Вы можете принимать имущество только на свой склад!"
                )

    try:
        # Вся бизнес-логика (включая партионный учет) инкапсулирована в сервисе
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
    """
    Получить текущие остатки по объекту.
    """
    # ПРОВЕРКА ПРАВ: Начальник видит остатки только своего подразделения
    if current_user.role == "unit_head" and obj_id != current_user.object_id:
        raise HTTPException(
            status_code=403,
            detail="Вы можете просматривать остатки только своего подразделения"
        )

    stmt = (
        select(WeaponRegistry)
        .join(Nomenclature)  # Джойн для сортировки по имени
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
        # Определяем, как отображать серийник
        is_numbered = weapon.nomenclature.is_numbered
        display_serial = weapon.serial_number

        # Если учет партионный, серийник - это номер партии
        if not is_numbered:
            display_serial = f"Партия {weapon.serial_number}"

        balance.append({
            "nomenclature": weapon.nomenclature.name,
            "code": weapon.nomenclature.code,
            "serial_number": display_serial,
            "quantity": weapon.quantity,  # Теперь здесь реальное количество
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

    # Генерируем новый пароль
    alphabet = string.ascii_letters + string.digits
    new_password = ''.join(secrets.choice(alphabet) for _ in range(8))

    # Хешируем и сохраняем
    user.hashed_password = pwd_context.hash(new_password)
    db.add(user)
    await db.commit()

    return {
        "message": "Пароль успешно сброшен",
        "username": user.username,
        "new_password": new_password
    }