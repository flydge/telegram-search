"""Evidence-backed search orchestration for one exact Telegram chat."""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from .discovery_state import CatalogPosition, ChatListName, DiscoveryRegistry
from .verified_targets import VerifiedTargetReader
from .chat_list_reader import ChatListReader
from .history_reader import HistoryReader
from .topic_history_reader import TopicHistoryReader
from .exact_search_reader import ExactSearchReader
from .selected_search_reader import SelectedSearchReader
from .forum_reader import ForumTopicReader
from .sanitize import normalize_search_text, render_evidence, sanitize_telegram_text
from .schemas import (
    VerifyTargetRequest, VerifyTargetResponse, ReadTargetMessagesRequest, ReadTargetMessagesResponse,
    ContextEvidence,
    Coverage,
    DiscoverTargetsRequest,
    EvidenceAnchor,
    FileEvidence,
    ResolveTargetRequest,
    ReadHistoryRequest, ReadHistoryResponse, ListTopicsRequest, ListTopicsResponse,
    ReadTopicHistoryRequest, ReadTopicHistoryResponse, ListChatsRequest, ListChatsResponse,
    SearchMessagesRequest, SearchMessagesResponse, SearchChatsRequest, SearchChatsResponse,
    SearchMatch,
    SearchRequest,
    SearchResponse,
    SourceEvidence,
    TargetDiscoveryResponse,
    TargetResolutionResponse,
)
from .target_discovery import TargetDiscoveryService, terminal_discovery_response
from .target_resolver import TargetResolver, terminal_resolution_response
from .tdjson import (
    AuthorizationBlocked,
    GlobalMessagePage,
    MessageNotFound,
    SecretChatRejected,
    TDLibError,
)


class ReadOnlyTelegramClient(Protocol):
    def get_account_id(self) -> int: ...

    def request_budget(self, deadline: float) -> AbstractContextManager[None]: ...

    def ensure_ready(self) -> None: ...

    def resolve_target(self, target: str | int) -> dict[str, Any]: ...

    def get_self_chat(self) -> dict[str, Any]: ...

    def search_known_chat_ids(self, query: str) -> tuple[list[int], bool]: ...

    def search_known_chat_ids_on_server(self, query: str) -> tuple[list[int], bool]: ...

    def get_recent_main_chat_ids(self) -> list[int]: ...

    def get_chat_list_prefix(self, chat_list: ChatListName) -> list[int]: ...

    def get_chat_list_snapshot(self, chat_list: ChatListName, *, limit: int) -> dict[str, Any]: ...

    def get_chat_metadata(self, chat_id: int) -> dict[str, Any]: ...

    def load_more_chats(
        self,
        chat_list: ChatListName,
        on_positions: Callable[[list[CatalogPosition]], None],
    ) -> bool: ...

    def search_global_messages(
        self,
        chat_list: ChatListName,
        query: str,
        *,
        offset: str,
    ) -> GlobalMessagePage: ...

    def search_chat_messages(
        self, chat_id: int, query: str, *, from_message_id: int, limit: int
    ) -> dict[str, Any]: ...

    def search_chat_media(
        self, chat_id: int, media_type: str, *, from_message_id: int, limit: int
    ) -> dict[str, Any]: ...

    def resolve_forum_chat(self, chat_id: int) -> dict[str, Any]: ...

    def get_forum_topics(self, chat_id: int, *, offset_date: int = 0,
                         offset_message_id: int = 0, offset_forum_topic_id: int = 0,
                         limit: int = 20) -> dict[str, Any]: ...

    def get_forum_topic(self, chat_id: int, forum_topic_id: int) -> dict[str, Any] | None: ...

    def get_chat_history(
        self, chat_id: int, *, from_message_id: int, limit: int
    ) -> list[dict[str, Any]]: ...

    def get_message(self, chat_id: int, message_id: int) -> dict[str, Any]: ...

    def get_sender_name(self, message: dict[str, Any]) -> str: ...

    def get_context_messages(
        self, chat_id: int, message_id: int, radius: int
    ) -> list[dict[str, Any]]: ...

    def get_chat_link(self, chat: dict[str, Any]) -> str | None: ...

    def get_message_link(self, chat_id: int, message_id: int) -> str | None: ...

    def close(self) -> None: ...


_FILE_CONTENTS: dict[str, tuple[str, str, str | None]] = {
    "messageDocument": ("document", "document", None),
    "messagePhoto": ("photo", "photo", "image/jpeg"),
    "messageVideo": ("video", "video", None),
    "messageAudio": ("audio", "audio", None),
    "messageAnimation": ("animation", "animation", None),
    "messageVoiceNote": ("voice_note", "voice_note", "audio/ogg"),
    "messageVideoNote": ("video_note", "video_note", "video/mp4"),
    "messageSticker": ("sticker", "sticker", None),
}

_MATCH_TEXT_LIMIT = 2**31 - 1
_MATCH_SNIPPET_BODY_LIMIT = 1800
_TDLIB_SERVER_MESSAGE_ID_STEP = 1 << 20
_TEXT_TOKEN = re.compile(r"\w+", re.UNICODE)
_NUMBER_SEQUENCE = re.compile(r"\d+", re.UNICODE)


def _formatted_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    text = value.get("text")
    return text if isinstance(text, str) else ""


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    if content.get("@type") == "messageText":
        return _formatted_text(content.get("text"))
    return _formatted_text(content.get("caption"))


def _normalize_match_text(value: object) -> str:
    """Apply the same unbounded NFKC/control/whitespace/case rule to query and evidence."""
    return normalize_search_text(value, max_length=_MATCH_TEXT_LIMIT)


def _display_span_for_folded_span(
    display_text: str, folded_start: int, folded_end: int
) -> tuple[int, int]:
    offset = 0
    display_start = 0
    display_end = len(display_text)
    found_start = False
    for index, character in enumerate(display_text):
        next_offset = offset + len(character.casefold())
        if not found_start and folded_start < next_offset:
            display_start = index
            found_start = True
        if found_start and folded_end <= next_offset:
            display_end = index + 1
            break
        offset = next_offset
    return display_start, display_end


def _provider_match_spans(
    display_text: str, normalized_query: str
) -> list[tuple[int, int]] | None:
    if not normalized_query:
        return None
    folded = display_text.casefold()
    phrase_start = folded.find(normalized_query)
    if phrase_start >= 0:
        return [
            _display_span_for_folded_span(
                display_text, phrase_start, phrase_start + len(normalized_query)
            )
        ]

    query_tokens = list(dict.fromkeys(_TEXT_TOKEN.findall(normalized_query)))
    if not query_tokens:
        return None
    wanted = set(query_tokens)
    found: dict[str, tuple[int, int]] = {}
    for match in _TEXT_TOKEN.finditer(folded):
        token = match.group(0)
        if token in wanted and token not in found:
            found[token] = _display_span_for_folded_span(
                display_text, match.start(), match.end()
            )
    if any(token not in found for token in query_tokens):
        return None
    return sorted(found[token] for token in query_tokens)


def _render_match_evidence(
    display_text: str, match_spans: list[tuple[int, int]]
) -> str:
    if len(display_text) <= _MATCH_SNIPPET_BODY_LIMIT:
        return render_evidence(display_text)
    match_start = min(start for start, _ in match_spans)
    match_end = max(end for _, end in match_spans)
    if match_end - match_start > _MATCH_SNIPPET_BODY_LIMIT - 2:
        excerpts = " … ".join(display_text[start:end] for start, end in match_spans)
        return render_evidence("…" + excerpts + "…")
    match_length = match_end - match_start
    remaining = max(0, _MATCH_SNIPPET_BODY_LIMIT - match_length - 2)
    slice_start = max(0, match_start - remaining // 2)
    slice_end = min(len(display_text), match_end + (remaining - (match_start - slice_start)))
    if slice_end - slice_start < _MATCH_SNIPPET_BODY_LIMIT - 2:
        slice_start = max(0, slice_end - (_MATCH_SNIPPET_BODY_LIMIT - 2))
    window = display_text[slice_start:slice_end]
    if slice_start > 0:
        window = "…" + window
    if slice_end < len(display_text):
        window += "…"
    return render_evidence(window)


def _strictly_older_history_cursor(message_id: int) -> int:
    """Map any pinned-TDLib message ID to the greatest valid server ID below it."""
    return ((message_id - 1) // _TDLIB_SERVER_MESSAGE_ID_STEP) * _TDLIB_SERVER_MESSAGE_ID_STEP


def _file_size(file_object: object) -> int | None:
    if not isinstance(file_object, dict):
        return None
    for field in ("size", "expected_size"):
        value = file_object.get(field)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _extract_file(message: dict[str, Any]) -> FileEvidence | None:
    content = message.get("content") or {}
    mapping = _FILE_CONTENTS.get(str(content.get("@type")))
    if mapping is None:
        return None
    media_type, object_key, default_mime = mapping
    media = content.get(object_key) or {}
    file_object: object = None
    if media_type == "photo":
        sizes = media.get("sizes") if isinstance(media, dict) else None
        if isinstance(sizes, list) and sizes:
            largest = max(
                (item for item in sizes if isinstance(item, dict)),
                key=lambda item: _file_size(item.get("photo")) or 0,
                default={},
            )
            file_object = largest.get("photo")
    elif isinstance(media, dict):
        file_key = {"voice_note": "voice", "video_note": "video"}.get(media_type, object_key)
        file_object = media.get(file_key)
    name = media.get("file_name") if isinstance(media, dict) else None
    mime = media.get("mime_type") if isinstance(media, dict) else None
    return FileEvidence(
        name=sanitize_telegram_text(name, max_length=255) or None,
        mime_type=sanitize_telegram_text(mime or default_mime, max_length=127).casefold() or None,
        media_type=media_type,
        size=_file_size(file_object),
    )


def _timestamp(message: dict[str, Any]) -> int | None:
    value = message.get("date")
    return value if isinstance(value, int) and value >= 0 else None


def _date_utc(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _metadata_requested(request: SearchRequest) -> bool:
    return any(
        value is not None
        for value in (
            request.query.file_name,
            request.query.mime_type,
            request.query.media_type,
        )
    )


def _content_requested(request: SearchRequest) -> bool:
    return request.query.text is not None or request.query.contains_number is True


def terminal_search_response(
    request: SearchRequest,
    status: Literal["blocked", "error"],
) -> SearchResponse:
    """Build the existing redacted terminal search schema at any process boundary."""
    detail = (
        "TDLib authorization is not ready"
        if status == "blocked"
        else "Telegram search failed safely"
    )
    return SearchResponse(
        status=status,
        coverage=Coverage(
            complete=False,
            text_status="not_started" if _content_requested(request) else "not_requested",
            metadata_status=(
                "not_started" if _metadata_requested(request) else "not_requested"
            ),
            date_from=request.date_from,
            date_to=request.date_to,
            detail=detail,
        ),
        matches=[],
    )


class SearchService:
    def __init__(
        self,
        *,
        client: ReadOnlyTelegramClient,
        discovery_registry: DiscoveryRegistry | None = None,
        owns_client: bool = True,
        client_id: str | None = None,
        broker_generation: str | None = None,
    ) -> None:
        self._client = client
        self._owns_client = owns_client
        client_id = client_id or secrets.token_hex(16)
        broker_generation = broker_generation or secrets.token_hex(16)
        self._verified_target_reader = VerifiedTargetReader(client=client,
            client_id=client_id, broker_generation=broker_generation)
        self._chat_list_reader = ChatListReader(client=client,
            client_id=client_id, broker_generation=broker_generation)
        self._selected_search_reader = SelectedSearchReader(client=client,
            client_id=client_id, broker_generation=broker_generation)
        self._exact_search_reader = ExactSearchReader(client=client,
            client_id=client_id, broker_generation=broker_generation)
        self._topic_history_reader = TopicHistoryReader(client=client,
            client_id=client_id, broker_generation=broker_generation)
        self._forum_reader = ForumTopicReader(client=client,
            client_id=client_id, broker_generation=broker_generation)
        self._history_reader = HistoryReader(client=client,
            client_id=client_id, broker_generation=broker_generation)
        self._discovery_registry = (
            discovery_registry
            if discovery_registry is not None
            else DiscoveryRegistry(ttl_seconds=300, capacity=4)
        )

    def close_verified_targets(self) -> None:
        self._verified_target_reader.close()

    def close(self) -> None:
        self.close_verified_targets()
        self._chat_list_reader.close()
        self._exact_search_reader.close()
        self._selected_search_reader.close()
        self._topic_history_reader.close()
        self._history_reader.close()
        self._forum_reader.close()
        if self._owns_client:
            self._client.close()

    def verify_target(self, request: VerifyTargetRequest, *, deadline: float | None = None) -> VerifyTargetResponse:
        return self._verified_target_reader.verify(request, deadline=deadline)

    def read_target_messages(self, request: ReadTargetMessagesRequest, *, deadline: float | None = None) -> ReadTargetMessagesResponse:
        return self._verified_target_reader.read(request, deadline=deadline)

    def list_chats(self, request: ListChatsRequest, *, deadline: float | None = None) -> ListChatsResponse:
        return self._chat_list_reader.read(request, deadline=deadline)

    def search_chats(self, request: SearchChatsRequest, *, deadline: float | None = None) -> SearchChatsResponse:
        return self._selected_search_reader.read(request, deadline=deadline)

    def search_messages(self, request: SearchMessagesRequest, *, deadline: float | None = None) -> SearchMessagesResponse:
        return self._exact_search_reader.read(request, deadline=deadline)

    def read_topic_history(self, request: ReadTopicHistoryRequest, *, deadline: float | None = None) -> ReadTopicHistoryResponse:
        return self._topic_history_reader.read(request, deadline=deadline)

    def list_topics(self, request: ListTopicsRequest, *, deadline: float | None = None) -> ListTopicsResponse:
        return self._forum_reader.read(request, deadline=deadline)

    def read_history(self, request: ReadHistoryRequest, *, deadline: float | None = None) -> ReadHistoryResponse:
        return self._history_reader.read(request, deadline=deadline)

    def resolve(self, request: ResolveTargetRequest) -> TargetResolutionResponse:
        try:
            self._client.ensure_ready()
        except AuthorizationBlocked:
            return terminal_resolution_response("blocked")
        except TDLibError:
            return terminal_resolution_response("error")
        try:
            return TargetResolver(self._client).resolve(request)
        except AuthorizationBlocked:
            return terminal_resolution_response("blocked")
        except (SecretChatRejected, TDLibError):
            return terminal_resolution_response("error")

    def discover(self, request: DiscoverTargetsRequest) -> TargetDiscoveryResponse:
        try:
            self._client.ensure_ready()
        except AuthorizationBlocked:
            return terminal_discovery_response(request, "blocked")
        except TDLibError:
            return terminal_discovery_response(request, "error")
        return TargetDiscoveryService(
            client=self._client,
            registry=self._discovery_registry,
        ).discover(request)

    def search(self, request: SearchRequest, *, deadline: float | None = None) -> SearchResponse:
        budget = self._client.request_budget(deadline) if deadline is not None else nullcontext()
        with budget:
            return self._search(request, deadline)

    def _search(self, request: SearchRequest, deadline: float | None) -> SearchResponse:
        try:
            self._client.ensure_ready()
        except AuthorizationBlocked:
            return self._terminal_response(request, "blocked", "TDLib authorization is not ready")
        except TDLibError:
            return self._terminal_response(request, "error", "Telegram search failed safely")
        try:
            chat = self._client.resolve_target(request.target)
            chat_id = chat.get("id")
            if type(chat_id) is not int or chat_id == 0:
                raise TDLibError("TDLib returned an invalid chat")
            if type(request.target) is int and chat_id != request.target:
                raise TDLibError("TDLib returned a different chat")
            chat_link_candidate = self._client.get_chat_link(chat)
            chat_url = self._safe_link(chat_link_candidate)
            return self._search_chat(request, chat_id, chat_url, deadline)
        except AuthorizationBlocked:
            return self._terminal_response(request, "blocked", "TDLib authorization is not ready")
        except SecretChatRejected:
            return self._terminal_response(request, "error", "secret chats are not supported")
        except TDLibError:
            return self._terminal_response(request, "error", "Telegram search failed safely")

    def _terminal_response(
        self,
        request: SearchRequest,
        status: str,
        detail: str,
    ) -> SearchResponse:
        response = terminal_search_response(request, status)
        if response.coverage.detail == detail:
            return response
        return response.model_copy(
            update={"coverage": response.coverage.model_copy(update={"detail": detail})}
        )

    @staticmethod
    def _metadata_requested(request: SearchRequest) -> bool:
        return _metadata_requested(request)

    @staticmethod
    def _content_requested(request: SearchRequest) -> bool:
        return _content_requested(request)

    @staticmethod
    def _bounds(request: SearchRequest) -> tuple[int | None, int | None]:
        start = int(request.date_from.timestamp()) if request.date_from else None
        end = int(request.date_to.timestamp()) if request.date_to else None
        return start, end

    def _search_chat(
        self,
        request: SearchRequest,
        chat_id: int,
        chat_url: str | None,
        deadline: float | None,
    ) -> SearchResponse:
        date_from, date_to = self._bounds(request)
        provider_text_by_id: dict[int, str] | None = None
        text_complete = True
        if request.query.text is not None:
            provider_text_by_id, text_complete = self._direct_text_hits(
                request, chat_id, date_from, date_to
            )

        metadata_ids: set[int] | None = None
        metadata_complete = True
        if self._metadata_requested(request):
            metadata_ids, metadata_complete = self._direct_metadata_hits(
                request, chat_id, date_from, date_to
            )

        numeric_ids: set[int] | None = None
        numeric_complete = True
        if request.query.contains_number is True:
            numeric_ids, numeric_complete = self._direct_numeric_hits(
                request, chat_id, date_from, date_to
            )

        requested_sets = [
            values
            for values in (
                set(provider_text_by_id) if provider_text_by_id is not None else None,
                metadata_ids,
                numeric_ids,
            )
            if values is not None
        ]
        candidate_ids = set.intersection(*requested_sets) if requested_sets else set()

        matches, evidence_complete = self._rehydrate(
            request,
            chat_id,
            candidate_ids,
            provider_text_by_id,
            date_from,
            date_to,
            chat_url,
            deadline,
        )

        content_complete = text_complete and numeric_complete
        complete = content_complete and metadata_complete and evidence_complete
        status = "matches" if matches else "no_match"
        if not complete:
            status = "incomplete"
        return SearchResponse(
            status=status,
            coverage=Coverage(
                complete=complete,
                text_status=("complete" if content_complete else "incomplete")
                if self._content_requested(request)
                else "not_requested",
                metadata_status=("complete" if metadata_complete else "incomplete")
                if self._metadata_requested(request)
                else "not_requested",
                date_from=request.date_from,
                date_to=request.date_to,
                detail=(
                    "complete for the exact target and requested date range"
                    if complete
                    else "partial results; the requested range was not completely covered"
                ),
            ),
            matches=matches[: request.limit],
        )

    def _direct_numeric_hits(
        self,
        request: SearchRequest,
        chat_id: int,
        date_from: int | None,
        date_to: int | None,
    ) -> tuple[set[int], bool]:
        hits: set[int] = set()
        from_message_id = 0
        seen_offsets: set[int] = set()
        integrity_complete = True
        while True:
            messages = self._client.get_chat_history(
                chat_id, from_message_id=from_message_id, limit=100
            )
            if not messages:
                return hits, integrity_complete
            valid = [
                item
                for item in messages
                if type(item.get("id")) is int and item.get("chat_id") == chat_id
            ]
            if not valid:
                return hits, False

            timestamps: list[int] = []
            for message in valid:
                timestamp = _timestamp(message)
                if timestamp is None:
                    integrity_complete = False
                    continue
                timestamps.append(timestamp)
                if date_from is not None and timestamp < date_from:
                    continue
                if date_to is not None and timestamp > date_to:
                    continue
                if _NUMBER_SEQUENCE.search(_message_text(message)):
                    hits.add(message["id"])

            if not request.require_complete:
                return hits, False
            if date_from is not None and timestamps and min(timestamps) < date_from:
                return hits, integrity_complete

            oldest_id = min(item["id"] for item in valid)
            next_offset = _strictly_older_history_cursor(oldest_id)
            if next_offset <= 0:
                return hits, integrity_complete
            if next_offset == from_message_id or next_offset in seen_offsets:
                return hits, False
            seen_offsets.add(next_offset)
            from_message_id = next_offset

    def _direct_text_hits(
        self,
        request: SearchRequest,
        chat_id: int,
        date_from: int | None,
        date_to: int | None,
    ) -> tuple[dict[int, str], bool]:
        provider_text_by_id: dict[int, str] = {}
        from_message_id = 0
        seen_offsets: set[int] = set()
        while True:
            response = self._client.search_chat_messages(
                chat_id,
                request.query.text or "",
                from_message_id=from_message_id,
                limit=100,
            )
            messages = response.get("messages")
            if response.get("@type") != "foundChatMessages" or not isinstance(messages, list):
                return provider_text_by_id, False
            for message in messages:
                if not isinstance(message, dict) or message.get("chat_id") != chat_id:
                    continue
                timestamp = _timestamp(message)
                message_id = message.get("id")
                if timestamp is None or not isinstance(message_id, int):
                    continue
                if date_from is not None and timestamp < date_from:
                    continue
                if date_to is not None and timestamp > date_to:
                    continue
                provider_text_by_id[message_id] = _normalize_match_text(_message_text(message))
            next_offset = response.get("next_from_message_id")
            if next_offset == 0:
                return provider_text_by_id, True
            if not request.require_complete:
                return provider_text_by_id, False
            if (
                not isinstance(next_offset, int)
                or next_offset in seen_offsets
                or next_offset == from_message_id
                or not messages
            ):
                return provider_text_by_id, False
            seen_offsets.add(next_offset)
            from_message_id = next_offset

    def _direct_metadata_hits(
        self,
        request: SearchRequest,
        chat_id: int,
        date_from: int | None,
        date_to: int | None,
    ) -> tuple[set[int], bool]:
        if request.query.media_type in {"video", "video_note"}:
            return self._direct_media_hits(request, chat_id, date_from, date_to)
        hits: set[int] = set()
        from_message_id = 0
        seen_offsets: set[int] = set()
        while True:
            messages = self._client.get_chat_history(
                chat_id, from_message_id=from_message_id, limit=100
            )
            if not messages:
                return hits, True
            valid = [
                item
                for item in messages
                if type(item.get("id")) is int and item.get("chat_id") == chat_id
            ]
            if not valid:
                return hits, False
            for message in valid:
                timestamp = _timestamp(message)
                file_evidence = _extract_file(message)
                if timestamp is None or file_evidence is None:
                    continue
                if date_from is not None and timestamp < date_from:
                    continue
                if date_to is not None and timestamp > date_to:
                    continue
                if self._file_matches(request, file_evidence):
                    hits.add(message["id"])
            if not request.require_complete:
                return hits, False
            oldest_id = min(item["id"] for item in valid)
            next_offset = _strictly_older_history_cursor(oldest_id)
            if next_offset <= 0:
                return hits, True
            if next_offset == from_message_id or next_offset in seen_offsets:
                return hits, False
            seen_offsets.add(next_offset)
            from_message_id = next_offset

    def _direct_media_hits(
        self,
        request: SearchRequest,
        chat_id: int,
        date_from: int | None,
        date_to: int | None,
    ) -> tuple[set[int], bool]:
        """Traverse the provider's media index, never unrelated chat history."""
        hits: set[int] = set()
        from_message_id = 0
        integrity_complete = True
        while True:
            try:
                response = self._client.search_chat_media(
                    chat_id, request.query.media_type,
                    from_message_id=from_message_id, limit=100,
                )
            except AuthorizationBlocked:
                raise
            except TDLibError:
                return hits, False
            messages = response.get("messages")
            if response.get("@type") != "foundChatMessages" or not isinstance(messages, list):
                return hits, False
            for message in messages:
                if (
                    not isinstance(message, dict)
                    or type(message.get("id")) is not int or message["id"] <= 0
                    or type(message.get("chat_id")) is not int or message["chat_id"] != chat_id
                    or type(message.get("date")) is not int or message["date"] < 0
                    or not isinstance(message.get("content"), dict)
                ):
                    integrity_complete = False
                    continue
                file_evidence = _extract_file(message)
                if file_evidence is None or file_evidence.media_type != request.query.media_type:
                    integrity_complete = False
                    continue
                if date_from is not None and message["date"] < date_from:
                    continue
                if date_to is not None and message["date"] > date_to:
                    continue
                if self._file_matches(request, file_evidence):
                    hits.add(message["id"])
            next_offset = response.get("next_from_message_id")
            if type(next_offset) is not int or next_offset < 0:
                return hits, False
            if next_offset == 0:
                return hits, integrity_complete
            if (
                not request.require_complete or not messages
                or from_message_id > 0 and next_offset >= from_message_id
            ):
                return hits, False
            from_message_id = next_offset

    def _rehydrate(
        self,
        request: SearchRequest,
        chat_id: int,
        candidate_ids: set[int],
        provider_text_by_id: dict[int, str] | None,
        date_from: int | None,
        date_to: int | None,
        chat_url: str | None,
        deadline: float | None,
    ) -> tuple[list[SearchMatch], bool]:
        matches: list[SearchMatch] = []
        complete = True
        # TDLib message IDs are ordered chronologically. Enrich only the
        # newest requested matches; deleted/changed candidates are backfilled.
        for message_id in sorted(candidate_ids, reverse=True):
            if len(matches) >= request.limit:
                break
            if deadline is not None and time.monotonic() >= deadline:
                return matches, False
            try:
                message = self._client.get_message(chat_id, message_id)
            except MessageNotFound:
                continue
            except AuthorizationBlocked:
                raise
            except TDLibError:
                complete = False
                continue
            if (
                type(message.get("chat_id")) is not int
                or message["chat_id"] != chat_id
                or type(message.get("id")) is not int
                or message["id"] != message_id
            ):
                complete = False
                continue
            timestamp = _timestamp(message)
            if timestamp is None:
                complete = False
                continue
            if date_from is not None and timestamp < date_from:
                continue
            if date_to is not None and timestamp > date_to:
                continue

            text = sanitize_telegram_text(_message_text(message), max_length=_MATCH_TEXT_LIMIT)
            normalized_query = (
                _normalize_match_text(request.query.text)
                if request.query.text is not None
                else None
            )
            match_spans: list[tuple[int, int]] | None = None
            if normalized_query is not None:
                match_spans = _provider_match_spans(text, normalized_query)
                if match_spans is None:
                    provider_text = (provider_text_by_id or {}).get(message_id)
                    current_text = _normalize_match_text(text)
                    if not provider_text or provider_text != current_text:
                        complete = False
                        continue
                    # The provider matched this exact, unchanged content using
                    # semantics broader than our local tokenizer. Keep the
                    # rehydrated evidence instead of inventing a false absence.
                    match_spans = []
            if request.query.contains_number is True:
                number_spans = [match.span() for match in _NUMBER_SEQUENCE.finditer(text)]
                if not number_spans:
                    complete = False
                    continue
                match_spans = sorted([*(match_spans or []), *number_spans])
            file_evidence = _extract_file(message)
            if self._metadata_requested(request):
                if file_evidence is None or not self._file_matches(request, file_evidence):
                    continue
            context, context_complete = self._context(request, chat_id, message)
            complete = complete and context_complete
            link = self._safe_link(self._client.get_message_link(chat_id, message_id))
            try:
                sender = self._client.get_sender_name(message)
            except AuthorizationBlocked:
                raise
            except TDLibError:
                complete = False
                continue
            snippet_source = text or (file_evidence.name if file_evidence else "") or "message"
            snippet = (
                _render_match_evidence(snippet_source, match_spans)
                if match_spans
                else render_evidence(snippet_source)
            )
            matches.append(
                SearchMatch(
                    chat_id=chat_id,
                    message_id=message_id,
                    date_utc=_date_utc(timestamp),
                    sender=sanitize_telegram_text(
                        sender, max_length=256
                    ),
                    snippet=snippet,
                    context=context,
                    file=file_evidence,
                    source=SourceEvidence(
                        telegram_url=link,
                        chat_url=chat_url,
                        evidence_anchor=EvidenceAnchor(chat_id=chat_id, message_id=message_id),
                    ),
                )
            )
        matches.sort(key=lambda item: (item.date_utc, item.message_id), reverse=True)
        return matches, complete

    @staticmethod
    def _file_matches(request: SearchRequest, file_evidence: FileEvidence) -> bool:
        if request.query.file_name is not None and normalize_search_text(
            request.query.file_name, max_length=255
        ) not in normalize_search_text(file_evidence.name or "", max_length=255):
            return False
        if request.query.mime_type is not None and request.query.mime_type.casefold() != (
            file_evidence.mime_type or ""
        ).casefold():
            return False
        if request.query.media_type is not None and request.query.media_type != file_evidence.media_type:
            return False
        return True

    def _context(
        self, request: SearchRequest, chat_id: int, message: dict[str, Any]
    ) -> tuple[list[ContextEvidence], bool]:
        radius = request.context_messages
        if radius == 0:
            return [], True
        try:
            candidates = self._client.get_context_messages(chat_id, message["id"], radius)
        except AuthorizationBlocked:
            raise
        except TDLibError:
            return [], False
        center = (_timestamp(message) or 0, int(message["id"]))
        before: list[dict[str, Any]] = []
        after: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.get("chat_id") != chat_id or candidate.get("id") == message.get("id"):
                continue
            timestamp = _timestamp(candidate)
            message_id = candidate.get("id")
            if timestamp is None or not isinstance(message_id, int):
                continue
            (before if (timestamp, message_id) < center else after).append(candidate)
        selected = sorted(before, key=lambda item: (_timestamp(item) or 0, item["id"]))[-radius:]
        selected += sorted(after, key=lambda item: (_timestamp(item) or 0, item["id"]))[:radius]
        return [
            ContextEvidence(
                message_id=item["id"],
                date_utc=_date_utc(_timestamp(item) or 0),
                sender=sanitize_telegram_text(self._client.get_sender_name(item), max_length=256),
                snippet=render_evidence(_message_text(item) or "message"),
            )
            for item in selected
        ], True

    @staticmethod
    def _safe_link(value: str | None) -> str | None:
        if value is None:
            return None
        value = sanitize_telegram_text(value, max_length=512)
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in {"t.me", "telegram.me"}:
            return None
        return value
