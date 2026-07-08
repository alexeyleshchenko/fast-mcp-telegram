"""Shared envelope validation for collector payload models."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class ValidationError(Exception):
    """The payload failed schema or business-rule validation."""


FUTURE_DRIFT_SECONDS = 300  # 5 min
OLD_WINDOW_SECONDS = 7 * 24 * 3600  # 7 days


def reject_extra_keys(
    data: dict[str, Any],
    known_fields: set[str],
) -> None:
    """Raise ValidationError when ``data`` contains keys outside ``known_fields``."""
    extra = set(data) - known_fields
    if extra:
        raise ValidationError(f"Unexpected fields: {', '.join(sorted(extra))}")


def coerce_ts(value: Any) -> int:
    """Normalize payload timestamps to integer unix seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("ts must be an integer")
    return int(value)


def validate_iid(iid: Any, errors: list[str]) -> None:
    if not isinstance(iid, str) or not iid:
        errors.append("iid must be a non-empty string")
    elif len(iid) > 128:
        errors.append(f"iid exceeds 128 chars ({len(iid)})")


def validate_ts(ts: Any, errors: list[str], *, field_name: str = "ts") -> None:
    if not isinstance(ts, int) or isinstance(ts, bool):
        errors.append(f"{field_name} must be an integer")
        return
    now = int(time.time())
    if ts > now + FUTURE_DRIFT_SECONDS:
        errors.append(
            f"{ts} is {ts - now}s in the future (max {FUTURE_DRIFT_SECONDS}s)"
            if field_name == "ts"
            else f"{field_name} {ts} is {ts - now}s in the future "
            f"(max {FUTURE_DRIFT_SECONDS}s)"
        )
    if ts < now - OLD_WINDOW_SECONDS:
        errors.append(
            f"{ts} is {now - ts}s old (max {OLD_WINDOW_SECONDS}s)"
            if field_name == "ts"
            else f"{field_name} {ts} is {now - ts}s old (max {OLD_WINDOW_SECONDS}s)"
        )


def validate_ver(ver: Any, errors: list[str]) -> None:
    if not isinstance(ver, str) or not ver:
        errors.append("ver must be a non-empty string")
    elif len(ver) > 64:
        errors.append(f"ver exceeds 64 chars ({len(ver)})")


def construct_from_dict(
    cls: type,
    data: dict[str, Any],
    *,
    normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Any:
    """Construct a dataclass payload after optional normalization."""
    reject_extra_keys(data, set(cls.__dataclass_fields__))
    payload = normalizer(dict(data)) if normalizer else data
    try:
        return cls(**payload)
    except TypeError as exc:
        raise ValidationError(str(exc)) from exc
