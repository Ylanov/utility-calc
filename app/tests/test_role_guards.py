"""Тесты role-guard зависимостей (RoleChecker + require_resident).

Главная проверка — что админ/бухгалтер/финансист НЕ могут попасть в
эндпоинты жильца (раздел личного кабинета). Раньше /api/calculate
принимал любого залогиненного пользователя и теоретически админ мог
случайно сохранить за себя «подачу показаний» — что приводило к мусорным
MeterReading. Защита: `require_resident` пропускает только role='user'.
"""
import pytest
from fastapi import HTTPException

from app.core.dependencies import (
    RoleChecker,
    require_resident,
    allow_accountant,
    allow_financier,
)


class _FakeUser:
    def __init__(self, role):
        self.role = role
        self.id = 1
        self.username = f"test_{role}"


# ==========================================================================
# require_resident — только жильцы
# ==========================================================================

def test_require_resident_allows_user():
    """role='user' — проходит без ошибок."""
    user = _FakeUser("user")
    result = require_resident(user)
    assert result is user


def test_require_resident_blocks_admin():
    """role='admin' — 403 (это страховка от случайных подач показаний от админа)."""
    user = _FakeUser("admin")
    with pytest.raises(HTTPException) as exc_info:
        require_resident(user)
    assert exc_info.value.status_code == 403
    assert "сотрудник" in exc_info.value.detail.lower() or "только для жильцов" in exc_info.value.detail.lower()


def test_require_resident_blocks_accountant():
    """Бухгалтер тоже не подаёт показания за себя."""
    user = _FakeUser("accountant")
    with pytest.raises(HTTPException) as exc_info:
        require_resident(user)
    assert exc_info.value.status_code == 403


def test_require_resident_blocks_financier():
    """Финансист тоже."""
    user = _FakeUser("financier")
    with pytest.raises(HTTPException) as exc_info:
        require_resident(user)
    assert exc_info.value.status_code == 403


def test_require_resident_blocks_unknown_role():
    """На случай добавления новых ролей — fail-safe: всё кроме user блокируем."""
    user = _FakeUser("operator")  # вымышленная роль
    with pytest.raises(HTTPException) as exc_info:
        require_resident(user)
    assert exc_info.value.status_code == 403


# ==========================================================================
# RoleChecker — на всякий случай регрессия
# ==========================================================================

def test_role_checker_allow_accountant_admins_passthrough():
    """admin всегда разрешён в endpoint'ах бухгалтера (наследует права)."""
    checker = RoleChecker(["accountant"])
    user = _FakeUser("admin")
    assert checker(user) is user


def test_role_checker_blocks_user_for_admin_endpoint():
    """Жилец не должен попасть в admin endpoint."""
    user = _FakeUser("user")
    with pytest.raises(HTTPException) as exc_info:
        allow_accountant(user)
    assert exc_info.value.status_code == 403


def test_role_checker_financier_allows_accountant():
    """allow_financier разрешает accountant и admin (см. определение в dependencies.py)."""
    for role in ("financier", "accountant", "admin"):
        user = _FakeUser(role)
        assert allow_financier(user) is user
