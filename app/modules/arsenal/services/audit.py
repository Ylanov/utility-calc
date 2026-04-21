"""Служебный модуль записи в журнал действий арсенала.

По духу совпадает с `utility.admin_dashboard.write_audit_log`, но пишет в
отдельную таблицу `arsenal_audit_log` (арсенал-БД, другая Base).
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.arsenal.models import ArsenalAuditLog

logger = logging.getLogger(__name__)


async def write_arsenal_audit(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    username: str,
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Записывает действие в arsenal_audit_log. Не делает commit — он придёт
    из вызывающей транзакции, что гарантирует атомарность лога с основной
    операцией (лог не появится если документ не создался, и наоборот).

    Использование:
        await write_arsenal_audit(
            db, user_id=current_user.id, username=current_user.username,
            action="create_document", entity_type="document", entity_id=new_doc.id,
            details={"doc_number": new_doc.doc_number, "op_type": new_doc.operation_type},
        )
    """
    try:
        log = ArsenalAuditLog(
            user_id=user_id,
            username=username or "system",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or None,
            ip_address=ip_address,
        )
        db.add(log)
    except Exception as e:
        # Лог не должен блокировать основную операцию. Пишем warning и идём дальше.
        logger.warning(f"[arsenal-audit] failed to queue audit entry: {e}")


def client_ip_from_request(request) -> Optional[str]:
    """Аккуратно достаёт IP клиента с учётом обратных прокси (nginx → app).
    Формат X-Forwarded-For: 'client, proxy1, proxy2' — берём первый."""
    if request is None:
        return None
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()[:45]
        return request.client.host[:45] if request.client else None
    except Exception:
        return None
