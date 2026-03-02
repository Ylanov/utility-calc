import secrets
import string
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_

from app.core.database import get_arsenal_db
from app.modules.arsenal.models import AccountingObject, ArsenalUser, WeaponRegistry, Nomenclature
from app.modules.arsenal.schemas import ObjCreate
from app.modules.arsenal.deps import get_current_arsenal_user, pwd_context

router = APIRouter(tags=["Arsenal Objects"])


@router.get("/objects")
async def get_objects(
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    result = await db.execute(select(AccountingObject).order_by(AccountingObject.name))
    return result.scalars().all()


@router.post("/objects")
async def create_object(
        data: ObjCreate,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может создавать новые объекты")

    existing = await db.execute(select(AccountingObject).where(AccountingObject.name == data.name))
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Объект с таким именем уже существует")

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
        "mol_name": obj.mol_name,
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
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может удалять объекты")

    obj = await db.get(AccountingObject, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Объект не найден")

    await db.delete(obj)
    await db.commit()
    return {"status": "deleted"}


@router.get("/balance/{obj_id}")
async def get_object_balance(
        obj_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user),
        skip: int = Query(0, ge=0, description="Смещение (для пагинации)"),
        limit: int = Query(1000, ge=1, le=5000, description="Максимальное количество возвращаемых строк"),
        q: Optional[str] = Query(None, description="Поиск по названию изделия, инвентарному или заводскому номеру")
):
    if current_user.role == "unit_head" and obj_id != current_user.object_id:
        raise HTTPException(status_code=403, detail="Вы можете просматривать остатки только своего подразделения")

    # 🔥 ОПТИМИЗАЦИЯ: Плоский запрос без ORM overhead
    stmt = (
        select(
            WeaponRegistry.nomenclature_id,
            WeaponRegistry.serial_number,
            WeaponRegistry.inventory_number,
            WeaponRegistry.price,
            WeaponRegistry.quantity,
            WeaponRegistry.account_code,
            WeaponRegistry.kbk,
            Nomenclature.name.label("nom_name"),
            Nomenclature.code.label("nom_code"),
            Nomenclature.is_numbered,
            Nomenclature.default_account
        )
        .join(Nomenclature, WeaponRegistry.nomenclature_id == Nomenclature.id)
        .where(
            WeaponRegistry.current_object_id == obj_id,
            WeaponRegistry.status == 1
        )
    )

    # 🔥 ОПТИМИЗАЦИЯ: Делегируем поиск базе данных (LIKE)
    if q:
        search_term = f"%{q}%"
        stmt = stmt.where(
            or_(
                Nomenclature.name.ilike(search_term),
                WeaponRegistry.serial_number.ilike(search_term),
                WeaponRegistry.inventory_number.ilike(search_term)
            )
        )

    # 🔥 ОПТИМИЗАЦИЯ: Пагинация. Защищаем сервер от выгрузки миллиона строк
    stmt = stmt.order_by(Nomenclature.name, WeaponRegistry.serial_number).offset(skip).limit(limit)

    result = await db.execute(stmt)
    rows = result.all()

    balance = []
    for row in rows:
        display_serial = row.serial_number if row.is_numbered else f"Партия {row.serial_number}"
        account = row.account_code or row.default_account or "Не указан"

        balance.append({
            "nomenclature_id": row.nomenclature_id,
            "nomenclature": row.nom_name,
            "code": row.nom_code,
            "serial_number": display_serial,
            "inventory_number": row.inventory_number or "Б/Н",
            "price": float(row.price) if row.price else 0.0,
            "account": account,
            "kbk": row.kbk or "---",
            "quantity": row.quantity,
            "is_numbered": row.is_numbered
        })

    return balance