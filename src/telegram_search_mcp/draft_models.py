"""Closed contracts for inspecting and cancelling owned pending drafts."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .schemas import ArtifactId, ChatId

DraftId = Annotated[str, Field(strict=True, pattern=r'^draft_[0-9a-f]{32}$', min_length=38, max_length=38)]
DraftLimit = Annotated[int, Field(strict=True, ge=1, le=50)]
AccountId = Annotated[int, Field(strict=True, gt=0, lt=2**53)]
Hash = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)]
Kind = Literal['document', 'photo', 'voice_note', 'text']

class _Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)

class ListDraftsRequest(_Closed):
    limit: DraftLimit = 20
    after_draft_id: DraftId | None = None

class GetDraftRequest(_Closed):
    draft_id: DraftId

class CancelDraftRequest(GetDraftRequest):
    pass

DraftText = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
DraftCaption = Annotated[str, Field(strict=True, max_length=1024)]

class UpdateDraftRequest(GetDraftRequest):
    text: DraftText | None = None
    caption: DraftCaption | None = None

    @model_validator(mode='after')
    def one_field(self):
        if (self.text is None) == (self.caption is None):
            raise ValueError('exactly one of text or caption is required')
        return self

class RefreshDraftRequest(GetDraftRequest):
    pass

class DraftSummary(_Closed):
    draft_id: DraftId
    account_id: AccountId
    recipient: ChatId
    recipient_title: Annotated[str, Field(strict=True, min_length=1, max_length=255)]
    kind: Kind
    expires_at: datetime
    approval_required: Annotated[bool, Field(strict=True, json_schema_extra={'const': True})] = True

    @field_validator('expires_at', mode='before')
    @classmethod
    def expiry_input(cls, value):
        if type(value) not in {str, datetime}:
            raise ValueError('expiry must be an ISO datetime')
        if type(value) is str:
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError('expiry must be an ISO datetime') from None
        return value

    @field_validator('expires_at')
    @classmethod
    def expiry_aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('expiry requires a timezone')
        return value.astimezone(timezone.utc)

    @field_validator('approval_required')
    @classmethod
    def requires_approval(cls, value):
        if value is not True:
            raise ValueError('draft always requires approval')
        return value

class DraftPreview(DraftSummary):
    sha256: Hash
    size_bytes: Annotated[int, Field(strict=True, gt=0, le=64*1024*1024)]
    text: Annotated[str, Field(strict=True, min_length=1, max_length=4096)] | None = None
    artifact_id: ArtifactId | None = None
    display_name: Annotated[str, Field(strict=True, min_length=1, max_length=255)] | None = None
    mime_type: Annotated[str, Field(strict=True, min_length=3, max_length=127)] | None = None
    caption: Annotated[str, Field(strict=True, max_length=1024)] | None = None
    duration_seconds: Annotated[int, Field(strict=True, gt=0, le=600)] | None = None
    waveform_base64: Annotated[str, Field(strict=True, max_length=256)] | None = None
    source_sha256: Hash | None = None
    source_display_name: Annotated[str, Field(strict=True, min_length=1, max_length=255)] | None = None
    converted: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode='after')
    def exact_content(self):
        voice = (self.duration_seconds, self.waveform_base64, self.source_sha256, self.source_display_name)
        if self.kind == 'text':
            if self.text is None or any(v is not None for v in (self.artifact_id, self.display_name, self.mime_type, self.caption, *voice)) or self.converted:
                raise ValueError('text preview contains unrelated artifact metadata')
            data = self.text.encode('utf-8')
            if len(data) != self.size_bytes or hashlib.sha256(data).hexdigest() != self.sha256:
                raise ValueError('text preview facts disagree')
        else:
            if self.text is not None or any(v is None for v in (self.artifact_id,self.display_name,self.mime_type,self.caption)):
                raise ValueError('artifact preview is incomplete')
            if self.kind == 'photo' and self.size_bytes > 10_000_000:
                raise ValueError('photo preview exceeds send limit')
            parts = self.artifact_id.split('_')
            if parts[2] != self.sha256 or int(parts[3]) != self.size_bytes:
                raise ValueError('artifact preview facts disagree')
            if self.kind == 'voice_note':
                if any(v is None for v in voice):
                    raise ValueError('voice preview is incomplete')
            elif any(v is not None for v in voice) or self.converted:
                raise ValueError('non-voice preview has conversion metadata')
        return self

class ListDraftsResponse(_Closed):
    status: Literal['listed', 'unavailable']
    drafts: Annotated[list[DraftSummary], Field(strict=True, max_length=50)] = Field(default_factory=list)
    has_more: Annotated[bool, Field(strict=True)] = False
    next_after_draft_id: DraftId | None = None
    detail: Literal['owned pending drafts; live view, no recipient verification or approval', 'drafts are unavailable']

    @model_validator(mode='after')
    def shape(self):
        if self.status == 'unavailable':
            if self.drafts or self.has_more or self.next_after_draft_id is not None or self.detail != 'drafts are unavailable':
                raise ValueError('unavailable response contains draft evidence')
        else:
            if self.detail != 'owned pending drafts; live view, no recipient verification or approval':
                raise ValueError('invalid list detail')
            if len({d.account_id for d in self.drafts}) > 1:
                raise ValueError('draft summaries must belong to one account')
            ids = [d.draft_id for d in self.drafts]
            if ids != sorted(set(ids)):
                raise ValueError('draft summaries must be distinct and ordered')
            if self.has_more:
                if not ids or self.next_after_draft_id != ids[-1]:
                    raise ValueError('continuation does not match last draft')
            elif self.next_after_draft_id is not None:
                raise ValueError('final page has a continuation')
        return self

class GetDraftResponse(_Closed):
    status: Literal['pending', 'unavailable']
    draft: DraftPreview | None = None
    detail: Literal['exact pending draft; no recipient verification or approval', 'draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        if self.status == 'pending':
            if self.draft is None or self.detail != 'exact pending draft; no recipient verification or approval':
                raise ValueError('pending response requires exact preview')
        elif self.draft is not None or self.detail != 'draft is unavailable':
            raise ValueError('unavailable response contains a preview')
        return self

class CancelDraftResponse(_Closed):
    status: Literal['cancelled', 'unavailable']
    draft_id: DraftId | None = None
    detail: Literal['pending draft cancelled; cached artifacts retained', 'draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        if self.status == 'cancelled':
            if self.draft_id is None or self.detail != 'pending draft cancelled; cached artifacts retained':
                raise ValueError('cancelled response requires a draft ID')
        elif self.draft_id is not None or self.detail != 'draft is unavailable':
            raise ValueError('unavailable response contains a draft ID')
        return self

class ReviseDraftResponse(_Closed):
    status: Literal['revised', 'unavailable']
    previous_draft_id: DraftId | None = None
    draft: DraftPreview | None = None
    detail: Literal['new immutable revision; prior draft invalidated; fresh approval required', 'draft is unavailable']

    @model_validator(mode='after')
    def shape(self):
        if self.status == 'revised':
            if (self.previous_draft_id is None or self.draft is None or
                    self.draft.draft_id == self.previous_draft_id or self.detail !=
                    'new immutable revision; prior draft invalidated; fresh approval required'):
                raise ValueError('revision requires distinct old/new IDs and exact preview')
        elif self.previous_draft_id is not None or self.draft is not None or self.detail != 'draft is unavailable':
            raise ValueError('unavailable revision contains evidence')
        return self
