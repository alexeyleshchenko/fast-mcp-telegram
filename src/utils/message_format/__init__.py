"""Format Telethon messages for MCP tool responses."""

from __future__ import annotations

from .attachments import response_attachment_warning
from .core import (
    build_message_result,
    build_send_edit_result,
    extract_topic_metadata,
    get_sender_info,
    message_has_displayable_content,
)
from .transcription import PremiumRequiredError, transcribe_voice_messages

__all__ = [
    "PremiumRequiredError",
    "build_message_result",
    "build_send_edit_result",
    "extract_topic_metadata",
    "get_sender_info",
    "message_has_displayable_content",
    "response_attachment_warning",
    "transcribe_voice_messages",
]
