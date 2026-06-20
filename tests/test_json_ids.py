#!/usr/bin/env python3
"""Tests for 64-bit id JSON serialization.

Covers:
- ``stringify_int64`` / ``id_to_str`` helpers
- lossless round-trip of real Telegram int64 ids through json.dumps/loads
- ``_json_safe`` (invoke_mtproto path) stringifying out-of-range ints in
  nested TL ``to_dict()`` trees
- ``build_entity_dict`` emitting ``access_hash`` as a string while leaving
  ``id`` numeric
"""

import json
from types import SimpleNamespace

from src.tools.mtproto import _json_safe
from src.utils.entity import _ENTITY_DICT_CACHE, _ENTITY_TYPE_CACHE, build_entity_dict
from src.utils.json_ids import (
    JS_SAFE_MAX,
    id_to_str,
    stringify_int64,
)

# Real Telegram custom-emoji document_ids (and a couple of synthetic edge values)
# that all exceed 2**53 and lose precision when JSON-decoded as doubles.
REAL_INT64_IDS = [
    5366182128746793135,
    5314748207455037000,
    5411225014148014000,
    9007199254740993,  # 2**53 + 1: smallest int a double cannot represent exactly
]


class TestStringifyInt64:
    def test_large_ints_become_strings(self):
        for value in REAL_INT64_IDS:
            assert stringify_int64(value) == str(value)

    def test_js_safe_ints_pass_through(self):
        assert stringify_int64(JS_SAFE_MAX) == JS_SAFE_MAX
        assert stringify_int64(123) == 123
        assert stringify_int64(0) == 0
        assert stringify_int64(-42) == -42

    def test_large_negative_int_becomes_string(self):
        # Channel peer ids are large negatives (e.g. -100<id>).
        assert stringify_int64(-1003419925550) == -1003419925550  # within JS-safe
        big_neg = -(2**60)
        assert stringify_int64(big_neg) == str(big_neg)

    def test_bool_and_non_int_pass_through(self):
        assert stringify_int64(True) is True
        assert stringify_int64(False) is False
        assert stringify_int64("abc") == "abc"
        assert stringify_int64(None) is None
        assert stringify_int64(1.5) == 1.5


class TestIdToStr:
    def test_any_int_becomes_string(self):
        assert id_to_str(123) == "123"
        assert id_to_str(5366182128746793135) == "5366182128746793135"

    def test_none_and_str_preserved(self):
        assert id_to_str(None) is None
        assert id_to_str("already") == "already"

    def test_bool_preserved(self):
        assert id_to_str(True) is True


class TestRoundTrip:
    def test_lossless_round_trip_through_json(self):
        """str(int64) survives json.dumps/loads with every digit intact."""
        for value in REAL_INT64_IDS:
            payload = {"document_id": id_to_str(value)}
            decoded = json.loads(json.dumps(payload))
            assert decoded["document_id"] == str(value)
            assert int(decoded["document_id"]) == value

    def test_raw_int_would_lose_precision(self):
        """Guard: raw int64 round-tripped via float (JS behavior) is lossy."""
        value = 5366182128746793135
        lossy = int(float(value))
        assert lossy != value  # demonstrates why we stringify


class TestJsonSafeMtproto:
    def test_json_safe_stringifies_large_int(self):
        assert _json_safe(5366182128746793135) == "5366182128746793135"

    def test_json_safe_preserves_small_int_and_bool(self):
        assert _json_safe(16) == 16
        assert _json_safe(True) is True

    def test_json_safe_recurses_into_dict_with_document_id(self):
        tl = {
            "_": "DocumentEmpty",
            "document_id": 5314748207455037000,
            "date": 1700000000,
        }
        out = _json_safe(tl)
        assert out["document_id"] == "5314748207455037000"
        assert out["date"] == 1700000000

    def test_json_safe_handles_list_of_int64s(self):
        # JS-safe ints stay numeric; out-of-range int64 values are stringified
        values = [
            1,
            9007199254740991,   # max JS-safe integer (2**53 - 1)
            9007199254740992,   # first JS-unsafe integer (2**53)
            5314748207455037000,
        ]
        out = _json_safe(values)
        assert out[0] == 1
        assert out[1] == 9007199254740991
        assert out[2] == "9007199254740992"
        assert out[3] == "5314748207455037000"

    def test_json_safe_handles_nested_list_and_dict(self):
        # Typical invoke_mtproto-style nested list/dict response
        tl = [
            {"_": "PeerUser", "user_id": 123456789},
            5314748207455037000,
            {
                "_": "Message",
                "id": 42,
                "fwd_from": {"channel_id": 5314748207455037001},
            },
        ]
        out = _json_safe(tl)
        # JS-safe ids remain numeric
        assert out[0]["user_id"] == 123456789
        assert out[2]["id"] == 42
        # Out-of-range int64 ids are stringified
        assert out[1] == "5314748207455037000"
        assert out[2]["fwd_from"]["channel_id"] == "5314748207455037001"


class TestBuildEntityDict:
    def setup_method(self):
        _ENTITY_DICT_CACHE.clear()
        _ENTITY_TYPE_CACHE.clear()

    def test_access_hash_is_string_id_is_numeric(self):
        entity = SimpleNamespace(
            id=5314748207455037000,
            access_hash=9007199254740993,
            title="Big",
        )
        result = build_entity_dict(entity)
        # id stays numeric (preserves existing wire contract); access_hash is
        # the only entity field stringified (always far above 2**53).
        assert result["id"] == 5314748207455037000
        assert isinstance(result["id"], int)
        assert result["access_hash"] == "9007199254740993"
        assert isinstance(result["access_hash"], str)
