"""Bounded, versioned JSON framing for the private local broker."""

from __future__ import annotations

import json
import math
import re
import socket
import struct
from typing import Any, Final

PROTOCOL_VERSION: Final = 2
MAX_REQUEST_BYTES: Final = 1024 * 1024
MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024
MAX_DEADLINE_SECONDS: Final = 570.0
FRAME_READ_TIMEOUT_SECONDS: Final = 0.25
OPERATIONS: Final = frozenset(
    {"prepare_reply_artifact_send", "get_reply_artifact_draft", "update_reply_artifact_draft", "refresh_reply_artifact_draft", "prepare_reply_text_send", "get_reply_draft", "update_reply_draft", "refresh_reply_draft", "update_draft", "refresh_draft", "get_send_status"} |
    {"list_drafts", "get_draft", "cancel_draft", "read_presentation", "read_spreadsheet", "read_attachment_page", "verify_target", "read_target_messages", "search_chats", "list_chats", "search_messages", "read_topic_history", "list_topics", "read_reply_chain", "read_history", "read_messages", "handshake", "resolve", "discover", "search", "get_message_context", "get_attachment", "read_attachment", "analyze_media", "create_local_artifact", "begin_local_upload", "append_local_upload", "finish_local_upload", "prepare_text_send", "send_prepared_text", "prepare_artifact_send", "send_prepared_artifact", "check", "health", "release_client"}
)

_REQUEST_KEYS = {
    "version",
    "client_id",
    "request_id",
    "operation",
    "payload",
    "deadline",
    "broker_generation",
}
_IDENTIFIER = re.compile(r"^(?:client|request)_[A-Za-z0-9_-]{24,80}$")


class BrokerProtocolError(RuntimeError):
    """Raised when an IPC peer violates the private broker contract."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _read_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise BrokerProtocolError("connection closed during frame")
        chunks.extend(chunk)
    return bytes(chunks)


def send_frame(connection: socket.socket, value: object, *, max_bytes: int) -> None:
    """Send one compact UTF-8 JSON value with a four-byte network-order length."""
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise BrokerProtocolError("frame is not strict JSON") from error
    if not body or len(body) > max_bytes:
        raise BrokerProtocolError("frame size is outside the allowed bound")
    connection.sendall(struct.pack("!I", len(body)) + body)


def receive_frame(connection: socket.socket, *, max_bytes: int) -> object:
    """Receive exactly one bounded JSON frame."""
    (size,) = struct.unpack("!I", _read_exact(connection, 4))
    if size < 1 or size > max_bytes:
        raise BrokerProtocolError("frame size is outside the allowed bound")
    raw = _read_exact(connection, size)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BrokerProtocolError("frame is not strict JSON") from error


def validate_request(value: object) -> dict[str, Any]:
    """Validate the fixed request envelope before operation dispatch."""
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise BrokerProtocolError("request envelope is invalid")
    if value["version"] != PROTOCOL_VERSION or type(value["version"]) is not int:
        raise BrokerProtocolError("protocol version is unsupported")
    for name, prefix in (("client_id", "client_"), ("request_id", "request_")):
        identifier = value[name]
        if (
            not isinstance(identifier, str)
            or not identifier.startswith(prefix)
            or not _IDENTIFIER.fullmatch(identifier)
        ):
            raise BrokerProtocolError(f"{name} is invalid")
    operation = value["operation"]
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise BrokerProtocolError("operation is not allowed")
    if not isinstance(value["payload"], dict):
        raise BrokerProtocolError("payload must be an object")
    generation = value["broker_generation"]
    if generation is not None and (not isinstance(generation, str) or not re.fullmatch(r"broker_[0-9a-f]{32}", generation)):
        raise BrokerProtocolError("broker generation is invalid")
    deadline = value["deadline"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0:
        raise BrokerProtocolError("deadline is invalid")
    return value


def receive_request(connection: socket.socket) -> dict[str, Any]:
    return validate_request(receive_frame(connection, max_bytes=MAX_REQUEST_BYTES))
