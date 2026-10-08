"""Closed immutable preview contracts for document, photo and voice replies."""
from typing import Literal
import base64
from pydantic import model_validator
from .draft_models import _Closed, DraftId, DraftPreview, DraftCaption, Hash
from .schemas import PrepareArtifactSendRequest, SelectedMessageAnchor
from .reply_drafts import ReplySource, ReplyTarget, digest, SEND_OPTIONS, PREPARED, PENDING, REVISED, UNAVAILABLE


def content_options(draft):
    if draft.kind == 'document':
        return {'thumbnail':None,'disable_content_type_detection':True}
    if draft.kind == 'photo':
        return {'thumbnail':None,'video':None,'added_sticker_file_ids':[],'width':0,'height':0,
            'show_caption_above_media':False,'self_destruct_type':None,'has_spoiler':False}
    return {'duration':draft.duration_seconds,'waveform':draft.waveform_base64,'self_destruct_type':None}


def preview_facts(draft: DraftPreview, target: ReplyTarget):
    return {'domain':'telegram-search-mcp.reply-artifact','version':1,'kind':draft.kind,
        'draft':draft.model_dump(mode='json'),'reply_target':target.model_dump(mode='json'),
        'topic_id':None,'outgoing_entities':[],'send_options':dict(SEND_OPTIONS),'reply_markup':None,
        'reply_to':{'@type':'inputMessageReplyToMessage','message_id':target.anchor.message_id,
            'quote':None,'checklist_task_id':0,'poll_option_id':''},'content_options':content_options(draft)}


class ReplyArtifactDraftPreview(_Closed):
    draft: DraftPreview
    reply_target: ReplyTarget
    preview_sha256: Hash

    @model_validator(mode='after')
    def exact(self):
        from .outgoing_drafts import _safe_caption, _safe_display_name, _MIME_TYPE
        draft=self.draft
        if (draft.kind not in {'document','photo','voice_note'} or draft.recipient!=self.reply_target.anchor.chat_id
            or _safe_caption(draft.caption)!=draft.caption or _safe_display_name(draft.display_name)!=draft.display_name
            or not _MIME_TYPE.fullmatch(draft.mime_type)
            or digest(preview_facts(draft,self.reply_target))!=self.preview_sha256):
            raise ValueError('artifact reply preview facts disagree')
        draft.caption.encode('utf-8')
        if draft.kind=='photo':
            from pathlib import Path
            from .outgoing_media import PHOTO_FORMATS
            if not any(draft.mime_type==mime and Path(draft.display_name).suffix.casefold() in extensions
                       for mime,extensions in PHOTO_FORMATS.values()):
                raise ValueError('photo reply MIME/name facts disagree')
        if draft.kind=='voice_note':
            from pathlib import Path
            waveform=base64.b64decode(draft.waveform_base64,validate=True)
            if (len(waveform)!=63 or len(draft.waveform_base64)!=84 or draft.mime_type!='audio/ogg' or draft.display_name!=_safe_display_name(Path(draft.source_display_name).stem+'.ogg')
                or draft.converted is not True or _safe_display_name(draft.source_display_name)!=draft.source_display_name):
                raise ValueError('voice reply provenance is unavailable')
        return self

    @classmethod
    def create(cls,draft: DraftPreview,source: ReplySource):
        target=source.target()
        return cls(draft=draft,reply_target=target,preview_sha256=digest(preview_facts(draft,target)))


class PrepareReplyArtifactSendRequest(PrepareArtifactSendRequest):
    reply_to: SelectedMessageAnchor

class GetReplyArtifactDraftRequest(_Closed):
    draft_id: DraftId

class UpdateReplyArtifactDraftRequest(GetReplyArtifactDraftRequest):
    caption: DraftCaption

class RefreshReplyArtifactDraftRequest(GetReplyArtifactDraftRequest):
    pass

class PrepareReplyArtifactSendResponse(_Closed):
    status: Literal['prepared','unavailable']
    reply: ReplyArtifactDraftPreview | None=None
    detail: Literal['immutable reply draft prepared; inspect full evidence and obtain fresh approval','reply draft is unavailable']
    @model_validator(mode='after')
    def shape(self):
        if (self.status=='prepared')!=(self.reply is not None) or self.detail!=(PREPARED if self.status=='prepared' else UNAVAILABLE):
            raise ValueError('invalid reply preparation response')
        return self

class GetReplyArtifactDraftResponse(_Closed):
    status: Literal['pending','unavailable']
    reply: ReplyArtifactDraftPreview | None=None
    detail: Literal['exact pending reply snapshot; inspection is not approval','reply draft is unavailable']
    @model_validator(mode='after')
    def shape(self):
        if (self.status=='pending')!=(self.reply is not None) or self.detail!=(PENDING if self.status=='pending' else UNAVAILABLE):
            raise ValueError('invalid reply inspection response')
        return self

class ReviseReplyArtifactDraftResponse(_Closed):
    status: Literal['revised','unavailable']
    previous_draft_id: DraftId | None=None
    reply: ReplyArtifactDraftPreview | None=None
    detail: Literal['new immutable reply revision; prior draft invalidated; fresh approval required','reply draft is unavailable']
    @model_validator(mode='after')
    def shape(self):
        if self.status=='revised':
            if self.reply is None or self.previous_draft_id is None or self.reply.draft.draft_id==self.previous_draft_id or self.detail!=REVISED:
                raise ValueError('invalid reply revision response')
        elif self.reply is not None or self.previous_draft_id is not None or self.detail!=UNAVAILABLE:
            raise ValueError('unavailable reply revision contains evidence')
        return self
