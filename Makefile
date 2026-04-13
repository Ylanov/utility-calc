# Makefile
#
# Удобные команды для управления проектом.
#
# ЛОКАЛЬНАЯ РАЗРАБОТКА:
#   make local-up        — поднять всё локально
#   make local-down      — остановить
#   make local-rebuild   — пересобрать образ и поднять
#   make local-logs      — логи всех сервисов
#   make local-shell     — bash внутри web_jkh контейнера
#   make local-test      — запустить тесты
#   make local-migrate   — применить новые миграции
#
# PRODUCTION (запускается на сервере вручную или через CI):
#   make prod-up         — поднять production
#   make prod-down       — остановить production
#   make prod-logs       — логи production
#   make prod-migrate    — применить миграции в production
#
# УТИЛИТЫ:
#   make clean           — удалить все остановленные контейнеры и образы
#   make new-migration   — создать новую alembic-миграцию (name=xxx)
#   make make-migration-arsenal — создать миграцию для Arsenal DB

LOCAL_COMPOSE  = docker compose -f docker-compose.local.yml
PROD_COMPOSE   = docker compose -f docker-compose.yml

# ──────────────────────────────────────────────────────────────
# ЛОКАЛЬНАЯ РАЗРАБОТКА
# ──────────────────────────────────────────────────────────────

.PHONY: local-up
local-up:
	@echo "🚀 Запуск локального окружения..."
	$(LOCAL_COMPOSE) --env-file .env.local up -d
	@echo ""
	@echo "✅ Готово! Доступно:"
	@echo "   http://localhost        — основное приложение (через Nginx)"
	@echo "   http://localhost:8001   — ЖКХ API напрямую + Swagger /docs"
	@echo "   http://localhost:8002   — Арсенал/ГСМ API напрямую + Swagger /docs"
	@echo "   http://localhost:9001   — MinIO Web Console"
	@echo "   localhost:5432          — PostgreSQL (для DBeaver/PgAdmin)"
	@echo "   localhost:6379          — Redis"

.PHONY: local-down
local-down:
	@echo "⏹  Остановка локального окружения..."
	$(LOCAL_COMPOSE) --env-file .env.local down

.PHONY: local-rebuild
local-rebuild:
	@echo "🔨 Пересборка образа и запуск..."
	$(LOCAL_COMPOSE) --env-file .env.local up -d --build

.PHONY: local-restart
local-restart:
	@echo "🔄 Перезапуск..."
	$(LOCAL_COMPOSE) --env-file .env.local restart

.PHONY: local-logs
local-logs:
	$(LOCAL_COMPOSE) --env-file .env.local logs -f --tail=100

.PHONY: local-logs-jkh
local-logs-jkh:
	$(LOCAL_COMPOSE) --env-file .env.local logs -f --tail=100 web_jkh

.PHONY: local-logs-worker
local-logs-worker:
	$(LOCAL_COMPOSE) --env-file .env.local logs -f --tail=100 worker

.PHONY: local-shell
local-shell:
	@echo "🐚 Открываю bash в web_jkh..."
	$(LOCAL_COMPOSE) --env-file .env.local exec web_jkh bash

.PHONY: local-shell-db
local-shell-db:
	@echo "🗄  Открываю psql..."
	$(LOCAL_COMPOSE) --env-file .env.local exec db psql -U postgres -d utility_db

.PHONY: local-test
local-test:
	@echo "🧪 Запуск тестов..."
	$(LOCAL_COMPOSE) --env-file .env.local exec web_jkh python -m pytest app/tests/ -v

.PHONY: local-test-math
local-test-math:
	@echo "🧮 Запуск тестов расчётов..."
	$(LOCAL_COMPOSE) --env-file .env.local exec web_jkh python -m pytest app/tests/test_calculations.py -v

.PHONY: local-migrate
local-migrate:
	@echo "📦 Применение миграций ЖКХ..."
	$(LOCAL_COMPOSE) --env-file .env.local exec web_jkh alembic upgrade head
	@echo "📦 Применение миграций Арсенал..."
	$(LOCAL_COMPOSE) --env-file .env.local exec web_jkh alembic -c alembic_arsenal.ini upgrade head

.PHONY: local-status
local-status:
	$(LOCAL_COMPOSE) --env-file .env.local ps

# ──────────────────────────────────────────────────────────────
# МИГРАЦИИ (создание новых)
# ──────────────────────────────────────────────────────────────

.PHONY: new-migration
new-migration:
ifndef name
	$(error Укажи имя: make new-migration name=add_something_table)
endif
	@echo "📝 Создание миграции ЖКХ: $(name)"
	$(LOCAL_COMPOSE) --env-file .env.local exec web_jkh \
		alembic revision --autogenerate -m "$(name)"

.PHONY: new-migration-arsenal
new-migration-arsenal:
ifndef name
	$(error Укажи имя: make new-migration-arsenal name=add_something)
endif
	@echo "📝 Создание миграции Арсенал: $(name)"
	$(LOCAL_COMPOSE) --env-file .env.local exec web_jkh \
		alembic -c alembic_arsenal.ini revision --autogenerate -m "$(name)"

.PHONY: migration-history
migration-history:
	$(LOCAL_COMPOSE) --env-file .env.local exec web_jkh alembic history --verbose

# ──────────────────────────────────────────────────────────────
# PRODUCTION
# ──────────────────────────────────────────────────────────────

.PHONY: prod-up
prod-up:
	@echo "🚀 Запуск production..."
	$(PROD_COMPOSE) --env-file .env up -d

.PHONY: prod-down
prod-down:
	@echo "⏹  Остановка production..."
	$(PROD_COMPOSE) --env-file .env down

.PHONY: prod-logs
prod-logs:
	$(PROD_COMPOSE) --env-file .env logs -f --tail=100

.PHONY: prod-logs-jkh
prod-logs-jkh:
	$(PROD_COMPOSE) --env-file .env logs -f --tail=100 web_jkh

.PHONY: prod-migrate
prod-migrate:
	@echo "📦 Применение миграций в production..."
	$(PROD_COMPOSE) --env-file .env run --rm migration_job \
		sh -c "alembic upgrade head && alembic -c alembic_arsenal.ini upgrade head"

.PHONY: prod-status
prod-status:
	$(PROD_COMPOSE) --env-file .env ps

.PHONY: prod-health
prod-health:
	@curl -sf http://localhost/health && echo "✅ Healthy" || echo "❌ Unhealthy"

# ──────────────────────────────────────────────────────────────
# УТИЛИТЫ
# ──────────────────────────────────────────────────────────────

.PHONY: clean
clean:
	@echo "🧹 Очистка остановленных контейнеров и образов..."
	docker container prune -f
	docker image prune -f
	@echo "✅ Готово"

.PHONY: clean-all
clean-all:
	@echo "🧹 Полная очистка (включая тома)..."
	$(LOCAL_COMPOSE) --env-file .env.local down -v
	docker container prune -f
	docker image prune -af
	docker volume prune -f
	@echo "✅ Готово"

.PHONY: help
help:
	@echo ""
	@echo "📖 Доступные команды:"
	@echo ""
	@echo "  ЛОКАЛЬНАЯ РАЗРАБОТКА:"
	@echo "    make local-up            — поднять всё локально"
	@echo "    make local-down          — остановить"
	@echo "    make local-rebuild       — пересобрать образ и поднять"
	@echo "    make local-logs          — логи всех сервисов"
	@echo "    make local-logs-jkh      — логи только ЖКХ"
	@echo "    make local-logs-worker   — логи воркера"
	@echo "    make local-shell         — bash в web_jkh"
	@echo "    make local-shell-db      — psql в PostgreSQL"
	@echo "    make local-test          — все тесты"
	@echo "    make local-test-math     — тесты расчётов"
	@echo "    make local-migrate       — применить новые миграции"
	@echo "    make local-status        — статус контейнеров"
	@echo ""
	@echo "  МИГРАЦИИ:"
	@echo "    make new-migration name=add_table       — ЖКХ"
	@echo "    make new-migration-arsenal name=add_table — Арсенал"
	@echo "    make migration-history                  — история"
	@echo ""
	@echo "  PRODUCTION:"
	@echo "    make prod-up             — запустить"
	@echo "    make prod-down           — остановить"
	@echo "    make prod-logs           — логи"
	@echo "    make prod-migrate        — применить миграции"
	@echo "    make prod-health         — проверить /health"
	@echo ""
	@echo "  УТИЛИТЫ:"
	@echo "    make clean               — очистить остановленные контейнеры/образы"
	@echo "    make clean-all           — полная очистка включая тома"
	@echo ""