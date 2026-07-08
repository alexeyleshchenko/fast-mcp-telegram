"""Tests for _fill_missing_int_defaults.

Covers:
- filling missing int params with 0
- filling missing nullable params with None
- not overriding existing values
- not touching params with defaults already set
"""

from src.tools.mtproto_tl import _fill_missing_int_defaults


class TestFillMissingIntDefaults:
    def test_fills_missing_int_params_with_zero(self):
        """GetHistoryRequest missing offset_id, add_offset, hash should get 0."""
        from telethon.tl.functions.messages import GetHistoryRequest

        params = {
            "peer": "dummy",  # would be an InputPeer in real usage
            "limit": 1,
            "max_id": 0,
            "min_id": 0,
        }
        _fill_missing_int_defaults(GetHistoryRequest, params)
        assert params["offset_id"] == 0
        assert params["add_offset"] == 0
        assert params["hash"] == 0

    def test_fills_missing_nullable_with_none(self):
        """offset_date (datetime | None) should get None when missing."""
        from telethon.tl.functions.messages import GetHistoryRequest

        params = {
            "peer": "dummy",
            "limit": 1,
            "max_id": 0,
            "min_id": 0,
        }
        _fill_missing_int_defaults(GetHistoryRequest, params)
        assert params["offset_date"] is None

    def test_does_not_override_existing_values(self):
        """Existing params should not be overridden."""
        from telethon.tl.functions.messages import GetHistoryRequest

        params = {
            "peer": "dummy",
            "offset_id": 42,
            "offset_date": "something",
            "add_offset": 10,
            "limit": 50,
            "max_id": 0,
            "min_id": 0,
            "hash": 999,
        }
        _fill_missing_int_defaults(GetHistoryRequest, params)
        assert params["offset_id"] == 42
        assert params["offset_date"] == "something"
        assert params["add_offset"] == 10
        assert params["hash"] == 999

    def test_all_params_present_no_changes(self):
        """When all params are present, nothing should change."""
        from telethon.tl.functions.messages import GetHistoryRequest

        params = {
            "peer": "dummy",
            "offset_id": 0,
            "offset_date": None,
            "add_offset": 0,
            "limit": 50,
            "max_id": 0,
            "min_id": 0,
            "hash": 0,
        }
        original = dict(params)
        _fill_missing_int_defaults(GetHistoryRequest, params)
        assert params == original
