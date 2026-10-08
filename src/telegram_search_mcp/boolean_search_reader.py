"""Bounded native-seed merge with current, full-text Boolean evidence."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from .boolean_query import boolean_matches, boolean_seeds
from .exact_search_reader import _InvalidTopic, _predicates, _row, terminal_search_messages
from .forum_reader import _project
from .message_reader import _integer, _message
from .schemas import SearchBranchCoverage, SearchMessageResult, SearchMessagesResponse, SelectedMessageAnchor
from .tdjson import (AuthorizationBlocked, ForumUnsupported, MessageNotFound, SecretChatRejected,
                     TDLibDeadlineExceeded, TDLibError)


@dataclass
class _Branch:
    index: int
    query: str
    offset: int = 0
    pending: list[tuple[int, int, str | None, bool]] = field(default_factory=list)
    state: str = 'active'
    scanned: int = 0
    processed: int = 0
    pages: int = 0


def _membership(raw, scope, digest):
    typed = _predicates(raw, scope)
    if digest is None:
        # Unknown local membership can produce only contentless unsupported evidence.
        return typed
    content = raw['content']
    body = content['text' if content['@type'] == 'messageText' else 'caption']['text']
    return typed and boolean_matches(scope.query, body)


def read_boolean_page(reader, scan, deadline):
    if not scan.branches:
        scan.branches = [_Branch(i, seed) for i, seed in enumerate(boolean_seeds(scan.scope.query))]
    branches = scan.branches
    results = []
    processed, text_budget = 0, 100000
    status, reason, continuing = 'page', 'page_limit', True

    def check_deadline():
        if time.monotonic() >= deadline:
            raise TDLibDeadlineExceeded('Boolean search deadline')

    def budget_available():
        return scan.pages < 10 and scan.scanned < 200

    def establish_heads():
        """No candidate is safe until every nonterminal branch has a known head."""
        for branch in branches:
            while not branch.pending and branch.state == 'active':
                check_deadline()
                if not budget_available():
                    return False
                limit = min(20, 200 - scan.scanned)
                branch.pages += 1
                scan.pages += 1
                filters = {}
                if scan.scope.sender is not None:
                    filters['sender'] = scan.scope.sender
                if scan.scope.topic is not None:
                    filters['topic'] = scan.scope.topic
                raw = reader._client.search_chat_messages(scan.scope.target, branch.query,
                    from_message_id=branch.offset, limit=limit, **filters)
                check_deadline()
                if not isinstance(raw, dict) or raw.get('@type') != 'foundChatMessages':
                    raise ValueError('invalid Boolean search page')
                _integer(raw.get('total_count'), minimum=-1, maximum=2**31 - 1)
                next_offset = _integer(raw.get('next_from_message_id'))
                rows = raw.get('messages')
                if not isinstance(rows, list) or len(rows) > limit:
                    raise ValueError('invalid Boolean search page size')
                pending = []
                for row in rows:
                    check_deadline()
                    mid, date, digest = _row(row, scan.scope.target)
                    pending.append((mid, date, digest, _membership(row, scan.scope, digest)))
                if any(a[0] <= b[0] for a, b in zip(pending, pending[1:])):
                    raise ValueError('invalid Boolean descending page order')
                del raw, rows
                branch.scanned += len(pending)
                scan.scanned += len(pending)
                branch.pending = pending
                if next_offset == 0:
                    branch.state = 'provider_end_unverified'
                elif branch.offset and next_offset >= branch.offset:
                    branch.state = 'provider_nonprogress'
                branch.offset = next_offset
        return True

    def native_reason():
        return ('provider_nonprogress' if any(b.state == 'provider_nonprogress' for b in branches)
                else 'provider_end_unverified')

    try:
        with reader._client.request_budget(deadline):
            check_deadline()
            reader._client.ensure_ready()
            check_deadline()
            account = _integer(reader._client.get_account_id(), minimum=1)
            check_deadline()
            if scan.account_id is not None and account != scan.account_id:
                return terminal_search_messages('invalid_cursor', 'invalid_cursor')
            scan.account_id = account
            chat = (reader._client.resolve_forum_chat(scan.scope.target) if scan.scope.topic is not None
                    else reader._client.resolve_target(scan.scope.target))
            check_deadline()
            if (not isinstance(chat, dict) or chat.get('@type') != 'chat' or
                    type(chat.get('id')) is not int or chat['id'] != scan.scope.target):
                raise ValueError('invalid Boolean search chat')
            if isinstance(chat.get('type'), dict) and chat['type'].get('@type') == 'chatTypeSecret':
                raise SecretChatRejected('secret chat')
            if scan.scope.topic is not None:
                raw_topic = reader._client.get_forum_topic(scan.scope.target, scan.scope.topic.id)
                check_deadline()
                try:
                    topic = _project(raw_topic, scan.scope.target, scan.scope.topic.id)
                except (ValueError, TypeError, OverflowError):
                    raise _InvalidTopic('invalid current Boolean filter topic') from None
                del raw_topic
                if topic.status == 'not_found':
                    status, reason, continuing = 'partial', 'topic_unavailable', False
            while continuing and len(results) < scan.scope.page_limit:
                check_deadline()
                if processed >= 100 or text_budget <= 0:
                    reason = 'call_budget_exhausted'
                    break
                if not establish_heads():
                    status, reason, continuing = 'limit_reached', 'scope_budget_exhausted', False
                    break
                heads = [b.pending[0][0] for b in branches if b.pending]
                if not heads:
                    status, reason, continuing = 'partial', native_reason(), False
                    break
                if scan.scope.upper_message_id is None:
                    scan.scope.upper_message_id = max(heads)
                mid = max(heads)
                group = [b for b in branches if b.pending and b.pending[0][0] == mid]
                if processed + len(group) > 100:
                    reason = 'call_budget_exhausted'
                    break
                observations = [b.pending[0] for b in group]
                eligible = mid <= scan.scope.upper_message_id and (scan.last_id is None or mid < scan.last_id)
                anchor = SelectedMessageAnchor(chat_id=scan.scope.target, message_id=mid)
                try:
                    # Every processed observation, including filtered and old duplicates, is rehydrated.
                    raw = reader._client.get_message(scan.scope.target, mid)
                    check_deadline()
                    hydrated_id, stamp, digest = _row(raw, scan.scope.target)
                    if hydrated_id != mid:
                        raise ValueError('invalid hydrated Boolean search identity')
                    current_match = _membership(raw, scan.scope, digest)
                    observed_match = any(o[3] for o in observations)
                    if eligible and reader._in_window(scan.scope, stamp) and (observed_match or current_match):
                        changed = len({o[2] for o in observations}) > 1 or any(o[2] != digest for o in observations)
                        if not current_match or changed:
                            result = SearchMessageResult(anchor=anchor, status='evidence_changed',
                                coverage_complete=False, issues=['evidence_changed'])
                        elif digest is None:
                            result = SearchMessageResult(anchor=anchor, status='unsupported',
                                coverage_complete=False, issues=['unsupported_content'])
                        else:
                            selected = _message(reader._client, raw, anchor, text_budget)
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
                    if eligible and any(o[2] is not None and o[3] and reader._in_window(scan.scope, o[1]) for o in observations):
                        results.append(SearchMessageResult(anchor=anchor, status='not_found',
                            coverage_complete=False, issues=['message_unavailable']))
                if eligible:
                    scan.last_id = mid
                for branch in group:
                    branch.pending.pop(0)
                    branch.processed += 1
                scan.processed += len(group)
                processed += len(group)
            if continuing:
                if all(not b.pending and b.state != 'active' for b in branches):
                    status, reason, continuing = 'partial', native_reason(), False
                elif not budget_available() and any(not b.pending and b.state == 'active' for b in branches):
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
    if reader._clock() >= scan.expires:
        status, reason, continuing = 'partial', 'invalid_cursor', False
    if continuing and processed == 0:
        status, reason, continuing = 'partial', 'call_budget_exhausted', False
    if not continuing:
        for branch in branches:
            if branch.state == 'active':
                branch.state = 'scope_budget_exhausted' if reason == 'scope_budget_exhausted' else 'interrupted'
    coverage = [SearchBranchCoverage(index=b.index, query=b.query, state=b.state,
        scanned_candidates=b.scanned, processed_candidates=b.processed, provider_pages=b.pages) for b in branches]
    return SearchMessagesResponse(status=status, scope=scan.scope.model_copy(deep=True), results=results,
        page_complete=bool(results) and all(r.coverage_complete for r in results),
        next_cursor='search_' + secrets.token_hex(32) if continuing else None,
        stop_reason=reason, scanned_candidates=scan.scanned, processed_candidates=scan.processed,
        provider_pages=scan.pages, branch_coverage=coverage)
