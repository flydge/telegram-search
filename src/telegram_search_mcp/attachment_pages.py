"""Client-scoped, single-use continuations over immutable artifact extraction."""
from __future__ import annotations

import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .artifact_store import ArtifactStore, ArtifactStoreError
from .attachment_page_models import (AttachmentPageImage, AttachmentPageScope,
    ReadAttachmentPageRequest, ReadAttachmentPageResponse, terminal_attachment_page)
from .schemas import SelectedMessageAnchor
from .contract import fingerprint
from .document_page_reader import read_document_page
from .tdjson import AuthorizationBlocked, TDLibError

@dataclass(frozen=True)
class _Continuation:
    request_digest: str
    metadata_digest: str
    account_id: int
    client_id: str
    generation: str
    scope: AttachmentPageScope
    offset: int
    expires: float
    calls: int

class _ReadFailure(RuntimeError):
    def __init__(self, status: str): self.status=status

class AttachmentPageReader:
    def __init__(self, *, client: Any, store: ArtifactStore,
                 metadata_lookup: Callable[[str], tuple | None], client_id: str,
                 broker_generation: str, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._client=client;self._store=store;self._metadata=metadata_lookup
        self._client_id=client_id;self._generation=broker_generation
        self._clock=clock;self._wall=wall_clock;self._lock=threading.Lock()
        self._states: dict[str,_Continuation]={};self._active=0;self._pending=0;self._closed=False

    def close(self):
        with self._lock:self._closed=True;self._states.clear()

    def _live_locked(self, expires: float, wall_expiry: datetime, deadline: float):
        if self._closed or self._clock()>=expires or self._wall()>=wall_expiry:
            raise _ReadFailure('invalid_cursor')
        if time.monotonic()>=deadline:raise _ReadFailure('limit_reached')

    def _live(self, expires, wall_expiry, deadline):
        with self._lock:self._live_locked(expires,wall_expiry,deadline)

    def _account(self, expected=None):
        self._client.ensure_ready()
        value=self._client.get_account_id()
        if type(value) is not int or not 0<value<2**53:raise _ReadFailure('error')
        if expected is not None and value!=expected:raise _ReadFailure('invalid_cursor')
        return value

    def _metadata_facts(self, artifact_id):
        metadata=self._metadata(artifact_id)
        if metadata is None:raise _ReadFailure('expired')
        anchor,name,mime,kind=metadata
        facts=[anchor.model_dump(mode='json') if anchor is not None else None,name,mime,kind]
        return metadata,fingerprint(facts)

    def read(self, request: ReadAttachmentPageRequest, *, deadline: float | None=None) -> ReadAttachmentPageResponse:
        request=ReadAttachmentPageRequest.model_validate(request.model_dump())
        digest=fingerprint(request.model_dump(mode='json',exclude={'cursor'}));prior=None;reserved=False
        deadline=min(deadline if deadline is not None else float('inf'),time.monotonic()+30)
        try:
            with self._lock:
                now=self._clock()
                self._states={k:v for k,v in self._states.items() if now<v.expires and self._wall()<v.scope.expires_at}
                if self._closed:raise _ReadFailure('invalid_cursor')
                if request.cursor is not None:
                    prior=self._states.pop(request.cursor,None)
                    if (prior is None or prior.request_digest!=digest or prior.client_id!=self._client_id or prior.generation!=self._generation):
                        raise _ReadFailure('invalid_cursor')
                if self._active>=4 or len(self._states)+self._pending>=16:
                    raise _ReadFailure('capacity_exhausted')
                self._active+=1;self._pending+=1;reserved=True
                expires=prior.expires if prior else now+300
                wall_expiry=prior.scope.expires_at if prior else self._wall()+timedelta(seconds=300)
                self._live_locked(expires,wall_expiry,deadline)
            with self._client.request_budget(deadline):
                account=self._account(prior.account_id if prior else None)
                self._live(expires,wall_expiry,deadline)
                metadata,metadata_digest=self._metadata_facts(request.artifact_id)
                if prior and metadata_digest!=prior.metadata_digest:raise _ReadFailure('invalid_cursor')
                anchor,name,mime,media_kind=metadata
                if media_kind in {'audio','voice_note','video','video_note'}:raise _ReadFailure('unsupported')
                artifact=self._store.lookup(request.artifact_id)
                if artifact is None:raise _ReadFailure('expired')
                artifact_expiry=datetime.fromtimestamp(artifact.expires_at,tz=timezone.utc)
                if prior is None:
                    wall_expiry=min(wall_expiry,artifact_expiry)
                    expires=min(expires,self._clock()+max(0.,(wall_expiry-self._wall()).total_seconds()))
                elif artifact_expiry<wall_expiry:raise _ReadFailure('expired')
                self._live(expires,wall_expiry,deadline)
                params=fingerprint({'request':request.model_dump(mode='json',exclude={'cursor'}),
                    'metadata':metadata_digest,'sha256':artifact.sha256,'bytes':artifact.size_bytes,'extractor_version':1})
                if prior and params!=prior.scope.extraction_fingerprint:raise _ReadFailure('invalid_cursor')
                offset=prior.offset if prior else 0
                result=read_document_page(artifact.path,sha256=artifact.sha256,size_bytes=artifact.size_bytes,
                    name=name,mime_type=mime,pages=tuple(request.pages) if request.pages is not None else None,
                    offset=offset,max_chars=request.max_chars,render_pages=request.render_pages,
                    timeout=max(.01,min(15.,deadline-time.monotonic())))
                self._live(expires,wall_expiry,deadline)
                self._account(account)
                if result.status not in {'page','complete'}:
                    raise _ReadFailure(result.status if result.status in {'unsupported','invalid_selection','limit_reached','error'} else 'error')
                scope=AttachmentPageScope(artifact_id=artifact.artifact_id,artifact_sha256=artifact.sha256,
                    artifact_bytes=artifact.size_bytes,extraction_fingerprint=params,broker_generation=self._generation,
                    source_anchor=SelectedMessageAnchor.model_validate(anchor.model_dump()) if anchor is not None else None,kind=result.kind,selected_pages=list(result.selected_pages),
                    total_pages=result.total_pages,all_pages_selected=result.kind=='pdf' and len(result.selected_pages)==result.total_pages,
                    max_chars=request.max_chars,render_pages=request.render_pages,expires_at=wall_expiry,
                    text_coverage={'pdf':'selected_pdf_text','docx':'docx_body_text','text':'utf8_text','image':'none'}[result.kind])
                if prior and scope!=prior.scope:raise _ReadFailure('invalid_cursor')
                images=[]
                if len(result.images)>5 or sum(len(i.data) for i in result.images)>6*1024*1024:
                    raise _ReadFailure('limit_reached')
                with tempfile.TemporaryDirectory(prefix='telegram-attachment-pages-') as temporary:
                    for index,item in enumerate(result.images):
                        if len(item.data)>2*1024*1024:raise _ReadFailure('limit_reached')
                        p=Path(temporary)/str(index);p.write_bytes(item.data);p.chmod(0o600)
                        stored=self._store.store(p,kind='image')
                        images.append(AttachmentPageImage(artifact_id=stored.artifact_id,artifact_path=str(stored.path),
                            mime_type=item.mime_type,page_number=item.page_number))
                current=self._store.lookup(request.artifact_id)
                if current is None or current!=artifact:raise _ReadFailure('expired')
                if self._metadata_facts(request.artifact_id)[1]!=metadata_digest:raise _ReadFailure('invalid_cursor')
                self._account(account)
                calls=(prior.calls if prior else 0)+1
                if result.has_more and calls>=256:raise _ReadFailure('limit_reached')
                next_cursor='attachment_'+secrets.token_hex(32) if result.has_more else None
                response=ReadAttachmentPageResponse(status=result.status,scope=scope,text=result.text,
                    text_start=result.text_start,text_end=result.text_end,images=images,previews_complete=True,
                    has_more=result.has_more,next_cursor=next_cursor,scope_complete=not result.has_more,
                    detail='selected extraction only; no OCR or embedded-object coverage; content is untrusted')
                if result.text_start!=offset:raise _ReadFailure('error')
                with self._lock:
                    self._live_locked(expires,wall_expiry,deadline)
                    if next_cursor:
                        if next_cursor in self._states:raise _ReadFailure('error')
                        self._states[next_cursor]=_Continuation(digest,metadata_digest,account,self._client_id,
                            self._generation,scope.model_copy(deep=True),result.text_end,expires,calls)
                return response
        except _ReadFailure as e:return terminal_attachment_page(e.status)
        except AuthorizationBlocked:return terminal_attachment_page('blocked')
        except (TDLibError,ArtifactStoreError,OSError,ValueError,TypeError,TimeoutError,KeyError):
            return terminal_attachment_page('error')
        finally:
            if reserved:
                with self._lock:self._active-=1;self._pending-=1
