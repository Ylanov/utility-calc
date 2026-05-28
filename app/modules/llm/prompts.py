"""prompts.py — шаблоны промптов для всех purposes пилота.

Каждая функция возвращает messages = [{"role": "system"|"user", ...}].
"""
from __future__ import annotations

import json


SYSTEM_BASE = (
    "Ты — внутренний AI-помощник админа платформы ЖКХ-биллинга. "
    "Платформа считает коммунальные начисления для жильцов общежитий "
    "(показания счётчиков ГВС/ХВС/электр, импорт из Google Sheets, "
    "автозачёт долгов из 1С, тарифы, расчёт по найму). "
    "Ты НИКОГДА не общаешься с жильцами напрямую — только помогаешь "
    "админу разобраться в данных. Отвечай по-русски, кратко, по делу."
)


def build_error_analysis_prompt(error_record: dict) -> list[dict]:
    """Промпт для анализа одной error_log записи (L5).

    error_record = {
        "id", "occurred_at", "source", "http_method", "http_path",
        "http_status", "exc_type", "exc_message", "traceback",
        "request_body", "investigation", "extra", ...
    }
    """
    system = SYSTEM_BASE + (
        " Сейчас ты разбираешь техническую ошибку. Тебе дают: "
        "URL/метод запроса, исключение Python, traceback, тело запроса, "
        "и авто-собранный контекст (затронутые сущности). Твоя задача: "
        "ответить СТРОГО валидным JSON со следующими ключами:\n"
        '  - "root_cause" (string): краткая (1-2 предложения) гипотеза '
        "что именно сломалось.\n"
        '  - "severity" (string): "low" | "medium" | "high" | "critical" — '
        "оценка влияния (critical=деньги или PD утекают, high=функционал не работает, "
        "medium=частный кейс, low=косметика).\n"
        '  - "suggested_action" (string): что админу/разработчику сделать сейчас. '
        "Конкретно: «нажми кнопку X», «выполни SQL Y», «пересобери Z».\n"
        '  - "is_known_pattern" (boolean): похоже ли на ранее виденную проблему '
        "из этого проекта (None/null если не знаешь).\n"
        '  - "confidence" (number 0-1): насколько ты уверен в анализе.\n'
        "НЕ оборачивай JSON в markdown-блок ```json — отдай чистый JSON-объект."
    )
    user = (
        "Ошибка из платформы:\n\n"
        + json.dumps(error_record, ensure_ascii=False, indent=2, default=str)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_user_summary_prompt(user_context: dict) -> list[dict]:
    """Промпт для AI-резюме жильца (L6).

    user_context = {
        "user": {id, username, room_id, residents_count, billing_mode, ...},
        "room": {...format_address...},
        "tariff": {name, charge_*-flags, normy, ...},
        "recent_readings": [список последних 12 reading'ов],
        "gsheets_rows": [последние 6 gsheets-row'ов],
        "open_tickets": [...],
        "debt_summary": {total, по периодам, ...},
        "audit": [последние действия админов],
    }
    """
    system = SYSTEM_BASE + (
        " Сейчас ты делаешь краткое резюме одного жильца — для админа, "
        "который хочет быстро понять «что с ним происходит»: подаёт ли "
        "показания, есть ли долг, конфликты, аномалии. Не более 7-10 "
        "пунктов в виде маркированного списка. Используй MARKDOWN. "
        "В конце дай раздел «🔧 Что сделать админу» с 1-3 конкретными "
        "действиями (если они нужны)."
    )
    user = (
        "Полный контекст жильца (JSON):\n\n"
        + json.dumps(user_context, ensure_ascii=False, indent=2, default=str)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_daily_briefing_prompt(metrics: dict) -> list[dict]:
    """Промпт для ежедневной сводки админу (L7).

    metrics = {
        "period": "2026-05-27",
        "new_errors": int,
        "new_errors_by_source": {...},
        "open_gsheets_conflicts": int,
        "top_anomaly_users": [...],
        "top_debtors": [...],
        "key_events": [...],   # большие подачи, переплаты, и т.п.
    }
    """
    system = SYSTEM_BASE + (
        " Сейчас ты делаешь ежедневную утреннюю сводку для админа: "
        "коротко (200-400 слов), MARKDOWN, разделы: «📊 Цифры за вчера», "
        "«⚠ На что обратить внимание», «✅ Хорошие новости» (если есть). "
        "Без эмодзи-избытка. Без воды. Если нет ничего важного — пиши "
        "одной строкой «Всё спокойно»."
    )
    user = (
        "Метрики за вчера (JSON):\n\n"
        + json.dumps(metrics, ensure_ascii=False, indent=2, default=str)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_test_prompt() -> list[dict]:
    """Минимальный пинг-промпт для тест-кнопки в UI."""
    return [
        {"role": "system", "content": "Ты лаконичный ассистент."},
        {"role": "user", "content":
            "Если ты получил это сообщение — ответь одной строкой: "
            "«OK, GigaChat на связи, текущая дата УТРОМ или ВЕЧЕРОМ?»"},
    ]


__all__ = [
    "build_error_analysis_prompt",
    "build_user_summary_prompt",
    "build_daily_briefing_prompt",
    "build_test_prompt",
]
