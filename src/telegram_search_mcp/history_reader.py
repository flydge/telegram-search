"""Bounded, single-use history continuations. Retain IDs/dates, never message bodies."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .contract import CONTRACT_VERSION, fingerprint
from .message_reader import _integer, _message
from .schemas import HistoryScope, MessageReadResult, ReadHistoryRequest, ReadHistoryResponse, SelectedMessageAnchor
from .tdjson import AuthorizationBlocked, MessageNotFound, SecretChatRejected, TDLibDeadlineExceeded, TDLibError


@dataclass
class _Scan:
    binding: tuple
    scope: HistoryScope
    expires: float
    account_id: int | None = None
    after: int = 0
    scanned: int = 0
    pending: list[tuple[int, int]] = field(default_factory=list)


def terminal_history(status: str, reason: str) -> ReadHistoryResponse:
    return ReadHistoryResponse(status=status, stop_reason=reason)


class HistoryReader:
    def __init__(self, *, client: Any, client_id: str, broker_generation: str,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._client = client
        self._identity = (client_id, broker_generation, CONTRACT_VERSION)
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._scans: dict[str, _Scan] = {}
        self._active = 0
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._scans.clear()

    def _claim(self, request: ReadHistoryRequest) -> _Scan | ReadHistoryResponse:
        binding = self._identity + (fingerprint(request.model_dump(mode="json", exclude={"cursor"})),)
        with self._lock:
            now = self._clock()
            self._scans = {k:v for k,v in self._scans.items() if v.expires > now}
            if self._closed:
                return terminal_history("invalid_cursor", "invalid_cursor")
            if request.cursor is not None:
                scan = self._scans.get(request.cursor)
                if scan is None or scan.binding != binding:
                    return terminal_history("invalid_cursor", "invalid_cursor")
                del self._scans[request.cursor]
            else:
                if len(self._scans) + self._active >= 4:
                    return terminal_history("capacity_exhausted", "capacity_exhausted")
                wall = self._now().astimezone(timezone.utc)
                scan = _Scan(binding=binding, expires=now + 300,
                    scope=HistoryScope(target=request.target, mode=request.mode, date_from=request.date_from,
                        date_to=request.date_to or wall, upper_message_id=None,
                        candidate_limit=100 if request.mode == "latest" else 1000,
                        page_limit=request.limit, expires_at=wall + timedelta(seconds=300)))
            self._active += 1
            return scan

    def read(self, request: ReadHistoryRequest, *, deadline: float | None = None) -> ReadHistoryResponse:
        claimed = self._claim(request)
        if isinstance(claimed, ReadHistoryResponse):
            return claimed
        scan = claimed
        deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
        response = None
        try:
            response = self._read_page(scan, deadline)
            return response
        finally:
            with self._lock:
                self._active -= 1
                if response is not None and response.next_cursor is not None:
                    if self._closed or self._clock() >= scan.expires:
                        response.status = "partial"
                        response.stop_reason = "invalid_cursor"
                        response.next_cursor = None
                        response.has_more = None
                    else:
                        self._scans[response.next_cursor] = scan

    def _read_page(self, scan: _Scan, deadline: float) -> ReadHistoryResponse:
        results: list[MessageReadResult] = []
        text_budget = 100000
        page_scanned = pages = 0
        reason, status, continuing = "page_limit", "page", True
        try:
            with self._client.request_budget(deadline):
                if time.monotonic() >= deadline:
                    raise TDLibDeadlineExceeded("history deadline")
                self._client.ensure_ready()
                account = _integer(self._client.get_account_id(), minimum=1)
                if scan.account_id is not None and scan.account_id != account:
                    return terminal_history("invalid_cursor", "invalid_cursor")
                scan.account_id = account
                chat = self._client.resolve_target(scan.scope.target)
                if (not isinstance(chat, dict) or chat.get("@type") != "chat" or
                        type(chat.get("id")) is not int or chat["id"] != scan.scope.target):
                    raise ValueError("invalid history chat")
                if (chat.get("type") or {}).get("@type") == "chatTypeSecret":
                    raise SecretChatRejected("secret chat")
                while len(results) < scan.scope.page_limit:
                    if scan.scanned >= scan.scope.candidate_limit:
                        status, reason, continuing = "limit_reached", "scope_budget_exhausted", False
                        break
                    if (time.monotonic() >= deadline or page_scanned >= 100 or text_budget <= 0
                            or (not scan.pending and pages >= 10)):
                        reason = "call_budget_exhausted"
                        break
                    if not scan.pending:
                        limit = min(20, scan.scope.candidate_limit - scan.scanned)
                        rows = self._client.get_chat_history(scan.scope.target, from_message_id=scan.after, limit=limit)
                        pages += 1
                        if not isinstance(rows, list) or len(rows) > limit:
                            raise ValueError("invalid history page")
                        candidates = []
                        previous = None
                        for row in rows:
                            if not isinstance(row, dict) or row.get("@type") != "message":
                                raise ValueError("invalid history row")
                            mid = _integer(row.get("id"), minimum=1)
                            stamp = _integer(row.get("date"), maximum=2**31 - 1)
                            if (type(row.get("chat_id")) is not int or row["chat_id"] != scan.scope.target
                                    or (previous is not None and mid >= previous)):
                                raise ValueError("invalid history page identity/order")
                            previous = mid
                            if not scan.after or mid < scan.after:
                                candidates.append((mid, stamp))
                        if scan.scope.upper_message_id is None and candidates:
                            scan.scope.upper_message_id = candidates[0][0]
                        scan.pending = candidates
                        # Discard provider bodies before hydration/continuation storage.
                        del rows
                        if not candidates:
                            status, reason, continuing = "partial", "provider_nonprogress" if previous is not None else "provider_end_unverified", False
                            break
                    mid, observed_date = scan.pending[0]
                    anchor = SelectedMessageAnchor(chat_id=scan.scope.target, message_id=mid)
                    try:
                        raw = self._client.get_message(scan.scope.target, mid)
                        if time.monotonic() >= deadline:
                            raise TDLibDeadlineExceeded("history deadline")
                        if (not isinstance(raw, dict) or raw.get("@type") != "message" or
                                type(raw.get("id")) is not int or raw["id"] != mid or
                                type(raw.get("chat_id")) is not int or raw["chat_id"] != scan.scope.target):
                            raise ValueError("invalid hydrated identity")
                        stamp = _integer(raw.get("date"), maximum=2**31 - 1)
                        if self._in_window(scan, stamp):
                            result = _message(self._client, raw, anchor, text_budget)
                            if time.monotonic() >= deadline:
                                raise TDLibDeadlineExceeded("history deadline")
                            results.append(result)
                            if result.message and result.message.text:
                                text_budget -= len(result.message.text.value)
                        del raw
                    except MessageNotFound:
                        if self._in_window(scan, observed_date):
                            results.append(MessageReadResult(anchor=anchor, status="not_found",
                                coverage_complete=False, issues=["message_unavailable"]))
                    # Advance only after the candidate was hydrated (or verified unavailable).
                    scan.pending.pop(0)
                    scan.after = mid
                    scan.scanned += 1
                    page_scanned += 1
                if scan.scanned >= scan.scope.candidate_limit:
                    status, reason, continuing = "limit_reached", "scope_budget_exhausted", False
        except AuthorizationBlocked:
            status, reason, continuing = "blocked", "authorization_unavailable", False
        except SecretChatRejected:
            status, reason, continuing = "error", "secret_chat", False
        except TDLibDeadlineExceeded:
            status, reason, continuing = "partial", "call_budget_exhausted", False
        except (TDLibError, TimeoutError, OSError):
            status, reason, continuing = "error", "provider_error", False
        except (ValueError, TypeError, OverflowError):
            status, reason, continuing = "error", "invalid_provider_page", False
        # A stopped operation cannot promise continuation or infer provider exhaustion.
        if self._clock() >= scan.expires:
            status, reason, continuing = "partial", "invalid_cursor", False
        if continuing and (page_scanned == 0 or scan.scope.upper_message_id is None):
            status, reason, continuing = "partial", "call_budget_exhausted", False
        cursor = "history_" + secrets.token_hex(32) if continuing else None
        return ReadHistoryResponse(status=status, scope=scan.scope.model_copy(deep=True), results=results,
            page_complete=len(results) == scan.scope.page_limit and all(r.coverage_complete for r in results),
            next_cursor=cursor, has_more=True if cursor and scan.pending else None,
            stop_reason=reason, scanned_candidates=scan.scanned)

    @staticmethod
    def _in_window(scan: _Scan, stamp: int) -> bool:
        if stamp == 0:
            return False  # scheduled/unknown time is not evidence in a committed date window
        value = datetime.fromtimestamp(stamp, tz=timezone.utc)
        return value < scan.scope.date_to and (scan.scope.date_from is None or value >= scan.scope.date_from)
