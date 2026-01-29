from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, Float, DateTime, String, ForeignKey, Boolean
from sqlalchemy.future import select
from datetime import datetime
from passlib.context import CryptContext
from jose import JWTError, jwt
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db/utility_db")
SECRET_KEY = "supersecretkey"
ALGORITHM = "HS256"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# --- МОДЕЛИ БД ---

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String)
    dormitory = Column(String, nullable=True)
    workplace = Column(String, nullable=True)
    residents_count = Column(Integer, default=1)
    apartment_area = Column(Float, default=0.0)


class Tariff(Base):
    __tablename__ = "tariffs"
    id = Column(Integer, primary_key=True)
    maintenance_repair = Column(Float, default=0.0)
    social_rent = Column(Float, default=0.0)
    heating = Column(Float, default=0.0)
    water_heating = Column(Float, default=0.0)
    water_supply = Column(Float, default=0.0)
    sewage = Column(Float, default=0.0)
    waste_disposal = Column(Float, default=0.0)
    electricity_per_sqm = Column(Float, default=0.0)


class MeterReading(Base):
    __tablename__ = "readings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))

    # Показания счетчиков (набежавшие)
    hot_water = Column(Float)
    cold_water = Column(Float)
    electricity = Column(Float)

    # Коррекции (вводит бухгалтер)
    hot_correction = Column(Float, default=0.0)
    cold_correction = Column(Float, default=0.0)

    # Итоговая сумма к оплате
    total_cost = Column(Float, default=0.0)

    is_approved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# --- PYDANTIC ---

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
    dormitory: str = ""
    workplace: str = ""
    residents_count: int = 1
    apartment_area: float = 0.0


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    dormitory: str | None
    workplace: str | None
    residents_count: int
    apartment_area: float

    class Config:
        from_attributes = True


class TariffSchema(BaseModel):
    maintenance_repair: float
    social_rent: float
    heating: float
    water_heating: float
    water_supply: float
    sewage: float
    waste_disposal: float
    electricity_per_sqm: float

    class Config:
        from_attributes = True


class ReadingSchema(BaseModel):
    hot_water: float
    cold_water: float
    electricity: float


class ReadingStateResponse(BaseModel):
    prev_hot: float
    prev_cold: float
    prev_elect: float
    current_hot: float | None
    current_cold: float | None
    current_elect: float | None
    total_cost: float | None
    is_draft: bool


# Схема для утверждения показаний с коррекцией
class ApproveRequest(BaseModel):
    hot_correction: float
    cold_correction: float


# --- ФУНКЦИИ ---

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def create_access_token(data: dict):
    return jwt.encode(data.copy(), SECRET_KEY, algorithm=ALGORITHM)


app = FastAPI()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        if not (await db.execute(select(User).where(User.username == "admin"))).scalars().first():
            db.add(User(username="admin", hashed_password=get_password_hash("admin"), role="accountant"))
            await db.commit()
        if not (await db.execute(select(Tariff).where(Tariff.id == 1))).scalars().first():
            db.add(Tariff(id=1))
            await db.commit()


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: raise HTTPException(status_code=401)
    except JWTError:
        raise HTTPException(status_code=401)
    user = (await db.execute(select(User).where(User.username == username))).scalars().first()
    if user is None: raise HTTPException(status_code=401)
    return user


# --- API ---

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.username == form_data.username))).scalars().first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Ошибка входа")
    return {"access_token": create_access_token({"sub": user.username, "role": user.role}), "token_type": "bearer",
            "role": user.role}


# USERS
@app.post("/api/users", response_model=UserResponse)
async def create_user(new_user: UserCreate, current_user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    if current_user.role != "accountant": raise HTTPException(status_code=403)
    if (await db.execute(select(User).where(User.username == new_user.username))).scalars().first():
        raise HTTPException(status_code=400, detail="User exists")
    db_user = User(username=new_user.username, hashed_password=get_password_hash(new_user.password), role=new_user.role,
                   dormitory=new_user.dormitory, workplace=new_user.workplace, residents_count=new_user.residents_count,
                   apartment_area=new_user.apartment_area)
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user


@app.get("/api/users")
async def read_users(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user.role != "accountant": raise HTTPException(status_code=403)
    return (await db.execute(select(User).order_by(User.id))).scalars().all()


# TARIFFS
@app.get("/api/tariffs", response_model=TariffSchema)
async def get_tariffs(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(Tariff).where(Tariff.id == 1))).scalars().first()


@app.post("/api/tariffs")
async def update_tariffs(data: TariffSchema, current_user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    if current_user.role != "accountant": raise HTTPException(status_code=403)
    tariff = (await db.execute(select(Tariff).where(Tariff.id == 1))).scalars().first()
    for k, v in data.dict().items(): setattr(tariff, k, v)
    await db.commit()
    return tariff


# --- ЛОГИКА ПОКАЗАНИЙ ---

@app.get("/api/readings/state", response_model=ReadingStateResponse)
async def get_reading_state(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    prev_res = await db.execute(
        select(MeterReading).where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True).order_by(
            MeterReading.created_at.desc()).limit(1))
    prev = prev_res.scalars().first()

    draft_res = await db.execute(
        select(MeterReading).where(MeterReading.user_id == current_user.id, MeterReading.is_approved == False).order_by(
            MeterReading.created_at.desc()).limit(1))
    draft = draft_res.scalars().first()

    return {
        "prev_hot": prev.hot_water if prev else 0.0,
        "prev_cold": prev.cold_water if prev else 0.0,
        "prev_elect": prev.electricity if prev else 0.0,
        "current_hot": draft.hot_water if draft else None,
        "current_cold": draft.cold_water if draft else None,
        "current_elect": draft.electricity if draft else None,
        "total_cost": draft.total_cost if draft else None,
        "is_draft": True if draft else False
    }


@app.post("/api/calculate")
async def save_reading(data: ReadingSchema, current_user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    # Предварительный расчет для жильца (без учета коррекций, т.к. их вводит бухгалтер позже)
    t = (await db.execute(select(Tariff).where(Tariff.id == 1))).scalars().first()
    prev_res = await db.execute(
        select(MeterReading).where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True).order_by(
            MeterReading.created_at.desc()).limit(1))
    prev = prev_res.scalars().first()

    p_hot = prev.hot_water if prev else 0.0
    p_cold = prev.cold_water if prev else 0.0
    p_elect = prev.electricity if prev else 0.0

    if data.hot_water < p_hot: raise HTTPException(400, f"Гор. вода меньше предыдущей ({p_hot})")
    if data.cold_water < p_cold: raise HTTPException(400, f"Хол. вода меньше предыдущей ({p_cold})")
    if data.electricity < p_elect: raise HTTPException(400, f"Свет меньше предыдущего ({p_elect})")

    d_hot = data.hot_water - p_hot
    d_cold = data.cold_water - p_cold
    d_elect = data.electricity - p_elect

    cost_hot = d_hot * (t.water_heating + t.water_supply + t.sewage)
    cost_cold = d_cold * (t.water_supply + t.sewage)
    cost_elect = d_elect * 5.0
    cost_fix = (current_user.apartment_area * (
                t.maintenance_repair + t.social_rent + t.heating + t.electricity_per_sqm)) + (
                           current_user.residents_count * t.waste_disposal)

    total = cost_hot + cost_cold + cost_elect + cost_fix

    draft_res = await db.execute(
        select(MeterReading).where(MeterReading.user_id == current_user.id, MeterReading.is_approved == False))
    draft = draft_res.scalars().first()

    if draft:
        draft.hot_water = data.hot_water
        draft.cold_water = data.cold_water
        draft.electricity = data.electricity
        draft.total_cost = total
        draft.created_at = datetime.utcnow()
    else:
        new_reading = MeterReading(
            user_id=current_user.id,
            hot_water=data.hot_water,
            cold_water=data.cold_water,
            electricity=data.electricity,
            total_cost=total,
            is_approved=False
        )
        db.add(new_reading)

    await db.commit()
    return {"status": "success", "total_cost": round(total, 2)}


# БУХГАЛТЕР: Получить черновики
@app.get("/api/admin/readings")
async def get_admin_readings(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user.role != "accountant": raise HTTPException(403)

    stmt = select(MeterReading, User).join(User, MeterReading.user_id == User.id).where(
        MeterReading.is_approved == False)
    results = await db.execute(stmt)

    data = []
    for reading, user in results:
        prev_res = await db.execute(
            select(MeterReading).where(MeterReading.user_id == user.id, MeterReading.is_approved == True).order_by(
                MeterReading.created_at.desc()).limit(1))
        prev = prev_res.scalars().first()

        data.append({
            "id": reading.id,
            "user_id": user.id,
            "username": user.username,
            "dormitory": user.dormitory,
            "prev_hot": prev.hot_water if prev else 0.0,
            "cur_hot": reading.hot_water,
            "prev_cold": prev.cold_water if prev else 0.0,
            "cur_cold": reading.cold_water,
            "prev_elect": prev.electricity if prev else 0.0,
            "cur_elect": reading.electricity,
            "total_cost": reading.total_cost,
            "created_at": reading.created_at
        })
    return data


# БУХГАЛТЕР: Утвердить с коррекцией и пересчетом
@app.post("/api/admin/approve/{reading_id}")
async def approve_reading(reading_id: int, correction_data: ApproveRequest,
                          current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user.role != "accountant": raise HTTPException(403)

    # 1. Загружаем черновик и пользователя
    reading = await db.get(MeterReading, reading_id)
    if not reading: raise HTTPException(404, detail="Запись не найдена")
    user = await db.get(User, reading.user_id)

    # 2. Загружаем тарифы
    t = (await db.execute(select(Tariff).where(Tariff.id == 1))).scalars().first()

    # 3. Загружаем предыдущие показания для дельты
    prev_res = await db.execute(
        select(MeterReading).where(MeterReading.user_id == user.id, MeterReading.is_approved == True).order_by(
            MeterReading.created_at.desc()).limit(1))
    prev = prev_res.scalars().first()

    p_hot = prev.hot_water if prev else 0.0
    p_cold = prev.cold_water if prev else 0.0
    p_elect = prev.electricity if prev else 0.0

    # 4. Считаем чистую дельту
    d_hot_raw = reading.hot_water - p_hot
    d_cold_raw = reading.cold_water - p_cold
    d_elect = reading.electricity - p_elect

    # 5. Применяем коррекцию (Дельта - Коррекция)
    # Пример: Расход 10, Коррекция 2. Итого к оплате 8 кубов.
    d_hot_final = d_hot_raw - correction_data.hot_correction
    d_cold_final = d_cold_raw - correction_data.cold_correction

    # Защита от отрицательных объемов (опционально, но лучше оставить для гибкости)
    # if d_hot_final < 0: d_hot_final = 0
    # if d_cold_final < 0: d_cold_final = 0

    # 6. Пересчитываем деньги ПО ТВОЕЙ ЛОГИКЕ

    # Горячая вода
    cost_hot = d_hot_final * t.water_heating

    # Холодная вода
    cost_cold = d_cold_final * t.water_supply

    # Электричество (оставляем как есть)
    cost_elect = d_elect * 5.0

    # Фиксированные платежи
    cost_fix = (
                       user.apartment_area * (
                       t.maintenance_repair +
                       t.social_rent +
                       t.heating +
                       t.electricity_per_sqm
               )
               ) + (user.residents_count * t.waste_disposal)

    # Итог
    new_total = cost_hot + cost_cold + cost_elect + cost_fix

    # 7. Обновляем запись
    reading.hot_correction = correction_data.hot_correction
    reading.cold_correction = correction_data.cold_correction
    reading.total_cost = new_total
    reading.is_approved = True  # ФИКСИРУЕМ

    await db.commit()
    return {"status": "approved", "new_total": new_total}


app.mount("/", StaticFiles(directory="static", html=True), name="static")