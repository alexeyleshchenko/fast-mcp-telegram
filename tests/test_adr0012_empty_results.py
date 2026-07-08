"""Integration-style tests for ADR 0012 empty-result success + note responses."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.search.replies import _handle_reply_mode
from src.tools.search.search_mode import _handle_query_mode


@pytest.mark.asyncio
@patch("src.tools.search.search_mode._collect_messages_in_chat", new_callable=AsyncMock)
@patch("src.tools.search.search_mode.get_connected_client", new_callable=AsyncMock)
async def test_handle_query_mode_empty_search_returns_note(mock_client, mock_collect):
    """Real _handle_query_mode path: empty collection → success + note."""
    mock_collect.return_value = None
    mock_client.return_value = MagicMock()

    result = await _handle_query_mode(
        query="missing-term",
        chat_id="me",
        limit=10,
        min_date=None,
        max_date=None,
        chat_type=None,
        public=None,
        auto_expand_batches=0,
        include_total_count=False,
        params={"operation": "get_messages"},
    )

    assert result["messages"] == []
    assert result["has_more"] is False
    assert "note" in result
    assert "missing-term" in result["note"]
    assert "error" not in result
    assert result.get("ok") is not False


@pytest.mark.asyncio
@patch("src.tools.search.replies._fetch_replies", new_callable=AsyncMock)
@patch("src.tools.search.replies.get_entity_by_id", new_callable=AsyncMock)
@patch("src.tools.search.replies.get_connected_client", new_callable=AsyncMock)
async def test_handle_reply_mode_empty_returns_note(
    mock_client, mock_entity, mock_fetch
):
    """Real _handle_reply_mode path: no replies → success + note."""
    mock_client.return_value = MagicMock()
    mock_entity.return_value = MagicMock(id=-100123)
    mock_fetch.return_value = ([], None)

    result = await _handle_reply_mode(
        chat_id="-1001234567890",
        reply_to_id=42,
        limit=20,
        query=None,
        params={"operation": "get_messages"},
    )

    assert result["messages"] == []
    assert result["has_more"] is False
    assert result["reply_to_id"] == 42
    assert "note" in result
    assert "42" in result["note"]
    assert "error" not in result
    assert result.get("ok") is not False
