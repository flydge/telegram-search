"""Sanitize all untrusted Telegram-originated strings."""

from __future__ import annotations

import re
import unicodedata

_SPACE_RUN = re.compile(r"\s+")
_EVIDENCE_PREFIX = "[untrusted Telegram evidence] "


def sanitize_telegram_text(value: object, *, max_length: int = 2000) -> str:
    """Return single-line text without controls, bidi formatting, or hidden payloads."""
    if max_length < 1:
        raise ValueError("max_length must be positive")
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    safe: list[str] = []
    for character in normalized:
        if character.isspace():
            safe.append(" ")
            continue
        if unicodedata.category(character).startswith("C"):
            continue
        safe.append(character)
    collapsed = _SPACE_RUN.sub(" ", "".join(safe)).strip()
    if len(collapsed) > max_length:
        return collapsed[: max_length - 1] + "…"
    return collapsed


def normalize_search_text(value: object, *, max_length: int) -> str:
    """Normalize search operands identically before case-insensitive comparison."""
    return sanitize_telegram_text(value, max_length=max_length).casefold()


def render_evidence(value: object, *, max_length: int = 2000) -> str:
    """Mark sanitized content so consumers cannot confuse it with instructions."""
    available = max_length - len(_EVIDENCE_PREFIX)
    if available < 1:
        raise ValueError("max_length is too small for the evidence marker")
    return _EVIDENCE_PREFIX + sanitize_telegram_text(value, max_length=available)
