"""In-memory attachment download tickets (UUID → session + message locator)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from src.config.server_config import cfg


@dataclass(frozen=True)
class AttachmentTicket:
    """Server-side record for streaming one message's media without Bearer on GET."""

    session_token: str
    chat_id: int
    message_id: int
    expires_at: float
    filename: str | None
    mime_type: str | None
    rich_kind: str | None = None
    rich_media_id: int | None = None


_tickets: dict[str, AttachmentTicket] = {}
_lock = asyncio.Lock()
_minted_ticket_ids: ContextVar[list[str] | None] = ContextVar(
    "_minted_ticket_ids", default=None
)


def _prune_expired_unlocked() -> None:
    now = time.time()
    dead = [k for k, v in _tickets.items() if v.expires_at <= now]
    for k in dead:
        del _tickets[k]


@contextmanager
def track_minted_attachment_tickets():
    """Collect ticket IDs minted during this block (for ACL post-filter cleanup)."""
    ids: list[str] = []
    token = _minted_ticket_ids.set(ids)
    try:
        yield ids
    finally:
        _minted_ticket_ids.reset(token)


async def revoke_attachment_tickets(ticket_ids: Iterable[str]) -> int:
    """Remove tickets by ID. Returns count removed."""
    ids = [tid for tid in ticket_ids if tid]
    if not ids:
        return 0
    async with _lock:
        removed = 0
        for tid in ids:
            if tid in _tickets:
                del _tickets[tid]
                removed += 1
        return removed


async def mint_attachment_ticket(
    session_token: str,
    chat_id: int,
    message_id: int,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    rich_kind: str | None = None,
    rich_media_id: int | None = None,
) -> str:
    """Create a ticket; returns UUID string. Multi-use until expiry."""
    config = cfg()
    tid = str(uuid.uuid4())
    expires_at = time.time() + float(config.attachment_ticket_ttl_seconds)
    rec = AttachmentTicket(
        session_token=session_token,
        chat_id=chat_id,
        message_id=message_id,
        expires_at=expires_at,
        filename=filename,
        mime_type=mime_type,
        rich_kind=rich_kind,
        rich_media_id=rich_media_id,
    )
    async with _lock:
        _prune_expired_unlocked()
        _tickets[tid] = rec
    tracking = _minted_ticket_ids.get()
    if tracking is not None:
        tracking.append(tid)
    return tid


async def get_attachment_ticket(ticket_id: str) -> AttachmentTicket | None:
    """Return ticket if present and not expired."""
    async with _lock:
        _prune_expired_unlocked()
        rec = _tickets.get(ticket_id)
        if rec is None:
            return None
        if rec.expires_at <= time.time():
            del _tickets[ticket_id]
            return None
        return rec


async def clear_attachment_tickets_for_tests() -> None:
    """Reset store (tests only)."""
    async with _lock:
        _tickets.clear()
