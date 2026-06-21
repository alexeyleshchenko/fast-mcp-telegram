"""Tests for build_send_edit_result handling of None sender/chat entities.

Bug: When message.sender is None (e.g. anonymous messages, service messages),
build_entity_dict(None) returns None, and build_send_edit_result puts that None
directly into the result dict. FastMCP validates against SendEditResult schema
where chat and sender are dict[str, Any] — None fails "type: object" validation.

Fix: build_send_edit_result should omit None-valued fields from the result dict.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from src.utils.message_format import build_send_edit_result


class TestBuildSendEditResultNoneHandling:
    """Verify build_send_edit_result never returns None for chat/sender fields."""

    def test_none_sender_is_omitted_from_result(self):
        """When message.sender is None, 'sender' must not be in the result dict."""
        message = SimpleNamespace(
            id=42,
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text="hello",
            sender=None,
            edit_date=None,
            reply_markup=None,
        )
        chat = SimpleNamespace(
            id=100,
            title="Test Chat",
            username="testchat",
            first_name=None,
            last_name=None,
            access_hash=None,
            megagroup=False,
            bot=False,
            forum=False,
        )

        result = build_send_edit_result(message, chat, "sent")

        # The critical assertion: sender must NOT be None
        assert result.get("sender") is not None or "sender" not in result, (
            f"sender should be omitted or non-None, got: {result.get('sender')!r}"
        )

    def test_none_chat_is_omitted_from_result(self):
        """When chat entity is None, 'chat' must not be in the result dict."""
        message = SimpleNamespace(
            id=42,
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text="hello",
            sender=SimpleNamespace(
                id=1,
                first_name="User",
                last_name=None,
                username="user",
                access_hash=None,
                bot=False,
                megagroup=False,
                forum=False,
            ),
            edit_date=None,
            reply_markup=None,
        )

        result = build_send_edit_result(message, None, "sent")

        assert result.get("chat") is not None or "chat" not in result, (
            f"chat should be omitted or non-None, got: {result.get('chat')!r}"
        )

    def test_both_none_sender_and_chat(self):
        """When both sender and chat are None, neither should be in the result."""
        message = SimpleNamespace(
            id=42,
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text="hello",
            sender=None,
            edit_date=None,
            reply_markup=None,
        )

        result = build_send_edit_result(message, None, "sent")

        assert result.get("sender") is not None or "sender" not in result, (
            f"sender should be omitted or non-None, got: {result.get('sender')!r}"
        )
        assert result.get("chat") is not None or "chat" not in result, (
            f"chat should be omitted or non-None, got: {result.get('chat')!r}"
        )
        # Core fields must still be present
        assert result["message_id"] == 42
        assert result["text"] == "hello"
        assert result["status"] == "sent"

    def test_valid_sender_and_chat_are_preserved(self):
        """Normal case: valid sender and chat dicts must be preserved."""
        message = SimpleNamespace(
            id=42,
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text="hello",
            sender=SimpleNamespace(
                id=1,
                first_name="User",
                last_name=None,
                username="user",
                access_hash=None,
                bot=False,
                megagroup=False,
                forum=False,
            ),
            edit_date=None,
            reply_markup=None,
        )
        chat = SimpleNamespace(
            id=100,
            title="Test Chat",
            username="testchat",
            first_name=None,
            last_name=None,
            access_hash=None,
            megagroup=False,
            bot=False,
            forum=False,
        )

        result = build_send_edit_result(message, chat, "sent")

        assert isinstance(result["sender"], dict)
        assert result["sender"]["id"] == 1
        assert isinstance(result["chat"], dict)
        assert result["chat"]["id"] == 100

    def test_no_none_values_in_result_dict(self):
        """Comprehensive: no field in the result dict should be None.

        FastMCP validates each field against its TypedDict schema. None values
        for object-typed fields (chat, sender, reply_markup) fail validation.
        """
        message = SimpleNamespace(
            id=42,
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text="hello",
            sender=None,
            edit_date=None,
            reply_markup=None,
        )

        result = build_send_edit_result(message, None, "sent")

        none_fields = [k for k, v in result.items() if v is None]
        assert none_fields == [], (
            f"These fields are None and will fail FastMCP validation: {none_fields}"
        )
