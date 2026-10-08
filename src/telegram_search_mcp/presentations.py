"""Client-scoped bounded presentation reads, with no retained content or cursors."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .artifact_store import ArtifactStore, ArtifactStoreError
from .contract import fingerprint
from .presentation_models import (PRESENTATION_DETAIL, PresentationSlide, PresentationScope,
    ReadPresentationRequest, ReadPresentationResponse, presentation_text_size, terminal_presentation)
from .schemas import SelectedMessageAnchor
from .tdjson import AuthorizationBlocked, TDLibError


def read_pptx(*args, **kwargs):
    # Loaded on demand so independently developed reader/parser slices can integrate.
    from .pptx_reader import read_pptx as extract
    return extract(*args, **kwargs)


class _ReadFailure(RuntimeError):
    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


class PresentationReader:
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
        self._active = 0
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True

    def _live_locked(self, expires, wall_expiry, deadline, generation):
        if self._closed or self._generation != generation or self._clock() >= expires or self._wall() >= wall_expiry:
            raise _ReadFailure('expired')
        if time.monotonic() >= deadline:
            raise _ReadFailure('limit_reached')

    def _live(self, expires, wall_expiry, deadline, generation):
        with self._lock:
            self._live_locked(expires, wall_expiry, deadline, generation)

    def _account(self, expected=None):
        self._client.ensure_ready()
        account = self._client.get_account_id()
        if type(account) is not int or not 0 < account < 2**53:
            raise _ReadFailure('error')
        if expected is not None and account != expected:
            raise _ReadFailure('expired')
        return account

    def _metadata_facts(self, artifact_id):
        metadata = self._metadata(artifact_id)
        if metadata is None:
            raise _ReadFailure('expired')
        anchor, name, mime, kind = metadata
        facts = [anchor.model_dump(mode='json') if anchor is not None else None, name, mime, kind]
        return metadata, fingerprint(facts)

    def read(self, request: ReadPresentationRequest, *, deadline: float | None = None) -> ReadPresentationResponse:
        reserved = False
        try:
            request = ReadPresentationRequest.model_validate(request.model_dump())
            if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
                raise _ReadFailure('error')
            deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + 30)
            with self._lock:
                if self._closed:
                    raise _ReadFailure('expired')
                if self._active >= 4:
                    raise _ReadFailure('capacity_exhausted')
                self._active += 1
                reserved = True
                generation = self._generation
                expires = self._clock() + 30
                wall_expiry = self._wall() + timedelta(seconds=30)
                self._live_locked(expires, wall_expiry, deadline, generation)
            with self._client.request_budget(deadline):
                account = self._account()
                self._live(expires, wall_expiry, deadline, generation)
                metadata, metadata_digest = self._metadata_facts(request.artifact_id)
                anchor, name, mime, kind = metadata
                if kind in {'audio', 'voice_note', 'video', 'video_note'}:
                    raise _ReadFailure('unsupported')
                artifact = self._store.lookup(request.artifact_id)
                if artifact is None:
                    raise _ReadFailure('expired')
                if artifact.artifact_id != request.artifact_id:
                    raise _ReadFailure('error')
                if artifact.size_bytes > 64 * 1024 * 1024:
                    raise _ReadFailure('limit_reached')
                wall_expiry = min(wall_expiry, datetime.fromtimestamp(artifact.expires_at, tz=timezone.utc))
                expires = min(expires, self._clock() + max(0., (wall_expiry - self._wall()).total_seconds()))
                self._live(expires, wall_expiry, deadline, generation)
                params = fingerprint({'request': request.model_dump(mode='json'), 'metadata': metadata_digest,
                                      'sha256': artifact.sha256, 'bytes': artifact.size_bytes, 'extractor_version': 1})
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ReadFailure('limit_reached')
                result = read_pptx(artifact.path, sha256=artifact.sha256, size_bytes=artifact.size_bytes,
                    name=name, mime_type=mime, slides=tuple(request.slides) if request.slides is not None else None,
                    include_notes=request.include_notes if request.slides is not None else False,
                    timeout=min(15., remaining))
                self._live(expires, wall_expiry, deadline, generation)
                self._account(account)
                if result.status not in {'catalog', 'complete'}:
                    raise _ReadFailure(result.status if result.status in {'unsupported', 'invalid_selection', 'limit_reached', 'error'} else 'error')
                if len(result.slides) > 5:
                    raise _ReadFailure('error')
                # Check public string cap before Pydantic's field length rejection so
                # over-budget extraction has the actionable limit_reached status.
                if sum(len(value) for slide in result.slides for key in ('text', 'notes')
                       if isinstance(value := slide.get(key), str)) > 20_000:
                    raise _ReadFailure('limit_reached')
                selected = [PresentationSlide.model_validate(item) for item in result.slides]
                if presentation_text_size(selected) > 20_000:
                    raise _ReadFailure('limit_reached')
                scope = PresentationScope(artifact_id=artifact.artifact_id, artifact_sha256=artifact.sha256,
                    artifact_bytes=artifact.size_bytes, extraction_fingerprint=params, broker_generation=generation,
                    source_anchor=SelectedMessageAnchor.model_validate(anchor.model_dump()) if anchor is not None else None,
                    catalog=list(result.catalog), slides=request.slides, include_notes=request.include_notes)
                response = ReadPresentationResponse(status=result.status, scope=scope, slides=selected,
                    selection_complete=True, detail=PRESENTATION_DETAIL)
                if self._store.lookup(request.artifact_id) != artifact:
                    raise _ReadFailure('expired')
                if self._metadata_facts(request.artifact_id)[1] != metadata_digest:
                    raise _ReadFailure('expired')
                self._account(account)
                with self._lock:
                    self._live_locked(expires, wall_expiry, deadline, generation)
                return response
        except _ReadFailure as error:
            return terminal_presentation(error.status)
        except AuthorizationBlocked:
            return terminal_presentation('blocked')
        except (TDLibError, ArtifactStoreError, OSError, ValueError, TypeError, TimeoutError,
                KeyError, AttributeError, OverflowError, ImportError):
            return terminal_presentation('error')
        finally:
            if reserved:
                with self._lock:
                    self._active -= 1
