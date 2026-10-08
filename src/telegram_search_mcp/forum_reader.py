"""Bounded forum inventory observations; continuation state contains no topic text."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .contract import CONTRACT_VERSION, fingerprint
from .message_reader import _integer, _text
from .schemas import (
    ForumTopicMetadata, ForumTopicName, ForumTopicReference, ForumTopicResult,
    ListTopicsRequest, ListTopicsResponse, TopicScope,
)
from .tdjson import AuthorizationBlocked, ForumUnsupported, SecretChatRejected, TDLibDeadlineExceeded, TDLibError


@dataclass
class _Scan:
    binding: tuple
    scope: TopicScope
    expires: float
    account_id: int | None = None
    after: tuple[int, int, int] = (0, 0, 0)
    offsets: set[tuple[int, int, int]] = field(default_factory=set)
    seen: set[int] = field(default_factory=set)
    pending: list[int] = field(default_factory=list)
    page_stop: tuple[str, str] | None = None
    scanned: int = 0
    pages: int = 0


def terminal_topics(status: str, reason: str) -> ListTopicsResponse:
    return ListTopicsResponse(status=status, stop_reason=reason)


def _identity(raw: object, chat_id: int) -> int:
    if not isinstance(raw, dict) or raw.get("@type") != "forumTopic":
        raise ValueError("invalid forum topic")
    info = raw.get("info")
    if (not isinstance(info, dict) or info.get("@type") != "forumTopicInfo" or
            type(info.get("chat_id")) is not int or info["chat_id"] != chat_id):
        raise ValueError("invalid forum topic chat")
    return _integer(info.get("forum_topic_id"), minimum=1, maximum=2**31 - 1)


def _unavailable(chat_id: int, topic_id: int, issue: str, status: str = "error") -> ForumTopicResult:
    return ForumTopicResult(chat_id=chat_id, topic=ForumTopicReference(id=topic_id), status=status, issues=[issue])


def _project(raw: object, chat_id: int, topic_id: int) -> ForumTopicResult:
    if raw is None:
        return _unavailable(chat_id, topic_id, "topic_unavailable", "not_found")
    if _identity(raw, chat_id) != topic_id:
        raise ValueError("invalid hydrated topic identity")
    info = raw["info"]
    for key in ("is_general", "is_closed", "is_hidden"):
        if type(info.get(key)) is not bool:
            raise ValueError("invalid forum metadata flag")
    if not isinstance(info.get("name"), str):
        raise ValueError("invalid forum name")
    stamp = _integer(info.get("creation_date"), maximum=2**31 - 1)
    text = _text(info["name"], 256)
    name = ForumTopicName(value=text.value, sanitized=text.sanitized, truncated=text.truncated)
    issues = []
    if name.truncated:
        issues.append("name_truncated")
    if not name.value:
        issues.append("name_unavailable")
    if not stamp:
        issues.append("creation_date_unavailable")
    metadata = ForumTopicMetadata(name=name,
        creation_date_utc=datetime.fromtimestamp(stamp, timezone.utc) if stamp else None,
        is_general=info["is_general"], is_closed=info["is_closed"], is_hidden=info["is_hidden"])
    return ForumTopicResult(chat_id=chat_id, topic=ForumTopicReference(id=topic_id),
        status="partial" if issues else "complete", metadata=metadata, coverage_complete=not issues, issues=issues)


class ForumTopicReader:
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

    def _claim(self, request: ListTopicsRequest) -> _Scan | ListTopicsResponse:
        binding = self._identity + (fingerprint(request.model_dump(mode="json", exclude={"cursor"})),)
        with self._lock:
            now = self._clock()
            self._scans = {key: scan for key, scan in self._scans.items() if scan.expires > now}
            if self._closed:
                return terminal_topics("invalid_cursor", "invalid_cursor")
            if request.cursor is not None:
                scan = self._scans.get(request.cursor)
                if scan is None or scan.binding != binding:
                    return terminal_topics("invalid_cursor", "invalid_cursor")
                del self._scans[request.cursor]
            else:
                if len(self._scans) + self._active >= 4:
                    return terminal_topics("capacity_exhausted", "capacity_exhausted")
                wall = self._now().astimezone(timezone.utc)
                scan = _Scan(binding=binding, expires=now + 300,
                    scope=TopicScope(target=request.target, limit=request.limit, expires_at=wall + timedelta(seconds=300)))
            self._active += 1
            return scan

    def read(self, request: ListTopicsRequest, *, deadline: float | None = None) -> ListTopicsResponse:
        claimed = self._claim(request)
        if isinstance(claimed, ListTopicsResponse):
            return claimed
        scan, response = claimed, None
        deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
        try:
            response = self._read_page(scan, deadline)
            return response
        finally:
            with self._lock:
                self._active -= 1
                if response is not None and response.next_cursor is not None:
                    if self._closed or self._clock() >= scan.expires:
                        response.status, response.stop_reason, response.next_cursor = "partial", "invalid_cursor", None
                    else:
                        self._scans[response.next_cursor] = scan

    def _read_page(self, scan: _Scan, deadline: float) -> ListTopicsResponse:
        results: list[ForumTopicResult] = []
        status, reason, continuing = "page", "page_limit", True

        def check_deadline() -> None:
            if time.monotonic() >= deadline:
                raise TDLibDeadlineExceeded("forum listing deadline")

        try:
            with self._client.request_budget(deadline):
                check_deadline()
                self._client.ensure_ready()
                check_deadline()
                account = _integer(self._client.get_account_id(), minimum=1)
                check_deadline()
                if scan.account_id is not None and scan.account_id != account:
                    return terminal_topics("invalid_cursor", "invalid_cursor")
                scan.account_id = account
                chat = self._client.resolve_forum_chat(scan.scope.target)
                check_deadline()
                if (not isinstance(chat, dict) or chat.get("@type") != "chat" or
                        type(chat.get("id")) is not int or chat["id"] != scan.scope.target):
                    raise ValueError("invalid forum chat")
                if not scan.pending:
                    if scan.pages >= 10 or scan.scanned >= 200:
                        status, reason, continuing = "limit_reached", "scope_budget_exhausted", False
                    else:
                        scan.pages += 1
                        raw = self._client.get_forum_topics(scan.scope.target, offset_date=scan.after[0],
                            offset_message_id=scan.after[1], offset_forum_topic_id=scan.after[2], limit=scan.scope.limit)
                        check_deadline()
                        if not isinstance(raw, dict) or raw.get("@type") != "forumTopics":
                            raise ValueError("invalid forum page")
                        rows = raw.get("topics")
                        # TDLib can over-return its requested limit. This is our local
                        # safety bound, not a provider cardinality guarantee.
                        if not isinstance(rows, list) or len(rows) > 200:
                            raise ValueError("invalid forum page length")
                        if len(rows) > 200 - scan.scanned:
                            # Never truncate a native page then skip its unseen tail.
                            status, reason, continuing = "limit_reached", "scope_budget_exhausted", False
                        else:
                            after = (_integer(raw.get("next_offset_date"), maximum=2**31 - 1),
                                     _integer(raw.get("next_offset_message_id")),
                                     _integer(raw.get("next_offset_forum_topic_id"), maximum=2**31 - 1))
                            # Validate the whole page before hydration; retain numeric IDs only.
                            observed = [_identity(row, scan.scope.target) for row in rows]
                            scan.scanned += len(observed)
                            scan.pending = list(dict.fromkeys(tid for tid in observed if tid not in scan.seen))
                            scan.seen.update(scan.pending)
                            if not scan.pending or after == scan.after or after in scan.offsets:
                                scan.page_stop = ("partial", "provider_nonprogress" if scan.scanned else "provider_end_unverified")
                            elif after == (0, 0, 0):
                                scan.page_stop = ("partial", "provider_end_unverified")
                            elif scan.pages >= 10 or scan.scanned >= 200:
                                scan.page_stop = ("limit_reached", "scope_budget_exhausted")
                            else:
                                scan.page_stop = None
                                scan.offsets.add(scan.after)
                                scan.after = after
                            del observed
                        del raw, rows
                candidates = scan.pending[:scan.scope.limit]
                del scan.pending[:scan.scope.limit]
                interrupted = None
                for topic_id in candidates:
                    if interrupted is not None:
                        results.append(_unavailable(scan.scope.target, topic_id, interrupted))
                        continue
                    try:
                        check_deadline()
                        hydrated = self._client.get_forum_topic(scan.scope.target, topic_id)
                        check_deadline()
                        results.append(_project(hydrated, scan.scope.target, topic_id))
                        del hydrated
                    except (ValueError, TypeError, OverflowError):
                        results.append(_unavailable(scan.scope.target, topic_id, "invalid_provider_topic"))
                    except AuthorizationBlocked:
                        interrupted = "authorization_unavailable"
                        status, reason, continuing = "blocked", interrupted, False
                        results.append(_unavailable(scan.scope.target, topic_id, interrupted))
                    except TDLibDeadlineExceeded:
                        interrupted = "budget_exhausted"
                        status, reason, continuing = "partial", "call_budget_exhausted", False
                        results.append(_unavailable(scan.scope.target, topic_id, interrupted))
                    except (TDLibError, TimeoutError, OSError):
                        interrupted = "provider_error"
                        status, reason, continuing = "error", interrupted, False
                        results.append(_unavailable(scan.scope.target, topic_id, interrupted))
                if continuing and not scan.pending and scan.page_stop is not None:
                    status, reason = scan.page_stop
                    continuing = False
        except AuthorizationBlocked:
            status, reason, continuing = "blocked", "authorization_unavailable", False
        except SecretChatRejected:
            status, reason, continuing = "unsupported", "secret_chat", False
        except ForumUnsupported:
            status, reason, continuing = "unsupported", "unsupported_forum", False
        except TDLibDeadlineExceeded:
            status, reason, continuing = "partial", "call_budget_exhausted", False
        except (TDLibError, TimeoutError, OSError):
            status, reason, continuing = "error", "provider_error", False
        except (ValueError, TypeError, OverflowError):
            status, reason, continuing = "error", "invalid_provider_page", False
        if self._clock() >= scan.expires:
            status, reason, continuing = "partial", "invalid_cursor", False
        return ListTopicsResponse(status=status, scope=scan.scope.model_copy(deep=True), results=results,
            page_complete=bool(results) and all(r.coverage_complete for r in results),
            next_cursor="topic_" + secrets.token_hex(32) if continuing else None,
            stop_reason=reason, scanned_candidates=scan.scanned, provider_pages=scan.pages)
