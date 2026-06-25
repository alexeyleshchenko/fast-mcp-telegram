"""Tests for entity resolution: -100 prefix stripping and get_entity_by_id."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.utils.entity import _strip_channel_prefix


class TestStripChannelPrefix:
    """Test _strip_channel_prefix helper for -100 channel prefix stripping."""

    def test_string_with_prefix(self):
        """String '-1001234567890' → (1234567890, True)."""
        raw, was_stripped = _strip_channel_prefix("-1001234567890")
        assert raw == 1234567890
        assert was_stripped is True

    def test_string_with_prefix_small_id(self):
        """String '-10012345' → (12345, True)."""
        raw, was_stripped = _strip_channel_prefix("-10012345")
        assert raw == 12345
        assert was_stripped is True

    def test_string_without_prefix(self):
        """String '1234567890' → ('1234567890', False)."""
        raw, was_stripped = _strip_channel_prefix("1234567890")
        assert raw == "1234567890"
        assert was_stripped is False

    def test_int_with_prefix(self):
        """Int -1001234567890 → (1234567890, True)."""
        raw, was_stripped = _strip_channel_prefix(-1001234567890)
        assert raw == 1234567890
        assert was_stripped is True

    def test_int_with_prefix_small_id(self):
        """Int -10012345 → (12345, True)."""
        raw, was_stripped = _strip_channel_prefix(-10012345)
        assert raw == 12345
        assert was_stripped is True

    def test_int_without_prefix(self):
        """Int 1234567890 → (1234567890, False)."""
        raw, was_stripped = _strip_channel_prefix(1234567890)
        assert raw == 1234567890
        assert was_stripped is False

    def test_negative_int_below_threshold(self):
        """Int -50 → (-50, False) — below -100 prefix range."""
        raw, was_stripped = _strip_channel_prefix(-50)
        assert raw == -50
        assert was_stripped is False

    def test_bool_not_treated_as_int(self):
        """Bool True → (True, False) — bool subclasses int."""
        raw, was_stripped = _strip_channel_prefix(True)
        assert raw is True
        assert was_stripped is False

    def test_non_numeric_string(self):
        """String 'alice' → ('alice', False)."""
        raw, was_stripped = _strip_channel_prefix("alice")
        assert raw == "alice"
        assert was_stripped is False

    def test_string_with_prefix_non_digits(self):
        """String '-100abc' → ('-100abc', False) — not all digits after prefix."""
        raw, was_stripped = _strip_channel_prefix("-100abc")
        assert raw == "-100abc"
        assert was_stripped is False

    def test_string_exactly_minus_100(self):
        """String '-100' → ('-100', False) — too short."""
        raw, was_stripped = _strip_channel_prefix("-100")
        assert raw == "-100"
        assert was_stripped is False


@pytest.mark.asyncio
@patch("src.utils.entity.get_connected_client", new_callable=AsyncMock)
async def test_get_entity_by_id_strips_prefix(mock_get_client):
    """get_entity_by_id should try stripped channel ID for -100 prefixed inputs."""
    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(side_effect=[
        Exception("raw -1001234567890 failed"),  # raw peer fails
        MagicMock(id=1234567890),                 # PeerChannel(1234567890) succeeds
    ])
    mock_get_client.return_value = mock_client

    from src.utils.entity import get_entity_by_id

    result = await get_entity_by_id("-1001234567890")
    assert result is not None
    assert result.id == 1234567890

    # Verify the first call tried the raw peer, second tried PeerChannel(1234567890)
    calls = mock_client.get_entity.call_args_list
    assert calls[0][0][0] == -1001234567890  # raw peer (int)
    # PeerChannel(1234567890) — stripped version
    from telethon.tl.types import PeerChannel
    assert calls[1][0][0] == PeerChannel(1234567890)


@pytest.mark.asyncio
@patch("src.utils.entity.get_connected_client", new_callable=AsyncMock)
async def test_get_entity_by_id_int_with_prefix(mock_get_client):
    """get_entity_by_id should strip -100 from int inputs too."""
    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(side_effect=[
        Exception("raw -1001234567890 failed"),  # raw peer fails
        MagicMock(id=1234567890),                 # PeerChannel(1234567890) succeeds
    ])
    mock_get_client.return_value = mock_client

    from src.utils.entity import get_entity_by_id

    result = await get_entity_by_id(-1001234567890)
    assert result is not None
    assert result.id == 1234567890


@pytest.mark.asyncio
@patch("src.utils.entity.get_connected_client", new_callable=AsyncMock)
async def test_get_entity_by_id_no_prefix(mock_get_client):
    """get_entity_by_id should NOT strip -100 from normal IDs."""
    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(return_value=MagicMock(id=1234567890))
    mock_get_client.return_value = mock_client

    from src.utils.entity import get_entity_by_id

    result = await get_entity_by_id("1234567890")
    assert result is not None
    assert result.id == 1234567890

    # Should have tried raw int, PeerChannel, PeerUser, PeerChat (no stripped version)
    calls = mock_client.get_entity.call_args_list
    assert len(calls) == 1  # first call succeeded
    assert calls[0][0][0] == 1234567890
