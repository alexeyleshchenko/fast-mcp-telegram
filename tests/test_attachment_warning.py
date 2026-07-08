"""Tests for response_attachment_warning() in message_format."""

import logging

from src.config.server_config import set_config
from src.utils import message_format as mf


class TestResponseAttachmentWarning:
    """Tests for response_attachment_warning(): per-response warning when DOMAIN is placeholder."""

    def test_warning_returns_none_when_stdio(self, stdio_config):
        """No warning in stdio mode even with placeholder domain and media."""
        stdio_config.domain = "your-domain.com"
        set_config(stdio_config)
        result = mf.response_attachment_warning([{"id": 1, "media": {"type": "photo"}}])
        assert result is None

    def test_warning_returns_none_when_valid_domain(self, http_no_auth_config):
        """No warning when domain resolves to a valid public URL."""
        http_no_auth_config.domain = "tg-mcp.example.com"
        set_config(http_no_auth_config)
        result = mf.response_attachment_warning([{"id": 1, "media": {"type": "photo"}}])
        assert result is None

    def test_warning_returns_none_when_no_media(self, http_no_auth_config):
        """No warning when messages have no media at all."""
        http_no_auth_config.domain = "your-domain.com"
        set_config(http_no_auth_config)
        result = mf.response_attachment_warning(
            [{"id": 1, "text": "hello"}, {"id": 2, "text": "world"}]
        )
        assert result is None

    def test_warning_returns_none_when_empty_list(self, http_no_auth_config):
        """No warning for an empty message list."""
        http_no_auth_config.domain = "your-domain.com"
        set_config(http_no_auth_config)
        result = mf.response_attachment_warning([])
        assert result is None

    def test_warning_returns_string_when_placeholder_with_media(
        self, http_no_auth_config
    ):
        """Warning string returned when domain is placeholder and messages include media."""
        http_no_auth_config.domain = "your-domain.com"
        set_config(http_no_auth_config)
        result = mf.response_attachment_warning(
            [
                {"id": 1, "text": "hello"},
                {
                    "id": 2,
                    "media": {"filename": "test.pdf", "mime_type": "application/pdf"},
                },
            ]
        )
        assert isinstance(result, str)
        assert "DOMAIN" in result
        assert "your-domain.com" in result
        assert "attachment_download_url" in result

    def test_warning_returns_string_when_localhost_placeholder(
        self, http_no_auth_config
    ):
        """localhost is now a placeholder — warning should fire."""
        http_no_auth_config.domain = "localhost"
        set_config(http_no_auth_config)
        result = mf.response_attachment_warning([{"id": 1, "media": {"type": "photo"}}])
        assert isinstance(result, str)
        assert "localhost" in result

    def test_warning_returns_none_when_public_ip(self, http_no_auth_config):
        """Raw IP addresses like 144.31.188.163 are NOT placeholders — no warning."""
        http_no_auth_config.domain = "144.31.188.163"
        set_config(http_no_auth_config)
        result = mf.response_attachment_warning([{"id": 1, "media": {"type": "photo"}}])
        assert result is None

    def test_warning_returns_none_when_multiple_messages_no_media(
        self, http_no_auth_config
    ):
        """Even with placeholder domain, no warning when no message has media."""
        http_no_auth_config.domain = "your-domain.com"
        set_config(http_no_auth_config)
        result = mf.response_attachment_warning(
            [
                {"id": 1, "text": "a", "error": "not found"},
                {"id": 2, "text": "b"},
            ]
        )
        assert result is None

    def test_warning_is_per_response_not_per_message(self, http_no_auth_config):
        """One warning string for the whole list, not one per message."""
        http_no_auth_config.domain = "your-domain.com"
        set_config(http_no_auth_config)
        messages = [
            {"id": 1, "media": {"type": "photo"}},
            {"id": 2, "media": {"filename": "x.pdf"}},
            {"id": 3, "text": "plain"},
        ]
        result = mf.response_attachment_warning(messages)
        assert isinstance(result, str)
        assert len(result) > 10  # verify it's a real message, not empty


class TestValidateConfigDomainWarning:
    """Tests that validate_config logs a warning when DOMAIN is a placeholder."""

    def test_validate_config_logs_warning_when_placeholder(
        self, http_auth_config, caplog
    ):
        """validate_config should log a warning when domain is your-domain.com."""
        http_auth_config.domain = "your-domain.com"
        with caplog.at_level(logging.WARNING):
            # sourcery: skip
            if hasattr(http_auth_config, "_config_logged"):
                del http_auth_config._config_logged
            http_auth_config.validate_config()
        assert any(
            "DOMAIN is 'your-domain.com'" in record.message for record in caplog.records
        )

    def test_validate_config_no_warning_when_valid_domain(
        self, http_auth_config, caplog
    ):
        """validate_config should NOT log a domain warning when domain is valid."""
        http_auth_config.domain = "tg-mcp.example.com"
        with caplog.at_level(logging.WARNING):
            # sourcery: skip
            if hasattr(http_auth_config, "_config_logged"):
                del http_auth_config._config_logged
            http_auth_config.validate_config()
        assert all("DOMAIN is" not in r.message for r in caplog.records)

    def test_validate_config_no_warning_when_stdio(self, stdio_config, caplog):
        """No domain warning in stdio mode (transport is not http)."""
        stdio_config.domain = "your-domain.com"
        with caplog.at_level(logging.WARNING):
            # sourcery: skip
            if hasattr(stdio_config, "_config_logged"):
                del stdio_config._config_logged
            stdio_config.validate_config()
        assert all("DOMAIN is" not in r.message for r in caplog.records)
