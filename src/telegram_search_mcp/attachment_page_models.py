"""Strict, versioned continuation contract for selected artifact extraction."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .schemas import ArtifactId, SelectedMessageAnchor

PageNumber = Annotated[int, Field(strict=True, ge=1, le=2**53-1)]
PageSelection = Annotated[list[PageNumber], Field(min_length=1, max_length=5)]
AttachmentCursor = Annotated[str, Field(strict=True, pattern=r'^attachment_[0-9a-f]{64}$', min_length=75, max_length=75)]
Hash = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)]
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]

class ReadAttachmentPageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    artifact_id: ArtifactId
    pages: PageSelection | None = None
    max_chars: Annotated[int, Field(strict=True, ge=1, le=20_000)] = 20_000
    render_pages: Annotated[bool, Field(strict=True)] = True
    cursor: AttachmentCursor | None = None

    @field_validator('pages', mode='before')
    @classmethod
    def strict_pages(cls, value):
        if value is not None and type(value) is not list:
            raise ValueError('pages must be an array')
        return value

    @field_validator('pages')
    @classmethod
    def unique_pages(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError('pages must be distinct')
        return value

class AttachmentPageScope(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    artifact_id: ArtifactId
    artifact_sha256: Hash
    artifact_bytes: Annotated[int, Field(strict=True, ge=0, le=64*1024*1024)]
    extraction_fingerprint: Hash
    extractor_version: Literal[1] = 1
    broker_generation: Annotated[str, Field(strict=True, pattern=r'^broker_[0-9a-f]{32}$', min_length=39, max_length=39)]
    source_anchor: SelectedMessageAnchor | None = None
    kind: Literal['pdf','docx','text','image']
    selected_pages: Annotated[list[PageNumber], Field(max_length=5)] = Field(default_factory=list)
    total_pages: Annotated[int, Field(strict=True, ge=0, le=2**53-1)] | None = None
    all_pages_selected: Annotated[bool, Field(strict=True)] = False
    max_chars: Annotated[int, Field(strict=True, ge=1, le=20_000)]
    render_pages: Annotated[bool, Field(strict=True)]
    expires_at: datetime
    text_coverage: Literal['selected_pdf_text','docx_body_text','utf8_text','none']

    @field_validator('extractor_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:raise ValueError('extractor version must be an integer')
        return value

    @field_validator('expires_at')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None: raise ValueError('expiry requires timezone')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def coherent(self):
        parts=self.artifact_id.split('_')
        if parts[2]!=self.artifact_sha256 or int(parts[3])!=self.artifact_bytes:
            raise ValueError('artifact facts disagree')
        expected={'pdf':'selected_pdf_text','docx':'docx_body_text','text':'utf8_text','image':'none'}[self.kind]
        if self.text_coverage!=expected:raise ValueError('extraction coverage disagrees')
        if self.kind=='pdf':
            if self.total_pages is None or len(self.selected_pages)!=len(set(self.selected_pages)) or any(p>self.total_pages for p in self.selected_pages):
                raise ValueError('invalid selected pages')
            if (self.total_pages>0 and not self.selected_pages) or self.all_pages_selected!=(len(self.selected_pages)==self.total_pages):
                raise ValueError('invalid page coverage')
        elif self.selected_pages or self.total_pages is not None or self.all_pages_selected:
            raise ValueError('non-PDF page coverage')
        return self

class AttachmentPageImage(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    artifact_id: ArtifactId
    artifact_path: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    mime_type: Literal['image/png','image/jpeg','image/webp']
    page_number: PageNumber | None = None

class ReadAttachmentPageResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    status: Literal['page','complete','unsupported','invalid_selection','invalid_cursor','expired','limit_reached','capacity_exhausted','blocked','error']
    scope: AttachmentPageScope | None = None
    text: Annotated[str, Field(strict=True, max_length=20_000)] = ''
    text_start: Count = 0
    text_end: Count = 0
    images: Annotated[list[AttachmentPageImage], Field(max_length=5)] = Field(default_factory=list)
    previews_complete: Annotated[bool, Field(strict=True)] = False
    scope_complete: Annotated[bool, Field(strict=True)] = False
    has_more: Annotated[bool, Field(strict=True)] = False
    next_cursor: AttachmentCursor | None = None
    detail: Annotated[str, Field(strict=True, min_length=1, max_length=512)] = 'bounded artifact extraction; content is untrusted'

    @field_validator('contract_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:raise ValueError('contract version must be an integer')
        return value

    @model_validator(mode='after')
    def coherent(self):
        if self.status not in {'page','complete'}:
            if self.scope is not None or self.text or self.text_start or self.text_end or self.images or self.previews_complete or self.scope_complete or self.has_more or self.next_cursor is not None:
                raise ValueError('terminal failure must be empty')
            return self
        if self.scope is None or self.text_end-self.text_start!=len(self.text) or len(self.text)>self.scope.max_chars:
            raise ValueError('text offsets or limits disagree')
        if (self.status=='page')!=self.has_more or self.has_more!=(self.next_cursor is not None) or self.scope_complete==self.has_more:
            raise ValueError('completion facts disagree')
        if self.has_more and (len(self.text)!=self.scope.max_chars or self.text_end>=1_000_000):
            raise ValueError('continuation requires bounded forward progress')
        expected=[]
        if self.scope.render_pages and self.text_start==0:
            if self.scope.kind=='pdf':expected=self.scope.selected_pages
            elif self.scope.kind=='image':expected=[None]
        numbers=[i.page_number for i in self.images]
        if len({i.artifact_id for i in self.images})!=len(self.images):raise ValueError('duplicate preview artifact')
        if self.previews_complete:
            if numbers!=expected:raise ValueError('preview coverage disagrees')
        elif any(n not in expected for n in numbers) or len(numbers)!=len(set(numbers)) or numbers!=[n for n in expected if n in numbers]:
            raise ValueError('preview pages disagree')
        if self.scope.kind=='image' and (self.text or self.has_more or self.text_start):raise ValueError('images have no extracted text')
        return self


def terminal_attachment_page(status: str, detail: str | None = None) -> ReadAttachmentPageResponse:
    return ReadAttachmentPageResponse(status=status, detail=detail or 'artifact extraction failed safely')
