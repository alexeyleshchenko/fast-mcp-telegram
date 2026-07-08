"""Tests for HTTP attachment URL helpers in message_format."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.server_config import set_config
from src.utils import message_format as mf
from src.utils.message_format.attachments import (
    _maybe_set_attachment_download_url,
    _message_supports_streaming_attachment,
)

_ATTACH = "src.utils.message_format.attachments"
_TRANSCRIBE = "src.utils.message_format.transcription"

FIXED_TICKET = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class DocumentAttributeAudio:
    def __init__(self, voice: bool = False):
        self.voice = voice


class DocumentAttributeVideo:
    def __init__(self, round_message: bool = False):
        self.round_message = round_message


class MessageMediaDocument:
    def __init__(self, document):
        self.document = document


class MessageMediaPhoto:
    pass


class MessageMediaVoice:
    def __init__(self, document):
        self.document = document


class DummyDocument:
    def __init__(self, attributes):
        self.attributes = attributes


def _message_with_document(attrs: list) -> MagicMock:
    m = MagicMock()
    m.id = 111
    m.media = MessageMediaDocument(DummyDocument(attrs))
    return m


def _message_photo() -> MagicMock:
    m = MagicMock()
    m.id = 222
    m.media = MessageMediaPhoto()
    return m


def _message_voice() -> MagicMock:
    m = MagicMock()
    m.id = 333
    doc = DummyDocument([DocumentAttributeAudio(voice=True)])
    m.media = MessageMediaVoice(doc)
    return m


def test_message_supports_streaming_document_and_photo():
    plain = _message_with_document([])
    assert _message_supports_streaming_attachment(plain) is True
    assert _message_supports_streaming_attachment(_message_photo()) is True


def test_message_supports_streaming_accepts_voice_and_round_video():
    voice = _message_with_document([DocumentAttributeAudio(voice=True)])
    assert _message_supports_streaming_attachment(voice) is True
    rnd = _message_with_document([DocumentAttributeVideo(round_message=True)])
    assert _message_supports_streaming_attachment(rnd) is True


def test_message_supports_streaming_accepts_message_media_voice():
    msg = _message_voice()
    assert _message_supports_streaming_attachment(msg) is True


def test_message_supports_streaming_no_media():
    m = MagicMock()
    m.media = None
    assert _message_supports_streaming_attachment(m) is False


@pytest.mark.asyncio
async def test_maybe_no_url_when_stdio(stdio_config):
    stdio_config.domain = "files.example.test"
    set_config(stdio_config)
    media: dict = {"filename": "a.txt", "mime_type": "text/plain"}
    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        await _maybe_set_attachment_download_url(
            media, _message_with_document([]), -100
        )
    assert "attachment_download_url" not in media
    mint_m.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_no_url_when_placeholder_domain(http_no_auth_config):
    http_no_auth_config.domain = "your-domain.com"
    set_config(http_no_auth_config)
    media: dict = {"filename": "a.txt", "mime_type": "text/plain"}
    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        await _maybe_set_attachment_download_url(
            media, _message_with_document([]), -100
        )
    assert "attachment_download_url" not in media
    mint_m.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_no_url_when_chat_id_none(http_no_auth_config):
    http_no_auth_config.domain = "files.example.test"
    set_config(http_no_auth_config)
    media: dict = {"filename": "a.txt", "mime_type": "text/plain"}
    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        await _maybe_set_attachment_download_url(
            media, _message_with_document([]), None
        )
    assert "attachment_download_url" not in media
    mint_m.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_no_url_when_chat_id_empty_or_whitespace(http_no_auth_config):
    http_no_auth_config.domain = "files.example.test"
    set_config(http_no_auth_config)
    msg = _message_with_document([])
    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        for chat_id in ("", "   ", "\t"):
            media: dict = {"filename": "a.txt", "mime_type": "text/plain"}
            await _maybe_set_attachment_download_url(media, msg, chat_id)
            assert "attachment_download_url" not in media
    mint_m.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_no_url_when_chat_id_not_int_convertible(http_no_auth_config):
    http_no_auth_config.domain = "files.example.test"
    set_config(http_no_auth_config)
    media: dict = {"filename": "a.txt", "mime_type": "text/plain"}
    msg = _message_with_document([])
    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        await _maybe_set_attachment_download_url(media, msg, "not-a-chat-id")
    assert "attachment_download_url" not in media
    mint_m.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sets_url_for_voice(http_no_auth_config):
    http_no_auth_config.domain = "files.example.test"
    http_no_auth_config.session_name = "fallback-session"
    set_config(http_no_auth_config)
    msg = _message_with_document([DocumentAttributeAudio(voice=True)])
    media: dict = {"mime_type": "audio/ogg"}

    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        mint_m.return_value = FIXED_TICKET
        with patch(f"{_ATTACH}.get_request_token", return_value="req-token-xyz"):
            await _maybe_set_attachment_download_url(media, msg, -50)

    assert "attachment_download_url" in media
    mint_m.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_sets_url_for_round_video(http_no_auth_config):
    http_no_auth_config.domain = "files.example.test"
    http_no_auth_config.session_name = "fallback-session"
    set_config(http_no_auth_config)
    msg = _message_with_document([DocumentAttributeVideo(round_message=True)])
    media: dict = {"mime_type": "video/mp4"}

    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        mint_m.return_value = FIXED_TICKET
        with patch(f"{_ATTACH}.get_request_token", return_value="req-token-xyz"):
            await _maybe_set_attachment_download_url(media, msg, -51)

    assert "attachment_download_url" in media
    mint_m.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_sets_url_and_mint_args_with_request_token(http_no_auth_config):
    http_no_auth_config.domain = "files.example.test"
    http_no_auth_config.session_name = "fallback-session"
    set_config(http_no_auth_config)
    msg = _message_with_document([])
    media: dict = {"filename": "report.pdf", "mime_type": "application/pdf"}

    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        mint_m.return_value = FIXED_TICKET
        with patch(f"{_ATTACH}.get_request_token", return_value="req-token-xyz"):
            await _maybe_set_attachment_download_url(media, msg, -999)

    assert (
        media["attachment_download_url"]
        == f"https://files.example.test/v1/attachments/{FIXED_TICKET}/report.pdf"
    )
    mint_m.assert_awaited_once_with(
        "req-token-xyz",
        -999,
        msg.id,
        filename="report.pdf",
        mime_type="application/pdf",
    )


@pytest.mark.asyncio
async def test_maybe_falls_back_to_session_name_when_no_request_token(
    http_no_auth_config,
):
    http_no_auth_config.domain = "files.example.test"
    http_no_auth_config.session_name = "only-session"
    set_config(http_no_auth_config)
    msg = _message_photo()
    media: dict = {"filename": "p.jpg", "mime_type": "image/jpeg"}

    with patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m:
        mint_m.return_value = FIXED_TICKET
        with patch(f"{_ATTACH}.get_request_token", return_value=None):
            await _maybe_set_attachment_download_url(media, msg, 321)

    assert (
        media["attachment_download_url"]
        == f"https://files.example.test/v1/attachments/{FIXED_TICKET}/p.jpg"
    )
    mint_m.assert_awaited_once_with(
        "only-session",
        321,
        msg.id,
        filename="p.jpg",
        mime_type="image/jpeg",
    )


@pytest.mark.asyncio
async def test_build_message_result_sets_attachment_download_url(http_no_auth_config):
    """Regression: build_message_result must use attachments helper (not a shadowed copy)."""
    from datetime import datetime
    from types import SimpleNamespace

    from src.utils.message_format import build_message_result

    http_no_auth_config.domain = "files.example.test"
    http_no_auth_config.session_name = "fallback-session"
    set_config(http_no_auth_config)

    msg = SimpleNamespace(
        id=222,
        text="photo caption",
        message=None,
        caption=None,
        date=datetime.now(),
        media=MessageMediaPhoto(),
        reply_to_msg_id=None,
        reply_to=None,
        forward=None,
        action=None,
        reply_markup=None,
        sender_id=None,
    )
    entity = SimpleNamespace(
        id=-100,
        title="Test Chat",
        username="testchat",
        first_name=None,
        last_name=None,
        forum=False,
        access_hash=None,
        min=None,
    )

    with (
        patch(
            "src.utils.message_format.core.get_sender_info",
            new=AsyncMock(return_value={"id": 1, "name": "Sender"}),
        ),
        patch(
            "src.utils.message_format.core._extract_forward_info",
            new=AsyncMock(return_value=None),
        ),
        patch(f"{_ATTACH}.mint_attachment_ticket", new_callable=AsyncMock) as mint_m,
        patch(f"{_ATTACH}.get_request_token", return_value="req-token-xyz"),
    ):
        mint_m.return_value = FIXED_TICKET
        result = await build_message_result(msg, entity, link=None)

    assert "media" in result
    assert (
        result["media"]["attachment_download_url"]
        == f"https://files.example.test/v1/attachments/{FIXED_TICKET}/photo_222.jpg"
    )
    mint_m.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcribe_selects_voice_and_round_video():
    """transcribe_voice_messages selects both voice and round_video messages."""
    transcribed_ids: list[int] = []

    async def _fake_transcribe(_client, _chat_entity, msg_id):
        transcribed_ids.append(msg_id)

    messages = [
        {"media": {"type": "voice"}, "id": 1},
        {"media": {"type": "round_video"}, "id": 2},
        {"media": {"type": "voice"}, "transcription": "already done", "id": 3},
        {"media": {"type": "photo"}, "id": 4},
        {"no_media_key": True},
    ]
    chat_entity = MagicMock()

    with (
        patch(f"{_TRANSCRIBE}._is_user_premium", return_value=True),
        patch(
            f"{_TRANSCRIBE}._transcribe_single_voice_message",
            side_effect=_fake_transcribe,
        ),
    ):
        await mf.transcribe_voice_messages(messages, chat_entity, client=MagicMock())

    assert transcribed_ids == [1, 2], (
        f"Expected voice(1) and round_video(2) to be transcribed, got {transcribed_ids}"
    )
