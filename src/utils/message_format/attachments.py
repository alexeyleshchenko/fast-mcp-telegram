"""Attachment download URLs and response warnings."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from src.client.connection import get_request_token
from src.config.server_config import cfg
from src.server_components.attachment_tickets import mint_attachment_ticket

logger = logging.getLogger(__name__)


def _message_supports_streaming_attachment(message) -> bool:
    """Whether attachment HTTP streaming is supported for this message (documents, photos, voice)."""
    media = getattr(message, "media", None)
    if not media:
        return False
    media_cls = media.__class__.__name__
    if media_cls == "MessageMediaPhoto":
        return True
    if media_cls == "MessageMediaDocument":
        return getattr(media, "document", None) is not None
    return media_cls == "MessageMediaVoice"


async def _maybe_set_attachment_download_url(
    media_dict: dict[str, Any],
    message,
    chat_id: int | None,
) -> None:
    """Set media['attachment_download_url'] when HTTP mode and DOMAIN resolves to a public origin."""
    if chat_id is None:
        return
    if isinstance(chat_id, str) and not chat_id.strip():
        return
    config = cfg()
    if config.transport != "http" or not config.public_base_url_normalized:
        return
    if not _message_supports_streaming_attachment(message):
        return

    session_token = get_request_token()
    if session_token is None:
        session_token = config.session_name

    filename = media_dict.get("filename")
    mime_type = media_dict.get("mime_type")
    try:
        cid = int(chat_id)
        mid = int(message.id)
    except (TypeError, ValueError) as _conv_err:
        logger.warning(
            "Skipping attachment URL: invalid chat_id=%r or message.id=%r (%s)",
            chat_id,
            getattr(message, "id", None),
            _conv_err,
        )
        return
    tid = await mint_attachment_ticket(
        session_token,
        cid,
        mid,
        filename=filename if isinstance(filename, str) else None,
        mime_type=mime_type if isinstance(mime_type, str) else None,
    )
    base = config.public_base_url_normalized
    url = f"{base}/v1/attachments/{tid}"
    if tid_filename := media_dict.get("filename"):
        url = f"{url}/{quote(tid_filename, safe='')}"
    else:
        msg_id = getattr(message, "id", "unknown")
        url = f"{url}/photo_{msg_id}.jpg"
    media_dict["attachment_download_url"] = url


def response_attachment_warning(messages: list[dict]) -> str | None:
    """Return a warning string if DOMAIN is missing and any message has media.

    One warning per entire response, not per message. Returns None when
    there is no problem (valid domain, stdio transport, or no media messages).
    """
    if not messages:
        return None
    config = cfg()
    if config.transport != "http" or config.public_base_url_normalized:
        return None
    has_media = any(bool(m.get("media")) for m in messages)
    if not has_media:
        return None
    return (
        f"⚠️ DOMAIN is '{config.domain}' — attachment_download_url DISABLED for media messages. "
        "Set DOMAIN=<your-public-host> in .env to enable download links."
    )
