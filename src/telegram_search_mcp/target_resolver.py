"""Deterministic, fail-closed resolution of direct Telegram chat identities."""

from __future__ import annotations

import re
from typing import Any, Literal, Protocol

from .sanitize import sanitize_telegram_text
from .schemas import (
    ResolveTargetRequest,
    ResolvedTarget,
    TargetDiscoveryCoverage,
    TargetDiscoveryLaneCoverage,
    TargetResolutionResponse,
    _normalize_resolve_target,
)
from .tdjson import AuthorizationBlocked, MessageNotFound, SecretChatRejected, TDLibError


class TargetResolutionClient(Protocol):
    def resolve_target(self, target: str | int) -> dict[str, Any]: ...

    def get_self_chat(self) -> dict[str, Any]: ...


_USERNAME = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
_LANES = (
    "saved_messages",
    "exact_username",
    "search_chats",
    "search_chats_on_server",
    "recent_main",
    "hydration",
)
_SAVED_ALIASES = (
    "Saved Messages",
    "Избранное",
    "Сохранённые сообщения",
    "Сохраненные сообщения",
)


def _normalize(value: str) -> str:
    return _normalize_resolve_target(value)


def _compact(value: str) -> str:
    return _normalize(value).replace(" ", "")


_SAVED_NORMALIZED = frozenset(_normalize(alias) for alias in _SAVED_ALIASES)
_SAVED_COMPACT = frozenset(_compact(alias) for alias in _SAVED_ALIASES)


def _lane(status: str, detail: str) -> TargetDiscoveryLaneCoverage:
    return TargetDiscoveryLaneCoverage(status=status, detail=detail)


def _coverage(
    *,
    complete: bool,
    statuses: dict[str, tuple[str, str]] | None = None,
    detail: str,
) -> TargetDiscoveryCoverage:
    values = {name: _lane("not_requested", "lane not required") for name in _LANES}
    for name, (status, lane_detail) in (statuses or {}).items():
        values[name] = _lane(status, lane_detail)
    return TargetDiscoveryCoverage(complete=complete, detail=detail, **values)


def terminal_resolution_response(
    status: Literal["blocked", "error"],
    *,
    lane_name: str | None = None,
) -> TargetResolutionResponse:
    statuses = {}
    if lane_name is not None:
        statuses[lane_name] = (
            status,
            "authorization unavailable" if status == "blocked" else "provider operation failed",
        )
    return TargetResolutionResponse(
        status=status,
        coverage=_coverage(
            complete=False,
            statuses=statuses,
            detail=(
                "authorization unavailable"
                if status == "blocked"
                else "target resolution failed safely"
            ),
        ),
    )


def _not_found(*, lane_name: str) -> TargetResolutionResponse:
    return TargetResolutionResponse(
        status="not_found",
        coverage=_coverage(
            complete=True,
            statuses={lane_name: ("complete", "direct lookup complete")},
            detail="complete target resolution coverage",
        ),
    )


def _chat_type(chat: dict[str, Any], *, self_chat: bool = False) -> str | None:
    if self_chat:
        return "self"
    chat_type = chat.get("type")
    if not isinstance(chat_type, dict):
        return None
    type_name = chat_type.get("@type")
    if type_name == "chatTypePrivate":
        return "private"
    if type_name == "chatTypeBasicGroup":
        return "basic_group"
    if type_name == "chatTypeSupergroup":
        return "channel" if chat_type.get("is_channel") else "supergroup"
    return None


def _resolved_target(chat: dict[str, Any], *, self_chat: bool = False) -> ResolvedTarget | None:
    chat_id = chat.get("id")
    if type(chat_id) is not int or chat_id == 0:
        return None
    mapped_type = _chat_type(chat, self_chat=self_chat)
    if mapped_type is None:
        return None
    title = sanitize_telegram_text(chat.get("title"), max_length=255)
    if not title:
        return None
    return ResolvedTarget(chat_id=chat_id, title=title, chat_type=mapped_type)


def _excluded_chat(chat: dict[str, Any]) -> bool:
    chat_type = chat.get("type")
    return isinstance(chat_type, dict) and chat_type.get("@type") == "chatTypeSecret"


class TargetResolver:
    def __init__(self, client: TargetResolutionClient) -> None:
        self._client = client

    def resolve(self, request: ResolveTargetRequest) -> TargetResolutionResponse:
        target = request.target.strip()
        normalized = _normalize(target)
        if not normalized:
            raise ValueError("target must not normalize to an empty resolver query")
        compact = normalized.replace(" ", "")
        if normalized in _SAVED_NORMALIZED or compact in _SAVED_COMPACT:
            return self._resolve_saved()
        if _USERNAME.fullmatch(target):
            return self._resolve_username(target)
        return TargetResolutionResponse(
            status="discovery_required",
            coverage=_coverage(
                complete=False,
                detail="account-wide discovery required",
            ),
        )

    def _resolve_saved(self) -> TargetResolutionResponse:
        try:
            chat = self._client.get_self_chat()
        except AuthorizationBlocked:
            return terminal_resolution_response("blocked", lane_name="saved_messages")
        except (SecretChatRejected, TDLibError):
            return terminal_resolution_response("error", lane_name="saved_messages")
        resolved = _resolved_target(chat, self_chat=True)
        if resolved is None:
            return terminal_resolution_response("error", lane_name="saved_messages")
        return TargetResolutionResponse(
            status="resolved",
            resolved_target=resolved,
            match_kind="saved_messages_alias",
            coverage=_coverage(
                complete=True,
                statuses={"saved_messages": ("complete", "verified self chat lookup complete")},
                detail="complete target resolution coverage",
            ),
        )

    def _resolve_username(self, target: str) -> TargetResolutionResponse:
        try:
            chat = self._client.resolve_target(target)
        except AuthorizationBlocked:
            return terminal_resolution_response("blocked", lane_name="exact_username")
        except (MessageNotFound, SecretChatRejected):
            return _not_found(lane_name="exact_username")
        except TDLibError:
            return terminal_resolution_response("error", lane_name="exact_username")
        if _excluded_chat(chat):
            return _not_found(lane_name="exact_username")
        resolved = _resolved_target(chat)
        if resolved is None:
            return terminal_resolution_response("error", lane_name="exact_username")
        return TargetResolutionResponse(
            status="resolved",
            resolved_target=resolved,
            match_kind="exact_username",
            coverage=_coverage(
                complete=True,
                statuses={"exact_username": ("complete", "direct lookup complete")},
                detail="complete target resolution coverage",
            ),
        )
