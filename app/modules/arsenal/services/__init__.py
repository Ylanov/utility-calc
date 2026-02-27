# app/modules/arsenal/services/__init__.py

# Экспортируем WeaponService из нового файла
from .weapon_service import WeaponService

# Экспортируем функцию импорта Excel (которую мы создали ранее)
from .excel_import import import_arsenal_from_excel