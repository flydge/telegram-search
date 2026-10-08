"""Bounded rehydration of selected anchors; no history traversal or persistent text."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .sanitize import sanitize_telegram_text
from .schemas import (
    EvidenceAnchor, MessageReadResult, ReadMessagesRequest, ReadMessagesResponse,
    SelectedMessage, SelectedMessageReply, SelectedMessageSender, SelectedMessageText,
    SelectedMessageTopic, SourceEvidence,
)
from .tdjson import AuthorizationBlocked, MessageNotFound, SecretChatRejected, TDLibDeadlineExceeded, TDLibError

_CONTENT = {
    "messageText": ("text", "text"), "messageDocument": ("document", "caption"),
    "messagePhoto": ("photo", "caption"), "messageVideo": ("video", "caption"),
    "messageAudio": ("audio", "caption"), "messageAnimation": ("animation", "caption"),
    "messageVoiceNote": ("voice_note", "caption"), "messageVideoNote": ("video_note", "none"),
    "messageSticker": ("sticker", "none"),
}
_TOPICS = {
    "messageTopicThread": ("thread", "message_thread_id"),
    "messageTopicForum": ("forum", "forum_topic_id"),
    "messageTopicDirectMessages": ("direct_messages", "direct_messages_chat_topic_id"),
    "messageTopicSavedMessages": ("saved_messages", "saved_messages_topic_id"),
}


def _integer(value: object, *, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid provider integer")
    return value


def _text(raw: str, limit: int) -> SelectedMessageText:
    # Bound work before Unicode normalization; it may expand a scalar into up to 18.
    prefix = raw[:65536]
    safe = sanitize_telegram_text(prefix, max_length=max(1, len(prefix) * 18 + 1))
    return SelectedMessageText(value=safe[:limit], sanitized=safe != prefix,
                               truncated=len(raw) > len(prefix) or len(safe) > limit,
                               original_characters=len(raw))


def terminal_read(request: ReadMessagesRequest, status: str, issue: str) -> ReadMessagesResponse:
    return ReadMessagesResponse(status=status, coverage_complete=False,
        results=[MessageReadResult(anchor=a, status=status, coverage_complete=False, issues=[issue])
                 for a in request.anchors])


def _reply(raw: object, issues: list[str]) -> SelectedMessageReply | None:
    if raw is None:
        return None
    try:
        if not isinstance(raw, dict):
            raise ValueError
        if raw.get("@type") == "messageReplyToMessage":
            chat_id = _integer(raw.get("chat_id"), minimum=-(2**53 - 1))
            message_id = _integer(raw.get("message_id"))
            if not chat_id or not message_id:
                issues.append("reply_unavailable")
            return SelectedMessageReply(kind="message", chat_id=chat_id or None, message_id=message_id or None)
        if raw.get("@type") == "messageReplyToStory":
            chat_id = _integer(raw.get("story_poster_chat_id"), minimum=-(2**53 - 1))
            if not chat_id:
                raise ValueError
            return SelectedMessageReply(kind="story", chat_id=chat_id,
                                        story_id=_integer(raw.get("story_id"), minimum=1, maximum=2**31 - 1))
    except ValueError:
        pass
    issues.append("reply_unavailable")
    return None


def _topic(raw: object, issues: list[str]) -> SelectedMessageTopic | None:
    if raw is None:
        return None
    try:
        if not isinstance(raw, dict) or raw.get("@type") not in _TOPICS:
            raise ValueError
        kind, key = _TOPICS[raw["@type"]]
        value = _integer(raw.get(key), minimum=-(2**53 - 1) if kind in {"saved_messages", "direct_messages"} else 1,
                         maximum=2**31 - 1 if kind == "forum" else 2**53 - 1)
        return SelectedMessageTopic(kind=kind, id=value)
    except ValueError:
        issues.append("topic_unavailable")
        return None


def _album(raw: object) -> str | None:
    if type(raw) is int:
        value = raw
    elif isinstance(raw, str) and len(raw) <= 20 and raw.lstrip("-").isascii() and raw.lstrip("-").isdigit():
        value = int(raw)
    else:
        raise ValueError("invalid provider album")
    if not -(2**63) <= value <= 2**63 - 1:
        raise ValueError("invalid provider album")
    return str(value) if value else None


def _message(client: Any, raw: dict, anchor, text_budget: int) -> MessageReadResult:
    if raw.get("sending_state") is not None or raw.get("ephemeral_content") is not None:
        return MessageReadResult(anchor=anchor, status="unsupported", coverage_complete=False, issues=["unsupported_content"])
    content = raw.get("content")
    if not isinstance(content, dict) or content.get("@type") not in _CONTENT:
        return MessageReadResult(anchor=anchor, status="unsupported", coverage_complete=False, issues=["unsupported_content"])
    kind, role = _CONTENT[content["@type"]]
    issues: list[str] = []
    text = None
    if role != "none":
        formatted = content.get(role)
        if not isinstance(formatted, dict) or not isinstance(formatted.get("text"), str):
            raise ValueError("invalid provider text")
        text = _text(formatted["text"], min(20000, text_budget))
        if text.truncated:
            issues.append("text_truncated")
    sender = raw.get("sender_id")
    if not isinstance(sender, dict) or sender.get("@type") not in {"messageSenderUser", "messageSenderChat"}:
        raise ValueError("invalid provider sender")
    sender_kind = "user" if sender["@type"] == "messageSenderUser" else "chat"
    sender_id = _integer(sender.get("user_id" if sender_kind == "user" else "chat_id"),
                         minimum=1 if sender_kind == "user" else -(2**53 - 1))
    if not sender_id:
        raise ValueError("invalid provider sender")
    identity = SelectedMessageSender(kind=sender_kind, id=sender_id, display_name=None)
    try:
        hydrated = client.get_sender_identity(sender)
        if (not isinstance(hydrated, dict) or hydrated.get("kind") != sender_kind or type(hydrated.get("id")) is not int or
                hydrated["id"] != sender_id or not isinstance(hydrated.get("display_name"), str)):
            raise TDLibError("invalid provider sender")
        name = _text(hydrated["display_name"], 256)
        identity.display_name = name.value
        identity.display_name_sanitized = name.sanitized
        identity.display_name_truncated = name.truncated
        if name.truncated:
            issues.append("sender_truncated")
    except (TDLibDeadlineExceeded, AuthorizationBlocked):
        raise
    except (TDLibError, TimeoutError):
        issues.append("sender_unavailable")
    outgoing = raw.get("is_outgoing")
    if type(outgoing) is not bool:
        raise ValueError("invalid provider direction")
    date = _integer(raw.get("date"), maximum=2**31 - 1)
    edited = _integer(raw.get("edit_date"), maximum=2**31 - 1)
    # TDLib omits nullable custom objects in native JSON.
    reply = _reply(raw.get("reply_to"), issues)
    topic = _topic(raw.get("topic_id"), issues)
    selected = SelectedMessage(content_kind=kind, text_role=role, text=text, sender=identity,
        is_outgoing=outgoing, date_utc=datetime.fromtimestamp(date, tz=timezone.utc) if date else None,
        edit_date_utc=datetime.fromtimestamp(edited, tz=timezone.utc) if edited else None,
        is_edited=bool(edited), reply_to=reply, topic=topic, media_album_id=_album(raw.get("media_album_id")),
        source=SourceEvidence(evidence_anchor=EvidenceAnchor(**anchor.model_dump())))
    return MessageReadResult(anchor=anchor, status="partial" if issues else "complete",
                             coverage_complete=not issues, message=selected, issues=issues)


def read_messages(client: Any, request: ReadMessagesRequest, *, deadline: float | None = None) -> ReadMessagesResponse:
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
    results = []
    chats = set()
    text_budget = 100000
    with client.request_budget(deadline):
        try:
            client.ensure_ready()
        except AuthorizationBlocked:
            return terminal_read(request, "blocked", "authorization_unavailable")
        except (TDLibError, TimeoutError, OSError):
            return terminal_read(request, "error", "provider_error")
        for anchor in request.anchors:
            status, issue = "error", "provider_error"
            if time.monotonic() >= deadline or text_budget <= 0:
                results.append(MessageReadResult(anchor=anchor, status="partial", coverage_complete=False,
                                                 issues=["budget_exhausted"]))
                continue
            try:
                if anchor.chat_id not in chats:
                    chat = client.resolve_target(anchor.chat_id)
                    if type(chat.get("id")) is not int or chat["id"] != anchor.chat_id:
                        status, issue = "wrong_chat", "chat_mismatch"
                        raise ValueError
                    if (chat.get("type") or {}).get("@type") == "chatTypeSecret":
                        raise SecretChatRejected("secret chat")
                    chats.add(anchor.chat_id)
                raw = client.get_message(anchor.chat_id, anchor.message_id)
                if not isinstance(raw, dict) or raw.get("@type") != "message":
                    raise ValueError
                if type(raw.get("chat_id")) is not int or raw["chat_id"] != anchor.chat_id:
                    status, issue = "wrong_chat", "chat_mismatch"
                    raise ValueError
                if type(raw.get("id")) is not int or raw["id"] != anchor.message_id:
                    issue = "message_mismatch"
                    raise ValueError
                result = _message(client, raw, anchor, text_budget)
                if result.message and result.message.text:
                    text_budget -= len(result.message.text.value)
                results.append(result)
                continue
            except AuthorizationBlocked:
                status, issue = "blocked", "authorization_unavailable"
            except SecretChatRejected:
                status, issue = "unsupported", "secret_chat"
            except MessageNotFound:
                status, issue = "not_found", "message_unavailable"
            except TDLibDeadlineExceeded:
                status, issue = "partial", "budget_exhausted"
            except (TDLibError, TimeoutError, OSError):
                pass
            except (ValueError, TypeError, OverflowError):
                if issue == "provider_error":
                    issue = "invalid_provider_message"
            results.append(MessageReadResult(anchor=anchor, status=status, coverage_complete=False, issues=[issue]))
    complete = all(r.coverage_complete for r in results)
    return ReadMessagesResponse(status="complete" if complete else "partial", coverage_complete=complete, results=results)
