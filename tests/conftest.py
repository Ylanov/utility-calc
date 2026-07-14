# tests/conftest.py
"""Общая подготовка тестов.

Тесты — ЧИСТЫЕ (без БД/Redis/S3): проверяют биллинг-критичную логику,
которая уже ловила деньги-баги (clean_decimal, хронология периодов,
выбор prev, валидаторы, аномалии). Env выставляется ДО импорта app.*,
иначе pydantic Settings упадёт на обязательном SECRET_KEY.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-prod-0123456789abcdef")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
