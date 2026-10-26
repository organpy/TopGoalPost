"""Audit trail for destructive and admin actions.

The acting Discord user is taken from ``current_actor``, which the bot sets
when a command or button starts, so services do not need an actor parameter.
"""

import json
import logging
from contextvars import ContextVar
from typing import Any, Optional

from bot.db import get_db_manager, AuditLog, AuditLogRepository

logger = logging.getLogger(__name__)

current_actor: ContextVar[Optional[int]] = ContextVar("current_actor", default=None)


async def record(
    action: str,
    target_type: Optional[str] = None,
    target_id: Any = None,
    actor_id: Optional[int] = None,
    **details: Any,
) -> None:
    """Write one audit row. Never raises: auditing must not break the action."""
    try:
        async with get_db_manager().get_session() as session:
            await AuditLogRepository(session).add(
                actor_id=actor_id if actor_id is not None else current_actor.get(),
                action=action,
                target_type=target_type,
                target_id=None if target_id is None else str(target_id),
                details=json.dumps(details, default=str) if details else None,
            )
            await session.commit()
    except Exception:
        logger.exception("Could not write audit entry %s", action)


async def recent(limit: int = 20, action: Optional[str] = None) -> list[AuditLog]:
    async with get_db_manager().get_session() as session:
        return await AuditLogRepository(session).recent(limit, action)
