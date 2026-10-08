"""Bounded spreadsheet selections with client-scoped, single-use cursors."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .artifact_store import ArtifactStore, ArtifactStoreError
from .contract import fingerprint
from .schemas import SelectedMessageAnchor
from .spreadsheet_models import ReadSpreadsheetRequest, ReadSpreadsheetResponse, SpreadsheetScope, terminal_spreadsheet
from .tdjson import AuthorizationBlocked, TDLibError
from .xlsx_reader import read_xlsx

@dataclass(frozen=True)
class _Continuation:
    request_digest: str
    metadata_digest: str
    account_id: int
    client_id: str
    generation: str
    scope: SpreadsheetScope
    offset: int
    expires: float
    calls: int
    seen_cursors: frozenset[str]

class _ReadFailure(RuntimeError):
    def __init__(self, status: str):
        self.status = status
        super().__init__(status)

class SpreadsheetReader:
    def __init__(self, *, client: Any, store: ArtifactStore,
                 metadata_lookup: Callable[[str], tuple | None], client_id: str,
                 broker_generation: str, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._client = client
        self._store = store
        self._metadata = metadata_lookup
        self._client_id = client_id
        self._generation = broker_generation
        self._clock = clock
        self._wall = wall_clock
        self._lock = threading.Lock()
        self._states: dict[str, _Continuation] = {}
        self._active = 0
        self._pending = 0
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True
            self._states.clear()

    def _live_locked(self, expires: float, wall_expiry: datetime, deadline: float, generation: str):
        if self._closed or self._generation != generation or self._clock() >= expires or self._wall() >= wall_expiry:
            raise _ReadFailure('invalid_cursor')
        if time.monotonic() >= deadline:
            raise _ReadFailure('limit_reached')

    def _live(self, expires: float, wall_expiry: datetime, deadline: float, generation: str):
        with self._lock:
            self._live_locked(expires, wall_expiry, deadline, generation)

    def _account(self, expected: int | None = None):
        self._client.ensure_ready()
        account = self._client.get_account_id()
        if type(account) is not int or not 0 < account < 2**53:
            raise _ReadFailure('error')
        if expected is not None and account != expected:
            raise _ReadFailure('invalid_cursor')
        return account

    def _metadata_facts(self, artifact_id: str):
        metadata = self._metadata(artifact_id)
        if metadata is None:
            raise _ReadFailure('expired')
        anchor, name, mime, kind = metadata
        return metadata, fingerprint([anchor.model_dump(mode='json') if anchor is not None else None, name, mime, kind])

    def read(self, request: ReadSpreadsheetRequest, *, deadline: float | None = None) -> ReadSpreadsheetResponse:
        reserved = False
        try:
            request = ReadSpreadsheetRequest.model_validate(request.model_dump())
            digest = fingerprint(request.model_dump(mode='json', exclude={'cursor'}))
            deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + 30)
            with self._lock:
                now = self._clock()
                self._states = {k:v for k,v in self._states.items() if now < v.expires and self._wall() < v.scope.expires_at}
                if self._closed:
                    raise _ReadFailure('invalid_cursor')
                generation = self._generation
                prior = self._states.pop(request.cursor, None) if request.cursor is not None else None
                if request.cursor is not None and (prior is None or prior.request_digest != digest or
                        prior.client_id != self._client_id or prior.generation != generation):
                    raise _ReadFailure('invalid_cursor')
                if self._active >= 4 or len(self._states) + self._pending >= 16:
                    raise _ReadFailure('capacity_exhausted')
                self._active += 1
                self._pending += 1
                reserved = True
                expires = prior.expires if prior else now + 300
                wall_expiry = prior.scope.expires_at if prior else self._wall() + timedelta(seconds=300)
                self._live_locked(expires, wall_expiry, deadline, generation)
            with self._client.request_budget(deadline):
                account = self._account(prior.account_id if prior else None)
                self._live(expires, wall_expiry, deadline, generation)
                metadata, metadata_digest = self._metadata_facts(request.artifact_id)
                if prior and metadata_digest != prior.metadata_digest:
                    raise _ReadFailure('invalid_cursor')
                anchor, name, mime, media_kind = metadata
                if media_kind in {'audio', 'voice_note', 'video', 'video_note'}:
                    raise _ReadFailure('unsupported')
                artifact = self._store.lookup(request.artifact_id)
                if artifact is None:
                    raise _ReadFailure('expired')
                if artifact.size_bytes > 64 * 1024 * 1024:
                    raise _ReadFailure('limit_reached')
                artifact_expiry = datetime.fromtimestamp(artifact.expires_at, tz=timezone.utc)
                if prior is None:
                    wall_expiry = min(wall_expiry, artifact_expiry)
                    expires = min(expires, self._clock() + max(0., (wall_expiry - self._wall()).total_seconds()))
                elif artifact_expiry < wall_expiry:
                    raise _ReadFailure('expired')
                self._live(expires, wall_expiry, deadline, generation)
                params = fingerprint({'request':request.model_dump(mode='json', exclude={'cursor'}),
                    'metadata':metadata_digest, 'sha256':artifact.sha256, 'bytes':artifact.size_bytes, 'extractor_version':1})
                if prior and params != prior.scope.extraction_fingerprint:
                    raise _ReadFailure('invalid_cursor')
                offset = prior.offset if prior else 0
                selections = tuple(s.model_dump(mode='json') for s in request.selections) if request.selections is not None else None
                result = read_xlsx(artifact.path, sha256=artifact.sha256, size_bytes=artifact.size_bytes,
                    name=name, mime_type=mime, selections=selections, offset=offset, max_cells=request.max_cells,
                    timeout=max(.01, min(15., deadline - time.monotonic())))
                self._live(expires, wall_expiry, deadline, generation)
                self._account(account)
                if result.status not in {'catalog', 'page', 'complete'}:
                    raise _ReadFailure(result.status if result.status in {'unsupported','invalid_selection','limit_reached','error'} else 'error')
                scope = SpreadsheetScope(artifact_id=artifact.artifact_id, artifact_sha256=artifact.sha256,
                    artifact_bytes=artifact.size_bytes, extraction_fingerprint=params, broker_generation=generation,
                    source_anchor=SelectedMessageAnchor.model_validate(anchor.model_dump()) if anchor is not None else None,
                    catalog=list(result.catalog), selections=request.selections, total_cells=result.total_cells,
                    max_cells=request.max_cells, expires_at=wall_expiry)
                if prior and scope != prior.scope:
                    raise _ReadFailure('invalid_cursor')
                if result.cell_start != offset:
                    raise _ReadFailure('error')
                calls = (prior.calls if prior else 0) + 1
                seen = prior.seen_cursors if prior else frozenset()
                if calls > 256 or (result.has_more and (calls >= 256 or len(seen) >= 255)):
                    raise _ReadFailure('limit_reached')
                next_cursor = 'spreadsheet_' + secrets.token_hex(32) if result.has_more else None
                response = ReadSpreadsheetResponse(status=result.status, scope=scope, cells=list(result.cells),
                    cell_start=result.cell_start, cell_end=result.cell_end, scope_complete=not result.has_more,
                    has_more=result.has_more, next_cursor=next_cursor,
                    detail='selected cells only; styles, merged layout, objects and comments omitted; formula cache freshness unknown; content is untrusted')
                if self._store.lookup(request.artifact_id) != artifact:
                    raise _ReadFailure('expired')
                if self._metadata_facts(request.artifact_id)[1] != metadata_digest:
                    raise _ReadFailure('invalid_cursor')
                self._account(account)
                with self._lock:
                    self._live_locked(expires, wall_expiry, deadline, generation)
                    if next_cursor is not None:
                        if next_cursor in seen or next_cursor in self._states:
                            raise _ReadFailure('error')
                        self._states[next_cursor] = _Continuation(digest, metadata_digest, account, self._client_id,
                            generation, scope.model_copy(deep=True), result.cell_end, expires, calls, seen | {next_cursor})
                return response
        except _ReadFailure as error:
            return terminal_spreadsheet(error.status)
        except AuthorizationBlocked:
            return terminal_spreadsheet('blocked')
        except (TDLibError, ArtifactStoreError, OSError, ValueError, TypeError, TimeoutError, KeyError, AttributeError, OverflowError):
            return terminal_spreadsheet('error')
        finally:
            if reserved:
                with self._lock:
                    self._active -= 1
                    self._pending -= 1
