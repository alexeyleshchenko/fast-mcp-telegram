"""In-memory QR login session manager for Telethon qr_login() flow.

Single-process only. Generates QR codes via Telethon's `client.qr_login()`,
polls for scan completion, and provides the authorized client on success.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.account import GetPasswordRequest

logger = logging.getLogger(__name__)


class QrLoginError(Exception):
    """Base exception for QR login failures."""


@dataclass
class SessionState:
    """State of a single pending QR login session."""

    telethon_client: Any
    qr_url: str
    created_at: float = field(default_factory=time.time)
    resulting_client: Any = None  # Set when QR is scanned successfully
    password_hint: str = ""  # 2FA hint when account has two-step verification
    _poll_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _qr_login_obj: Any = field(
        default=None, repr=False
    )  # Telethon QRLogin object, set by manager
    _status: str = field(default="pending", repr=False)

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_completed(self) -> bool:
        return self._status == "completed"

    def mark_expired(self) -> None:
        self._status = "expired"

    def mark_completed(self, client: Any) -> None:
        self.resulting_client = client
        self._status = "completed"

    def mark_2fa_required(self, hint: str = "") -> None:
        """Mark the session as requiring a 2FA password after QR scan."""
        self._status = "2fa_required"
        self.password_hint = hint


class QrLoginManager:
    """Manages in-memory QR login sessions (single process).

    Each session uses Telethon's ``client.qr_login()`` to generate a QR code.
    The ``wait()`` method on the QR login object blocks until:
    - The user scans the QR from Telegram mobile → returns authorized Telethon client
    - The QR expires (default ~60s) → raises TimeoutError

    Polling ``poll_status()`` drives the internal ``wait()`` in a background task.
    """

    def __init__(
        self,
        timeout_seconds: int = 60,
        on_complete: Callable[[str, Any], None] | None = None,
    ) -> None:
        """Initialize the QR login manager.

        Args:
            timeout_seconds: How long to wait for QR scan before expiring.
            on_complete: Optional callback(session_id, client) when QR is scanned.
        """
        self._timeout = timeout_seconds
        self._on_complete = on_complete
        self._sessions: dict[str, SessionState] = {}

    async def create_session(self, telethon_client: Any) -> tuple[str, str]:
        """Create a new QR login session.

        Args:
            telethon_client: A connected Telethon client to generate the QR.

        Returns:
            Tuple of (session_id, qr_url). The QR URL is a ``tg://login?token=...``
            link that the user scans from Telegram mobile, or shows as a QR code.

        Raises:
            QrLoginError: If Telethon fails to create a QR login or returns no URL.
        """
        # Telethon's qr_login() is a coroutine — must await
        try:
            qr_login = await telethon_client.qr_login()
        except Exception as exc:
            raise QrLoginError("Failed to create Telegram QR login session") from exc

        qr_url = str(getattr(qr_login, "url", "")).strip()
        if not qr_url:
            raise QrLoginError("Telethon QR login did not return a valid QR URL")

        session_id = uuid.uuid4().hex[:16]
        state = SessionState(telethon_client, qr_url)
        # Store the qr_login object for use in the background wait task
        state._qr_login_obj = qr_login
        self._sessions[session_id] = state

        logger.debug(
            "QR session %s created, timeout=%ss, url=%s...",
            session_id,
            self._timeout,
            qr_url[:40],
        )
        return session_id, qr_url

    async def poll_status(self, session_id: str) -> str:
        """Poll the status of a QR login session.

        On first poll, kicks off the background ``wait()`` task.
        Subsequent polls return the cached status.

        Returns:
            One of: ``"pending"``, ``"completed"``, ``"expired"``, ``"2fa_required"``,
            ``"not_found"``.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return "not_found"

        if state.is_completed:
            return "completed"

        if state.status == "expired":
            return "expired"

        # Start the background wait task if not already started
        if state._poll_task is None:
            state._poll_task = asyncio.create_task(
                self._wait_for_login(session_id, state)
            )

        # If the task is done, we have a result
        if state._poll_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                exc = state._poll_task.exception()
                if exc is not None:
                    logger.warning(
                        "QR session %s background task failed: %s", session_id, exc
                    )
                    state.mark_expired()
        return state.status

    async def _wait_for_login(self, session_id: str, state: SessionState) -> None:
        """Background task that waits for QR login completion or timeout."""
        qr_login = getattr(state, "_qr_login_obj", None)
        if qr_login is None:
            state.mark_expired()
            return

        try:
            # Telethon's QRLogin.wait() — blocks until scan or timeout
            connected_client = await asyncio.wait_for(
                qr_login.wait(), timeout=self._timeout
            )
            state.mark_completed(connected_client)
            logger.info("QR session %s completed — user scanned the QR", session_id)

            # Disconnect the temporary Telethon client (no longer needed)
            with contextlib.suppress(Exception):
                await state.telethon_client.disconnect()

            if self._on_complete:
                try:
                    self._on_complete(session_id, connected_client)
                except Exception as exc:
                    logger.warning("on_complete callback failed: %s", exc)

        except TimeoutError:
            logger.info(
                "QR session %s expired (timeout=%ss)", session_id, self._timeout
            )
            state.mark_expired()
            with contextlib.suppress(Exception):
                await state.telethon_client.disconnect()

        except SessionPasswordNeededError:
            # Account has two-step verification enabled — get the password hint
            hint = ""
            with contextlib.suppress(Exception):
                pw = await state.telethon_client(GetPasswordRequest())
                hint = (pw.hint or "").strip()
            state.mark_2fa_required(hint=hint)
            logger.info(
                "QR session %s requires 2FA password%s",
                session_id,
                f" (hint: {hint})" if hint else "",
            )
            # Don't disconnect telethon_client — it's needed for sign_in(password=)

        except Exception as exc:
            logger.warning("QR session %s failed: %s", session_id, exc)
            state.mark_expired()
            with contextlib.suppress(Exception):
                await state.telethon_client.disconnect()

    def get_client(self, session_id: str) -> Any:
        """Get the authorized Telethon client after successful QR scan.

        Returns None if the session doesn't exist or hasn't completed yet.
        """
        state = self._sessions.get(session_id)
        if state is None or not state.is_completed:
            return None
        return state.resulting_client

    def get_password_hint(self, session_id: str) -> str:
        """Get the 2FA password hint for a QR session (if any).

        Returns an empty string if the session doesn't exist or has no hint.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return ""
        return getattr(state, "password_hint", "")

    async def regenerate_qr(self, session_id: str, telethon_client: Any) -> str | None:
        """Generate a new QR code for an existing session (e.g., after timeout).

        Args:
            session_id: The existing session ID.
            telethon_client: A connected Telethon client.

        Returns:
            The new QR URL, or None if the session doesn't exist.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return None

        # Cancel any existing poll task
        if state._poll_task is not None and not state._poll_task.done():
            state._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await state._poll_task

        # Disconnect old client if it was for QR polling
        with contextlib.suppress(Exception):
            await state.telethon_client.disconnect()

        # Create new QR login
        try:
            qr_login = await telethon_client.qr_login()
        except Exception as exc:
            logger.warning("QR session %s regenerate failed: %s", session_id, exc)
            return None

        new_url = str(getattr(qr_login, "url", "")).strip()
        if not new_url:
            logger.warning(
                "QR session %s regenerate: invalid URL from Telethon", session_id
            )
            return None
        state.qr_url = new_url
        state.telethon_client = telethon_client
        state._qr_login_obj = qr_login
        state._poll_task = None
        state._status = "pending"
        state.created_at = time.time()

        logger.debug("QR session %s regenerated", session_id)
        return new_url

    def cleanup_expired(self) -> int:
        """Remove expired and completed sessions and return the count of removed entries.

        Expired sessions (exceeded timeout) have already been marked as expired
        by their background poll tasks. Completed sessions (scanned) older than 2x
        timeout are also purged. This method removes them from memory.
        """
        expired_ids: list[str] = []
        now = time.time()
        for sid, state in list(self._sessions.items()):
            if state.status == "expired":
                expired_ids.append(sid)
            elif state.is_completed and now - state.created_at > self._timeout * 2:
                # Completed sessions older than 2x timeout
                expired_ids.append(sid)
            elif (
                state.status == "2fa_required"
                and now - state.created_at > self._timeout * 2
            ):
                # 2FA-required sessions older than 2x timeout
                expired_ids.append(sid)
            elif state.age > self._timeout * 2 and state.status == "pending":
                # Safety net: sessions stuck in "pending" beyond 2x timeout
                state.mark_expired()
                expired_ids.append(sid)

        for sid in expired_ids:
            self._sessions.pop(sid, None)

        if expired_ids:
            logger.debug(
                "Cleanup removed %d expired/completed QR session(s)", len(expired_ids)
            )

        return len(expired_ids)

    @property
    def active_session_count(self) -> int:
        """Number of active (non-expired) QR sessions."""
        return sum(s.status != "expired" for s in self._sessions.values())
