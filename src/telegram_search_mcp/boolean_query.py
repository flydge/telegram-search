"""Explicit local Boolean predicates; no provider query language or recall claim."""
from __future__ import annotations

import unicodedata
from typing import Any


def normalize_search_text(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def boolean_matches(query: Any, text: str) -> bool:
    value = normalize_search_text(text)
    return (all(normalize_search_text(term) in value for term in query.all)
            and (not query.any or any(normalize_search_text(term) in value for term in query.any))
            and not any(normalize_search_text(term) in value for term in query.none))


def boolean_seeds(query: Any) -> list[str]:
    return list(query.any) if query.any else list(query.all[:1])
