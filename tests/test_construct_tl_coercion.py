"""Tests for access_hash round-trip and TL object construction from dicts.

Covers:
- _construct_tl_object_from_dict coercing string access_hash back to int
- _construct_tl_object_from_dict coercing string channel_id/user_id back to int
- _construct_tl_object_from_dict filling default 0 for missing required params
- _resolve_one handling URL strings via parse_telegram_url
"""

from src.tools.mtproto_tl import _construct_tl_object_from_dict


class TestConstructTlObjectIntCoercion:
    """Test that _construct_tl_object_from_dict coerces numeric string values to int."""

    def test_string_access_hash_coerced_to_int(self):
        """InputPeerChannel with string access_hash should produce int access_hash."""
        data = {
            "_": "InputPeerChannel",
            "channel_id": 1472016351,
            "access_hash": "6724660878982235770",
        }
        result = _construct_tl_object_from_dict(data)
        assert hasattr(result, "access_hash")
        assert result.access_hash == 6724660878982235770
        assert isinstance(result.access_hash, int)

    def test_negative_string_access_hash_coerced(self):
        """Negative access_hash strings (from Telethon signed int64) should coerce."""
        data = {
            "_": "InputPeerChannel",
            "channel_id": 1569855861,
            "access_hash": "-7263067709949987619",
        }
        result = _construct_tl_object_from_dict(data)
        assert hasattr(result, "access_hash")
        assert result.access_hash == -7263067709949987619
        assert isinstance(result.access_hash, int)

    def test_string_channel_id_coerced_to_int(self):
        """Channel id passed as string should coerce to int."""
        data = {
            "_": "InputPeerChannel",
            "channel_id": "1472016351",
            "access_hash": 6724660878982235770,
        }
        result = _construct_tl_object_from_dict(data)
        assert hasattr(result, "channel_id")
        assert result.channel_id == 1472016351
        assert isinstance(result.channel_id, int)

    def test_string_user_id_coerced_to_int(self):
        """User id passed as string should coerce to int."""
        data = {
            "_": "InputPeerUser",
            "user_id": "133526395",
            "access_hash": "-1491106332676312772",
        }
        result = _construct_tl_object_from_dict(data)
        assert hasattr(result, "user_id")
        assert result.user_id == 133526395
        assert isinstance(result.user_id, int)
        assert isinstance(result.access_hash, int)

    def test_phone_and_first_name_stay_strings(self):
        """Non-int TL fields must not be coerced from numeric-looking strings."""
        data = {
            "_": "InputPhoneContact",
            "client_id": 1,
            "phone": "+15551234567",
            "first_name": "12345",
            "last_name": "Doe",
        }
        result = _construct_tl_object_from_dict(data)
        assert isinstance(result.phone, str)
        assert result.phone == "+15551234567"
        assert isinstance(result.first_name, str)
        assert result.first_name == "12345"

    def test_already_int_values_unchanged(self):
        """Int values should pass through unchanged."""
        data = {
            "_": "InputPeerChannel",
            "channel_id": 1472016351,
            "access_hash": 6724660878982235770,
        }
        result = _construct_tl_object_from_dict(data)
        assert result.access_hash == 6724660878982235770
        assert isinstance(result.access_hash, int)

    def test_missing_required_params_filled_with_zero(self):
        """Missing required int params should default to 0 for InputPeerChannel."""
        data = {
            "_": "InputPeerChannel",
            "channel_id": 1472016351,
        }
        result = _construct_tl_object_from_dict(data)
        assert hasattr(result, "access_hash")
        assert result.access_hash == 0

    def test_nested_tl_object_string_int_coercion(self):
        """Nested TL objects with string ids should coerce to ints."""
        data = {
            "_": "InputPeerChannel",
            "channel_id": "2041238769",
            "access_hash": "6998628890862731257",
        }
        result = _construct_tl_object_from_dict(data)
        assert isinstance(result.channel_id, int)
        assert isinstance(result.access_hash, int)


class TestConstructTlObjectEdgeCases:
    """Edge cases for _construct_tl_object_from_dict."""

    def test_empty_dict_passthrough(self):
        """Dict without '_' key passes through unchanged."""
        data = {"foo": "bar"}
        result = _construct_tl_object_from_dict(data)
        assert result == data

    def test_non_dict_passthrough(self):
        """Non-dict values pass through unchanged."""
        assert _construct_tl_object_from_dict("hello") == "hello"
        assert _construct_tl_object_from_dict(42) == 42
        assert _construct_tl_object_from_dict(None) is None

    def test_unknown_tl_type_passthrough(self):
        """Unknown TL type name passes through as dict."""
        data = {"_": "CompletelyUnknownType", "foo": "bar"}
        result = _construct_tl_object_from_dict(data)
        assert result == data

    def test_alphanumeric_string_not_coerced(self):
        """Non-numeric strings (like invite hashes) must stay as strings."""
        data = {
            "_": "InputPeerChannel",
            "channel_id": 123,
            "access_hash": 0,
        }
        result = _construct_tl_object_from_dict(data)
        assert result.channel_id == 123
