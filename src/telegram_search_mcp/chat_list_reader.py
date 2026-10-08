"""Frozen bounded getChats observations with current local-state metadata only."""
from __future__ import annotations

import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .contract import CONTRACT_VERSION, fingerprint
from .sanitize import sanitize_telegram_text
from .schemas import (ChatListCoverage, ChatListResult, ChatListScope, ForumTopicName,
                      ListChatsRequest, ListChatsResponse)
from .tdjson import AuthorizationBlocked, MessageNotFound, SecretChatRejected, TDLibDeadlineExceeded, TDLibError


@dataclass
class _Scan:
    binding: tuple
    scope: ChatListScope
    expires: float
    account: int | None = None
    observations: list[tuple[int, str, int]] = field(default_factory=list)
    coverage: list[ChatListCoverage] = field(default_factory=list)
    emitted: set[int] = field(default_factory=set)
    offset: int = 0
    snapshotted: bool = False


def terminal_chat_listing(status: str, reason: str) -> ListChatsResponse:
    return ListChatsResponse(status=status, stop_reason=reason)


def _int(value: object, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid native integer")
    return value


def _id(value: object) -> int:
    result = _int(value, minimum=-(2**53 - 1), maximum=2**53 - 1)
    if result == 0:
        raise ValueError("invalid native identity")
    return result


def _prefix(raw: object, limit: int) -> list[int]:
    if not isinstance(raw, dict) or raw.get("@type") != "chats":
        raise ValueError("invalid native prefix")
    _int(raw.get("total_count"))
    values = raw.get("chat_ids")
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError("invalid native prefix cardinality")
    ids = [_id(value) for value in values]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate native observation")
    return ids


class _Omit(ValueError):
    def __init__(self, issue: str):
        self.issue = issue


def _project(raw: object, cid: int, lane: str, rank: int, scope: ChatListScope,
             hydrated_at: datetime) -> ChatListResult:
    if not isinstance(raw, dict) or raw.get("@type") != "chat":
        raise _Omit("invalid_metadata")
    if type(raw.get("id")) is not int or raw["id"] != cid:
        raise _Omit("identity_mismatch")
    native_type = raw.get("type")
    if not isinstance(native_type, dict):
        raise _Omit("invalid_metadata")
    constructor = native_type.get("@type")
    if constructor == "chatTypeSecret":
        raise _Omit("secret")
    if constructor == "chatTypePrivate":
        _int(native_type.get("user_id"), minimum=1, maximum=2**53-1)
        kind = "private"
    elif constructor == "chatTypeBasicGroup":
        _int(native_type.get("basic_group_id"), minimum=1, maximum=2**53-1)
        kind = "basic_group"
    elif constructor == "chatTypeSupergroup":
        _int(native_type.get("supergroup_id"), minimum=1, maximum=2**53-1)
        if type(native_type.get("is_channel")) is not bool:
            raise _Omit("invalid_metadata")
        kind = "channel" if native_type["is_channel"] else "supergroup"
    else:
        raise _Omit("invalid_metadata")
    memberships = raw.get("chat_lists")
    # Product work caps, not promises about TDLib's native metadata limits.
    if not isinstance(memberships, list) or len(memberships) > 128:
        raise _Omit("invalid_metadata")
    names = set()
    for item in memberships:
        if not isinstance(item, dict):
            raise _Omit("invalid_metadata")
        constructor = item.get("@type")
        if constructor == "chatListMain":
            names.add("main")
        elif constructor == "chatListArchive":
            names.add("archive")
        elif constructor == "chatListFolder":
            _int(item.get("chat_folder_id"), minimum=1)
        else:
            raise _Omit("invalid_metadata")
    current = [name for name in scope.selected_lists if name in names]
    if not current:
        raise _Omit("out_of_scope")
    counts = {key: _int(raw.get(key)) for key in ("unread_count", "unread_mention_count", "unread_reaction_count")}
    marked = raw.get("is_marked_as_unread")
    title = raw.get("title")
    if type(marked) is not bool or type(title) is not str or len(title) > 4096:
        raise _Omit("invalid_metadata")
    # Normalize independently of display truncation so both flags remain meaningful.
    normalized_input = unicodedata.normalize("NFKC", title)
    normalized = sanitize_telegram_text(normalized_input, max_length=max(1, len(normalized_input)))
    shown = sanitize_telegram_text(normalized, max_length=256)
    return ChatListResult(chat_id=cid, kind=kind,
        title=ForumTopicName(value=shown, sanitized=normalized != title, truncated=len(normalized) > 256),
        observed_list=lane, observed_rank=rank, current_lists=current, hydrated_at=hydrated_at,
        is_marked_as_unread=marked, **counts)


class ChatListReader:
    def __init__(self, *, client: Any, client_id: str, broker_generation: str,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._client = client
        self._identity = (client_id, broker_generation, CONTRACT_VERSION)
        self._clock, self._now = clock, now
        self._lock = threading.Lock()
        self._scans: dict[str, _Scan] = {}
        self._active = 0
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._scans.clear()

    def _claim(self, request: ListChatsRequest) -> _Scan | ListChatsResponse:
        binding = self._identity + (fingerprint(request.model_dump(mode="json", exclude={"cursor"})),)
        with self._lock:
            tick = self._clock()
            self._scans = {key: scan for key, scan in self._scans.items() if scan.expires > tick}
            if self._closed:
                return terminal_chat_listing("invalid_cursor", "invalid_cursor")
            if request.cursor:
                scan = self._scans.get(request.cursor)
                if scan is None or scan.binding != binding:
                    return terminal_chat_listing("invalid_cursor", "invalid_cursor")
                del self._scans[request.cursor]
            else:
                if len(self._scans) + self._active >= 4:
                    return terminal_chat_listing("capacity_exhausted", "capacity_exhausted")
                selected = ["main", "archive"] if request.scope == "both" else [request.scope]
                scan = _Scan(binding=binding, expires=tick + 300,
                    scope=ChatListScope(selection=request.scope, selected_lists=selected, limit=request.limit,
                        prefix_limit_per_list=100 if request.scope == "both" else 200,
                        expires_at=self._now().astimezone(timezone.utc) + timedelta(seconds=300)),
                    coverage=[ChatListCoverage(chat_list=name) for name in selected])
            self._active += 1
            return scan

    def read(self, request: ListChatsRequest, *, deadline: float | None = None) -> ListChatsResponse:
        claimed = self._claim(request)
        if isinstance(claimed, ListChatsResponse):
            return claimed
        scan, response = claimed, None
        deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
        try:
            response = self._read_page(scan, deadline)
            return response
        finally:
            with self._lock:
                self._active -= 1
                if response is not None and response.scope is not None:
                    if self._closed or self._clock() >= scan.expires:
                        response.status, response.stop_reason, response.next_cursor = "partial", "invalid_cursor", None
                        response.page_complete = False
                    elif response.next_cursor:
                        self._scans[response.next_cursor] = scan

    def _read_page(self, scan: _Scan, deadline: float) -> ListChatsResponse:
        results: list[ChatListResult] = []
        processed = omitted = 0
        status, reason = "prefix_exhausted_unverified", "prefix_exhausted_unverified"
        current: ChatListCoverage | None = None
        native_pending = False

        def check() -> None:
            with self._lock:
                closed = self._closed
            if closed or time.monotonic() >= deadline or self._clock() >= scan.expires:
                raise TDLibDeadlineExceeded("chat listing deadline")

        def omit(issue: str) -> None:
            nonlocal omitted
            current.omitted += 1
            setattr(current.issues, issue, getattr(current.issues, issue) + 1)
            omitted += 1

        try:
            with self._client.request_budget(deadline):
                check(); self._client.ensure_ready(); check()
                account = _int(self._client.get_account_id(), minimum=1, maximum=2**53-1)
                check()
                if scan.account is not None and scan.account != account:
                    return terminal_chat_listing("invalid_cursor", "invalid_cursor")
                scan.account = account
                if not scan.snapshotted:
                    for current in scan.coverage:
                        check()
                        try:
                            raw = self._client.get_chat_list_snapshot(current.chat_list,
                                limit=scan.scope.prefix_limit_per_list)
                            check()
                            ids = _prefix(raw, scan.scope.prefix_limit_per_list)
                        except (ValueError, TypeError, OverflowError):
                            current.native_state = "invalid"
                            raise
                        except (TDLibError, TimeoutError, OSError):
                            current.native_state = "unavailable"
                            raise
                        current.native_state = "observed"
                        current.observed = current.pending = len(ids)
                        scan.observations.extend((cid, current.chat_list, rank) for rank, cid in enumerate(ids, 1))
                        del raw, ids
                    scan.snapshotted = True
                while scan.offset < len(scan.observations) and processed < scan.scope.limit:
                    check()
                    cid, lane, rank = scan.observations[scan.offset]
                    current = scan.coverage[scan.scope.selected_lists.index(lane)]
                    # First native occurrence owns hydration, even if it was omitted.
                    duplicate = any(other[0] == cid for other in scan.observations[:scan.offset])
                    scan.offset += 1; current.processed += 1; current.pending -= 1; processed += 1
                    if duplicate:
                        omit("duplicate_observation")
                        continue
                    native_pending = True
                    try:
                        raw = self._client.get_chat_metadata(cid)
                        check()
                        row = _project(raw, cid, lane, rank, scan.scope, self._now().astimezone(timezone.utc))
                        del raw
                        check()
                        if cid in scan.emitted:
                            omit("duplicate_observation")
                        else:
                            results.append(row); scan.emitted.add(cid); current.returned += 1
                        native_pending = False
                    except _Omit as error:
                        omit(error.issue); native_pending = False
                    except SecretChatRejected:
                        omit("secret"); native_pending = False
                    except MessageNotFound:
                        check(); omit("unavailable"); native_pending = False
                    except (ValueError, TypeError, OverflowError):
                        omit("invalid_metadata"); native_pending = False
                if scan.offset < len(scan.observations):
                    status, reason = "page", "page_limit"
        except AuthorizationBlocked:
            status, reason = "blocked", "authorization_unavailable"
            if native_pending:omit("authorization_unavailable")
        except TDLibDeadlineExceeded:
            status, reason = "partial", "call_budget_exhausted"
            if native_pending:omit("deadline")
        except (TDLibError, TimeoutError, OSError):
            status, reason = "error", "provider_error"
            if native_pending:omit("provider_error")
        except (ValueError, TypeError, OverflowError):
            status, reason = "error", "invalid_provider_prefix"
        if self._clock() >= scan.expires:
            status, reason = "partial", "invalid_cursor"
        counts = {name + "_candidates": sum(getattr(c, name) for c in scan.coverage)
                  for name in ("observed", "processed", "returned", "omitted", "pending")}
        return ListChatsResponse(status=status, stop_reason=reason, scope=scan.scope.model_copy(deep=True),
            results=results, list_coverage=[c.model_copy(deep=True) for c in scan.coverage], **counts,
            processed_this_page=processed, returned_this_page=len(results), omitted_this_page=omitted,
            snapshot_complete=scan.snapshotted,
            page_complete=bool(processed) and omitted == 0 and status in {"page", "prefix_exhausted_unverified"},
            next_cursor="chats_" + secrets.token_hex(32) if status == "page" else None)
