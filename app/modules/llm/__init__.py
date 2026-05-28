"""app.modules.llm — пилот ИИ-помощника (L1-L7, 28.05.2026).

Структура:
  crypto.py   — Fernet-шифрование токена провайдера.
  client.py   — REST-клиент LLM (сейчас GigaChat; будущее vLLM/ollama).
  service.py  — высокоуровневый ask() с audit + budget check.
  prompts.py  — шаблоны промптов для всех purposes.
  tasks.py    — celery-задачи (error_analysis, daily_briefing).
  router.py   — /api/admin/llm/* endpoints.

Принципы:
  - LLM НИКОГДА не отвечает жильцам напрямую; только помощь админу.
  - LLM не имеет WRITE-доступа в БД; только чтение + текст.
  - Каждый вызов через service.ask() → audit + budget check.
  - Превышение бюджета → автоматический disabled_until до полуночи.
"""
