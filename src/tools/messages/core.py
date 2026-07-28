"""Core message formatting utilities."""

from __future__ import annotations

import re
from typing import Literal

# Phase 1 frozen whitelist for parse_mode="rich" HTML dialect detection.
# Source: Telegram Bot API HTML / rich formatting tags agents commonly use.
_RICH_HTML_TAGS = frozenset(
    {
        "a",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "blockquote",
        "tg-spoiler",
        "tg-emoji",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "br",
        "hr",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "details",
        "summary",
    }
)

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_HTML_TAG_RE = re.compile(r"</?\s*([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*>")


def _normalize_parse_mode(parse_mode: str | None) -> str | None:
    """Return parse_mode lowercased if not None, otherwise None. Ensures case-insensitive handling."""
    return parse_mode.lower() if parse_mode is not None else None


def detect_message_formatting(message: str) -> str | None:
    """
    Detect message formatting based on content patterns.

    Returns:
        "html" if HTML tags are detected
        "markdown" if Markdown syntax is detected
        None if no formatting is detected
    """
    if not message or not isinstance(message, str):
        return None

    html_pattern = r"<[^>]+>"
    if re.search(html_pattern, message):
        return "html"

    markdown_patterns = [
        r"```.+?```",
        r"`[^`]+`",
        r"\*\*[^[*].*?\*\*",
        r"\*[^**].*?\*",
        r"_[^_]*?_",
        r"\[.*?\]\(.*?\)",
        r"^#{1,6}\s",
        r"^\d+\.\s",
        r"^\*\s",
        r"^\-\s",
    ]

    return next(
        (
            "markdown"
            for pattern in markdown_patterns
            if re.search(pattern, message, re.MULTILINE)
        ),
        None,
    )


def _strip_code_spans_for_rich_detect(text: str) -> str:
    """Remove fenced and inline code so HTML-in-docs does not flip dialect."""
    without_fences = _FENCED_CODE_RE.sub(" ", text)
    return _INLINE_CODE_RE.sub(" ", without_fences)


def detect_rich_dialect(text: str) -> Literal["html", "markdown"]:
    """Choose InputRichMessage HTML vs markdown for parse_mode='rich'.

    Strips fenced/inline code, then looks for known rich HTML tags only.
    Inequalities and tags mentioned only inside code stay markdown.
    """
    if not text or not isinstance(text, str):
        return "markdown"
    remainder = _strip_code_spans_for_rich_detect(text)
    for match in _HTML_TAG_RE.finditer(remainder):
        if match.group(1).lower() in _RICH_HTML_TAGS:
            return "html"
    return "markdown"
