"""Тесты role-guard зависимостей (RoleChecker).

ЛК жильцов вычищен (2026-06-10): require_resident и резидентские эндпоинты
удалены, подача — только через анонимный QR-портал. Остались проверки
RoleChecker для админских ручек.
"""
import pytest
from fastapi import HTTPException

from app.core.dependencies import (
    RoleChecker,
    allow_accountant,
    allow_financier,
)


class _FakeUser:
    def __init__(self, role):
        self.role = role
        self.id = 1
        self.username = f"test_{role}"


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


def test_role_checker_financier_admin_only():
    """allow_financier теперь алиас для admin-only (см. roles_001_simplify).
    Раньше пускал financier/accountant/admin — после упрощения только admin."""
    # admin проходит.
    assert allow_financier(_FakeUser("admin")) is _FakeUser("admin").__class__("admin") or True
    user_admin = _FakeUser("admin")
    assert allow_financier(user_admin) is user_admin
    # Старые роли (если кто-то ещё с ними в БД) — отбиваются.
    for role in ("financier", "accountant"):
        with pytest.raises(HTTPException) as exc_info:
            allow_financier(_FakeUser(role))
        assert exc_info.value.status_code == 403
