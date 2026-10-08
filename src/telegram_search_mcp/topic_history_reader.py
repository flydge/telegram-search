"""Bounded exact forum history; cursors retain numeric observations, never bodies."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .contract import CONTRACT_VERSION, fingerprint
from .forum_reader import _project
from .message_reader import _integer, _message
from .schemas import (MessageReadResult, ReadTopicHistoryRequest, ReadTopicHistoryResponse,
                      SelectedMessageAnchor, TopicHistoryScope)
from .tdjson import (AuthorizationBlocked, ForumUnsupported, MessageNotFound, SecretChatRejected,
                     TDLibDeadlineExceeded, TDLibError)


@dataclass
class _Scan:
    binding: tuple
    scope: TopicHistoryScope
    expires: float
    account_id: int | None = None
    after: int = 0
    pending: list[tuple[int, int]] = field(default_factory=list)
    page_advanced: bool = False
    scanned: int = 0
    processed: int = 0
    pages: int = 0


class _TopicMismatch(ValueError):
    pass


class _InvalidTopic(ValueError):
    pass


def terminal_topic_history(status: str, reason: str) -> ReadTopicHistoryResponse:
    return ReadTopicHistoryResponse(status=status, stop_reason=reason)


def _row(raw: object, scope: TopicHistoryScope) -> tuple[int, int]:
    if (not isinstance(raw, dict) or raw.get("@type") != "message" or
            type(raw.get("chat_id")) is not int or raw["chat_id"] != scope.target):
        raise ValueError("invalid topic history identity")
    mid = _integer(raw.get("id"), minimum=1)
    stamp = _integer(raw.get("date"), maximum=2**31 - 1)
    topic = raw.get("topic_id")
    if (not isinstance(topic, dict) or topic.get("@type") != "messageTopicForum" or
            type(topic.get("forum_topic_id")) is not int or topic["forum_topic_id"] != scope.topic.id):
        raise _TopicMismatch("message is outside exact forum topic")
    return mid, stamp


class TopicHistoryReader:
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

    def _claim(self, request: ReadTopicHistoryRequest) -> _Scan | ReadTopicHistoryResponse:
        binding = self._identity + (fingerprint(request.model_dump(mode="json", exclude={"cursor"})),)
        with self._lock:
            now = self._clock()
            self._scans = {key: scan for key, scan in self._scans.items() if scan.expires > now}
            if self._closed:
                return terminal_topic_history("invalid_cursor", "invalid_cursor")
            if request.cursor is not None:
                scan = self._scans.get(request.cursor)
                if scan is None or scan.binding != binding:
                    return terminal_topic_history("invalid_cursor", "invalid_cursor")
                del self._scans[request.cursor]
            else:
                if len(self._scans) + self._active >= 4:
                    return terminal_topic_history("capacity_exhausted", "capacity_exhausted")
                wall = self._now().astimezone(timezone.utc)
                scan = _Scan(binding=binding, expires=now + 300,
                    scope=TopicHistoryScope(target=request.target, topic=request.topic.model_copy(deep=True),
                        mode=request.mode, date_from=request.date_from, date_to=request.date_to or wall,
                        upper_message_id=None, page_limit=request.limit, expires_at=wall + timedelta(seconds=300)))
            self._active += 1
            return scan

    def read(self, request: ReadTopicHistoryRequest, *, deadline: float | None = None) -> ReadTopicHistoryResponse:
        claimed = self._claim(request)
        if isinstance(claimed, ReadTopicHistoryResponse):
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

    def _read_page(self, scan: _Scan, deadline: float) -> ReadTopicHistoryResponse:
        results: list[MessageReadResult] = []
        topic = None
        text_budget, processed = 100000, 0
        status, reason, continuing = "page", "page_limit", True

        def check_deadline() -> None:
            if time.monotonic() >= deadline:
                raise TDLibDeadlineExceeded("topic history deadline")

        try:
            with self._client.request_budget(deadline):
                check_deadline()
                self._client.ensure_ready()
                check_deadline()
                account = _integer(self._client.get_account_id(), minimum=1)
                check_deadline()
                if scan.account_id is not None and scan.account_id != account:
                    return terminal_topic_history("invalid_cursor", "invalid_cursor")
                scan.account_id = account
                chat = self._client.resolve_forum_chat(scan.scope.target)
                check_deadline()
                if (not isinstance(chat, dict) or chat.get("@type") != "chat" or
                        type(chat.get("id")) is not int or chat["id"] != scan.scope.target):
                    raise ValueError("invalid forum chat")
                if isinstance(chat.get("type"), dict) and chat["type"].get("@type") == "chatTypeSecret":
                    raise SecretChatRejected("secret chat")
                raw_topic = self._client.get_forum_topic(scan.scope.target, scan.scope.topic.id)
                check_deadline()
                try:
                    topic = _project(raw_topic, scan.scope.target, scan.scope.topic.id)
                except (ValueError, TypeError, OverflowError):
                    raise _InvalidTopic("invalid current topic") from None
                del raw_topic
                if topic.status == "not_found":
                    status, reason, continuing = "partial", "topic_unavailable", False
                while continuing and len(results) < scan.scope.page_limit:
                    check_deadline()
                    if not scan.pending:
                        if scan.pages and not scan.page_advanced:
                            status, reason, continuing = "partial", "provider_nonprogress", False
                            break
                        if scan.pages >= 10 or scan.scanned >= 200:
                            status, reason, continuing = "limit_reached", "scope_budget_exhausted", False
                            break
                    if processed >= 100 or text_budget <= 0:
                        reason = "call_budget_exhausted"
                        break
                    if not scan.pending:
                        scan.pages += 1
                        raw = self._client.get_forum_topic_history(scan.scope.target, scan.scope.topic.id,
                            from_message_id=scan.after, limit=min(20, 200 - scan.scanned))
                        check_deadline()
                        if not isinstance(raw, dict) or raw.get("@type") != "messages":
                            raise ValueError("invalid history page")
                        _integer(raw.get("total_count"), minimum=-1, maximum=2**31 - 1)
                        rows = raw.get("messages")
                        if not isinstance(rows, list) or len(rows) > 200:
                            raise ValueError("invalid history page size")
                        if len(rows) > 200 - scan.scanned:
                            status, reason, continuing = "limit_reached", "scope_budget_exhausted", False
                            break
                        # No hydration until every observed row has exact modern membership.
                        pending = [_row(row, scan.scope) for row in rows]
                        if any(a[0] <= b[0] for a, b in zip(pending, pending[1:])):
                            raise ValueError("invalid descending history order")
                        del raw, rows
                        if not pending:
                            status, reason, continuing = "partial", "provider_end_unverified", False
                            break
                        if scan.scope.upper_message_id is None:
                            scan.scope.upper_message_id = pending[0][0]
                        scan.scanned += len(pending)
                        scan.pending, scan.page_advanced = pending, False
                    mid, observed_date = scan.pending[0]
                    if mid <= scan.scope.upper_message_id and (not scan.after or mid < scan.after):
                        anchor = SelectedMessageAnchor(chat_id=scan.scope.target, message_id=mid)
                        try:
                            raw = self._client.get_message(scan.scope.target, mid)
                            check_deadline()
                            hydrated_id, stamp = _row(raw, scan.scope)
                            if hydrated_id != mid:
                                raise ValueError("invalid hydrated message identity")
                            if self._in_window(scan.scope, stamp):
                                result = _message(self._client, raw, anchor, text_budget)
                                check_deadline()
                                results.append(result)
                                if result.message and result.message.text:
                                    text_budget -= len(result.message.text.value)
                            del raw
                        except MessageNotFound:
                            check_deadline()
                            if self._in_window(scan.scope, observed_date):
                                results.append(MessageReadResult(anchor=anchor, status="not_found",
                                    coverage_complete=False, issues=["message_unavailable"]))
                        scan.after, scan.page_advanced = mid, True
                    scan.pending.pop(0)
                    scan.processed += 1
                    processed += 1
                if continuing and not scan.pending:
                    if not scan.page_advanced:
                        status, reason, continuing = "partial", "provider_nonprogress", False
                    elif scan.pages >= 10 or scan.scanned >= 200:
                        status, reason, continuing = "limit_reached", "scope_budget_exhausted", False
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
        except _InvalidTopic:
            status, reason, continuing = "error", "invalid_provider_topic", False
        except _TopicMismatch:
            status, reason, continuing = "error", "topic_mismatch", False
        except (ValueError, TypeError, OverflowError):
            status, reason, continuing = "error", "invalid_provider_page", False
        if self._clock() >= scan.expires:
            status, reason, continuing = "partial", "invalid_cursor", False
        if continuing and processed == 0:
            status, reason, continuing = "partial", "call_budget_exhausted", False
        return ReadTopicHistoryResponse(status=status, scope=scan.scope.model_copy(deep=True), topic=topic,
            results=results, page_complete=bool(results) and all(r.coverage_complete for r in results),
            next_cursor="topic_history_" + secrets.token_hex(32) if continuing else None, stop_reason=reason,
            scanned_candidates=scan.scanned, processed_candidates=scan.processed, provider_pages=scan.pages)

    @staticmethod
    def _in_window(scope: TopicHistoryScope, stamp: int) -> bool:
        if stamp == 0:
            return False
        value = datetime.fromtimestamp(stamp, timezone.utc)
        return value < scope.date_to and (scope.date_from is None or value >= scope.date_from)
