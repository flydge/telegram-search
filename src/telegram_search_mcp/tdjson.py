"""Narrow legacy tdjson ABI wrapper and read-only TDLib client."""

from __future__ import annotations

import ctypes
import base64
import binascii
import hashlib
import itertools
import json
import os
import platform
import secrets
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .config import TDLIB_SESSION_DIRECTORY, ensure_private_directory, validate_tdlib_runtime
from .discovery_state import CatalogPosition, ChatListName
from .keychain import ApiCredentials, read_api_credentials
from .outgoing_stage import STAGING_ROOT
from .reply_drafts import ReplySource, source_from_message, LINK_OPTIONS, SEND_OPTIONS, strict_shape

_ALLOWED_REQUEST_TYPES = frozenset(
    {
        "getAuthorizationState",
        "setTdlibParameters",
        "checkDatabaseEncryptionKey",
        "getMe",
        "createPrivateChat",
        "searchPublicChat",
        "searchChats",
        "searchChatsOnServer",
        "getChats",
        "loadChats",
        "getChat",
        "getSupergroup",
        "getForumTopics",
        "getForumTopic",
        "getForumTopicHistory",
        "searchMessages",
        "searchChatMessages",
        "getMessage",
        "getMessageProperties",
        "getChatHistory",
        "getMessageLink",
        "getUser",
        "downloadFile",
        "getFile",
        "cancelDownloadFile",
        "sendMessage",
    }
)

_AUTHORIZATION_CONTROL_TYPES = frozenset(
    {"getAuthorizationState", "setTdlibParameters", "checkDatabaseEncryptionKey"}
)
_CHAT_MEDIA_FILTERS = {
    "video": {"@type": "searchMessagesFilterVideo"},
    "video_note": {"@type": "searchMessagesFilterVideoNote"},
}


def _is_strict_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_nonzero_integer(value: object) -> bool:
    return _is_strict_integer(value) and value != 0


def _is_integer_in_range(value: object, minimum: int, maximum: int) -> bool:
    return _is_strict_integer(value) and minimum <= value <= maximum


def _is_forum_chat_id(value: object) -> bool:
    return _is_integer_in_range(value, -(2**53 - 1), 2**53 - 1) and value != 0


def _valid_forum_offsets(date: object, message_id: object, topic_id: object) -> bool:
    return (
        _is_integer_in_range(date, 0, 2**31 - 1)
        and _is_integer_in_range(message_id, 0, 2**53 - 1)
        and _is_integer_in_range(topic_id, 0, 2**31 - 1)
    )


def _valid_voice_waveform(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 84:
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 63
    except (binascii.Error, ValueError):
        return False


def _valid_staged_input_file(value: object) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"@type", "path"}
        and value.get("@type") == "inputFileLocal" and isinstance(value.get("path"), str)
        and Path(value["path"]).parent.parent == STAGING_ROOT
        and Path(value["path"]).parent.name.startswith("draft_")
    )


def _parse_wire_int64(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    digits = value[1:] if value.startswith("-") else value
    if not digits or not digits.isascii() or not digits.isdigit():
        return None
    if len(digits) > 19 or (len(digits) > 1 and digits.startswith("0")):
        return None
    if value == "-0":
        return None
    parsed = int(value)
    return parsed if -(1 << 63) <= parsed <= (1 << 63) - 1 else None


def _chat_list_payload(chat_list: object) -> dict[str, str]:
    if chat_list == "main":
        return {"@type": "chatListMain"}
    if chat_list == "archive":
        return {"@type": "chatListArchive"}
    raise ValueError("chat_list must be main or archive")


def _is_search_sender(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if value.get("@type") == "messageSenderUser":
        return set(value) == {"@type", "user_id"} and _is_integer_in_range(value.get("user_id"), 1, 2**53 - 1)
    if value.get("@type") == "messageSenderChat":
        return set(value) == {"@type", "chat_id"} and _is_forum_chat_id(value.get("chat_id"))
    return False


def _is_search_topic(value: object) -> bool:
    return value is None or (isinstance(value, dict) and set(value) == {"@type", "forum_topic_id"}
        and value.get("@type") == "messageTopicForum"
        and _is_integer_in_range(value.get("forum_topic_id"), 1, 2**31 - 1))


def _validate_bounded_request(payload: dict[str, Any]) -> None:
    request_type = payload.get("@type")
    if request_type == "getForumTopicHistory" and (
        set(payload) != {"@type", "chat_id", "forum_topic_id", "from_message_id", "offset", "limit"}
        or not _is_forum_chat_id(payload.get("chat_id"))
        or not _is_integer_in_range(payload.get("forum_topic_id"), 1, 2**31 - 1)
        or not _is_integer_in_range(payload.get("from_message_id"), 0, 2**53 - 1)
        or type(payload.get("offset")) is not int or payload["offset"] != 0
        or not _is_integer_in_range(payload.get("limit"), 1, 20)
    ):
        raise ForbiddenTDLibRequest("TDLib forum history request shape is not allowed")
    if request_type == "getSupergroup" and (
        set(payload) != {"@type", "supergroup_id"}
        or not _is_integer_in_range(payload.get("supergroup_id"), 1, 2**53 - 1)
    ):
        raise ForbiddenTDLibRequest("TDLib forum request shape is not allowed")
    if request_type == "getForumTopic" and (
        set(payload) != {"@type", "chat_id", "forum_topic_id"}
        or not _is_forum_chat_id(payload.get("chat_id"))
        or not _is_integer_in_range(payload.get("forum_topic_id"), 1, 2**31 - 1)
    ):
        raise ForbiddenTDLibRequest("TDLib forum request shape is not allowed")
    if request_type == "getForumTopics" and (
        set(payload) != {
            "@type", "chat_id", "query", "offset_date", "offset_message_id",
            "offset_forum_topic_id", "limit",
        }
        or not _is_forum_chat_id(payload.get("chat_id"))
        or payload.get("query") != ""
        or not _valid_forum_offsets(
            payload.get("offset_date"), payload.get("offset_message_id"),
            payload.get("offset_forum_topic_id"),
        )
        or not _is_integer_in_range(payload.get("limit"), 1, 20)
    ):
        raise ForbiddenTDLibRequest("TDLib forum request shape is not allowed")
    if request_type == "getMessageProperties" and (
        set(payload)!={"@type","chat_id","message_id"}
        or not _is_forum_chat_id(payload.get("chat_id"))
        or not _is_integer_in_range(payload.get("message_id"),1,2**53-1)
    ):
        raise ForbiddenTDLibRequest("TDLib message properties request shape is not allowed")
    if request_type == "sendMessage":
        content = payload.get("input_message_content")
        document = content.get("document") if isinstance(content, dict) else None
        caption = content.get("caption") if isinstance(content, dict) else None
        options = payload.get("options")
        sending_id = options.get("sending_id") if isinstance(options, dict) else None
        expected_options = {
            "@type": "messageSendOptions", "suggested_post_info": None,
            "disable_notification": False, "from_background": False,
            "protect_content": False, "allow_paid_broadcast": False,
            "paid_message_star_count": 0,
            "update_order_of_installed_sticker_sets": False,
            "scheduling_state": None, "effect_id": 0,
            "sending_id": sending_id, "only_preview": False,
        }
        valid_text = (
            isinstance(content, dict)
            and set(content) == {"@type", "text", "link_preview_options", "clear_draft"}
            and content.get("@type") == "inputMessageText"
            and content.get("link_preview_options") == {
                "@type": "linkPreviewOptions", "is_disabled": True, "url": "",
                "force_small_media": False, "force_large_media": False,
                "show_above_text": False,
            }
            and content.get("clear_draft") is False
            and isinstance(content.get("text"), dict)
            and set(content["text"]) == {"@type", "text", "entities"}
            and content["text"].get("@type") == "formattedText"
            and isinstance(content["text"].get("text"), str)
            and 0 < len(content["text"]["text"]) <= 4096
            and content["text"].get("entities") == []
        )
        reply_to=payload.get("reply_to")
        valid_reply=(reply_to is None or (isinstance(reply_to,dict)
            and strict_shape(reply_to,{"@type":"inputMessageReplyToMessage",
                "message_id":reply_to.get("message_id"),"quote":None,"checklist_task_id":0,"poll_option_id":""})
            and _is_integer_in_range(reply_to.get("message_id"),1,2**53-1)))
        valid_document = (
            isinstance(content, dict) and set(content) == {"@type", "document", "caption"}
            and content.get("@type") == "inputMessageDocument"
            and isinstance(document, dict)
            and set(document) == {"@type", "document", "thumbnail", "disable_content_type_detection"}
            and document.get("@type") == "inputDocument"
            and document.get("thumbnail") is None
            and document.get("disable_content_type_detection") is True
            and _valid_staged_input_file(document.get("document"))
            and isinstance(caption, dict) and caption.get("@type") == "formattedText"
            and set(caption) == {"@type", "text", "entities"}
            and isinstance(caption.get("text"), str) and len(caption["text"]) <= 1024
            and caption.get("entities") == []
        )
        photo = content.get("photo") if isinstance(content, dict) else None
        valid_photo = (
            isinstance(content, dict)
            and set(content) == {"@type", "photo", "caption", "show_caption_above_media", "self_destruct_type", "has_spoiler"}
            and content.get("@type") == "inputMessagePhoto"
            and isinstance(photo, dict)
            and set(photo) == {"@type", "photo", "thumbnail", "video", "added_sticker_file_ids", "width", "height"}
            and photo.get("@type") == "inputPhoto"
            and _valid_staged_input_file(photo.get("photo"))
            and photo.get("thumbnail") is None and photo.get("video") is None
            and photo.get("added_sticker_file_ids") == []
            and photo.get("width") == 0 and photo.get("height") == 0
            and content.get("show_caption_above_media") is False
            and content.get("self_destruct_type") is None and content.get("has_spoiler") is False
            and isinstance(caption, dict) and set(caption) == {"@type", "text", "entities"}
            and caption.get("@type") == "formattedText"
            and isinstance(caption.get("text"), str) and len(caption["text"]) <= 1024
            and caption.get("entities") == []
        )
        voice = content.get("voice_note") if isinstance(content, dict) else None
        valid_voice = (
            isinstance(content, dict)
            and set(content) == {"@type", "voice_note", "caption", "self_destruct_type"}
            and content.get("@type") == "inputMessageVoiceNote"
            and isinstance(voice, dict) and set(voice) == {"@type", "voice_note", "duration", "waveform"}
            and voice.get("@type") == "inputVoiceNote"
            and _valid_staged_input_file(voice.get("voice_note"))
            and type(voice.get("duration")) is int and 1 <= voice["duration"] <= 600
            and _valid_voice_waveform(voice.get("waveform"))
            and content.get("self_destruct_type") is None
            and isinstance(caption, dict) and set(caption) == {"@type", "text", "entities"}
            and caption.get("@type") == "formattedText"
            and isinstance(caption.get("text"), str) and len(caption["text"]) <= 1024
            and caption.get("entities") == []
        )
        if reply_to is not None and valid_photo and (type(photo.get('width')) is not int or type(photo.get('height')) is not int):
            valid_photo=False
        if (
            set(payload) != {"@type", "chat_id", "topic_id", "reply_to", "options", "reply_markup", "input_message_content"}
            or not _is_strict_nonzero_integer(payload.get("chat_id"))
            or payload.get("topic_id") is not None or not valid_reply
            or payload.get("reply_markup") is not None
            or not isinstance(options, dict) or options != expected_options
            or (reply_to is not None and (not strict_shape(options,expected_options) or not _is_forum_chat_id(payload.get("chat_id"))))
            or type(sending_id) is not int or not 0 < sending_id <= (1 << 31) - 1
            or not (valid_text or valid_document or valid_photo or valid_voice)
        ):
            raise ForbiddenTDLibRequest("TDLib send request shape is not allowed")
    if request_type in {"downloadFile", "getFile", "cancelDownloadFile"}:
        file_id = payload.get("file_id")
        if not _is_strict_integer(file_id) or not 0 < file_id <= (1 << 31) - 1:
            raise ForbiddenTDLibRequest("TDLib file request shape is not allowed")
        if request_type == "downloadFile" and (
            set(payload) != {"@type", "file_id", "priority", "offset", "limit", "synchronous"}
            or payload.get("priority") != 1
            or payload.get("offset") != 0
            or payload.get("limit") not in {64 * 1024 * 1024 + 1, 256 * 1024 * 1024 + 1}
            or payload.get("synchronous") is not False
        ):
            raise ForbiddenTDLibRequest("TDLib file request shape is not allowed")
        if request_type == "getFile" and set(payload) != {"@type", "file_id"}:
            raise ForbiddenTDLibRequest("TDLib file request shape is not allowed")
        if request_type == "cancelDownloadFile" and (
            set(payload) != {"@type", "file_id", "only_if_pending"}
            or payload.get("only_if_pending") is not False
        ):
            raise ForbiddenTDLibRequest("TDLib file request shape is not allowed")
    if request_type == "loadChats":
        if (
            set(payload) != {"@type", "chat_list", "limit"}
            or payload.get("chat_list")
            not in ({"@type": "chatListMain"}, {"@type": "chatListArchive"})
            or payload.get("limit") != 50
        ):
            raise ForbiddenTDLibRequest("TDLib request shape is not allowed")
    if request_type == "searchMessages":
        expected_keys = {
            "@type",
            "chat_list",
            "query",
            "offset",
            "limit",
            "filter",
            "chat_type_filter",
            "min_date",
            "max_date",
        }
        if (
            set(payload) != expected_keys
            or payload.get("chat_list")
            not in ({"@type": "chatListMain"}, {"@type": "chatListArchive"})
            or not isinstance(payload.get("query"), str)
            or not isinstance(payload.get("offset"), str)
            or payload.get("limit") != 10
            or payload.get("filter") is not None
            or payload.get("chat_type_filter") is not None
            or not _is_strict_integer(payload.get("min_date"))
            or payload.get("min_date") != 0
            or not _is_strict_integer(payload.get("max_date"))
            or payload.get("max_date") != 0
        ):
            raise ForbiddenTDLibRequest("TDLib request shape is not allowed")
    if request_type == "searchChatMessages":
        expected_keys = {
            "@type", "chat_id", "topic_id", "query", "sender_id",
            "from_message_id", "offset", "limit", "filter",
        }
        media_filter = payload.get("filter")
        if (
            set(payload) != expected_keys
            or not _is_strict_nonzero_integer(payload.get("chat_id"))
            or not _is_search_topic(payload.get("topic_id"))
            or not isinstance(payload.get("query"), str)
            or not _is_search_sender(payload.get("sender_id"))
            or not _is_strict_integer(payload.get("from_message_id"))
            or payload["from_message_id"] < 0
            or not _is_strict_integer(payload.get("offset"))
            or payload["offset"] != 0
            or not _is_strict_integer(payload.get("limit"))
            or not 1 <= payload["limit"] <= 100
            or (
                media_filter is not None
                and media_filter not in tuple(_CHAT_MEDIA_FILTERS.values())
            )
            or (media_filter is not None and (payload.get("topic_id") is not None or payload.get("sender_id") is not None))
            or ((payload["query"] == "") != (media_filter is not None))
        ):
            raise ForbiddenTDLibRequest("TDLib request shape is not allowed")


def _formatted_text_is_valid(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get("text"), str)


_CAPTION_EVIDENCE_CONTENT_TYPES = frozenset(
    {
        "messageAnimation",
        "messageAudio",
        "messageDocument",
        "messagePaidMedia",
        "messagePhoto",
        "messageVideo",
        "messageVoiceNote",
    }
)
_KNOWN_NON_EVIDENCE_CONTENT_TYPES = frozenset(
    {
        "messageCall",
        "messageChatAddMembers",
        "messageChatBoost",
        "messageChatChangePhoto",
        "messageChatChangeTitle",
        "messageChatDeleteMember",
        "messageChatDeletePhoto",
        "messageChatHasProtectedContentDisableRequested",
        "messageChatHasProtectedContentToggled",
        "messageChatJoinByLink",
        "messageChatJoinByRequest",
        "messageChatOwnerChanged",
        "messageChatOwnerLeft",
        "messageChatSetBackground",
        "messageChatSetMessageAutoDeleteTime",
        "messageChatSetTheme",
        "messageChatShared",
        "messageChatUpgradeFrom",
        "messageChatUpgradeTo",
        "messageChecklist",
        "messageContact",
        "messageDice",
        "messageExpiredPhoto",
        "messageExpiredVideo",
        "messageGame",
        "messageInvoice",
        "messageLocation",
        "messagePoll",
        "messageSticker",
        "messageStory",
        "messageUnsupported",
        "messageVenue",
        "messageVideoNote",
    }
)


def _sender_is_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("@type") == "messageSenderUser":
        user_id = value.get("user_id")
        return _is_strict_integer(user_id) and user_id > 0
    if value.get("@type") == "messageSenderChat":
        return _is_strict_nonzero_integer(value.get("chat_id"))
    return False


def _validated_global_evidence_message(
    message: object,
) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(message, dict):
        return None, True
    if (
        message.get("@type") != "message"
        or not _is_strict_nonzero_integer(message.get("id"))
        or not _is_strict_nonzero_integer(message.get("chat_id"))
        or not _is_strict_integer(message.get("date"))
        or message["date"] < 0
        or not _sender_is_valid(message.get("sender_id"))
    ):
        return None, True
    content = message.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("@type"), str):
        return None, True
    content_type = content["@type"]
    if content_type == "messageText":
        return (
            (message, False)
            if _formatted_text_is_valid(content.get("text"))
            else (None, True)
        )
    if content_type in _CAPTION_EVIDENCE_CONTENT_TYPES:
        return (
            (message, False)
            if _formatted_text_is_valid(content.get("caption"))
            else (None, True)
        )
    if content_type in _KNOWN_NON_EVIDENCE_CONTENT_TYPES:
        return None, False
    return None, True


@dataclass(frozen=True)
class GlobalMessagePage:
    messages: list[dict[str, Any]]
    next_offset: str
    integrity_partial: bool


def _validate_global_message_page(response: dict[str, Any]) -> GlobalMessagePage:
    total_count = response.get("total_count")
    messages = response.get("messages")
    next_offset = response.get("next_offset")
    if (
        response.get("@type") != "foundMessages"
        or not _is_strict_integer(total_count)
        or total_count < -1
        or not isinstance(messages, list)
        or len(messages) > 10
        or not isinstance(next_offset, str)
    ):
        raise GlobalMessageEnvelopeError()
    valid_messages: list[dict[str, Any]] = []
    integrity_partial = False
    for message in messages:
        valid_message, invalid = _validated_global_evidence_message(message)
        integrity_partial = integrity_partial or invalid
        if valid_message is not None:
            valid_messages.append(valid_message)
    return GlobalMessagePage(
        messages=valid_messages,
        next_offset=next_offset,
        integrity_partial=integrity_partial,
    )


def _catalog_change_from_update(
    update: dict[str, Any],
) -> tuple[bool, int, dict[ChatListName, int], set[ChatListName]] | None:
    update_type = update.get("@type")
    replace_all = update_type in {
        "updateNewChat",
        "updateChatLastMessage",
        "updateChatDraftMessage",
    }
    if update_type == "updateNewChat":
        chat = update.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_type = chat.get("type")
        if isinstance(chat_type, dict) and chat_type.get("@type") == "chatTypeSecret":
            return None
        chat_id = chat.get("id")
        raw_positions = chat.get("positions")
    elif update_type in {"updateChatLastMessage", "updateChatDraftMessage"}:
        chat_id = update.get("chat_id")
        raw_positions = update.get("positions")
    elif update_type == "updateChatPosition":
        chat_id = update.get("chat_id")
        raw_positions = [update.get("position")]
    else:
        return None
    if not _is_strict_nonzero_integer(chat_id) or not isinstance(raw_positions, list):
        return None
    changes: dict[ChatListName, int] = {}
    protected_lists: set[ChatListName] = set()
    for raw_position in raw_positions:
        if not isinstance(raw_position, dict):
            protected_lists.update(("main", "archive"))
            continue
        raw_list = raw_position.get("list")
        if raw_list == {"@type": "chatListMain"}:
            chat_list: ChatListName = "main"
        elif raw_list == {"@type": "chatListArchive"}:
            chat_list = "archive"
        else:
            continue
        order = _parse_wire_int64(raw_position.get("order"))
        if raw_position.get("@type") != "chatPosition" or order is None or order < 0:
            if chat_list not in changes:
                protected_lists.add(chat_list)
            continue
        changes[chat_list] = order
        protected_lists.discard(chat_list)
    return replace_all, chat_id, changes, protected_lists


class TDLibError(RuntimeError):
    """A redacted TDLib failure."""


class ForumUnsupported(TDLibError):
    """The verified chat does not support forum topics."""


class TDLibDeadlineExceeded(TDLibError):
    """An aggregate request deadline expired before the provider replied."""


class DownloadTooLarge(TDLibError):
    """TDLib reported a file beyond the selected transfer limit."""


class GlobalMessageEnvelopeError(TDLibError):
    """The provider page cannot be continued without trusting malformed state."""

    def __init__(self) -> None:
        super().__init__("TDLib returned an invalid global message envelope")


class ForbiddenTDLibRequest(TDLibError):
    """A request outside the fixed read-only allowlist."""


class AuthorizationBlocked(TDLibError):
    """The dedicated TDLib session is not authorizationStateReady."""


class SecretChatRejected(TDLibError):
    """Secret chats are outside the product boundary."""


class MessageNotFound(TDLibError):
    """A previously discovered message no longer exists."""


class MessageSendFailed(TDLibError):
    """TDLib explicitly rejected or failed the single send attempt."""

    def __init__(self, message: str, *, provider_error: object = None) -> None:
        super().__init__(message)
        error = provider_error if isinstance(provider_error, dict) else {}
        code = error.get("code")
        self.provider_code = code if type(code) is int and 0 < code < 10000 else None
        # Provider text can contain private request data; never copy it.

    def public_detail(self) -> str:
        if self.provider_code is None:
            return "TDLib rejected the send"
        return f"TDLib rejected the send (code {self.provider_code})"


class MessageSendNotAttempted(TDLibError):
    """Local preparation refused before the raw provider send was invoked."""


class MessageSendOutcomeUnknown(TDLibError):
    """The provider attempt may have happened but delivery is unconfirmed."""


class RawTDJson(Protocol):
    def send(self, payload: dict[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class LegacyTDJson:
    """Own one client created through the installed legacy tdjson C ABI."""

    def __init__(self) -> None:
        library_path = validate_tdlib_runtime()
        try:
            library = ctypes.CDLL(str(library_path))
        except OSError as error:
            raise TDLibError("unable to load the pinned TDLib runtime") from error

        library.td_json_client_create.argtypes = []
        library.td_json_client_create.restype = ctypes.c_void_p
        library.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.td_json_client_send.restype = None
        library.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
        library.td_json_client_receive.restype = ctypes.c_char_p
        library.td_json_client_execute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.td_json_client_execute.restype = ctypes.c_char_p
        library.td_json_client_destroy.argtypes = [ctypes.c_void_p]
        library.td_json_client_destroy.restype = None

        quiet_request = b'{"@type":"setLogVerbosityLevel","new_verbosity_level":0}'
        quiet_response = library.td_json_client_execute(None, quiet_request)
        if quiet_response is None:
            raise TDLibError("TDLib rejected its initialization log setting")
        client = library.td_json_client_create()
        if not client:
            raise TDLibError("TDLib did not create a client")
        self._library = library
        self._client: int | None = client

    def send(self, payload: dict[str, Any]) -> None:
        if self._client is None:
            raise TDLibError("TDLib client is closed")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._library.td_json_client_send(self._client, encoded)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if self._client is None:
            raise TDLibError("TDLib client is closed")
        encoded = self._library.td_json_client_receive(self._client, timeout)
        if encoded is None:
            return None
        try:
            value = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TDLibError("TDLib returned an invalid JSON response") from error
        if not isinstance(value, dict):
            raise TDLibError("TDLib returned an invalid response object")
        return value

    def close(self) -> None:
        if self._client is not None:
            self._library.td_json_client_destroy(self._client)
            self._client = None

    def __enter__(self) -> "LegacyTDJson":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


from .send_observations import SendObservations, ObservationUnavailable


class TDLibClient:
    """Typed read-only operations over a strictly allowlisted tdjson transport."""

    def __init__(
        self,
        *,
        raw: RawTDJson,
        session_directory: Path = TDLIB_SESSION_DIRECTORY,
        credential_loader: Callable[[], ApiCredentials] = read_api_credentials,
        request_timeout: float = 30.0,
    ) -> None:
        self._raw = raw
        self._session_directory = session_directory
        self._credential_loader = credential_loader
        self._request_timeout = request_timeout
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._download_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._serialization_wait_count = 0
        self._request_context = threading.local()
        self._ready = False
        self._send_observations = SendObservations()
        self._send_account_id: int | None = None
        self._send_observation_epoch = object()
        self._catalog_positions: dict[ChatListName, dict[int, int]] = {
            "main": {},
            "archive": {},
        }

    @classmethod
    def from_defaults(cls) -> "TDLibClient":
        os.umask(0o077)
        return cls(raw=LegacyTDJson())

    def _apply_catalog_update(self, update: dict[str, Any]) -> None:
        change = _catalog_change_from_update(update)
        if change is None:
            return
        replace_all, chat_id, positions, protected_lists = change
        if replace_all:
            for chat_list, catalog in self._catalog_positions.items():
                if chat_list not in protected_lists:
                    catalog.pop(chat_id, None)
        for chat_list, order in positions.items():
            if order == 0:
                self._catalog_positions[chat_list].pop(chat_id, None)
            else:
                self._catalog_positions[chat_list][chat_id] = order

    @property
    def send_observation_epoch(self) -> object:
        return self._send_observation_epoch

    def _invalidate_send_observations(self) -> None:
        self._send_observations.invalidate()
        self._send_observation_epoch = object()
        self._send_account_id = None

    def _reduce_receive_event(self, response: dict[str, Any]) -> None:
        self._apply_catalog_update(response)
        if response.get("@type") == "updateAuthorizationState":
            state = response.get("authorization_state")
            self._ready = isinstance(state, dict) and state.get("@type") == "authorizationStateReady"
            if not self._ready:
                self._invalidate_send_observations()
        self._send_observations.reduce(response)

    def get_send_observation(self, attempt_id: str):
        """Drain at most 64 immediately available events; never wait for the lock.

        Authentication/account checks belong to the owning broker's bounded call.
        The fixed initial retention deadline is never renewed by reading.
        """
        if self._lock.acquire(blocking=False):
            try:
                deadline = min(time.monotonic() + 0.05,
                    getattr(self._request_context, "deadline", float("inf")))
                for _ in range(64):
                    if time.monotonic() >= deadline:
                        break
                    try:
                        response = self._raw.receive(0.0)
                    except Exception:
                        break
                    if response is None:
                        break
                    self._reduce_receive_event(response)
            finally:
                self._lock.release()
        return self._send_observations.snapshot(attempt_id) if self._ready else None

    def _catalog_snapshot(self, chat_list: ChatListName) -> list[CatalogPosition]:
        with self._lock:
            return [
                CatalogPosition(chat_id=chat_id, order=order)
                for chat_id, order in sorted(
                    self._catalog_positions[chat_list].items(),
                    key=lambda item: (item[1], item[0]),
                    reverse=True,
                )
            ]

    @property
    def serialization_wait_count(self) -> int:
        with self._metrics_lock:
            return self._serialization_wait_count

    @contextmanager
    def request_budget(self, deadline: float):
        previous = getattr(self._request_context, "deadline", None)
        self._request_context.deadline = deadline
        try:
            yield
        finally:
            if previous is None:
                del self._request_context.deadline
            else:
                self._request_context.deadline = previous

    @contextmanager
    def _serialized_provider_call(self, deadline: float | None = None):
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            with self._metrics_lock:
                self._serialization_wait_count += 1
            if deadline is None:
                self._lock.acquire()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                acquired = self._lock.acquire(timeout=remaining)
                if not acquired:
                    raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
        try:
            yield
        finally:
            self._lock.release()

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_type = payload.get("@type")
        if not isinstance(request_type, str) or request_type not in _ALLOWED_REQUEST_TYPES:
            raise ForbiddenTDLibRequest("TDLib request is not allowed")
        _validate_bounded_request(payload)
        aggregate_deadline = getattr(self._request_context, "deadline", None)
        with self._serialized_provider_call(aggregate_deadline):
            if aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
            if request_type not in _AUTHORIZATION_CONTROL_TYPES and not self._ready:
                raise AuthorizationBlocked(
                    "the dedicated TDLib session is not authorizationStateReady"
                )
            per_call_deadline = time.monotonic() + self._request_timeout
            deadline = (
                min(per_call_deadline, aggregate_deadline)
                if aggregate_deadline is not None
                else per_call_deadline
            )
            if time.monotonic() >= deadline:
                if aggregate_deadline is not None and aggregate_deadline <= per_call_deadline:
                    raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                raise TDLibError("TDLib request timed out")
            extra = f"telegram-search-mcp-{next(self._ids)}"
            request = dict(payload)
            request["@extra"] = extra
            if time.monotonic() >= deadline:
                if aggregate_deadline is not None and aggregate_deadline <= per_call_deadline:
                    raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                raise TDLibError("TDLib request timed out")
            self._raw.send(request)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if aggregate_deadline is not None and aggregate_deadline <= per_call_deadline:
                        raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                    raise TDLibError("TDLib request timed out")
                response = self._raw.receive(min(1.0, remaining))
                if response is not None:
                    self._reduce_receive_event(response)
                if time.monotonic() >= deadline:
                    if aggregate_deadline is not None and aggregate_deadline <= per_call_deadline:
                        raise TDLibDeadlineExceeded("TDLib request deadline exceeded")
                    raise TDLibError("TDLib request timed out")
                if response is None:
                    continue
                if response.get("@type") == "updateAuthorizationState":
                    authorization_state = response.get("authorization_state")
                    state_type = (
                        authorization_state.get("@type")
                        if isinstance(authorization_state, dict)
                        else None
                    )
                    self._ready = state_type == "authorizationStateReady"
                    if not self._ready and request_type not in _AUTHORIZATION_CONTROL_TYPES:
                        raise AuthorizationBlocked(
                            "the dedicated TDLib session lost authorization"
                        )
                    continue
                if response.get("@extra") != extra:
                    continue
                if response.get("@type") == "error":
                    code = response.get("code")
                    if not _is_strict_integer(code):
                        raise TDLibError("TDLib returned an invalid error response")
                    if code == 404:
                        raise MessageNotFound("Telegram message is no longer available")
                    raise TDLibError(f"TDLib request failed with code {code}")
                return response

    def ensure_ready(self) -> None:
        for _ in range(8):
            state = self._call({"@type": "getAuthorizationState"}).get("@type")
            self._ready = state == "authorizationStateReady"
            if not self._ready:
                self._invalidate_send_observations()
            if state == "authorizationStateReady":
                return
            if state == "authorizationStateWaitTdlibParameters":
                credentials = self._credential_loader()
                session = ensure_private_directory(self._session_directory)
                database = ensure_private_directory(session / "db")
                files = ensure_private_directory(session / "files")
                self._call(
                    {
                        "@type": "setTdlibParameters",
                        "use_test_dc": False,
                        "database_directory": str(database),
                        "files_directory": str(files),
                        "database_encryption_key": "",
                        "use_file_database": False,
                        "use_chat_info_database": True,
                        "use_message_database": True,
                        "use_secret_chats": False,
                        "api_id": credentials.api_id,
                        "api_hash": credentials.api_hash,
                        "system_language_code": "en",
                        "device_model": "TelegramSearchMCP",
                        "system_version": platform.mac_ver()[0] or "macOS",
                        "application_version": "0.1.0",
                    }
                )
                continue
            if state == "authorizationStateWaitEncryptionKey":
                self._call({"@type": "checkDatabaseEncryptionKey", "encryption_key": ""})
                continue
            raise AuthorizationBlocked("the dedicated TDLib session is not authorizationStateReady")
        raise AuthorizationBlocked("the dedicated TDLib session did not become ready")

    def resolve_target(self, target: str | int) -> dict[str, Any]:
        numeric_target = _is_strict_integer(target)
        if numeric_target:
            chat = self._call({"@type": "getChat", "chat_id": target})
        elif isinstance(target, str) and target.startswith("@"):
            chat = self._call({"@type": "searchPublicChat", "username": target[1:]})
        else:
            raise TDLibError("target must be a numeric chat ID or exact @username")
        chat_id = chat.get("id")
        if chat.get("@type") != "chat" or not _is_strict_nonzero_integer(chat_id):
            raise TDLibError("TDLib returned an invalid chat")
        if numeric_target and chat_id != target:
            raise TDLibError("TDLib returned a different chat")
        chat_type = chat.get("type")
        if chat_type is not None and not isinstance(chat_type, dict):
            raise TDLibError("TDLib returned an invalid chat type")
        if (chat_type or {}).get("@type") == "chatTypeSecret":
            raise SecretChatRejected("secret chats are not supported")
        return chat

    def resolve_forum_chat(self, chat_id: int) -> dict[str, Any]:
        """Verify exact chat identity and the native forum capability."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("chat_id is invalid")
        chat = self._call({"@type": "getChat", "chat_id": chat_id})
        if (
            chat.get("@type") != "chat"
            or not _is_forum_chat_id(chat.get("id"))
            or chat["id"] != chat_id
            or not isinstance(chat.get("type"), dict)
        ):
            raise TDLibError("TDLib returned an invalid forum chat")
        chat_type = chat["type"]
        kind = chat_type.get("@type")
        if kind == "chatTypeSupergroup":
            identifier = chat_type.get("supergroup_id")
            if (
                not _is_integer_in_range(identifier, 1, 2**53 - 1)
                or type(chat_type.get("is_channel")) is not bool
            ):
                raise TDLibError("TDLib returned an invalid forum chat type")
            if chat_type["is_channel"]:
                raise ForumUnsupported("the chat does not support forum topics")
            supergroup = self._call({"@type": "getSupergroup", "supergroup_id": identifier})
            if (
                supergroup.get("@type") != "supergroup"
                or not _is_integer_in_range(supergroup.get("id"), 1, 2**53 - 1)
                or supergroup["id"] != identifier
                or type(supergroup.get("is_forum")) is not bool
            ):
                raise TDLibError("TDLib returned an invalid forum capability")
            if supergroup["is_forum"] is not True:
                raise ForumUnsupported("the chat does not support forum topics")
        elif kind == "chatTypePrivate":
            identifier = chat_type.get("user_id")
            if not _is_integer_in_range(identifier, 1, 2**53 - 1):
                raise TDLibError("TDLib returned an invalid forum chat type")
            user = self._call({"@type": "getUser", "user_id": identifier})
            if (
                user.get("@type") != "user"
                or not _is_integer_in_range(user.get("id"), 1, 2**53 - 1)
                or user["id"] != identifier
                or not isinstance(user.get("type"), dict)
            ):
                raise TDLibError("TDLib returned an invalid forum capability")
            user_type = user["type"]
            user_kind = user_type.get("@type")
            if not isinstance(user_kind, str):
                raise TDLibError("TDLib returned an invalid forum capability")
            if user_kind in {"userTypeRegular", "userTypeDeleted", "userTypeUnknown"}:
                raise ForumUnsupported("the chat does not support forum topics")
            if user_kind != "userTypeBot" or type(user_type.get("has_topics")) is not bool:
                raise TDLibError("TDLib returned an invalid forum capability")
            if user_type["has_topics"] is not True:
                raise ForumUnsupported("the chat does not support forum topics")
        elif kind == "chatTypeBasicGroup":
            if not _is_integer_in_range(chat_type.get("basic_group_id"), 1, 2**53 - 1):
                raise TDLibError("TDLib returned an invalid forum chat type")
            raise ForumUnsupported("the chat does not support forum topics")
        elif kind == "chatTypeSecret":
            if not _is_integer_in_range(chat_type.get("secret_chat_id"), 1, 2**31 - 1):
                raise TDLibError("TDLib returned an invalid forum chat type")
            raise ForumUnsupported("the chat does not support forum topics")
        else:
            raise TDLibError("TDLib returned an invalid forum chat type")
        return chat

    def get_forum_topics(
        self, chat_id: int, *, offset_date: int = 0, offset_message_id: int = 0,
        offset_forum_topic_id: int = 0, limit: int = 20,
    ) -> dict[str, Any]:
        """Return one bounded raw page and its entire native continuation triple."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("chat_id is invalid")
        if not _valid_forum_offsets(offset_date, offset_message_id, offset_forum_topic_id):
            raise ValueError("forum offsets are invalid")
        if not _is_integer_in_range(limit, 1, 20):
            raise ValueError("forum topic limit is invalid")
        response = self._call({
            "@type": "getForumTopics", "chat_id": chat_id, "query": "",
            "offset_date": offset_date, "offset_message_id": offset_message_id,
            "offset_forum_topic_id": offset_forum_topic_id, "limit": limit,
        })
        topics = response.get("topics")
        # TDLib can return more rows than requested. This is our local safety
        # bound, not a native guarantee; retain every accepted row and offset.
        if (
            response.get("@type") != "forumTopics"
            or not isinstance(topics, list)
            or len(topics) > 200
            or any(not isinstance(topic, dict) or topic.get("@type") != "forumTopic" for topic in topics)
            or not _valid_forum_offsets(
                response.get("next_offset_date"), response.get("next_offset_message_id"),
                response.get("next_offset_forum_topic_id"),
            )
        ):
            raise TDLibError("TDLib returned an invalid forum topic page")
        # total_count is approximate; neither it nor the continuation triple
        # proves that this observed page is a complete topic inventory.
        return response

    def get_forum_topic(self, chat_id: int, forum_topic_id: int) -> dict[str, Any] | None:
        """Return native metadata, or None for the pinned native null/404 result."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("chat_id is invalid")
        if not _is_integer_in_range(forum_topic_id, 1, 2**31 - 1):
            raise ValueError("forum_topic_id is invalid")
        try:
            response = self._call({
                "@type": "getForumTopic", "chat_id": chat_id, "forum_topic_id": forum_topic_id,
            })
        except MessageNotFound:
            # Td::send_result converts ForumTopicManager's nullptr to a
            # correlated error(404); no JSON null object is emitted.
            return None
        if response.get("@type") != "forumTopic":
            raise TDLibError("TDLib returned an invalid forum topic")
        return response

    def get_forum_topic_history(
        self, chat_id: int, forum_topic_id: int, *, from_message_id: int = 0, limit: int = 20,
    ) -> dict[str, Any]:
        """One observed native page, with no completeness inference or ID conversion."""
        if not _is_forum_chat_id(chat_id):
            raise ValueError("chat_id is invalid")
        if not _is_integer_in_range(forum_topic_id, 1, 2**31 - 1):
            raise ValueError("forum_topic_id is invalid")
        if not _is_integer_in_range(from_message_id, 0, 2**53 - 1):
            raise ValueError("history boundary is invalid")
        if not _is_integer_in_range(limit, 1, 20):
            raise ValueError("history limit is invalid")
        response = self._call({"@type": "getForumTopicHistory", "chat_id": chat_id,
            "forum_topic_id": forum_topic_id, "from_message_id": from_message_id,
            "offset": 0, "limit": limit})
        rows = response.get("messages")
        # The pinned native path slices to limit. The 200-row guard is local
        # defense against drift, not a promise that Telegram returns this size.
        if (response.get("@type") != "messages" or not isinstance(rows, list) or len(rows) > 200
                or not _is_integer_in_range(response.get("total_count"), -1, 2**31 - 1)
                or any(not isinstance(row, dict) or row.get("@type") != "message" for row in rows)):
            raise TDLibError("TDLib returned an invalid forum history page")
        return response

    def get_account_id(self) -> int:
        user = self._call({"@type": "getMe"})
        account_id = user.get("id")
        if user.get("@type") != "user" or type(account_id) is not int or not 0 < account_id < 2**53:
            raise TDLibError("TDLib returned an invalid account identity")
        if self._send_account_id is not None and self._send_account_id != account_id:
            self._invalidate_send_observations()
        self._send_account_id = account_id
        return account_id

    def get_self_chat(self) -> dict[str, Any]:
        user = self._call({"@type": "getMe"})
        user_id = user.get("id")
        if (
            user.get("@type") != "user"
            or not _is_strict_integer(user_id)
            or user_id <= 0
        ):
            raise TDLibError("TDLib returned an invalid account identity")
        chat = self._call({"@type": "createPrivateChat", "user_id": user_id, "force": True})
        chat_type = chat.get("type")
        chat_type_name = chat_type.get("@type") if isinstance(chat_type, dict) else None
        if chat_type_name == "chatTypeSecret":
            raise SecretChatRejected("secret chats are not supported")
        if (
            chat.get("@type") != "chat"
            or not _is_strict_nonzero_integer(chat.get("id"))
            or chat_type_name != "chatTypePrivate"
            or not _is_strict_integer(chat_type.get("user_id") if isinstance(chat_type, dict) else None)
            or chat_type["user_id"] != user_id
        ):
            raise TDLibError("TDLib returned an invalid self chat")
        return chat

    def search_known_chat_ids(self, query: str) -> tuple[list[int], bool]:
        return self._search_chat_ids(
            {"@type": "searchChats", "query": query, "limit": 20, "type_filter": None},
            cap=20,
            with_total_count=True,
        )

    def search_known_chat_ids_on_server(self, query: str) -> tuple[list[int], bool]:
        return self._search_chat_ids(
            {
                "@type": "searchChatsOnServer",
                "query": query,
                "limit": 20,
                "type_filter": None,
            },
            cap=20,
            with_total_count=True,
        )

    def get_recent_main_chat_ids(self) -> list[int]:
        chat_ids, _ = self._search_chat_ids(
            {"@type": "getChats", "chat_list": {"@type": "chatListMain"}, "limit": 50},
            cap=50,
            with_total_count=False,
        )
        return chat_ids

    def get_chat_list_snapshot(self, chat_list: ChatListName, *, limit: int) -> dict[str, Any]:
        """Return the raw bounded getChats envelope without catalog normalization."""
        if chat_list not in ("main", "archive") or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid chat listing request")
        return self._call({"@type": "getChats", "chat_list": _chat_list_payload(chat_list), "limit": limit})

    def get_chat_metadata(self, chat_id: int) -> dict[str, Any]:
        """Read exact local-state chat metadata; never open or mark a chat read."""
        if type(chat_id) is not int or chat_id == 0 or abs(chat_id) > 2**53 - 1:
            raise ValueError("invalid chat listing identity")
        return self._call({"@type": "getChat", "chat_id": chat_id})

    def get_chat_list_prefix(self, chat_list: ChatListName) -> list[int]:
        chat_ids, _ = self._search_chat_ids(
            {
                "@type": "getChats",
                "chat_list": _chat_list_payload(chat_list),
                "limit": 15,
            },
            cap=15,
            with_total_count=False,
        )
        return chat_ids

    def load_more_chats(
        self,
        chat_list: ChatListName,
        on_positions: Callable[[list[CatalogPosition]], None],
    ) -> bool:
        list_payload = _chat_list_payload(chat_list)
        try:
            self._call({"@type": "loadChats", "chat_list": list_payload, "limit": 50})
        except MessageNotFound:
            on_positions(self._catalog_snapshot(chat_list))
            return True
        except AuthorizationBlocked:
            raise
        except TDLibError:
            on_positions(self._catalog_snapshot(chat_list))
            raise
        on_positions(self._catalog_snapshot(chat_list))
        return False

    def search_global_messages(
        self,
        chat_list: Literal["main", "archive"],
        query: str,
        *,
        offset: str,
    ) -> GlobalMessagePage:
        list_payload = _chat_list_payload(chat_list)
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if not isinstance(offset, str):
            raise TypeError("offset must be a string")
        response = self._call(
            {
                "@type": "searchMessages",
                "chat_list": list_payload,
                "query": query,
                "offset": offset,
                "limit": 10,
                "filter": None,
                "chat_type_filter": None,
                "min_date": 0,
                "max_date": 0,
            }
        )
        return _validate_global_message_page(response)

    def _search_chat_ids(
        self, payload: dict[str, Any], *, cap: int, with_total_count: bool
    ) -> tuple[list[int], bool]:
        response = self._call(payload)
        chat_ids = response.get("chat_ids")
        total_count = response.get("total_count")
        if (
            response.get("@type") != "chats"
            or not isinstance(chat_ids, list)
            or (
                with_total_count
                and (not _is_strict_integer(total_count) or total_count < 0)
            )
        ):
            raise TDLibError("TDLib returned an invalid chat discovery response")
        if not all(_is_strict_nonzero_integer(chat_id) for chat_id in chat_ids):
            raise TDLibError("TDLib returned an invalid chat discovery response")
        unique_chat_ids = list(dict.fromkeys(chat_ids))[:cap]
        return unique_chat_ids, bool(with_total_count and total_count <= len(unique_chat_ids))

    def search_chat_messages(
        self,
        chat_id: int,
        query: str,
        *,
        from_message_id: int,
        limit: int,
        sender: Any = None,
        topic: Any = None,
    ) -> dict[str, Any]:
        from .schemas import SearchSenderReference, ForumTopicReference
        if sender is not None and not isinstance(sender, SearchSenderReference):
            raise ValueError("search sender must be a typed reference")
        if topic is not None and not isinstance(topic, ForumTopicReference):
            raise ValueError("search topic must be a typed forum reference")
        native_sender = None if sender is None else {
            "@type": "messageSenderUser" if sender.kind == "user" else "messageSenderChat",
            "user_id" if sender.kind == "user" else "chat_id": sender.id,
        }
        native_topic = None if topic is None else {"@type": "messageTopicForum", "forum_topic_id": topic.id}
        return self._call(
            {
                "@type": "searchChatMessages",
                "chat_id": chat_id,
                "topic_id": native_topic,
                "query": query,
                "sender_id": native_sender,
                "from_message_id": from_message_id,
                "offset": 0,
                "limit": min(max(limit, 1), 100),
                "filter": None,
            }
        )

    def search_chat_media(
        self,
        chat_id: int,
        media_type: str,
        *,
        from_message_id: int,
        limit: int,
    ) -> dict[str, Any]:
        media_filter = _CHAT_MEDIA_FILTERS.get(media_type)
        if media_filter is None:
            raise ValueError("media_type must be video or video_note")
        return self._call(
            {
                "@type": "searchChatMessages",
                "chat_id": chat_id,
                "topic_id": None,
                "query": "",
                "sender_id": None,
                "from_message_id": from_message_id,
                "offset": 0,
                "limit": min(max(limit, 1), 100),
                "filter": media_filter,
            }
        )

    def get_chat_history(
        self,
        chat_id: int,
        *,
        from_message_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        response = self._call(
            {
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": from_message_id,
                "offset": 0,
                "limit": min(max(limit, 1), 100),
                "only_local": False,
            }
        )
        messages = response.get("messages")
        if (response.get("@type") != "messages" or not isinstance(messages, list)
                or len(messages) > min(max(limit, 1), 100) or any(not isinstance(item, dict) for item in messages)):
            raise TDLibError("TDLib returned an invalid chat history")
        return messages

    def get_message(self, chat_id: int, message_id: int) -> dict[str, Any]:
        message = self._call(
            {"@type": "getMessage", "chat_id": chat_id, "message_id": message_id}
        )
        if message.get("@type") != "message":
            raise TDLibError("TDLib returned an invalid message")
        return message

    def send_document_message(self, chat_id: int, path: Path, caption: str, *, attempt_id: str | None = None) -> int:
        """Make one provider attempt and wait for a matching delivery update."""
        content = {
            "@type": "inputMessageDocument",
            "document": {
                "@type": "inputDocument",
                "document": {"@type": "inputFileLocal", "path": str(path)},
                "thumbnail": None, "disable_content_type_detection": True,
            },
            "caption": {"@type": "formattedText", "text": caption, "entities": []},
        }
        return self._send_message_content(chat_id, content, "messageDocument", attempt_id=attempt_id)

    def send_text_message(self, chat_id: int, text: str, *, attempt_id: str | None = None) -> int:
        """Send plain text without entities, link previews, replies, or scheduling."""
        content = {
            "@type": "inputMessageText",
            "text": {"@type": "formattedText", "text": text, "entities": []},
            "link_preview_options": {
                "@type": "linkPreviewOptions", "is_disabled": True, "url": "",
                "force_small_media": False, "force_large_media": False,
                "show_above_text": False,
            },
            "clear_draft": False,
        }
        return self._send_message_content(chat_id, content, "messageText", attempt_id=attempt_id)

    def read_reply_source(self, chat_id: int, message_id: int) -> ReplySource:
        return source_from_message(self.get_message(chat_id,message_id),chat_id,message_id)

    @contextmanager
    def _serialized_reply_call(self):
        # Convert only failed lock acquisition into no-attempt evidence. Errors
        # after yielding keep their delivery-uncertainty classification.
        scope=self._serialized_provider_call(getattr(self._request_context,"deadline",None))
        try:
            scope.__enter__()
        except Exception as error:
            raise MessageSendNotAttempted("reply provider lock is unavailable") from error
        try:
            yield
        finally:
            scope.__exit__(None,None,None)

    def send_reply_text_message(self, chat_id: int, text: str, *, reply_source: ReplySource,
                                expected_account_id: int, expected_recipient_title: str,
                                attempt_id: str, pre_send_guard: Callable[[], bool]) -> int:
        """Revalidate cached/offline TDLib evidence under one local provider lock.

        Remote edits may still race these reads; exact output reply correlation is
        mandatory. No refusal before registration/rawsend creates a send attempt.
        """
        from .sanitize import sanitize_telegram_text
        with self._serialized_reply_call():
            try:
                if (not isinstance(reply_source,ReplySource) or reply_source.anchor[0]!=chat_id
                        or type(expected_account_id) is not int or self.get_account_id()!=expected_account_id):
                    raise ValueError('reply source is unavailable')
                chat=self.resolve_target(chat_id)
                title=sanitize_telegram_text(chat.get('title'),max_length=255) or str(chat_id)
                if title!=expected_recipient_title:
                    raise ValueError('reply recipient is unavailable')
                current=self.read_reply_source(*reply_source.anchor)
                if current.source_sha256!=reply_source.source_sha256:
                    raise ValueError('reply source changed')
                properties=self._call({'@type':'getMessageProperties','chat_id':chat_id,'message_id':reply_source.anchor[1]})
                if properties.get('@type')!='messageProperties' or properties.get('can_be_replied') is not True:
                    raise ValueError('reply eligibility is unavailable')
                if self.get_account_id()!=expected_account_id or pre_send_guard() is not True:
                    raise ValueError('reply scope changed')
                content={'@type':'inputMessageText','text':{'@type':'formattedText','text':text,'entities':[]},
                         'link_preview_options':dict(LINK_OPTIONS),'clear_draft':False}
            except Exception as error:
                raise MessageSendNotAttempted('reply revalidation failed') from error
            return self._send_message_content(chat_id,content,'messageText',attempt_id=attempt_id,
                reply_anchor=reply_source.anchor,**({'pre_transport_guard':pre_send_guard} if reply_source.required_capabilities else {}))

    def send_reply_artifact_message(self,chat_id: int,path: Path,caption: str,*,kind: str,
            reply_source: ReplySource,expected_account_id: int,expected_recipient_title: str,
            attempt_id: str,pre_send_guard: Callable[[],bool],duration_seconds: int | None=None,
            waveform_base64: str | None=None) -> int:
        from .sanitize import sanitize_telegram_text
        self._request_context.artifact_transport_attempted=False
        try:
            with self._serialized_reply_call():
                if (not isinstance(reply_source,ReplySource) or reply_source.anchor[0]!=chat_id
                    or type(expected_account_id) is not int or self.get_account_id()!=expected_account_id):
                    raise ValueError('reply account changed')
                chat=self.resolve_target(chat_id)
                if (sanitize_telegram_text(chat.get('title'),max_length=255) or str(chat_id))!=expected_recipient_title:
                    raise ValueError('reply recipient changed')
                if self.read_reply_source(*reply_source.anchor).source_sha256!=reply_source.source_sha256:
                    raise ValueError('reply source changed')
                properties=self._call({'@type':'getMessageProperties','chat_id':chat_id,'message_id':reply_source.anchor[1]})
                if properties.get('@type')!='messageProperties' or properties.get('can_be_replied') is not True:
                    raise ValueError('reply eligibility changed')
                if self.get_account_id()!=expected_account_id or pre_send_guard() is not True:
                    raise ValueError('reply scope changed')
                formatted={'@type':'formattedText','text':caption,'entities':[]}
                local={'@type':'inputFileLocal','path':str(path)}
                if kind=='document':
                    content={'@type':'inputMessageDocument','document':{'@type':'inputDocument','document':local,
                        'thumbnail':None,'disable_content_type_detection':True},'caption':formatted}
                    expected='messageDocument'
                elif kind=='photo':
                    content={'@type':'inputMessagePhoto','photo':{'@type':'inputPhoto','photo':local,'thumbnail':None,
                        'video':None,'added_sticker_file_ids':[],'width':0,'height':0},'caption':formatted,
                        'show_caption_above_media':False,'self_destruct_type':None,'has_spoiler':False}
                    expected='messagePhoto'
                elif kind=='voice_note':
                    content={'@type':'inputMessageVoiceNote','voice_note':{'@type':'inputVoiceNote','voice_note':local,
                        'duration':duration_seconds,'waveform':waveform_base64},'caption':formatted,'self_destruct_type':None}
                    expected='messageVoiceNote'
                else:raise ValueError('unsupported reply kind')
                return self._send_message_content(chat_id,content,expected,attempt_id=attempt_id,
                    reply_anchor=reply_source.anchor,artifact_reply=True,pre_transport_guard=pre_send_guard)
        except (MessageSendOutcomeUnknown,MessageSendFailed):
            raise
        except Exception as error:
            if self._request_context.artifact_transport_attempted:
                raise MessageSendOutcomeUnknown("artifact reply observation was lost") from error
            self._send_observations.discard_unattempted(attempt_id)
            raise MessageSendNotAttempted('artifact reply was refused before transport') from error

    def send_photo_message(self, chat_id: int, path: Path, caption: str, *, attempt_id: str | None = None) -> int:
        content = {
            "@type": "inputMessagePhoto",
            "photo": {
                "@type": "inputPhoto", "photo": {"@type": "inputFileLocal", "path": str(path)},
                "thumbnail": None, "video": None, "added_sticker_file_ids": [], "width": 0, "height": 0,
            },
            "caption": {"@type": "formattedText", "text": caption, "entities": []},
            "show_caption_above_media": False, "self_destruct_type": None, "has_spoiler": False,
        }
        return self._send_message_content(chat_id, content, "messagePhoto", attempt_id=attempt_id)

    def send_voice_note_message(self, chat_id: int, path: Path, caption: str,
                                duration_seconds: int, waveform_base64: str, *, attempt_id: str | None = None) -> int:
        content = {
            "@type": "inputMessageVoiceNote",
            "voice_note": {
                "@type": "inputVoiceNote",
                "voice_note": {"@type": "inputFileLocal", "path": str(path)},
                "duration": duration_seconds, "waveform": waveform_base64,
            },
            "caption": {"@type": "formattedText", "text": caption, "entities": []},
            "self_destruct_type": None,
        }
        return self._send_message_content(chat_id, content, "messageVoiceNote", attempt_id=attempt_id)

    def _send_message_content(
        self, chat_id: int, content: dict[str, Any], expected_type: str,
        *, attempt_id: str | None = None, reply_anchor: tuple[int,int] | None = None, artifact_reply: bool = False, pre_transport_guard: Callable[[],bool] | None = None,
    ) -> int:
        sending_id = secrets.randbelow((1 << 31) - 1) + 1
        for _ in range(8):
            if self._send_observations.sending_id_available(sending_id):
                break
            sending_id = secrets.randbelow((1 << 31) - 1) + 1
        else:
            raise MessageSendNotAttempted("send correlation is unavailable")
        payload = {
            "@type": "sendMessage", "chat_id": chat_id, "topic_id": None,
            "reply_to": ({"@type":"inputMessageReplyToMessage","message_id":reply_anchor[1],
                "quote":None,"checklist_task_id":0,"poll_option_id":""} if reply_anchor is not None else None), "reply_markup": None,
            "options": {**SEND_OPTIONS,"sending_id":sending_id},
            "input_message_content": content,
        }
        _validate_bounded_request(payload)
        with self._serialized_provider_call():
            if not self._ready:
                raise AuthorizationBlocked("the dedicated TDLib session is not authorizationStateReady")
            extra = f"telegram-search-mcp-{next(self._ids)}"
            attempt_id = attempt_id if attempt_id is not None else extra
            aggregate_deadline = getattr(self._request_context, "deadline", None)
            if reply_anchor is not None and aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                raise MessageSendNotAttempted("reply request deadline expired before send")
            try:
                self._send_observations.register(attempt_id, extra, sending_id, chat_id, expected_type,
                    text_sha256=hashlib.sha256(content["text"]["text"].encode("utf-8")).hexdigest()
                    if expected_type == "messageText" else None, reply_anchor=reply_anchor,
                    **({'caption_sha256':hashlib.sha256(content['caption']['text'].encode('utf-8')).hexdigest(),
                        'voice_duration':content['voice_note']['duration'] if expected_type=='messageVoiceNote' else None,
                        'waveform_sha256':hashlib.sha256(base64.b64decode(content['voice_note']['waveform'],validate=True)).hexdigest() if expected_type=='messageVoiceNote' else None,
                        'waveform_size':len(base64.b64decode(content['voice_note']['waveform'],validate=True)) if expected_type=='messageVoiceNote' else None} if artifact_reply else {}))
            except ObservationUnavailable as error:
                raise MessageSendNotAttempted("send correlation is unavailable") from error
            if pre_transport_guard is not None:
                try:
                    if pre_transport_guard() is not True:
                        raise ValueError('reply scope changed')
                except Exception as error:
                    self._send_observations.discard_unattempted(attempt_id)
                    raise MessageSendNotAttempted("reply scope changed before transport") from error
            if reply_anchor is not None and aggregate_deadline is not None and time.monotonic() >= aggregate_deadline:
                self._send_observations.discard_unattempted(attempt_id)
                raise MessageSendNotAttempted("reply request deadline expired before transport")
            try:
                if artifact_reply:self._request_context.artifact_transport_attempted=True
                self._raw.send({**payload, "@extra": extra})
            except Exception as error:
                raise MessageSendOutcomeUnknown("TDLib send transport failed") from error
            deadline = time.monotonic() + min(max(self._request_timeout, 30), 300)
            aggregate_deadline = getattr(self._request_context, "deadline", None)
            if aggregate_deadline is not None:
                deadline = min(deadline, aggregate_deadline)
            while time.monotonic() < deadline:
                try:
                    response = self._raw.receive(min(1.0, max(0.0, deadline - time.monotonic())))
                except Exception as error:
                    raise MessageSendOutcomeUnknown("TDLib confirmation transport was lost") from error
                if response is None:
                    continue
                self._reduce_receive_event(response)
                if not self._ready:
                    raise MessageSendOutcomeUnknown("authorization changed before confirmation")
                observation = self._send_observations.snapshot(attempt_id)
                if observation is not None and observation.status == "failed":
                    provider_error = response if response.get("@type") == "error" else response.get("error")
                    raise MessageSendFailed("TDLib reported send failure", provider_error=provider_error)
                if observation is not None and observation.status == "sent":
                    return observation.message_id
                if (observation is not None and response.get("@type") == "updateMessageSendSucceeded"
                        and type(response.get("old_message_id")) is int
                        and response["old_message_id"] == observation.temporary_id):
                    raise MessageSendOutcomeUnknown("TDLib returned an invalid final message")
                if response.get("@extra") == extra and observation is not None and observation.status != "pending":
                    raise MessageSendOutcomeUnknown("TDLib returned an invalid preliminary message")
            raise MessageSendOutcomeUnknown("TDLib delivery confirmation timed out")

    def download_file(
        self, file_id: int, *, max_bytes: int = 64 * 1024 * 1024,
        timeout: float = 300.0, poll_seconds: float = 0.2
    ) -> Path:
        """Download one internally selected file without holding the provider lock while waiting."""
        if not _is_strict_integer(file_id) or not 0 < file_id <= (1 << 31) - 1:
            raise ValueError("file_id is invalid")
        if not 0 <= timeout <= 540 or not 0 <= poll_seconds <= 2:
            raise ValueError("download timing is invalid")
        if max_bytes not in {64 * 1024 * 1024, 256 * 1024 * 1024}:
            raise ValueError("download size limit is invalid")
        deadline = time.monotonic() + timeout
        if not self._download_lock.acquire(timeout=timeout):
            raise TimeoutError("Telegram file download queue expired")
        completed = False
        started = False
        try:
            state = self._call({
                "@type": "downloadFile", "file_id": file_id, "priority": 1,
                "offset": 0, "limit": max_bytes + 1, "synchronous": False,
            })
            started = True
            while True:
                if state.get("@type") != "file" or state.get("id") != file_id:
                    raise TDLibError("TDLib returned an invalid file")
                local = state.get("local")
                if not isinstance(local, dict):
                    raise TDLibError("TDLib returned an invalid local file state")
                if any(type(value) is int and value > max_bytes for value in (
                    state.get("size"), state.get("expected_size"), local.get("downloaded_size"),
                )):
                    raise DownloadTooLarge("Telegram file exceeds the transfer limit")
                if local.get("is_downloading_completed") is True:
                    path = local.get("path")
                    if not isinstance(path, str) or not path or not Path(path).is_absolute():
                        raise TDLibError("TDLib returned an invalid completed path")
                    completed = True
                    return Path(path)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Telegram file download deadline expired")
                if poll_seconds:
                    time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
                state = self._call({"@type": "getFile", "file_id": file_id})
        finally:
            if started and not completed:
                try:
                    self._call({
                        "@type": "cancelDownloadFile", "file_id": file_id,
                        "only_if_pending": False,
                    })
                except TDLibError:
                    pass
            self._download_lock.release()

    def get_context_messages(
        self, chat_id: int, message_id: int, radius: int
    ) -> list[dict[str, Any]]:
        if not 1 <= radius <= 4:
            return []
        response = self._call(
            {
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": message_id,
                "offset": -radius,
                "limit": radius * 2 + 1,
                "only_local": False,
            }
        )
        messages = response.get("messages")
        if response.get("@type") != "messages" or not isinstance(messages, list):
            raise TDLibError("TDLib returned invalid message context")
        return [item for item in messages if isinstance(item, dict)]

    def get_message_link(self, chat_id: int, message_id: int) -> str | None:
        try:
            response = self._call(
                {
                    "@type": "getMessageLink",
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "media_timestamp": 0,
                    "checklist_task_id": 0,
                    "poll_option_id": "",
                    "for_album": False,
                    "in_message_thread": False,
                }
            )
        except AuthorizationBlocked:
            raise
        except TDLibError:
            return None
        link = response.get("link")
        return link if response.get("@type") == "messageLink" and isinstance(link, str) else None

    def get_chat_link(self, chat: dict[str, Any]) -> str | None:
        # TDLib does not return a canonical chat URL for this boundary. Do not
        # derive one from usernames; optional URL fields remain empty unless a
        # provider operation returns the complete link directly.
        del chat
        return None

    def get_sender_identity(self, sender: dict[str, Any]) -> dict[str, Any]:
        """Hydrate exactly one sender, verifying the provider's returned identity."""
        kind = sender.get("@type")
        if kind == "messageSenderUser":
            identifier = sender.get("user_id")
            if not _is_strict_integer(identifier) or not 0 < identifier < 2**53:
                raise TDLibError("invalid sender identity")
            user = self._call({"@type": "getUser", "user_id": identifier})
            if user.get("@type") != "user" or type(user.get("id")) is not int or user["id"] != identifier:
                raise TDLibError("invalid sender identity")
            first, last = user.get("first_name"), user.get("last_name")
            if not isinstance(first, str) or not isinstance(last, str):
                raise TDLibError("invalid sender name")
            return {"kind": "user", "id": identifier, "display_name": " ".join(p for p in (first, last) if p)}
        if kind == "messageSenderChat":
            identifier = sender.get("chat_id")
            if not _is_strict_nonzero_integer(identifier) or not -(2**53) < identifier < 2**53:
                raise TDLibError("invalid sender identity")
            chat = self._call({"@type": "getChat", "chat_id": identifier})
            if (chat.get("@type") != "chat" or type(chat.get("id")) is not int or
                    chat["id"] != identifier or not isinstance(chat.get("title"), str)):
                raise TDLibError("invalid sender identity")
            return {"kind": "chat", "id": identifier, "display_name": chat["title"]}
        raise TDLibError("unsupported sender identity")

    def get_sender_name(self, message: dict[str, Any]) -> str:
        sender = message.get("sender_id") or {}
        sender_type = sender.get("@type")
        if sender_type == "messageSenderUser" and isinstance(sender.get("user_id"), int):
            user = self._call({"@type": "getUser", "user_id": sender["user_id"]})
            pieces = [str(user.get("first_name") or ""), str(user.get("last_name") or "")]
            name = " ".join(piece for piece in pieces if piece).strip()
            usernames = (user.get("usernames") or {}).get("active_usernames") or []
            username = usernames[0] if usernames and isinstance(usernames[0], str) else None
            if username:
                return f"{name} (@{username})" if name else f"@{username}"
            return name or f"user:{sender['user_id']}"
        if sender_type == "messageSenderChat" and isinstance(sender.get("chat_id"), int):
            chat = self._call({"@type": "getChat", "chat_id": sender["chat_id"]})
            title = chat.get("title")
            return str(title) if title else f"chat:{sender['chat_id']}"
        return "unknown sender"

    def close(self) -> None:
        self._invalidate_send_observations()
        self._ready = False
        self._raw.close()
