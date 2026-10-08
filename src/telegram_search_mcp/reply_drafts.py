"""Closed, immutable evidence for bounded same-chat reply sources."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator
from .draft_models import _Closed, DraftId, DraftPreview, DraftText, Hash
from .schemas import ChatId, SelectedMessageAnchor
from .sanitize import sanitize_telegram_text

EVIDENCE_MARKER = '[untrusted Telegram evidence] '
UNAVAILABLE = 'reply draft is unavailable'
PREPARED = 'immutable reply draft prepared; inspect full evidence and obtain fresh approval'
PENDING = 'exact pending reply snapshot; inspection is not approval'
REVISED = 'new immutable reply revision; prior draft invalidated; fresh approval required'

# sending_id is random transport correlation, not a user-significant send option.
SEND_OPTIONS = {'@type':'messageSendOptions','suggested_post_info':None,
    'disable_notification':False,'from_background':False,'protect_content':False,
    'allow_paid_broadcast':False,'paid_message_star_count':0,
    'update_order_of_installed_sticker_sets':False,'scheduling_state':None,
    'effect_id':0,'only_preview':False}
LINK_OPTIONS = {'@type':'linkPreviewOptions','is_disabled':True,'url':'',
    'force_small_media':False,'force_large_media':False,'show_above_text':False}


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def strict_shape(value: object, expected: dict) -> bool:
    return (isinstance(value, dict) and set(value) == set(expected) and
            all(type(value[k]) is type(v) and value[k] == v for k,v in expected.items()))


def integer(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and -(2**53) < value < 2**53 and (value > 0 if positive else value != 0)


@dataclass(frozen=True)
class ReplySource:
    """Canonical serialized projection prevents nested mutable provider state escaping."""
    projection_json: str

    def __post_init__(self):
        self.target()

    def _validate_projection(self):
        if type(self.projection_json) is not str:
            raise ValueError('invalid reply projection')
        if len(self.projection_json)>65536 or len(self.projection_json.encode('utf-8'))>65536:
            raise ValueError('invalid reply projection')
        try:
            value=json.loads(self.projection_json)
        except RecursionError as error:
            raise ValueError('invalid reply projection') from error
        if (type(value) is not dict or set(value)!={'version','anchor','message'} or
                self.projection_json!=canonical(value) or type(value['version']) is not int or
                type(value['anchor']) is not dict or set(value['anchor'])!={'chat_id','message_id'} or
                type(value['message']) is not dict):
            raise ValueError('invalid reply projection')
        if value['version'] in (2,4,6,8,10):
            from .reply_media_sources import validate_content
            from .reply_formatted_sources import LEXICAL, IDENTITY
            content=value['message'].get('content')
            caption=content.get('caption') if type(content) is dict else None
            entities=caption.get('entities') if type(caption) is dict else None
            lexical=value['version']==6 or (value['version'] in (8,10) and type(entities) is list and any(
                type(e) is dict and type(e.get('type')) is dict and type(e['type'].get('@type')) is str
                and e['type']['@type'] in LEXICAL for e in entities))
            identity=value['version']==8 or (value['version']==10 and type(entities) is list and any(
                type(e) is dict and type(e.get('type')) is dict and type(e['type'].get('@type')) is str
                and e['type']['@type'] in IDENTITY for e in entities))
            validate_content(content,styled=value['version']==4,lexical=lexical,identity=identity,datetime=value['version']==10)
            surrogate={**value['message'],'content':{'@type':'messageText','text':{'@type':'formattedText','text':'validation','entities':[]}}}
            shell=_projection_from_message(surrogate,value['anchor']['chat_id'],value['anchor']['message_id'])['message']
            shell['content']=value['message']['content']
            if canonical(shell)!=canonical(value['message']):
                raise ValueError('invalid reply projection')
        elif value['version'] in (1,3,5,7,9):
            rebuilt=_projection_from_message(value['message'],value['anchor']['chat_id'],value['anchor']['message_id'])
            if canonical(rebuilt)!=self.projection_json:
                raise ValueError('invalid reply projection')
        else:
            raise ValueError('invalid reply projection')
        return value

    @classmethod
    def _validated(cls, value):
        result = object.__new__(cls)
        object.__setattr__(result, 'projection_json', canonical(value))
        return result

    @property
    def anchor(self) -> tuple[int, int]:
        value=json.loads(self.projection_json)['anchor']
        return value['chat_id'], value['message_id']

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.projection_json.encode('utf-8')).hexdigest()

    @property
    def is_media(self) -> bool:
        return json.loads(self.projection_json)['version'] in (2,4,6,8,10)

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        value=self._validate_projection()
        if value['version'] in (9,10):
            from .reply_formatted_sources import LEXICAL, IDENTITY
            entities=value['message']['content']['caption' if value['version']==10 else 'text']['entities']
            return (*(('reply_media_targets',) if value['version']==10 else ()),
                'reply_formatted_targets','reply_datetime_targets',
                *(('reply_lexical_targets',) if any(e['type']['@type'] in LEXICAL for e in entities) else ()),
                *(('reply_identity_targets',) if any(e['type']['@type'] in IDENTITY for e in entities) else ()))
        if value['version'] in (7,8):
            from .reply_formatted_sources import LEXICAL
            formatted=value['message']['content']['caption' if value['version']==8 else 'text']
            lexical=any(e['type']['@type'] in LEXICAL for e in formatted['entities'])
            return (*(('reply_media_targets',) if value['version']==8 else ()),
                'reply_formatted_targets','reply_identity_targets',*(('reply_lexical_targets',) if lexical else ()))
        return {1:(),2:('reply_media_targets',),3:('reply_formatted_targets',),
            4:('reply_media_targets','reply_formatted_targets'),
            5:('reply_formatted_targets','reply_lexical_targets'),
            6:('reply_media_targets','reply_formatted_targets','reply_lexical_targets')}[value['version']]

    @property
    def required_capability(self) -> str | None:
        capabilities=self.required_capabilities
        if len(capabilities)>1:
            raise ValueError('reply source requires multiple capabilities')
        return capabilities[0] if capabilities else None

    @property
    def raw_text(self) -> str:
        return json.loads(self.projection_json)['message']['content']['text']['text']

    def target(self) -> 'ReplyTarget':
        value=self._validate_projection()
        if value['version'] in (2,3,4,5,6,7,8,9,10):
            if value['version'] in (2,4,6,8,10):
                from .reply_media_sources import render_content
            else:
                from .reply_formatted_sources import render_content
            display,changed=render_content(value['message']['content'],EVIDENCE_MARKER,
                **({'datetime':True,'identity':'reply_identity_targets' in self.required_capabilities,
                    'lexical':'reply_lexical_targets' in self.required_capabilities} if value['version'] in (9,10) else
                   {'identity':True,'lexical':'reply_lexical_targets' in self.required_capabilities} if value['version'] in (7,8) else
                   {'styled':True} if value['version']==4 else {'lexical':True} if value['version'] in (5,6) else {}))
            return ReplyTarget(anchor=SelectedMessageAnchor(chat_id=self.anchor[0],message_id=self.anchor[1]),
                text=display,source_sha256=self.source_sha256,sanitized=changed,truncated=False)
        raw=self.raw_text
        # No truncation: expansion outside the full preview bound fails closed.
        safe=sanitize_telegram_text(raw, max_length=4096*18+1)
        if len(safe)>4096:
            raise ValueError('reply evidence exceeds preview bound')
        return ReplyTarget(anchor=SelectedMessageAnchor(chat_id=self.anchor[0],message_id=self.anchor[1]),
            text=EVIDENCE_MARKER+safe,source_sha256=self.source_sha256,sanitized=safe!=raw,truncated=False)


def _projection_from_message(raw: object, chat_id: int, message_id: int) -> dict:
    """Validate only the immutable relevant subset, never volatile whole-message hashes.

    TDLib omits nullable object fields in JSON. Required scalar safety fields must
    still be present and correctly typed; absence never becomes a safe zero.
    """
    if not integer(chat_id) or not integer(message_id,positive=True) or not isinstance(raw,dict):
        raise ValueError('reply source is unavailable')
    if (raw.get('@type')!='message' or type(raw.get('chat_id')) is not int or raw['chat_id']!=chat_id or
        type(raw.get('id')) is not int or raw['id']!=message_id or type(raw.get('is_outgoing')) is not bool):
        raise ValueError('reply source is unavailable')
    sender=raw.get('sender_id')
    if not isinstance(sender,dict) or type(sender.get('@type')) is not str:raise ValueError('reply source is unavailable')
    sender_key={'messageSenderUser':'user_id','messageSenderChat':'chat_id'}.get(sender.get('@type'))
    if (sender_key is None or set(sender)!= {'@type',sender_key} or
        not integer(sender.get(sender_key),positive=sender_key=='user_id')):
        raise ValueError('reply source is unavailable')
    for key in ('date','edit_date'):
        if type(raw.get(key)) is not int or not 0 <= raw[key] < 2**31:
            raise ValueError('reply source is unavailable')
    for key in ('self_destruct_in','auto_delete_in'):
        if type(raw.get(key)) not in {int,float} or raw[key]!=0:
            raise ValueError('reply source is unavailable')
    nulls=('sending_state','scheduling_state','topic_id','self_destruct_type','ephemeral_content',
           'receiver_id','reply_to','forward_info','import_info','reply_markup')
    if any(raw.get(key) is not None for key in nulls):raise ValueError('reply source is unavailable')
    if (type(raw.get('ephemeral_message_id')) is not int or raw['ephemeral_message_id']!=0 or
        type(raw.get('is_from_offline')) is not bool):
        raise ValueError('reply source is unavailable')
    content=raw.get('content')
    text='';link_options=None;entities=[];lexical=False;identity=False;datetime=False
    media=type(content) is dict and content.get('@type') in ('messageDocument','messagePhoto','messageVoiceNote')
    if media:
        from .reply_media_sources import media_content
        from .reply_formatted_sources import LEXICAL, IDENTITY, DATETIME
        caption=content.get('caption')
        if type(caption) is dict and type(caption.get('entities')) is list and len(caption['entities'])<=32:
            lexical=any(type(e) is dict and type(e.get('type')) is dict and
                type(e['type'].get('@type')) is str and e['type']['@type'] in LEXICAL
                for e in caption['entities'])
            identity=any(type(e) is dict and type(e.get('type')) is dict and
                type(e['type'].get('@type')) is str and e['type']['@type'] in IDENTITY
                for e in caption['entities'])
            datetime=any(type(e) is dict and type(e.get('type')) is dict and
                type(e['type'].get('@type')) is str and e['type']['@type'] in DATETIME
                for e in caption['entities'])
        projected_content=media_content(content,lexical=lexical,identity=identity,datetime=datetime)
    else:
        if (not isinstance(content,dict) or content.get('@type')!='messageText' or content.get('link_preview') is not None or
            set(content)-{'@type','text','link_preview','link_preview_options'}):
            raise ValueError('reply source is unavailable')
        link_options=content.get('link_preview_options')
        if link_options is not None and not strict_shape(link_options,LINK_OPTIONS):raise ValueError('reply source is unavailable')
        formatted=content.get('text')
        if (not isinstance(formatted,dict) or set(formatted)!= {'@type','text','entities'} or
            formatted.get('@type')!='formattedText' or type(formatted.get('text')) is not str or type(formatted.get('entities')) is not list):
            raise ValueError('reply source is unavailable')
        if formatted['entities']:
            if len(formatted['entities'])>32:
                raise ValueError('invalid formatted reply source')
            from .reply_formatted_sources import formatted_text, LEXICAL, IDENTITY, DATETIME
            lexical=any(type(e) is dict and type(e.get('type')) is dict and
                type(e['type'].get('@type')) is str and e['type']['@type'] in LEXICAL
                for e in formatted['entities'])
            identity=any(type(e) is dict and type(e.get('type')) is dict and
                type(e['type'].get('@type')) is str and e['type']['@type'] in IDENTITY
                for e in formatted['entities'])
            datetime=any(type(e) is dict and type(e.get('type')) is dict and
                type(e['type'].get('@type')) is str and e['type']['@type'] in DATETIME
                for e in formatted['entities'])
            entities=formatted_text(formatted,lexical=lexical,identity=identity,datetime=datetime)['entities']
        text=formatted['text']
        if (not 0<len(text)<=4096 or not text.strip() or
            any(unicodedata.category(c).startswith('C') and c not in '\n\t' for c in text)):
            raise ValueError('reply source is unavailable')
        text.encode('utf-8')
    message={'@type':'message','chat_id':chat_id,'id':message_id,'sender_id':dict(sender),
        'is_outgoing':raw['is_outgoing'],'is_from_offline':raw['is_from_offline'],
        'ephemeral_message_id':0,'date':raw['date'],'edit_date':raw['edit_date'],
        'self_destruct_in':0.0,'auto_delete_in':0.0,**{key:None for key in nulls},
        'content':{'@type':'messageText','text':{'@type':'formattedText','text':text,'entities':entities},
                   'link_preview':None,'link_preview_options':None if link_options is None else dict(link_options)}}
    if media:
        message['content']=projected_content
    return {'version':(10 if datetime else 8 if identity else 6 if lexical else 4 if projected_content['caption']['entities'] else 2) if media else 9 if datetime else 7 if identity else 5 if lexical else 3 if entities else 1,
        'anchor':{'chat_id':chat_id,'message_id':message_id},'message':message}


def source_from_message(raw: object, chat_id: int, message_id: int) -> ReplySource:
    source=ReplySource._validated(_projection_from_message(raw,chat_id,message_id))
    source.target() # validates raw canonical bytes and full display before registry mutation
    return source


class ReplyTarget(_Closed):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True,frozen=True)
    anchor: SelectedMessageAnchor
    text: Annotated[str,Field(strict=True,min_length=1,max_length=4096+len(EVIDENCE_MARKER))]
    source_sha256: Hash
    sanitized: Annotated[bool,Field(strict=True)]
    truncated: Annotated[bool,Field(strict=True,json_schema_extra={'const':False})]=False

    @model_validator(mode='after')
    def evidence(self):
        if self.truncated or not self.text.startswith(EVIDENCE_MARKER):raise ValueError('full marked reply evidence required')
        value=self.text[len(EVIDENCE_MARKER):]
        if not value or sanitize_telegram_text(value,max_length=4097)!=value:
            raise ValueError('reply display is not sanitized full evidence')
        return self


def preview_facts(draft: DraftPreview, target: ReplyTarget) -> dict:
    return {'version':1,'draft':draft.model_dump(mode='json'),'reply_target':target.model_dump(mode='json'),
            'topic_id':None,'outgoing_entities':[],'send_options':dict(SEND_OPTIONS),
            'link_preview_options':dict(LINK_OPTIONS),'clear_draft':False,'reply_markup':None,
            'reply_to':{'@type':'inputMessageReplyToMessage','message_id':target.anchor.message_id,
                        'quote':None,'checklist_task_id':0,'poll_option_id':''}}


class ReplyDraftPreview(_Closed):
    draft: DraftPreview
    reply_target: ReplyTarget
    preview_sha256: Hash

    @model_validator(mode='after')
    def exact(self):
        from .outgoing_drafts import _safe_message_text
        if self.draft.text is None or _safe_message_text(self.draft.text)!=self.draft.text:
            raise ValueError('reply text must be final normalized plain text')
        if (self.draft.kind!='text' or self.draft.recipient!=self.reply_target.anchor.chat_id or
            digest(preview_facts(self.draft,self.reply_target))!=self.preview_sha256):
            raise ValueError('reply preview facts disagree')
        return self

    @classmethod
    def create(cls, draft: DraftPreview, source: ReplySource):
        target=source.target()
        return cls(draft=draft,reply_target=target,preview_sha256=digest(preview_facts(draft,target)))


class PrepareReplyTextSendRequest(_Closed):
    recipient: ChatId
    text: DraftText
    reply_to: SelectedMessageAnchor

class GetReplyDraftRequest(_Closed):
    draft_id: DraftId

class UpdateReplyDraftRequest(GetReplyDraftRequest):
    text: DraftText

class RefreshReplyDraftRequest(GetReplyDraftRequest):
    pass

class PrepareReplyTextSendResponse(_Closed):
    status: Literal['prepared','unavailable']
    reply: ReplyDraftPreview | None=None
    detail: Literal['immutable reply draft prepared; inspect full evidence and obtain fresh approval','reply draft is unavailable']
    @model_validator(mode='after')
    def shape(self):
        if (self.status=='prepared')!=(self.reply is not None) or self.detail!=(PREPARED if self.status=='prepared' else UNAVAILABLE):
            raise ValueError('invalid reply preparation response')
        return self

class GetReplyDraftResponse(_Closed):
    status: Literal['pending','unavailable']
    reply: ReplyDraftPreview | None=None
    detail: Literal['exact pending reply snapshot; inspection is not approval','reply draft is unavailable']
    @model_validator(mode='after')
    def shape(self):
        if (self.status=='pending')!=(self.reply is not None) or self.detail!=(PENDING if self.status=='pending' else UNAVAILABLE):
            raise ValueError('invalid reply inspection response')
        return self

class ReviseReplyDraftResponse(_Closed):
    status: Literal['revised','unavailable']
    previous_draft_id: DraftId | None=None
    reply: ReplyDraftPreview | None=None
    detail: Literal['new immutable reply revision; prior draft invalidated; fresh approval required','reply draft is unavailable']
    @model_validator(mode='after')
    def shape(self):
        if self.status=='revised':
            if self.reply is None or self.previous_draft_id is None or self.reply.draft.draft_id==self.previous_draft_id or self.detail!=REVISED:
                raise ValueError('invalid reply revision response')
        elif self.reply is not None or self.previous_draft_id is not None or self.detail!=UNAVAILABLE:
            raise ValueError('unavailable reply revision contains evidence')
        return self
