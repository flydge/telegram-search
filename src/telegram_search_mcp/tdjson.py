"""Narrow legacy tdjson ABI wrapper and read-only TDLib client."""

from __future__ import annotations

import ctypes
import itertools
import json
import os
import platform
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
        "searchMessages",
        "searchChatMessages",
        "getMessage",
        "getChatHistory",
        "getMessageLink",
        "getUser",
    }
)

_AUTHORIZATION_CONTROL_TYPES = frozenset(
    {"getAuthorizationState", "setTdlibParameters", "checkDatabaseEncryptionKey"}
)


def _is_strict_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_nonzero_integer(value: object) -> bool:
    return _is_strict_integer(value) and value != 0


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


def _validate_bounded_request(payload: dict[str, Any]) -> None:
    request_type = payload.get("@type")
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
        self._metrics_lock = threading.Lock()
        self._serialization_wait_count = 0
        self._ready = False
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
    def _serialized_provider_call(self):
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            with self._metrics_lock:
                self._serialization_wait_count += 1
            self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_type = payload.get("@type")
        if not isinstance(request_type, str) or request_type not in _ALLOWED_REQUEST_TYPES:
            raise ForbiddenTDLibRequest("TDLib request is not allowed")
        _validate_bounded_request(payload)
        with self._serialized_provider_call():
            if request_type not in _AUTHORIZATION_CONTROL_TYPES and not self._ready:
                raise AuthorizationBlocked(
                    "the dedicated TDLib session is not authorizationStateReady"
                )
            extra = f"telegram-search-mcp-{next(self._ids)}"
            request = dict(payload)
            request["@extra"] = extra
            self._raw.send(request)
            deadline = time.monotonic() + self._request_timeout
            while time.monotonic() < deadline:
                response = self._raw.receive(min(1.0, max(0.0, deadline - time.monotonic())))
                if response is None:
                    continue
                self._apply_catalog_update(response)
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
        raise TDLibError("TDLib request timed out")

    def ensure_ready(self) -> None:
        for _ in range(8):
            state = self._call({"@type": "getAuthorizationState"}).get("@type")
            self._ready = state == "authorizationStateReady"
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
        if (chat.get("type") or {}).get("@type") == "chatTypeSecret":
            raise SecretChatRejected("secret chats are not supported")
        return chat

    def get_account_id(self) -> int:
        user = self._call({"@type": "getMe"})
        account_id = user.get("id")
        if user.get("@type") != "user" or not isinstance(account_id, int) or account_id <= 0:
            raise TDLibError("TDLib returned an invalid account identity")
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
    ) -> dict[str, Any]:
        return self._call(
            {
                "@type": "searchChatMessages",
                "chat_id": chat_id,
                "topic_id": None,
                "query": query,
                "sender_id": None,
                "from_message_id": from_message_id,
                "offset": 0,
                "limit": min(max(limit, 1), 100),
                "filter": None,
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
        if response.get("@type") != "messages" or not isinstance(messages, list):
            raise TDLibError("TDLib returned an invalid chat history")
        return [item for item in messages if isinstance(item, dict)]

    def get_message(self, chat_id: int, message_id: int) -> dict[str, Any]:
        message = self._call(
            {"@type": "getMessage", "chat_id": chat_id, "message_id": message_id}
        )
        if message.get("@type") != "message":
            raise TDLibError("TDLib returned an invalid message")
        return message

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
        self._raw.close()
