"""One bounded exact-chat page per call over an explicit, verified selection."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .contract import CONTRACT_VERSION, fingerprint
from .exact_search_reader import ExactSearchReader
from .message_reader import _integer
from .schemas import (SearchChatsRequest, SearchChatsResponse, SearchChatsScope,
                      SearchMessagesRequest, SearchMessagesResponse, SelectedChatCoverage)
from .tdjson import (AuthorizationBlocked, SecretChatRejected, TDLibDeadlineExceeded, TDLibError)


def terminal_selected_search(status: str, reason: str) -> SearchChatsResponse:
    return SearchChatsResponse(status=status, stop_reason=reason)


def _verified_chat(raw: object, target: int) -> None:
    if (not isinstance(raw, dict) or raw.get('@type') != 'chat' or
            type(raw.get('id')) is not int or raw['id'] != target):
        raise ValueError('invalid selected chat identity')
    native = raw.get('type')
    if not isinstance(native, dict):
        raise ValueError('missing selected chat type')
    kind = native.get('@type')
    if kind == 'chatTypeSecret':
        raise SecretChatRejected('secret chat')
    field = {'chatTypePrivate': 'user_id', 'chatTypeBasicGroup': 'basic_group_id',
             'chatTypeSupergroup': 'supergroup_id'}.get(kind)
    if field is None:
        raise ValueError('unsupported selected chat type')
    _integer(native.get(field), minimum=1)
    if kind == 'chatTypeSupergroup' and type(native.get('is_channel')) is not bool:
        raise ValueError('invalid selected channel type')


@dataclass
class _Scan:
    binding: tuple
    scope: SearchChatsScope
    expires: float
    coverage: list[SelectedChatCoverage]
    account: int | None = None
    verified: bool = False
    index: int = 0
    child: ExactSearchReader | None = None
    child_cursor: str | None = None


class _GuardedClient:
    """Recheck exact identity and account immediately around child content work."""
    def __init__(self, owner, scan, deadline):
        self.owner, self.scan, self.deadline = owner, scan, deadline
        self.reason = None
        self.transport_lost = False

    def __getattr__(self, name):
        method = getattr(self.owner._client, name)
        if not callable(method):
            return method
        return lambda *args, **kwargs: self._call(name, *args, **kwargs)

    def _call(self, name, *args, **kwargs):
        try:
            return getattr(self.owner._client, name)(*args, **kwargs)
        except (TimeoutError, OSError):
            self.transport_lost = True
            raise

    def check(self):
        self.owner._check(self.scan, self.deadline)

    def get_account_id(self):
        self.check()
        account = _integer(self._call('get_account_id'), minimum=1)
        self.check()
        if account != self.scan.account:
            self.reason = 'account_changed'
            raise TDLibError('selected account changed')
        return account

    def resolve_target(self, target):
        self.get_account_id()
        raw = self._call('resolve_target', target)
        self.check()
        try:
            _verified_chat(raw, self.scan.scope.targets[self.scan.index])
        except ValueError:
            self.reason = 'invalid_provider_chat'
            raise TDLibError('invalid selected chat') from None
        self.get_account_id()
        return raw

    def search_chat_messages(self, target, query, **kwargs):
        self.get_account_id()
        return self._call('search_chat_messages', target, query, **kwargs)

    def get_message(self, target, mid):
        self.get_account_id()
        return self._call('get_message', target, mid)


class _OwnedExactReader(ExactSearchReader):
    """Private child capacity and scope lifetime belong solely to one outer scan."""
    def __init__(self, owner, scan, guarded):
        super().__init__(client=guarded, client_id=owner._identity[0], broker_generation=owner._identity[1],
                         clock=owner._clock, now=lambda: scan.scope.date_to)
        self._outer = scan

    def _claim(self, request):
        claimed = super()._claim(request)
        if not isinstance(claimed, SearchMessagesResponse):
            claimed.account_id = self._outer.account
            claimed.expires = self._outer.expires
            claimed.scope.date_to = self._outer.scope.date_to
            claimed.scope.expires_at = self._outer.scope.expires_at
        return claimed


class SelectedSearchReader:
    def __init__(self, *, client: Any, client_id: str, broker_generation: str,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._client = client
        self._identity = (client_id, broker_generation, CONTRACT_VERSION)
        self._clock, self._now = clock, now
        self._lock = threading.Lock()
        self._scans: dict[str, _Scan] = {}
        self._inflight: dict[int, _Scan] = {}
        self._closed = False

    @staticmethod
    def _dispose(scan):
        child, scan.child = scan.child, None
        scan.child_cursor = None
        if child is not None:
            try:
                child.close()
            except Exception:
                return False
        return True

    def close(self):
        with self._lock:
            self._closed = True
            for scan in list(self._scans.values()) + list(self._inflight.values()):
                self._dispose(scan)
            self._scans.clear()

    def _claim(self, request):
        binding = self._identity + (fingerprint(request.model_dump(mode='json', exclude={'cursor'})),)
        with self._lock:
            now = self._clock()
            for token, scan in list(self._scans.items()):
                if scan.expires <= now:
                    del self._scans[token]
                    self._dispose(scan)
            if self._closed:
                return terminal_selected_search('invalid_cursor', 'invalid_cursor')
            if request.cursor is not None:
                scan = self._scans.get(request.cursor)
                if scan is None or scan.binding != binding:
                    return terminal_selected_search('invalid_cursor', 'invalid_cursor')
                del self._scans[request.cursor]
                if scan.coverage[scan.index].state == 'stopped':
                    scan.index += 1
            else:
                if len(self._scans) + len(self._inflight) >= 4:
                    return terminal_selected_search('capacity_exhausted', 'capacity_exhausted')
                wall = self._now().astimezone(timezone.utc)
                scan = _Scan(binding=binding, expires=now+300,
                    scope=SearchChatsScope(targets=request.targets.copy(), query=request.query,
                        sender=request.sender, direction=request.direction, mode=request.mode,
                        date_from=request.date_from, date_to=request.date_to or wall,
                        page_limit=request.limit, expires_at=wall+timedelta(seconds=300)),
                    coverage=[SelectedChatCoverage(target=target) for target in request.targets])
            self._inflight[id(scan)] = scan
            return scan

    def _check(self, scan, deadline):
        if self._closed or self._clock() >= scan.expires:
            raise _ScopeLost('invalid_cursor')
        if time.monotonic() >= deadline:
            raise TDLibDeadlineExceeded('selected search deadline')

    @staticmethod
    def _stop(scan, status, reason):
        lane = scan.coverage[scan.index]
        lane.state, lane.status, lane.stop_reason = 'stopped', status, reason
        for branch in lane.branch_coverage:
            if branch.state == 'active':
                branch.state = 'interrupted'

    def _response(self, scan, status, reason, results=(), cursor=None):
        return SearchChatsResponse(status=status, scope=scan.scope.model_copy(deep=True),
            current_index=scan.index, coverage=[lane.model_copy(deep=True) for lane in scan.coverage],
            results=list(results), page_complete=bool(results) and all(r.coverage_complete for r in results),
            next_cursor=cursor, stop_reason=reason)

    def read(self, request: SearchChatsRequest, *, deadline: float | None = None) -> SearchChatsResponse:
        # Raw/forged shape failures precede claiming and preserve a legitimate continuation.
        request = SearchChatsRequest.model_validate(request.model_dump(mode='python'))
        claimed = self._claim(request)
        if isinstance(claimed, SearchChatsResponse):
            return claimed
        scan = claimed
        response = None
        deadline = min(deadline if deadline is not None else float('inf'), time.monotonic()+30)
        try:
            with self._client.request_budget(deadline):
                self._check(scan, deadline)
                self._client.ensure_ready()
                self._check(scan, deadline)
                account = _integer(self._client.get_account_id(), minimum=1)
                self._check(scan, deadline)
                if scan.account is not None and account != scan.account:
                    raise _ScopeLost('account_changed')
                scan.account = account
                if not scan.verified:
                    for index,target in enumerate(scan.scope.targets):
                        scan.index = index
                        raw = self._client.resolve_target(target)
                        self._check(scan, deadline)
                        try:
                            _verified_chat(raw, target)
                        except ValueError:
                            raise _InvalidChat() from None
                        del raw
                    scan.verified, scan.index = True, 0
                guarded = _GuardedClient(self, scan, deadline)
                with self._lock:
                    self._check(scan, deadline)
                    if scan.child is None:
                        scan.child = _OwnedExactReader(self, scan, guarded)
                    child = scan.child
                # Keep the captured child alive if close detaches owned state here.
                child._client = guarded
                args = request.model_dump(exclude={'targets', 'cursor'})
                child_response = child.read(SearchMessagesRequest(target=scan.scope.targets[scan.index],
                    cursor=scan.child_cursor, **args), deadline=deadline)
                self._check(scan, deadline)
                if guarded.transport_lost:
                    raise _TransportLost('selected transport lost')
                if guarded.reason == 'account_changed':
                    raise _ScopeLost('account_changed')
                if _integer(self._client.get_account_id(), minimum=1) != scan.account:
                    raise _ScopeLost('account_changed')
                self._check(scan, deadline)
                lane = scan.coverage[scan.index]
                if child_response.scope is not None:
                    lane.upper_message_id = child_response.scope.upper_message_id
                    lane.scanned_candidates = child_response.scanned_candidates
                    lane.processed_candidates = child_response.processed_candidates
                    lane.provider_pages = child_response.provider_pages
                    lane.branch_coverage = [branch.model_copy(deep=True) for branch in child_response.branch_coverage]
                lane.state = 'active' if child_response.next_cursor else 'stopped'
                lane.status, lane.stop_reason = child_response.status, child_response.stop_reason
                if guarded.reason == 'invalid_provider_chat':
                    self._stop(scan, 'error', 'invalid_provider_chat')
                scan.child_cursor = child_response.next_cursor
                group_lost = lane.stop_reason in {'authorization_unavailable', 'invalid_cursor', 'call_budget_exhausted'}
                # A voluntary per-call processing pause may resume; uncertain native deadlines may not.
                if child_response.next_cursor is not None and lane.stop_reason == 'call_budget_exhausted':
                    group_lost = False
                if lane.state == 'stopped' and not self._dispose(scan):
                    raise _ScopeLost('invalid_cursor')
                continuing = not group_lost and (lane.state == 'active' or scan.index < len(scan.coverage)-1)
                status = 'page' if continuing else ('partial' if lane.status == 'page' else lane.status)
                results = [] if group_lost else child_response.results
                response = self._response(scan, status, lane.stop_reason, results,
                    'selected_search_'+secrets.token_hex(32) if continuing else None)
        except _ScopeLost as error:
            self._stop(scan, 'invalid_cursor', error.reason)
            response = self._response(scan, 'invalid_cursor', error.reason)
        except AuthorizationBlocked:
            self._stop(scan, 'blocked', 'authorization_unavailable')
            response = self._response(scan, 'blocked', 'authorization_unavailable')
        except SecretChatRejected:
            self._stop(scan, 'unsupported', 'secret_chat')
            response = self._response(scan, 'unsupported', 'secret_chat')
        except _InvalidChat:
            self._stop(scan, 'error', 'invalid_provider_chat')
            response = self._response(scan, 'error', 'invalid_provider_chat')
        except TDLibDeadlineExceeded:
            self._stop(scan, 'partial', 'call_budget_exhausted')
            response = self._response(scan, 'partial', 'call_budget_exhausted')
        except _TransportLost:
            self._stop(scan, 'error', 'provider_error')
            response = self._response(scan, 'error', 'provider_error')
        except (TDLibError, TimeoutError, OSError):
            self._stop(scan, 'error', 'provider_error')
            response = self._response(scan, 'error', 'provider_error')
        except (ValueError, TypeError, OverflowError):
            self._stop(scan, 'error', 'invalid_provider_page')
            response = self._response(scan, 'error', 'invalid_provider_page')
        finally:
            with self._lock:
                self._inflight.pop(id(scan), None)
                if self._closed or self._clock() >= scan.expires:
                    self._stop(scan, 'invalid_cursor', 'invalid_cursor')
                    response = self._response(scan, 'invalid_cursor', 'invalid_cursor')
                if response is not None and response.next_cursor:
                    self._scans[response.next_cursor] = scan
                elif not self._dispose(scan):
                    self._stop(scan, 'invalid_cursor', 'invalid_cursor')
                    response = self._response(scan, 'invalid_cursor', 'invalid_cursor')
        return response


class _ScopeLost(TDLibError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('selected search scope lost')


class _InvalidChat(ValueError):
    pass


class _TransportLost(TDLibError):
    pass
