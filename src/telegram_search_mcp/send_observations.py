"""Bounded volatile content-free exact TDLib send observations.

No payload, text, caption, path, or provider error string is retained. A snapshot
is evidence for one provider instance, never a lease or permission to retry.
"""
from __future__ import annotations
import base64
import hashlib
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable
from .reply_drafts import LINK_OPTIONS, strict_shape

RETENTION_SECONDS = 900
MAX_OBSERVATIONS = 4096

class ObservationUnavailable(RuntimeError):
    pass

@dataclass(frozen=True)
class SendObservation:
    attempt_id: str
    extra: str
    sending_id: int
    chat_id: int
    content_type: str
    started_at: float
    text_sha256: str | None = None
    temporary_id: int | None = None
    status: str = "registered"
    message_id: int | None = None
    reply_anchor: tuple[int, int] | None = None
    caption_sha256: str | None = None
    voice_duration: int | None = None
    waveform_sha256: str | None = None
    waveform_size: int | None = None


def integer(value, *, positive=False):
    return type(value) is int and -(2**53) < value < 2**53 and (value > 0 if positive else value != 0)


def _shape(value,kind,required,nullable=()):
    return (isinstance(value,dict) and value.get('@type')==kind
        and set(value)-{'@type',*required,*nullable}==set()
        and {'@type',*required}<=set(value))

def _number(value,maximum=2**53-1,minimum=0):
    return type(value) is int and minimum<=value<=maximum

def _string(value,maximum=65536):
    if type(value) is not str or len(value)>maximum:return False
    try:return len(value.encode('utf-8'))<=maximum
    except UnicodeError:return False

def _blob(value,maximum=65536):
    if not _string(value,maximum*2):return None
    try:
        raw=base64.b64decode(value,validate=True)
        return raw if len(raw)<=maximum else None
    except ValueError:return None

def _file(value):
    if not _shape(value,'file',('id','size','expected_size','local','remote')):return False
    if not (_number(value['id'],2**31-1,1) and _number(value['size']) and _number(value['expected_size'])):return False
    local=value['local'];remote=value['remote']
    if not _shape(local,'localFile',('path','can_be_downloaded','can_be_deleted','is_downloading_active','is_downloading_completed','download_offset','downloaded_prefix_size','downloaded_size')):return False
    if not (_string(local['path'],4096) and all(type(local[k]) is bool for k in ('can_be_downloaded','can_be_deleted','is_downloading_active','is_downloading_completed'))
        and all(_number(local[k]) for k in ('download_offset','downloaded_prefix_size','downloaded_size'))):return False
    return (_shape(remote,'remoteFile',('id','unique_id','is_uploading_active','is_uploading_completed','uploaded_size'))
        and _string(remote['id'],4096) and _string(remote['unique_id'],4096)
        and type(remote['is_uploading_active']) is bool and type(remote['is_uploading_completed']) is bool
        and _number(remote['uploaded_size']))

def _minithumbnail(value):
    return (value is None or (_shape(value,'minithumbnail',('width','height','data'))
        and _number(value['width'],16384,1) and _number(value['height'],16384,1) and _blob(value['data']) is not None))

def _thumbnail(value):
    if value is None:return True
    if not _shape(value,'thumbnail',('format','width','height','file')):return False
    fmt=value['format']
    return (isinstance(fmt,dict) and set(fmt)=={'@type'} and type(fmt['@type']) is str and fmt['@type'] in {
        'thumbnailFormatJpeg','thumbnailFormatGif','thumbnailFormatMpeg4','thumbnailFormatPng','thumbnailFormatTgs','thumbnailFormatWebm','thumbnailFormatWebp'}
        and _number(value['width'],16384,1) and _number(value['height'],16384,1) and _file(value['file']))

def _artifact_matches(content,observation):
    caption=content.get('caption')
    if not (_shape(caption,'formattedText',('text','entities')) and type(caption['text']) is str
        and len(caption['text'])<=1024 and caption['entities']==[]):return False
    try:
        if hashlib.sha256(caption['text'].encode('utf-8')).hexdigest()!=observation.caption_sha256:return False
    except UnicodeError:return False
    if observation.content_type=='messageDocument':
        if not _shape(content,'messageDocument',('document','caption')):return False
        doc=content['document']
        return (_shape(doc,'document',('file_name','mime_type','document'),('minithumbnail','thumbnail'))
            and _string(doc['file_name'],255) and _string(doc['mime_type'],127)
            and _minithumbnail(doc.get('minithumbnail')) and _thumbnail(doc.get('thumbnail')) and _file(doc['document']))
    if observation.content_type=='messagePhoto':
        if not (_shape(content,'messagePhoto',('photo','caption','show_caption_above_media','has_spoiler','is_secret'),('video',))
            and content.get('video') is None and all(content[k] is False for k in ('show_caption_above_media','has_spoiler','is_secret'))):return False
        photo=content['photo']
        if not (_shape(photo,'photo',('has_stickers','sizes'),('minithumbnail',)) and photo['has_stickers'] is False
            and _minithumbnail(photo.get('minithumbnail')) and type(photo['sizes']) is list and 1<=len(photo['sizes'])<=20):return False
        for size in photo['sizes']:
            if not (_shape(size,'photoSize',('type','photo','width','height','progressive_sizes'))
                and _string(size['type'],16) and _number(size['width'],16384,1) and _number(size['height'],16384,1)
                and type(size['progressive_sizes']) is list and len(size['progressive_sizes'])<=100
                and all(_number(n,2**31-1) for n in size['progressive_sizes']) and _file(size['photo'])):return False
        return True
    if not (_shape(content,'messageVoiceNote',('voice_note','caption','is_listened')) and type(content['is_listened']) is bool):return False
    voice=content['voice_note']
    if not (_shape(voice,'voiceNote',('duration','waveform','mime_type','voice'),('speech_recognition_result',))
        and _number(voice['duration'],600,1) and voice['duration']==observation.voice_duration
        and voice['mime_type']=='audio/ogg' and voice.get('speech_recognition_result') is None and _file(voice['voice'])):return False
    waveform=_blob(voice['waveform'],100)
    return (waveform is not None and len(waveform)==observation.waveform_size
        and hashlib.sha256(waveform).hexdigest()==observation.waveform_sha256)

class SendObservations:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 capacity: int = MAX_OBSERVATIONS):
        if type(capacity) is not int or not 1 <= capacity <= MAX_OBSERVATIONS:
            raise ValueError("invalid observation capacity")
        self._clock = clock
        self._capacity = capacity
        self._entries: dict[str, SendObservation] = {}
        self._lock = threading.Lock()

    def _prune(self):
        now = self._clock()
        self._entries = {key: value for key,value in self._entries.items()
                         if now - value.started_at < RETENTION_SECONDS}

    def invalidate(self):
        with self._lock:
            self._entries.clear()

    def register(self, attempt_id, extra, sending_id, chat_id, content_type, *, text_sha256=None, reply_anchor=None, caption_sha256=None, voice_duration=None, waveform_sha256=None, waveform_size=None):
        if reply_anchor is not None and (type(reply_anchor) is not tuple or len(reply_anchor)!=2 or
                not integer(reply_anchor[0]) or reply_anchor[0]!=chat_id or not integer(reply_anchor[1],positive=True)):
            raise ObservationUnavailable("invalid reply observation")
        if caption_sha256 is not None:
            if (reply_anchor is None or content_type not in {'messageDocument','messagePhoto','messageVoiceNote'}
                or type(caption_sha256) is not str or len(caption_sha256)!=64
                or any(c not in '0123456789abcdef' for c in caption_sha256)):
                raise ObservationUnavailable('invalid artifact reply expectation')
            if content_type=='messageVoiceNote' and not (_number(voice_duration,600,1)
                and _number(waveform_size,100,1) and type(waveform_sha256) is str and len(waveform_sha256)==64
                and all(c in '0123456789abcdef' for c in waveform_sha256)):
                raise ObservationUnavailable('invalid voice reply expectation')
        with self._lock:
            self._prune()
            if (attempt_id in self._entries or len(self._entries) >= self._capacity or
                    any(v.sending_id == sending_id or v.extra == extra for v in self._entries.values())):
                raise ObservationUnavailable("send observation capacity or correlation is unavailable")
            self._entries[attempt_id] = SendObservation(attempt_id,extra,sending_id,chat_id,content_type,self._clock(),text_sha256,reply_anchor=reply_anchor,caption_sha256=caption_sha256,voice_duration=voice_duration,waveform_sha256=waveform_sha256,waveform_size=waveform_size)

    def discard_unattempted(self, attempt_id):
        """Remove a registration refused before transport under the provider lock.

        Never remove pending or terminal provider evidence. This is only for the
        local gap between registering correlation and attempting rawsend.
        """
        with self._lock:
            observation=self._entries.get(attempt_id)
            if observation is not None and observation.status=="registered":
                self._entries.pop(attempt_id)

    def sending_id_available(self, sending_id):
        with self._lock:
            self._prune()
            return not any(v.sending_id == sending_id for v in self._entries.values())

    def snapshot(self, attempt_id):
        with self._lock:
            self._prune()
            return self._entries.get(attempt_id)

    @staticmethod
    def _text_matches(content, expected_sha256):
        if expected_sha256 is None:
            return True
        formatted = content.get("text")
        if not isinstance(formatted, dict) or formatted.get("@type") != "formattedText":
            return False
        text = formatted.get("text")
        if not isinstance(text, str) or len(text) > 4096:
            return False
        try:
            encoded = text.encode("utf-8")
        except UnicodeError:
            return False
        return hashlib.sha256(encoded).hexdigest() == expected_sha256

    @staticmethod
    def _reply_matches(value, anchor):
        if anchor is None:
            return True
        reply=value.get("reply_to")
        return (value.get("topic_id") is None and isinstance(reply,dict)
            and not set(reply)-{"@type","chat_id","message_id","quote","checklist_task_id","poll_option_id","origin","origin_send_date","content"}
            and reply.get("@type")=="messageReplyToMessage"
            and type(reply.get("chat_id")) is int and reply["chat_id"]==anchor[0]
            and type(reply.get("message_id")) is int and reply["message_id"]==anchor[1]
            and type(reply.get("checklist_task_id")) is int and reply["checklist_task_id"]==0
            and type(reply.get("poll_option_id")) is str and reply["poll_option_id"]==""
            and type(reply.get("origin_send_date")) is int and reply["origin_send_date"]==0
            and all(reply.get(k) is None for k in ("quote","origin","content")))

    @staticmethod
    def _message(value, observation, *, final=False):
        return (isinstance(value,dict) and value.get("@type") == "message"
            and integer(value.get("chat_id")) and value["chat_id"] == observation.chat_id
            and integer(value.get("id"),positive=final) and value.get("is_outgoing") is True
            and isinstance(value.get("content"),dict)
            and value["content"].get("@type") == observation.content_type
            and SendObservations._text_matches(value["content"], observation.text_sha256)
            and SendObservations._reply_matches(value, observation.reply_anchor)
            and (observation.caption_sha256 is None or _artifact_matches(value['content'],observation))
            and (observation.reply_anchor is None or observation.caption_sha256 is not None or (
                isinstance(value["content"].get("text"),dict)
                and set(value["content"]["text"])=={"@type","text","entities"}
                and value["content"]["text"].get("entities")==[]
                and value["content"].get("link_preview") is None
                and (value["content"].get("link_preview_options") is None or
                    strict_shape(value["content"]["link_preview_options"],LINK_OPTIONS))))
            and (not final or value.get("sending_state") is None))

    def reduce(self, event):
        if not isinstance(event,dict):
            return
        with self._lock:
            self._prune()
            for key,old in list(self._entries.items()):
                if old.status in {"sent","failed"}:
                    continue
                updated = old
                kind = event.get("@type")
                if event.get("@extra") == old.extra:
                    if kind == "error" and type(event.get("code")) is int and 0 < event["code"] < 2**31:
                        updated = replace(old,status="failed")
                    elif self._message(event,old):
                        state = event.get("sending_state")
                        if (isinstance(state,dict) and state.get("@type") == "messageSendingStatePending"
                                and type(state.get("sending_id")) is int and state["sending_id"] == old.sending_id):
                            if old.temporary_id is None or old.temporary_id == event["id"]:
                                updated = replace(old,temporary_id=event["id"],status="pending")
                elif kind == "updateNewMessage":
                    value = event.get("message")
                    if self._message(value,old):
                        state = value.get("sending_state")
                        if (isinstance(state,dict) and state.get("@type") == "messageSendingStatePending"
                                and type(state.get("sending_id")) is int and state["sending_id"] == old.sending_id
                                and (old.temporary_id is None or old.temporary_id == value["id"])):
                            updated = replace(old,temporary_id=value["id"],status="pending")
                elif (old.temporary_id is not None and integer(event.get("old_message_id"))
                        and event["old_message_id"] == old.temporary_id):
                    value = event.get("message")
                    if kind == "updateMessageSendSucceeded" and self._message(value,old,final=True):
                        updated = replace(old,status="sent",message_id=value["id"])
                    elif kind == "updateMessageSendFailed" and self._message(value,old):
                        error = event.get("error")
                        state = value.get("sending_state")
                        inner = state.get("error") if isinstance(state,dict) else None
                        if (isinstance(error,dict) and error.get("@type") == "error"
                                and type(error.get("code")) is int and 0 < error["code"] < 2**31
                                and isinstance(state,dict) and state.get("@type") == "messageSendingStateFailed"
                                and isinstance(inner,dict) and inner.get("@type") == "error"
                                and type(inner.get("code")) is int and inner["code"] == error["code"]):
                            updated = replace(old,status="failed")
                self._entries[key] = updated
