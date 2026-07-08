"""Tests for telemetry error trace sanitization."""

from src.telemetry import sanitize_error_trace


def test_sanitize_error_trace_omits_stack_frames():
    """Sanitized traces contain type and message only."""
    try:
        raise ValueError("operation failed")
    except ValueError as exc:
        trace = sanitize_error_trace(exc)

    assert trace == "ValueError: operation failed"
    assert "Traceback" not in trace
