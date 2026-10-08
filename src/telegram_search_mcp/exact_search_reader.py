"""Bounded provider lexical search with current evidence and opaque continuation."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .contract import CONTRACT_VERSION, fingerprint
from .message_reader import _integer, _message, _TOPICS
from .forum_reader import _project
from .schemas import (SearchMessageResult, SearchMessagesRequest, SearchMessagesResponse,
                      SearchMessagesScope, SelectedMessageAnchor)
from .tdjson import AuthorizationBlocked, ForumUnsupported, MessageNotFound, SecretChatRejected, TDLibDeadlineExceeded, TDLibError


@dataclass
class _Scan:
    binding: tuple
    scope: SearchMessagesScope
    expires: float
    account_id: int | None = None
    offset: int = 0
    last_id: int | None = None
    pending: list[tuple[int, int, str | None, bool]] = field(default_factory=list)
    end_reason: str | None = None
    scanned: int = 0
    processed: int = 0
    pages: int = 0
    branches: list[Any] = field(default_factory=list)


def terminal_search_messages(status: str, reason: str) -> SearchMessagesResponse:
    return SearchMessagesResponse(status=status, stop_reason=reason)


def _digest(raw: dict) -> str | None:
    content = raw.get('content')
    if not isinstance(content, dict):
        return None
    kind = content.get('@type')
    if kind == 'messageText':
        value = content.get('text')
    elif kind in {'messageDocument', 'messagePhoto', 'messageVideo', 'messageAudio',
                  'messageAnimation', 'messageVoiceNote'}:
        value = content.get('caption')
    else:
        return None
    if not isinstance(value, dict) or not isinstance(value.get('text'), str) or len(value['text']) > 65536:
        return None
    # Hash raw evidence before display normalization or output truncation.
    body = json.dumps([kind, raw['edit_date'], value['text']], ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _row(raw: object, target: int) -> tuple[int, int, str | None]:
    if (not isinstance(raw, dict) or raw.get('@type') != 'message' or
            type(raw.get('chat_id')) is not int or raw['chat_id'] != target):
        raise ValueError('invalid exact search identity')
    mid = _integer(raw.get('id'), minimum=1)
    date = _integer(raw.get('date'), maximum=2**31 - 1)
    _integer(raw.get('edit_date'), maximum=2**31 - 1)
    return mid, date, _digest(raw)


def _predicates(raw: dict, scope: SearchMessagesScope) -> bool:
    """Validate requested raw membership before combining predicates; never short circuit validation."""
    matches = True
    if scope.sender is not None:
        sender = raw.get('sender_id')
        if not isinstance(sender, dict) or sender.get('@type') not in {'messageSenderUser', 'messageSenderChat'}:
            raise ValueError('invalid filter sender')
        kind = 'user' if sender['@type'] == 'messageSenderUser' else 'chat'
        identifier = _integer(sender.get('user_id' if kind == 'user' else 'chat_id'),
            minimum=1 if kind == 'user' else -(2**53 - 1))
        if not identifier:
            raise ValueError('invalid filter sender ID')
        matches = matches and (kind == scope.sender.kind and identifier == scope.sender.id)
    if scope.direction is not None:
        outgoing = raw.get('is_outgoing')
        if type(outgoing) is not bool:
            raise ValueError('invalid filter direction')
        matches = matches and outgoing == (scope.direction == 'outgoing')
    if scope.topic is not None:
        topic = raw.get('topic_id')
        if topic is None:
            member = False
        else:
            if not isinstance(topic, dict) or topic.get('@type') not in _TOPICS:
                raise ValueError('invalid filter topic')
            kind, key = _TOPICS[topic['@type']]
            identifier = _integer(topic.get(key),
                minimum=-(2**53 - 1) if kind in {'saved_messages', 'direct_messages'} else 1,
                maximum=2**31 - 1 if kind == 'forum' else 2**53 - 1)
            member = kind == 'forum' and identifier == scope.topic.id
        matches = matches and member
    return matches


class _InvalidTopic(ValueError):
    pass


class ExactSearchReader:
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

    def _claim(self, request: SearchMessagesRequest) -> _Scan | SearchMessagesResponse:
        binding = self._identity + (fingerprint(request.model_dump(mode='json', exclude={'cursor'})),)
        with self._lock:
            now = self._clock()
            self._scans = {key: value for key, value in self._scans.items() if value.expires > now}
            if self._closed:
                return terminal_search_messages('invalid_cursor', 'invalid_cursor')
            if request.cursor is not None:
                scan = self._scans.get(request.cursor)
                if scan is None or scan.binding != binding:
                    return terminal_search_messages('invalid_cursor', 'invalid_cursor')
                del self._scans[request.cursor]
            else:
                if len(self._scans) + self._active >= 4:
                    return terminal_search_messages('capacity_exhausted', 'capacity_exhausted')
                wall = self._now().astimezone(timezone.utc)
                scan = _Scan(binding=binding, expires=now + 300,
                    scope=SearchMessagesScope(target=request.target,
                        query=request.query if isinstance(request.query, str) else request.query.model_copy(deep=True), mode=request.mode,
                        sender=request.sender.model_copy(deep=True) if request.sender is not None else None,
                        direction=request.direction,
                        topic=request.topic.model_copy(deep=True) if request.topic is not None else None,
                        date_from=request.date_from, date_to=request.date_to or wall, upper_message_id=None,
                        page_limit=request.limit, expires_at=wall + timedelta(seconds=300)))
            self._active += 1
            return scan

    def read(self, request: SearchMessagesRequest, *, deadline: float | None = None) -> SearchMessagesResponse:
        claimed = self._claim(request)
        if isinstance(claimed, SearchMessagesResponse):
            return claimed
        scan, response = claimed, None
        deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + 30)
        try:
            if isinstance(scan.scope.query, str):
                response = self._read_page(scan, deadline)
            else:
                from .boolean_search_reader import read_boolean_page
                response = read_boolean_page(self, scan, deadline)
            return response
        finally:
            with self._lock:
                self._active -= 1
                if response is not None and response.next_cursor is not None:
                    if self._closed or self._clock() >= scan.expires:
                        response.status, response.stop_reason, response.next_cursor = 'partial', 'invalid_cursor', None
                        for branch in response.branch_coverage:
                            if branch.state == 'active':
                                branch.state = 'interrupted'
                    else:
                        self._scans[response.next_cursor] = scan

    def _read_page(self, scan: _Scan, deadline: float) -> SearchMessagesResponse:
        results: list[SearchMessageResult] = []
        text_budget, processed = 100000, 0
        status, reason, continuing = 'page', 'page_limit', True

        def check_deadline() -> None:
            if time.monotonic() >= deadline:
                raise TDLibDeadlineExceeded('exact search deadline')

        try:
            with self._client.request_budget(deadline):
                check_deadline()
                self._client.ensure_ready()
                check_deadline()
                account = _integer(self._client.get_account_id(), minimum=1)
                check_deadline()
                if scan.account_id is not None and account != scan.account_id:
                    return terminal_search_messages('invalid_cursor', 'invalid_cursor')
                scan.account_id = account
                chat = (self._client.resolve_forum_chat(scan.scope.target) if scan.scope.topic is not None
                        else self._client.resolve_target(scan.scope.target))
                check_deadline()
                if (not isinstance(chat, dict) or chat.get('@type') != 'chat' or
                        type(chat.get('id')) is not int or chat['id'] != scan.scope.target):
                    raise ValueError('invalid exact search chat')
                if isinstance(chat.get('type'), dict) and chat['type'].get('@type') == 'chatTypeSecret':
                    raise SecretChatRejected('secret chat')
                if scan.scope.topic is not None:
                    raw_topic = self._client.get_forum_topic(scan.scope.target, scan.scope.topic.id)
                    check_deadline()
                    try:
                        current_topic = _project(raw_topic, scan.scope.target, scan.scope.topic.id)
                    except (ValueError, TypeError, OverflowError):
                        raise _InvalidTopic('invalid current filter topic') from None
                    del raw_topic
                    if current_topic.status == 'not_found':
                        status, reason, continuing = 'partial', 'topic_unavailable', False
                while continuing and len(results) < scan.scope.page_limit:
                    check_deadline()
                    if not scan.pending:
                        if scan.end_reason is not None:
                            status, reason, continuing = 'partial', scan.end_reason, False
                            break
                        if scan.pages >= 10 or scan.scanned >= 200:
                            status, reason, continuing = 'limit_reached', 'scope_budget_exhausted', False
                            break
                    if processed >= 100 or text_budget <= 0:
                        reason = 'call_budget_exhausted'
                        break
                    if not scan.pending:
                        limit = min(20, 200 - scan.scanned)
                        scan.pages += 1
                        filters = {}
                        if scan.scope.sender is not None:
                            filters['sender'] = scan.scope.sender
                        if scan.scope.topic is not None:
                            filters['topic'] = scan.scope.topic
                        raw = self._client.search_chat_messages(scan.scope.target, scan.scope.query,
                            from_message_id=scan.offset, limit=limit, **filters)
                        check_deadline()
                        if not isinstance(raw, dict) or raw.get('@type') != 'foundChatMessages':
                            raise ValueError('invalid exact search page')
                        _integer(raw.get('total_count'), minimum=-1, maximum=2**31 - 1)
                        next_offset = _integer(raw.get('next_from_message_id'))
                        rows = raw.get('messages')
                        if not isinstance(rows, list) or len(rows) > limit:
                            raise ValueError('invalid exact search page size')
                        pending = []
                        for row in rows:
                            check_deadline()
                            pending.append((*_row(row, scan.scope.target), _predicates(row, scan.scope)))
                        if any(a[0] <= b[0] for a, b in zip(pending, pending[1:])):
                            raise ValueError('invalid descending exact search order')
                        # Validate the entire page before hydrating any candidate.
                        del raw, rows
                        if pending and scan.scope.upper_message_id is None:
                            scan.scope.upper_message_id = pending[0][0]
                        scan.scanned += len(pending)
                        scan.pending = pending
                        if next_offset == 0:
                            scan.end_reason = 'provider_end_unverified'
                        elif scan.offset and next_offset >= scan.offset:
                            scan.end_reason = 'provider_nonprogress'
                        scan.offset = next_offset
                        if not scan.pending:
                            continue
                    mid, observed_date, digest, observed_match = scan.pending[0]
                    eligible = mid <= scan.scope.upper_message_id and (scan.last_id is None or mid < scan.last_id)
                    typed = any(value is not None for value in (scan.scope.sender, scan.scope.direction, scan.scope.topic))
                    if eligible or typed:
                        anchor = SelectedMessageAnchor(chat_id=scan.scope.target, message_id=mid)
                        try:
                            raw = self._client.get_message(scan.scope.target, mid)
                            check_deadline()
                            hydrated_id, stamp, current_digest = _row(raw, scan.scope.target)
                            if hydrated_id != mid:
                                raise ValueError('invalid hydrated exact search identity')
                            current_match = _predicates(raw, scan.scope)
                            if eligible and self._in_window(scan.scope, stamp) and (observed_match or current_match):
                                if not current_match or digest != current_digest:
                                    result = SearchMessageResult(anchor=anchor, status='evidence_changed',
                                        coverage_complete=False, issues=['evidence_changed'])
                                elif digest is None:
                                    result = SearchMessageResult(anchor=anchor, status='unsupported',
                                        coverage_complete=False, issues=['unsupported_content'])
                                else:
                                    selected = _message(self._client, raw, anchor, text_budget)
                                    check_deadline()
                                    payload = selected.model_dump()
                                    payload['status'] = 'match' if selected.status == 'complete' else selected.status
                                    result = SearchMessageResult(**payload)
                                results.append(result)
                                if result.message and result.message.text:
                                    text_budget -= len(result.message.text.value)
                            del raw
                        except MessageNotFound:
                            check_deadline()
                            if eligible and observed_match and self._in_window(scan.scope, observed_date):
                                results.append(SearchMessageResult(anchor=anchor, status='not_found',
                                    coverage_complete=False, issues=['message_unavailable']))
                        if eligible:
                            scan.last_id = mid
                    scan.pending.pop(0)
                    scan.processed += 1
                    processed += 1
                if continuing and not scan.pending:
                    if scan.end_reason is not None:
                        status, reason, continuing = 'partial', scan.end_reason, False
                    elif scan.pages >= 10 or scan.scanned >= 200:
                        status, reason, continuing = 'limit_reached', 'scope_budget_exhausted', False
        except AuthorizationBlocked:
            status, reason, continuing = 'blocked', 'authorization_unavailable', False
        except SecretChatRejected:
            status, reason, continuing = 'unsupported', 'secret_chat', False
        except ForumUnsupported:
            status, reason, continuing = 'unsupported', 'unsupported_forum', False
        except _InvalidTopic:
            status, reason, continuing = 'error', 'invalid_provider_topic', False
        except TDLibDeadlineExceeded:
            status, reason, continuing = 'partial', 'call_budget_exhausted', False
        except (TDLibError, TimeoutError, OSError):
            status, reason, continuing = 'error', 'provider_error', False
        except (ValueError, TypeError, OverflowError):
            status, reason, continuing = 'error', 'invalid_provider_page', False
        if self._clock() >= scan.expires:
            status, reason, continuing = 'partial', 'invalid_cursor', False
        if continuing and processed == 0:
            status, reason, continuing = 'partial', 'call_budget_exhausted', False
        return SearchMessagesResponse(status=status, scope=scan.scope.model_copy(deep=True), results=results,
            page_complete=bool(results) and all(r.coverage_complete for r in results),
            next_cursor='search_' + secrets.token_hex(32) if continuing else None,
            stop_reason=reason, scanned_candidates=scan.scanned, processed_candidates=scan.processed,
            provider_pages=scan.pages)

    @staticmethod
    def _in_window(scope: SearchMessagesScope, stamp: int) -> bool:
        if stamp == 0:
            return False
        value = datetime.fromtimestamp(stamp, timezone.utc)
        return value < scope.date_to and (scope.date_from is None or value >= scope.date_from)
