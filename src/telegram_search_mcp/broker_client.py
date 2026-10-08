"""Thin per-process client for the owner-only TelegramSearch broker."""

from __future__ import annotations

import secrets
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .broker_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_DEADLINE_SECONDS,
    PROTOCOL_VERSION,
    BrokerProtocolError,
    receive_frame,
    send_frame,
)
from .config import RuntimePolicy, load_runtime_policy
from .contract import (CompatibilityError, COMPATIBILITY_CODES, contract_descriptor, require_compatible, fingerprint,
                       require_current, OPERATION_CAPABILITIES)
from .send_status_models import GetSendStatusRequest, GetSendStatusResponse, send_status
from .reply_artifact_drafts import (PrepareReplyArtifactSendRequest, PrepareReplyArtifactSendResponse,
    GetReplyArtifactDraftRequest, GetReplyArtifactDraftResponse, UpdateReplyArtifactDraftRequest,
    RefreshReplyArtifactDraftRequest, ReviseReplyArtifactDraftResponse)
from .reply_drafts import (PrepareReplyTextSendRequest, PrepareReplyTextSendResponse,
    GetReplyDraftRequest, GetReplyDraftResponse, UpdateReplyDraftRequest, RefreshReplyDraftRequest,
    ReviseReplyDraftResponse, UNAVAILABLE as REPLY_UNAVAILABLE)
from .draft_models import (ListDraftsRequest, ListDraftsResponse, GetDraftRequest, GetDraftResponse,
                           CancelDraftRequest, CancelDraftResponse,
                           UpdateDraftRequest, RefreshDraftRequest, ReviseDraftResponse)
from .schemas import (
    AnalyzeMediaRequest, AnalyzeMediaResponse, CreateLocalArtifactRequest, CreateLocalArtifactResponse,
    PrepareArtifactSendRequest, PrepareArtifactSendResponse, SendPreparedArtifactRequest, SendPreparedArtifactResponse,
    PrepareTextSendRequest, PrepareTextSendResponse, SendPreparedTextRequest, SendPreparedTextResponse,
    BeginLocalUploadRequest, BeginLocalUploadResponse, AppendLocalUploadRequest, AppendLocalUploadResponse,
    FinishLocalUploadRequest, FinishLocalUploadResponse,
    AttachmentRequest,
    AttachmentResponse,
    ReadReplyChainRequest, ReadReplyChainResponse,
    ListTopicsRequest, ListTopicsResponse, TopicScope,
    ListChatsRequest, ListChatsResponse, ChatListScope, ChatListCoverage,
    ReadTopicHistoryRequest, ReadTopicHistoryResponse, TopicHistoryScope,
    SearchMessagesRequest, SearchMessagesResponse, SearchMessagesScope, BooleanSearchQuery, SearchBranchCoverage,
    SearchChatsRequest, SearchChatsResponse, SearchChatsScope, SelectedChatCoverage,
    VerifyTargetRequest, VerifyTargetResponse, ReadTargetMessagesRequest, ReadTargetMessagesResponse,
    MessageContextRequest, ReadMessagesRequest, ReadMessagesResponse, ReadHistoryRequest, ReadHistoryResponse, HistoryScope,
    MessageContextResponse,
    ReadAttachmentRequest,
    ReadAttachmentResponse,
    DiscoverTargetsRequest,
    ResolveTargetRequest,
    SearchRequest,
    SearchResponse,
    TargetDiscoveryResponse,
    TargetResolutionResponse,
)
from .attachment_page_models import (ReadAttachmentPageRequest, ReadAttachmentPageResponse,
    AttachmentPageScope, terminal_attachment_page)
from .spreadsheet_models import (ReadSpreadsheetRequest, ReadSpreadsheetResponse, SpreadsheetScope, terminal_spreadsheet)
from .presentation_models import PRESENTATION_DETAIL, ReadPresentationRequest, ReadPresentationResponse, terminal_presentation
from .verified_targets import TargetGuardFailure
from .boolean_query import boolean_matches, boolean_seeds
from .search_service import terminal_search_response
from .target_discovery import terminal_discovery_response
from .target_resolver import terminal_resolution_response


@dataclass(frozen=True)
class _VerifiedTargetIssuance:
    chat_id: int
    chat_type: str
    expires_at: datetime
    expires_monotonic: float
    generation: str
    client_id: str
    contract_version: int = 1


@dataclass(frozen=True)
class _AttachmentContinuation:
    request_fingerprint: str
    scope: AttachmentPageScope
    text_end: int
    expires: float
    generation: str
    seen_cursors: frozenset[str]


@dataclass(frozen=True)
class _SpreadsheetContinuation:
    request_fingerprint: str
    scope: SpreadsheetScope
    cell_end: int
    expires: float
    generation: str
    seen_cursors: frozenset[str]
    calls: int


class _SpreadsheetGuardFailure(RuntimeError):
    def __init__(self, status: str = "invalid_cursor"):
        self.status = status
        super().__init__(status)


class _AttachmentGuardFailure(RuntimeError):
    def __init__(self, status: str = "invalid_cursor"):
        self.status = status
        super().__init__(status)


class BrokerUnavailable(RuntimeError):
    """Raised when the local broker cannot safely complete a request."""


class BrokerCompatibilityError(BrokerUnavailable):
    def __init__(self, code: str):
        self.code = code
        super().__init__("TelegramSearch compatibility check failed: " + code + "; inspect _manifest diagnostics")


@dataclass(frozen=True)
class _HistoryContinuation:
    request_fingerprint: str
    scope: HistoryScope
    last_result_id: int | None
    scanned: int
    expires: float


@dataclass(frozen=True)
class _TopicContinuation:
    request_fingerprint: str
    scope: TopicScope
    seen_ids: frozenset[int]
    seen_cursors: frozenset[str]
    scanned: int
    provider_pages: int
    expires: float


@dataclass(frozen=True)
class _TopicHistoryContinuation:
    request_fingerprint: str
    scope: TopicHistoryScope
    last_result_id: int | None
    seen_cursors: frozenset[str]
    scanned: int
    processed: int
    provider_pages: int
    expires: float


@dataclass(frozen=True)
class _SearchContinuation:
    request_fingerprint: str
    scope: SearchMessagesScope
    last_result_id: int | None
    seen_cursors: frozenset[str]
    scanned: int
    processed: int
    provider_pages: int
    expires: float
    branches: tuple[SearchBranchCoverage, ...] = ()


@dataclass(frozen=True)
class _ChatListContinuation:
    request_fingerprint: str
    scope: ChatListScope
    coverage: tuple[ChatListCoverage, ...]
    seen_ids: frozenset[int]
    seen_cursors: frozenset[str]
    expires: float


@dataclass(frozen=True)
class _SelectedSearchContinuation:
    request_fingerprint: str
    scope: SearchChatsScope
    coverage: tuple[SelectedChatCoverage, ...]
    index: int
    last_result_id: int | None
    seen_cursors: frozenset[str]
    expires: float


Connector = Callable[[Path], socket.socket]


def _connect(socket_path: Path) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(socket_path))
    except Exception:
        connection.close()
        raise
    return connection


def _request_launchd_restart() -> None:
    from .launch_agent import restart_launch_agent

    restart_launch_agent()


class BrokerClient:
    """Public-model-aware proxy with one isolated process-lifetime client ID."""

    def __init__(
        self,
        *,
        socket_path: Path,
        connector: Connector = _connect,
        request_timeout: float = 570.0,
        client_id: str | None = None,
        restart_callback: Callable[[], None] | None = None,
        retry_backoff_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
        policy: RuntimePolicy | None = None,
    ) -> None:
        if request_timeout <= 0 or request_timeout > MAX_DEADLINE_SECONDS:
            raise ValueError("request_timeout must be between zero and 570 seconds")
        if retry_backoff_seconds < 0 or retry_backoff_seconds > 5:
            raise ValueError("retry_backoff_seconds must be between zero and five")
        self._policy = policy if policy is not None else load_runtime_policy()
        self._has_client_state = False
        self._socket_path = socket_path
        self._connector = connector
        self._request_timeout = float(request_timeout)
        self._client_id = client_id or f"client_{secrets.token_urlsafe(24)}"
        self._restart_callback = restart_callback or _request_launchd_restart
        self._retry_backoff_seconds = float(retry_backoff_seconds)
        self._sleeper = sleeper
        self._closed = False
        self._history_lock = threading.Lock()
        self._history_states: dict[str, _HistoryContinuation] = {}
        self._history_active = 0
        self._topics_lock = threading.Lock()
        self._topics_states: dict[str, _TopicContinuation] = {}
        self._topics_active = 0
        self._search_lock = threading.Lock()
        self._search_states: dict[str, _SearchContinuation] = {}
        self._search_active = 0
        self._topic_history_lock = threading.Lock()
        self._topic_history_states: dict[str, _TopicHistoryContinuation] = {}
        self._topic_history_active = 0
        self._chat_list_lock = threading.Lock()
        self._chat_list_states: dict[str, _ChatListContinuation] = {}
        self._chat_list_active = 0
        self._selected_search_lock = threading.Lock()
        self._selected_search_states: dict[str, _SelectedSearchContinuation] = {}
        self._selected_search_active = 0
        self._target_lock = threading.Lock()
        self._target_states: dict[str, _VerifiedTargetIssuance] = {}
        self._target_active = 0
        self._target_pending = 0
        self._target_closed = False
        self._target_generation: str | None = None
        self._target_operation_state = threading.local()
        self._attachment_lock = threading.Lock()
        self._attachment_states: dict[str, _AttachmentContinuation] = {}
        self._attachment_active = 0
        self._attachment_pending = 0
        self._attachment_closed = False
        self._attachment_generation: str | None = None
        self._attachment_operation_state = threading.local()
        self._spreadsheet_lock = threading.Lock()
        self._spreadsheet_states: dict[str, _SpreadsheetContinuation] = {}
        self._spreadsheet_active = 0
        self._spreadsheet_pending = 0
        self._spreadsheet_closed = False
        self._spreadsheet_generation: str | None = None
        self._spreadsheet_operation_state = threading.local()
        self._presentation_lock = threading.Lock()
        self._presentation_active = 0
        self._presentation_closed = False
        self._presentation_generation: str | None = None
        self._presentation_operation_state = threading.local()

    @property
    def client_id(self) -> str:
        return self._client_id

    def _exchange(self, connection: socket.socket, operation: str, payload: dict[str, Any],
                  *, deadline: float, generation: str | None) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerUnavailable("TelegramSearch request deadline expired")
        connection.settimeout(remaining)
        request_id = f"request_{secrets.token_urlsafe(24)}"
        send_frame(connection, {"version": PROTOCOL_VERSION, "client_id": self._client_id,
                   "request_id": request_id, "operation": operation, "payload": payload,
                   "deadline": deadline, "broker_generation": generation}, max_bytes=MAX_REQUEST_BYTES)
        response = receive_frame(connection, max_bytes=MAX_RESPONSE_BYTES)
        if (not isinstance(response, dict) or type(response.get("version")) is not int
                or response.get("version") != PROTOCOL_VERSION or response.get("request_id") != request_id
                or type(response.get("ok")) is not bool):
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        if response["ok"] is not True:
            code = response.get("error")
            if set(response) != {"version", "request_id", "ok", "error"} or type(code) is not str:
                raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
            if code in COMPATIBILITY_CODES:
                raise BrokerCompatibilityError(code)
            if code not in {"expired", "invalid_request", "overloaded", "unavailable"}:
                raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
            raise BrokerUnavailable("TelegramSearch broker could not complete the request")
        if set(response) != {"version", "request_id", "ok", "result"} or not isinstance(response["result"], dict):
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        return response["result"]

    def _request(self, operation: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            require_current(self._policy)
            local = contract_descriptor(self._policy)
            capability = OPERATION_CAPABILITIES.get(operation)
            if capability is not None and (capability not in self._policy.enabled_capabilities or
                    (capability in {"reply_text_send","reply_artifact_send"} and "send" not in self._policy.enabled_capabilities)):
                raise CompatibilityError("capability_disabled")
        except CompatibilityError as error:
            raise BrokerCompatibilityError(error.code) from None
        deadline = time.monotonic() + min(self._request_timeout, 30 if operation in {"get_reply_artifact_draft", "update_reply_artifact_draft", "refresh_reply_artifact_draft", "prepare_reply_text_send", "get_reply_draft", "update_reply_draft", "refresh_reply_draft", "get_send_status", "search_chats", "verify_target", "read_target_messages", "read_presentation"} else self._request_timeout)
        try:
            connection = self._connector(self._socket_path)
        except OSError:
            try:
                self._restart_callback()
            except Exception:
                pass
            self._sleeper(self._retry_backoff_seconds)
            try:
                connection = self._connector(self._socket_path)
            except OSError as error:
                raise BrokerUnavailable("TelegramSearch broker is unavailable") from error
        handshaken = False
        try:
            with connection:
                remote = self._exchange(connection, "handshake", local, deadline=deadline, generation=None)
                require_compatible(remote, local, broker=True)
                handshaken = True
                with self._target_lock:
                    generation = remote["broker_generation"]
                    if self._target_generation != generation:
                        self._target_states.clear()
                        self._target_generation = generation
                    if operation == "read_target_messages":
                        self._target_check(payload.get("target_handle"))
                with self._attachment_lock:
                    if self._attachment_generation != generation:
                        self._attachment_states.clear()
                        self._attachment_generation = generation
                    if operation == "read_attachment_page":
                        self._attachment_check(getattr(self._attachment_operation_state, "prior", None))
                with self._spreadsheet_lock:
                    if self._spreadsheet_generation != generation:
                        self._spreadsheet_states.clear()
                        self._spreadsheet_generation = generation
                    if operation == "read_spreadsheet":
                        self._spreadsheet_check(getattr(self._spreadsheet_operation_state, "prior", None))
                with self._presentation_lock:
                    self._presentation_generation = generation
                    if operation == "read_presentation" and self._presentation_closed:
                        raise BrokerUnavailable("presentation reader closed")
                if operation is None:
                    return remote
                require_current(self._policy)
                self._has_client_state = True
                result = self._exchange(connection, operation, payload, deadline=deadline,
                                        generation=remote["broker_generation"])
                if operation in {"verify_target", "read_target_messages"}:
                    self._target_operation_state.generation = remote["broker_generation"]
                if operation == "read_attachment_page":
                    self._attachment_operation_state.generation = remote["broker_generation"]
                if operation == "read_spreadsheet":
                    self._spreadsheet_operation_state.generation = remote["broker_generation"]
                if operation == "read_presentation":
                    self._presentation_operation_state.generation = remote["broker_generation"]
                return result
        except CompatibilityError as error:
            raise BrokerCompatibilityError(error.code) from None
        except (OSError, BrokerProtocolError) as error:
            if not handshaken:
                raise BrokerCompatibilityError("handshake_unavailable_or_legacy_broker") from error
            raise BrokerUnavailable("TelegramSearch broker is unavailable") from error

    def handshake(self) -> dict[str, Any]:
        return self._request(None, {})

    def diagnostics(self) -> dict[str, Any]:
        try:
            return {"status": "compatible", "broker": self.handshake(),
                    "detail": "loaded proxy and broker agree; refresh tools/list if the consumer listing differs"}
        except BrokerCompatibilityError as error:
            return {"status": error.code, "broker": None,
                    "detail": "no operation dispatched; install and restart matching proxy, broker, and trusted policy"}
        except BrokerUnavailable:
            return {"status": "broker_unavailable", "broker": None, "detail": "local broker unavailable"}

    def resolve(self, request: ResolveTargetRequest) -> TargetResolutionResponse:
        try:
            result = self._request("resolve", request.model_dump(mode="json"))
            return TargetResolutionResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return terminal_resolution_response("error")

    def discover(self, request: DiscoverTargetsRequest) -> TargetDiscoveryResponse:
        try:
            result = self._request("discover", request.model_dump(mode="json"))
            return TargetDiscoveryResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return terminal_discovery_response(request, "error")

    def search(self, request: SearchRequest) -> SearchResponse:
        try:
            result = self._request("search", request.model_dump(mode="json"))
            return SearchResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return terminal_search_response(request, "error")

    def get_attachment(self, request: AttachmentRequest) -> AttachmentResponse:
        try:
            result = self._request("get_attachment", request.model_dump(mode="json"))
            return AttachmentResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return AttachmentResponse(
                status="error", anchor=request.anchor, coverage="none",
                detail="attachment broker is unavailable",
            )

    def read_reply_chain(self, request: ReadReplyChainRequest) -> ReadReplyChainResponse:
        from .reply_reader import reply_response
        try:
            response = ReadReplyChainResponse.model_validate(self._request("read_reply_chain", request.model_dump(mode="json")))
            if response.anchor != request.anchor or response.max_depth != request.max_depth:
                raise ValueError("broker reply-chain scope differs")
            return response
        except (BrokerUnavailable, ValueError):
            return reply_response(request, [], "broker_unavailable")

    def _require_verified_targets(self) -> None:
        try:
            require_current(self._policy)
            contract_descriptor(self._policy)
            if "verified_targets" not in self._policy.enabled_capabilities:
                raise CompatibilityError("capability_disabled")
        except CompatibilityError as error:
            raise BrokerCompatibilityError(error.code) from None

    def _target_check(self, token: str) -> _VerifiedTargetIssuance:
        # Caller owns the lock. Independently retained issuance is the only authority.
        binding = self._target_states.get(token)
        if (self._target_closed or binding is None or time.monotonic() >= binding.expires_monotonic or
                binding.generation != self._target_generation or binding.client_id != self._client_id or
                binding.contract_version != 1):
            self._target_states.pop(token, None)
            raise TargetGuardFailure()
        return binding

    def _target_reserve(self, token: str | None = None) -> _VerifiedTargetIssuance | None:
        with self._target_lock:
            now = time.monotonic()
            self._target_states = {k:v for k,v in self._target_states.items() if now < v.expires_monotonic}
            if self._target_closed:
                raise TargetGuardFailure()
            binding = self._target_check(token) if token is not None else None
            if self._target_active >= 4 or (token is None and len(self._target_states) + self._target_pending >= 16):
                raise TargetGuardFailure("capacity")
            self._target_active += 1
            if token is None:self._target_pending += 1
            return binding

    def verify_target(self, request: VerifyTargetRequest) -> VerifyTargetResponse:
        self._require_verified_targets()
        request = VerifyTargetRequest.model_validate(request.model_dump())
        reserved = False
        try:
            self._target_reserve();reserved = True
            self._target_operation_state.generation = None
            raw = self._request("verify_target", request.model_dump(mode="json"))
            response = VerifyTargetResponse.model_validate_json(json.dumps(raw), strict=True)
            if response.status != "verified":return response
            now = time.monotonic()
            remaining = response.expires_at.timestamp() - time.time()
            if response.target.chat_id != request.target or not 0 < remaining <= 301:
                raise ValueError("broker verified target binding differs")
            generation = self._target_operation_state.generation
            with self._target_lock:
                if (self._target_closed or not generation or generation != self._target_generation or
                        response.target_handle in self._target_states):
                    raise TargetGuardFailure() if self._target_closed else ValueError("broker target issuance is stale or repeated")
                self._target_states[response.target_handle] = _VerifiedTargetIssuance(request.target,
                    response.target.chat_type, response.expires_at, now + min(300, remaining), generation, self._client_id)
                return response
        except TargetGuardFailure as error:
            return VerifyTargetResponse(status=error.status)
        except (BrokerUnavailable, ValueError, TypeError, OverflowError):
            return VerifyTargetResponse(status="error")
        finally:
            if reserved:
                with self._target_lock:
                    self._target_active -= 1
                    self._target_pending -= 1

    def read_target_messages(self, request: ReadTargetMessagesRequest) -> ReadTargetMessagesResponse:
        self._require_verified_targets()
        request = ReadTargetMessagesRequest.model_validate(request.model_dump())
        reserved = False
        token = request.target_handle
        try:
            binding = self._target_reserve(token);reserved = True
            self._target_operation_state.generation = None
            raw = self._request("read_target_messages", request.model_dump(mode="json"))
            response = ReadTargetMessagesResponse.model_validate_json(json.dumps(raw), strict=True)
            with self._target_lock:
                if self._target_check(token) is not binding:
                    raise TargetGuardFailure()
                if self._target_operation_state.generation != binding.generation:
                    raise TargetGuardFailure()
                if response.status not in {"complete", "partial"}:
                    if response.status != "capacity":self._target_states.pop(token, None)
                    return response
            if (response.target_handle != token or response.target.chat_id != binding.chat_id or
                    response.target.chat_type != binding.chat_type or response.expires_at != binding.expires_at or
                    [(r.anchor.chat_id, r.anchor.message_id) for r in response.messages.results] !=
                    [(binding.chat_id, i) for i in request.message_ids]):
                raise ValueError("broker handle read differs from issued binding or selection")
            with self._target_lock:
                if self._target_check(token) is not binding:raise TargetGuardFailure()
                return response
        except TargetGuardFailure as error:
            return ReadTargetMessagesResponse(status=error.status)
        except (BrokerUnavailable, ValueError, TypeError, OverflowError):
            with self._target_lock:self._target_states.pop(token, None)
            return ReadTargetMessagesResponse(status="error")
        finally:
            if reserved:
                with self._target_lock:self._target_active -= 1

    def read_messages(self, request: ReadMessagesRequest) -> ReadMessagesResponse:
        from .message_reader import terminal_read
        try:
            response = ReadMessagesResponse.model_validate(self._request("read_messages", request.model_dump(mode="json")))
            if [r.anchor for r in response.results] != request.anchors:
                raise ValueError("broker selected-read anchors differ")
            return response
        except (BrokerUnavailable, ValueError):
            return terminal_read(request, "error", "broker_unavailable")

    def list_chats(self, request: ListChatsRequest) -> ListChatsResponse:
        from .chat_list_reader import terminal_chat_listing
        digest = fingerprint(request.model_dump(mode="json", exclude={"cursor"}))
        prior = None
        with self._chat_list_lock:
            started = time.monotonic()
            wall_started = time.time()
            self._chat_list_states = {k: v for k, v in self._chat_list_states.items() if v.expires > started}
            if self._closed:
                return terminal_chat_listing("invalid_cursor", "invalid_cursor")
            if request.cursor:
                prior = self._chat_list_states.get(request.cursor)
                if prior is None or prior.request_fingerprint != digest:
                    return terminal_chat_listing("invalid_cursor", "invalid_cursor")
                del self._chat_list_states[request.cursor]
            elif len(self._chat_list_states) + self._chat_list_active >= 4:
                return terminal_chat_listing("capacity_exhausted", "capacity_exhausted")
            self._chat_list_active += 1
        try:
            response = ListChatsResponse.model_validate(self._request("list_chats", request.model_dump(mode="json")))
            scope = response.scope
            ids = frozenset(row.chat_id for row in response.results)
            seen_ids = prior.seen_ids if prior else frozenset()
            seen_cursors = prior.seen_cursors if prior else frozenset()
            if scope is not None:
                if scope.selection != request.scope or scope.limit != request.limit:
                    raise ValueError("broker chat listing scope differs")
                if prior is not None and scope != prior.scope:
                    raise ValueError("broker chat listing changed frozen scope")
                if prior is None and scope.expires_at.timestamp() > wall_started + 301:
                    raise ValueError("broker chat listing widened scope lifetime")
                if response.status in {"page", "prefix_exhausted_unverified"} and scope.expires_at.timestamp() <= time.time():
                    raise ValueError("broker chat listing reported success after scope expiry")
                if ids & seen_ids:
                    raise ValueError("broker chat listing repeated an identity")
                if any(not wall_started - 1 <= row.hydrated_at.timestamp() <= time.time() + 1
                       for row in response.results):
                    raise ValueError("broker chat listing metadata was not observed during this call")
                processed_delta = returned_delta = omitted_delta = 0
                remaining = response.processed_this_page
                for index, current in enumerate(response.list_coverage):
                    old = prior.coverage[index] if prior else ChatListCoverage(chat_list=current.chat_list)
                    if prior is not None and (current.native_state != old.native_state or current.observed != old.observed):
                        raise ValueError("broker chat listing reopened a frozen prefix")
                    delta = current.processed - old.processed
                    returned = current.returned - old.returned
                    omitted = current.omitted - old.omitted
                    # Raw observation slots progress in selected-list order, including omissions.
                    expected = min(remaining, current.observed - old.processed)
                    if (min(delta, returned, omitted) < 0 or delta != expected or
                            current.pending != current.observed - current.processed or
                            returned != sum(row.observed_list == current.chat_list for row in response.results) or
                            any(getattr(current.issues, name) < value for name, value in old.issues.model_dump().items())):
                        raise ValueError("broker chat listing forged per-list progress")
                    for row in response.results:
                        if row.observed_list == current.chat_list and not old.processed < row.observed_rank <= current.processed:
                            raise ValueError("broker chat listing row is outside this page's observations")
                    remaining -= delta
                    processed_delta += delta; returned_delta += returned; omitted_delta += omitted
                if (remaining or processed_delta != response.processed_this_page or
                        returned_delta != response.returned_this_page or omitted_delta != response.omitted_this_page or
                        (not response.snapshot_complete and response.processed_candidates)):
                    raise ValueError("broker chat listing forged page deltas")
                if response.next_cursor and response.next_cursor in seen_cursors:
                    raise ValueError("broker chat listing reused a cursor")
            if response.next_cursor:
                expires = prior.expires if prior else min(started + 300,
                    time.monotonic() + scope.expires_at.timestamp() - time.time())
                if expires <= time.monotonic():
                    raise ValueError("broker chat listing scope expired")
                state = _ChatListContinuation(digest, scope.model_copy(deep=True),
                    tuple(value.model_copy(deep=True) for value in response.list_coverage),
                    seen_ids | ids, seen_cursors | {response.next_cursor}, expires)
            with self._chat_list_lock:
                if self._closed:
                    raise ValueError("broker chat listing client closed")
                if response.next_cursor:
                    if response.next_cursor in self._chat_list_states:
                        raise ValueError("broker chat listing cursor is not unique")
                    self._chat_list_states[response.next_cursor] = state
            return response
        except (BrokerUnavailable, ValueError, TypeError):
            return terminal_chat_listing("error", "broker_unavailable")
        finally:
            with self._chat_list_lock:
                self._chat_list_active -= 1

    def list_topics(self, request: ListTopicsRequest) -> ListTopicsResponse:
        from .forum_reader import terminal_topics
        digest = fingerprint(request.model_dump(mode="json", exclude={"cursor"}))
        prior = None
        with self._topics_lock:
            now = time.monotonic()
            self._topics_states = {k:v for k,v in self._topics_states.items() if v.expires > now}
            if self._closed:
                return terminal_topics("invalid_cursor", "invalid_cursor")
            if request.cursor:
                prior = self._topics_states.get(request.cursor)
                if prior is None or prior.request_fingerprint != digest:
                    return terminal_topics("invalid_cursor", "invalid_cursor")
                del self._topics_states[request.cursor]
            elif len(self._topics_states) + self._topics_active >= 4:
                return terminal_topics("capacity_exhausted", "capacity_exhausted")
            self._topics_active += 1
        try:
            response = ListTopicsResponse.model_validate(self._request("list_topics", request.model_dump(mode="json")))
            scope = response.scope
            if scope is not None and (scope.target != request.target or scope.limit != request.limit or
                    scope.candidate_limit != 200 or scope.provider_page_limit != 10):
                raise ValueError("broker topic scope differs")
            seen_ids = prior.seen_ids if prior else frozenset()
            seen_cursors = prior.seen_cursors if prior else frozenset()
            ids = frozenset(r.topic.id for r in response.results)
            if ids & seen_ids:
                raise ValueError("broker topic continuation repeated a topic")
            if scope is not None:
                previous_scanned = prior.scanned if prior else 0
                previous_pages = prior.provider_pages if prior else 0
                scanned_delta = response.scanned_candidates - previous_scanned
                pages_delta = response.provider_pages - previous_pages
                if (scanned_delta < 0 or pages_delta not in (0, 1) or
                        (prior is not None and pages_delta != 0 and
                         (prior.scanned >= scope.candidate_limit or
                          prior.provider_pages >= scope.provider_page_limit)) or
                        (pages_delta == 0 and scanned_delta != 0) or
                        (response.scanned_candidates and not response.provider_pages) or
                        len(seen_ids | ids) > response.scanned_candidates):
                    raise ValueError("broker topic call exceeded its observed provider bounds")
                if prior is not None and scope != prior.scope:
                    raise ValueError("broker topic continuation changed its scope")
                if response.next_cursor and (response.next_cursor in seen_cursors or
                        ((response.scanned_candidates >= scope.candidate_limit or
                          response.provider_pages >= scope.provider_page_limit) and
                         len(seen_ids | ids) >= response.scanned_candidates)):
                    raise ValueError("broker topic cursor did not preserve pending observations")
            if response.next_cursor:
                expires = prior.expires if prior else min(now + 300,
                    time.monotonic() + scope.expires_at.timestamp() - time.time())
                if expires <= time.monotonic():
                    raise ValueError("broker topic scope has expired")
                state = _TopicContinuation(digest, scope.model_copy(deep=True), seen_ids | ids,
                    seen_cursors | {response.next_cursor}, response.scanned_candidates,
                    response.provider_pages, expires)
            with self._topics_lock:
                if self._closed:
                    raise ValueError("broker topic client is closed")
                if response.next_cursor:
                    if response.next_cursor in self._topics_states:
                        raise ValueError("broker topic cursor is not unique")
                    self._topics_states[response.next_cursor] = state
            return response
        except (BrokerUnavailable, ValueError, TypeError):
            return terminal_topics("error", "broker_unavailable")
        finally:
            with self._topics_lock:
                self._topics_active -= 1

    def search_chats(self, request: SearchChatsRequest) -> SearchChatsResponse:
        from .selected_search_reader import terminal_selected_search
        request = SearchChatsRequest.model_validate(request.model_dump(mode='python'))
        wall_started = time.time()
        digest = fingerprint(request.model_dump(mode='json', exclude={'cursor'}))
        prior = None
        with self._selected_search_lock:
            now = time.monotonic()
            self._selected_search_states = {token:state for token,state in self._selected_search_states.items() if state.expires > now}
            if self._closed:
                return terminal_selected_search('invalid_cursor', 'invalid_cursor')
            if request.cursor is not None:
                prior = self._selected_search_states.get(request.cursor)
                if prior is None or prior.request_fingerprint != digest:
                    return terminal_selected_search('invalid_cursor', 'invalid_cursor')
                del self._selected_search_states[request.cursor]
            elif len(self._selected_search_states)+self._selected_search_active >= 4:
                return terminal_selected_search('capacity_exhausted', 'capacity_exhausted')
            self._selected_search_active += 1
        try:
            response = SearchChatsResponse.model_validate(self._request('search_chats',request.model_dump(mode='json')))
            wall_finished = time.time()
            if time.monotonic() - now >= 30:
                raise ValueError('selected search response exceeded the aggregate deadline')
            if response.stop_reason in {'account_changed', 'authorization_unavailable', 'invalid_cursor'} and (response.results or response.next_cursor):
                raise ValueError('selected group lifecycle loss returned content or continuation')
            scope = response.scope
            seen = prior.seen_cursors if prior else frozenset()
            last = None
            expires = prior.expires if prior else now+300
            if scope is not None:
                if (scope.targets != request.targets or scope.query != request.query or scope.mode != request.mode or
                        scope.sender != request.sender or scope.direction != request.direction or scope.page_limit != request.limit or
                        scope.order != 'selected_chat_then_message_id_desc' or scope.candidate_limit != 200 or scope.provider_page_limit != 10 or
                        (request.mode == 'latest' and scope.date_from is not None) or
                        (request.mode == 'interval' and (scope.date_from != request.date_from or scope.date_to != request.date_to))):
                    raise ValueError('selected search request scope differs')
                semantics = 'local_nfkc_casefold_whitespace_substring' if isinstance(request.query,BooleanSearchQuery) else 'tdlib_lexical'
                if scope.matching_semantics != semantics:
                    raise ValueError('selected search matching semantics differ')
                if prior:
                    if scope != prior.scope:
                        raise ValueError('selected search frozen scope changed')
                    expected = prior.index + (prior.coverage[prior.index].state == 'stopped')
                    if response.current_index != expected:
                        raise ValueError('selected search skipped or reopened a chat')
                    for index,old in enumerate(prior.coverage):
                        if index != expected and response.coverage[index] != old:
                            raise ValueError('selected search altered an independent lane')
                    old = prior.coverage[expected]
                    if expected == prior.index:
                        last = prior.last_result_id
                else:
                    if request.mode == 'latest' and not wall_started <= scope.date_to.timestamp() <= wall_finished:
                        raise ValueError('selected latest date lies outside the call wall envelope')
                    if response.next_cursor is not None and response.current_index != 0:
                        raise ValueError('selected search skipped the first chat')
                    if response.current_index != 0 and (response.results or any(
                            lane.upper_message_id is not None or lane.scanned_candidates or lane.processed_candidates or
                            lane.provider_pages or lane.branch_coverage for lane in response.coverage)):
                        raise ValueError('initial selected preflight failure carries native evidence')
                    for index,lane in enumerate(response.coverage):
                        if index != response.current_index and lane.state != 'pending':
                            raise ValueError('initial selected search contains foreign lane evidence')
                    old = SelectedChatCoverage(target=scope.targets[response.current_index])
                lane = response.coverage[response.current_index]
                if old.upper_message_id is not None and lane.upper_message_id != old.upper_message_id:
                    raise ValueError('selected search changed its frozen chat head')
                scanned = lane.scanned_candidates-old.scanned_candidates
                processed = lane.processed_candidates-old.processed_candidates
                pages = lane.provider_pages-old.provider_pages
                if (scanned < 0 or not 0 <= processed <= 100 or not 0 <= pages <= 10 or scanned > 20*pages or
                        (pages and (old.scanned_candidates >= 200 or old.provider_pages >= 10)) or
                        len(response.results) > processed or
                        (last is not None and any(result.anchor.message_id >= last for result in response.results))):
                    raise ValueError('selected search violated native or descending progress')
                if isinstance(request.query,BooleanSearchQuery) and lane.branch_coverage:
                    seeds = boolean_seeds(request.query)
                    if len(lane.branch_coverage) != len(seeds):
                        raise ValueError('selected Boolean seed coverage differs')
                    for index,(branch,seed) in enumerate(zip(lane.branch_coverage,seeds)):
                        if branch.index != index or branch.query != seed:
                            raise ValueError('selected Boolean branch mapping differs')
                        if old.branch_coverage:
                            previous = old.branch_coverage[index]
                            bp = branch.provider_pages-previous.provider_pages
                            bs = branch.scanned_candidates-previous.scanned_candidates
                            bd = branch.processed_candidates-previous.processed_candidates
                            if (bp < 0 or bs < 0 or bd < 0 or bs > 20*bp or
                                    (previous.state != 'active' and (bp or bs or branch.state != previous.state))):
                                raise ValueError('selected Boolean branch changed stopped evidence')
                elif not isinstance(request.query,BooleanSearchQuery) and lane.branch_coverage:
                    raise ValueError('selected lexical query contains Boolean branches')
                if old.branch_coverage and not lane.branch_coverage:
                    raise ValueError('selected search discarded prior branch coverage')
                if response.next_cursor:
                    if response.next_cursor in seen or (lane.state == 'active' and processed <= 0 and pages <= 0):
                        raise ValueError('selected search cursor replays or lacks local progress')
                    if lane.state == 'active' and (lane.scanned_candidates >= 200 or lane.provider_pages >= 10) and lane.processed_candidates >= lane.scanned_candidates:
                        raise ValueError('selected chat cannot resume exhausted evidence')
                for result in response.results:
                    if result.anchor.chat_id != scope.targets[response.current_index]:
                        raise ValueError('selected search result belongs to another chat')
                    value = result.message
                    if value is not None:
                        if (value.date_utc is None or value.date_utc >= scope.date_to or
                                (scope.date_from is not None and value.date_utc < scope.date_from) or
                                (request.sender is not None and (value.sender.kind != request.sender.kind or value.sender.id != request.sender.id)) or
                                (request.direction is not None and value.is_outgoing != (request.direction == 'outgoing'))):
                            raise ValueError('selected search result violates filters or dates')
                        if (isinstance(request.query,BooleanSearchQuery) and value.text is not None and
                                not value.text.sanitized and not value.text.truncated and not boolean_matches(request.query,value.text.value)):
                            raise ValueError('selected search body violates Boolean membership')
                if response.results:
                    last = response.results[-1].anchor.message_id
                if scope.expires_at.timestamp()-time.time() > 300:
                    raise ValueError('selected search expiry exceeds the original lifetime')
                expires = prior.expires if prior else min(now+300,time.monotonic()+scope.expires_at.timestamp()-time.time())
                if time.monotonic() >= expires:
                    raise ValueError('selected search expired in flight')
            with self._selected_search_lock:
                if self._closed or time.monotonic() >= expires or (scope is not None and scope.expires_at.timestamp() <= time.time()):
                    raise ValueError('selected search closed or expired')
                if response.next_cursor:
                    if response.next_cursor in self._selected_search_states:
                        raise ValueError('selected search cursor collides')
                    self._selected_search_states[response.next_cursor] = _SelectedSearchContinuation(digest,
                        scope.model_copy(deep=True), tuple(lane.model_copy(deep=True) for lane in response.coverage),
                        response.current_index,last,seen | {response.next_cursor},expires)
            return response
        except (BrokerUnavailable, ValueError, TypeError):
            return terminal_selected_search('error', 'broker_unavailable')
        finally:
            with self._selected_search_lock:
                self._selected_search_active -= 1

    def search_messages(self, request: SearchMessagesRequest) -> SearchMessagesResponse:
        from .exact_search_reader import terminal_search_messages
        wall_started = time.time()
        digest = fingerprint(request.model_dump(mode="json", exclude={"cursor"}))
        prior = None
        with self._search_lock:
            now = time.monotonic()
            self._search_states = {k: v for k, v in self._search_states.items() if v.expires > now}
            if self._closed:
                return terminal_search_messages("invalid_cursor", "invalid_cursor")
            if request.cursor:
                prior = self._search_states.get(request.cursor)
                if prior is None or prior.request_fingerprint != digest:
                    return terminal_search_messages("invalid_cursor", "invalid_cursor")
                del self._search_states[request.cursor]
            elif len(self._search_states) + self._search_active >= 4:
                return terminal_search_messages("capacity_exhausted", "capacity_exhausted")
            self._search_active += 1
        try:
            raw = self._request("search_messages", request.model_dump(mode="json"))
            wall_finished = time.time()
            response = SearchMessagesResponse.model_validate(raw)
            scope = response.scope
            seen_cursors = prior.seen_cursors if prior else frozenset()
            last = prior.last_result_id if prior else None
            if scope is not None:
                if (scope.target != request.target or scope.query != request.query or scope.mode != request.mode or
                        scope.sender != request.sender or scope.direction != request.direction or scope.topic != request.topic or
                        scope.page_limit != request.limit or scope.candidate_limit != 200 or scope.provider_page_limit != 10 or
                        (request.mode == "latest" and scope.date_from is not None) or
                        (request.mode == "interval" and (scope.date_from != request.date_from or scope.date_to != request.date_to))):
                    raise ValueError("broker exact search request scope differs")
                if prior is None and request.mode == "latest" and not (
                        wall_started <= scope.date_to.timestamp() <= wall_finished):
                    raise ValueError("broker latest search date lies outside the request wall-time envelope")
                if prior is not None and scope != prior.scope:
                    raise ValueError("broker exact search changed its frozen scope")
                if isinstance(request.query, BooleanSearchQuery):
                    seeds = boolean_seeds(request.query)
                    if (scope.matching_semantics != "local_nfkc_casefold_whitespace_substring" or
                            len(response.branch_coverage) != len(seeds)):
                        raise ValueError("broker Boolean search semantics differ")
                    for index, (branch, seed) in enumerate(zip(response.branch_coverage, seeds)):
                        if branch.index != index or branch.query != seed:
                            raise ValueError("broker Boolean branch differs")
                        if prior:
                            old = prior.branches[index]
                            delta_pages = branch.provider_pages - old.provider_pages
                            delta_scanned = branch.scanned_candidates - old.scanned_candidates
                            if (delta_pages < 0 or delta_scanned < 0 or delta_scanned > 20 * delta_pages or
                                    branch.processed_candidates < old.processed_candidates or
                                    (old.state != "active" and (delta_pages or branch.state != old.state))):
                                raise ValueError("broker Boolean branch progress differs")
                    for name in ("scanned_candidates", "processed_candidates", "provider_pages"):
                        if sum(getattr(branch, name) for branch in response.branch_coverage) != getattr(response, name):
                            raise ValueError("broker Boolean branch totals differ")
                elif response.branch_coverage or scope.matching_semantics != "tdlib_lexical":
                    raise ValueError("broker lexical string search semantics differ")
                previous_scanned = prior.scanned if prior else 0
                previous_processed = prior.processed if prior else 0
                previous_pages = prior.provider_pages if prior else 0
                scanned_delta = response.scanned_candidates - previous_scanned
                processed_delta = response.processed_candidates - previous_processed
                pages_delta = response.provider_pages - previous_pages
                if (scanned_delta < 0 or not 0 <= processed_delta <= 100 or not 0 <= pages_delta <= 10 or
                        scanned_delta > 20 * pages_delta or
                        (prior is not None and pages_delta != 0 and
                         (prior.scanned >= 200 or prior.provider_pages >= 10)) or
                        len(response.results) > processed_delta or
                        (last is not None and any(r.anchor.message_id >= last for r in response.results))):
                    raise ValueError("broker exact search violated observation, processing or ID progress")
                if response.next_cursor and ((processed_delta <= 0 and pages_delta <= 0) or response.next_cursor in seen_cursors or
                        ((response.scanned_candidates >= 200 or response.provider_pages >= 10) and
                         response.processed_candidates >= response.scanned_candidates)):
                    raise ValueError("broker exact search cursor lacks progress or pending observations")
                for result in response.results:
                    value = result.message
                    if value is not None:
                        if (isinstance(request.query, BooleanSearchQuery) and value.text is not None
                                and not value.text.sanitized and not value.text.truncated
                                and not boolean_matches(request.query, value.text.value)):
                            raise ValueError("broker search body violates Boolean predicates")
                        if request.sender is not None and (value.sender.kind != request.sender.kind or value.sender.id != request.sender.id):
                            raise ValueError("broker search body violates requested sender")
                        if request.direction is not None and value.is_outgoing != (request.direction == "outgoing"):
                            raise ValueError("broker search body violates requested direction")
                        if request.topic is not None and (value.topic is None or value.topic.kind != "forum" or value.topic.id != request.topic.id):
                            raise ValueError("broker search body violates requested topic")
                if response.results:
                    last = response.results[-1].anchor.message_id
            expires = None
            if scope is not None:
                if scope.expires_at.timestamp() - time.time() > 300:
                    raise ValueError("broker exact search lifetime exceeds its fixed TTL")
                expires = prior.expires if prior else min(now + 300,
                    time.monotonic() + scope.expires_at.timestamp() - time.time())
                if expires <= time.monotonic():
                    raise ValueError("broker exact search scope expired")
            if response.next_cursor:
                state = _SearchContinuation(digest, scope.model_copy(deep=True), last,
                    seen_cursors | {response.next_cursor}, response.scanned_candidates,
                    response.processed_candidates, response.provider_pages, expires,
                    tuple(branch.model_copy(deep=True) for branch in response.branch_coverage))
            with self._search_lock:
                if (self._closed or (scope is not None and scope.expires_at.timestamp() <= time.time()) or
                        (expires is not None and time.monotonic() >= expires)):
                    raise ValueError("broker exact search client closed or scope expired")
                if response.next_cursor:
                    if response.next_cursor in self._search_states:
                        raise ValueError("broker exact search cursor is not unique")
                    self._search_states[response.next_cursor] = state
            return response
        except (BrokerUnavailable, ValueError, TypeError):
            return terminal_search_messages("error", "broker_unavailable")
        finally:
            with self._search_lock:
                self._search_active -= 1

    def read_topic_history(self, request: ReadTopicHistoryRequest) -> ReadTopicHistoryResponse:
        from .topic_history_reader import terminal_topic_history
        digest = fingerprint(request.model_dump(mode="json", exclude={"cursor"}))
        prior = None
        with self._topic_history_lock:
            now = time.monotonic()
            self._topic_history_states = {k: v for k, v in self._topic_history_states.items() if v.expires > now}
            if self._closed:
                return terminal_topic_history("invalid_cursor", "invalid_cursor")
            if request.cursor:
                prior = self._topic_history_states.get(request.cursor)
                if prior is None or prior.request_fingerprint != digest:
                    return terminal_topic_history("invalid_cursor", "invalid_cursor")
                del self._topic_history_states[request.cursor]
            elif len(self._topic_history_states) + self._topic_history_active >= 4:
                return terminal_topic_history("capacity_exhausted", "capacity_exhausted")
            self._topic_history_active += 1
        try:
            response = ReadTopicHistoryResponse.model_validate(
                self._request("read_topic_history", request.model_dump(mode="json")))
            scope = response.scope
            seen_cursors = prior.seen_cursors if prior else frozenset()
            last = prior.last_result_id if prior else None
            if scope is not None:
                if (scope.target != request.target or scope.topic != request.topic or scope.mode != request.mode or
                        scope.page_limit != request.limit or scope.candidate_limit != 200 or scope.provider_page_limit != 10 or
                        (request.mode == "latest" and scope.date_from is not None) or
                        (request.mode == "interval" and (scope.date_from != request.date_from or scope.date_to != request.date_to))):
                    raise ValueError("broker topic history request scope differs")
                if prior is not None and scope != prior.scope:
                    raise ValueError("broker topic history changed its frozen scope")
                previous_scanned = prior.scanned if prior else 0
                previous_processed = prior.processed if prior else 0
                previous_pages = prior.provider_pages if prior else 0
                scanned_delta = response.scanned_candidates - previous_scanned
                processed_delta = response.processed_candidates - previous_processed
                pages_delta = response.provider_pages - previous_pages
                if (scanned_delta < 0 or not 0 <= processed_delta <= 100 or not 0 <= pages_delta <= 10 or
                        (pages_delta == 0 and scanned_delta != 0) or
                        (prior is not None and pages_delta != 0 and
                         (prior.scanned >= 200 or prior.provider_pages >= 10)) or
                        len(response.results) > processed_delta or
                        (last is not None and any(r.anchor.message_id >= last for r in response.results))):
                    raise ValueError("broker topic history violated observation, processing or ID progress")
                if response.next_cursor and (processed_delta <= 0 or response.next_cursor in seen_cursors or
                        ((response.scanned_candidates >= 200 or response.provider_pages >= 10) and
                         response.processed_candidates >= response.scanned_candidates)):
                    raise ValueError("broker topic history cursor lacks progress or pending observations")
                if response.results:
                    last = response.results[-1].anchor.message_id
            if response.next_cursor:
                expires = prior.expires if prior else min(now + 300,
                    time.monotonic() + scope.expires_at.timestamp() - time.time())
                if expires <= time.monotonic():
                    raise ValueError("broker topic history scope expired")
                state = _TopicHistoryContinuation(digest, scope.model_copy(deep=True), last,
                    seen_cursors | {response.next_cursor}, response.scanned_candidates,
                    response.processed_candidates, response.provider_pages, expires)
            with self._topic_history_lock:
                if (self._closed or (scope is not None and scope.expires_at.timestamp() <= time.time()) or
                        (prior is not None and time.monotonic() >= prior.expires)):
                    raise ValueError("broker topic history client closed or scope expired")
                if response.next_cursor:
                    if response.next_cursor in self._topic_history_states:
                        raise ValueError("broker topic history cursor is not unique")
                    self._topic_history_states[response.next_cursor] = state
            return response
        except (BrokerUnavailable, ValueError, TypeError):
            return terminal_topic_history("error", "broker_unavailable")
        finally:
            with self._topic_history_lock:
                self._topic_history_active -= 1

    def read_history(self, request: ReadHistoryRequest) -> ReadHistoryResponse:
        from .history_reader import terminal_history
        digest = fingerprint(request.model_dump(mode="json", exclude={"cursor"}))
        prior = None
        with self._history_lock:
            now = time.monotonic()
            self._history_states = {k:v for k,v in self._history_states.items() if v.expires > now}
            if self._closed:
                return terminal_history("invalid_cursor", "invalid_cursor")
            if request.cursor:
                prior = self._history_states.get(request.cursor)
                if prior is None or prior.request_fingerprint != digest:
                    return terminal_history("invalid_cursor", "invalid_cursor")
                del self._history_states[request.cursor]
            elif len(self._history_states) + self._history_active >= 4:
                return terminal_history("capacity_exhausted", "capacity_exhausted")
            self._history_active += 1
        try:
            response = ReadHistoryResponse.model_validate(self._request("read_history", request.model_dump(mode="json")))
            scope = response.scope
            if scope is not None and (scope.target != request.target or scope.mode != request.mode or
                    scope.page_limit != request.limit or
                    scope.candidate_limit != (100 if request.mode == "latest" else 1000) or
                    (request.mode == "latest" and scope.date_from is not None) or
                    (request.mode == "interval" and (scope.date_from != request.date_from or scope.date_to != request.date_to))):
                raise ValueError("broker history scope differs")
            if prior is not None and scope is not None:
                if (scope != prior.scope or response.scanned_candidates < prior.scanned or
                        (response.next_cursor and response.scanned_candidates <= prior.scanned) or
                        (prior.last_result_id is not None and any(r.anchor.message_id >= prior.last_result_id for r in response.results)) or
                        response.next_cursor == request.cursor):
                    raise ValueError("broker history continuation changed or repeated its scope")
            if response.next_cursor:
                last = response.results[-1].anchor.message_id if response.results else (prior.last_result_id if prior else None)
                state = _HistoryContinuation(digest, scope.model_copy(deep=True), last, response.scanned_candidates,
                    prior.expires if prior else now + 300)
                with self._history_lock:
                    if self._closed or response.next_cursor in self._history_states:
                        raise ValueError("broker history cursor is not unique")
                    self._history_states[response.next_cursor] = state
            return response
        except (BrokerUnavailable, ValueError, TypeError):
            return terminal_history("error", "broker_unavailable")
        finally:
            with self._history_lock:
                self._history_active -= 1

    def get_message_context(self, request: MessageContextRequest) -> MessageContextResponse:
        try:
            result = self._request("get_message_context", request.model_dump(mode="json"))
            return MessageContextResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return MessageContextResponse(
                status="error", anchor=request.anchor, coverage_complete=False,
                detail="context broker is unavailable",
            )

    def _require_attachment_pages(self) -> None:
        try:
            require_current(self._policy)
            contract_descriptor(self._policy)
            if "attachment_pages" not in self._policy.enabled_capabilities:
                raise CompatibilityError("capability_disabled")
        except CompatibilityError as error:
            raise BrokerCompatibilityError(error.code) from None

    def _attachment_check(self, prior: _AttachmentContinuation | None) -> None:
        # Caller owns registry lock; a consumed binding stays local to its operation.
        if self._attachment_closed:
            raise _AttachmentGuardFailure()
        if prior is not None:
            if time.monotonic() >= prior.expires or time.time() >= prior.scope.expires_at.timestamp():
                raise _AttachmentGuardFailure("expired")
            if prior.generation != self._attachment_generation:
                raise _AttachmentGuardFailure()

    def read_attachment_page(self, request: ReadAttachmentPageRequest) -> ReadAttachmentPageResponse:
        self._require_attachment_pages()
        try:
            request = ReadAttachmentPageRequest.model_validate(request.model_dump())
        except (ValueError, TypeError):
            return terminal_attachment_page("invalid_cursor")
        digest = fingerprint(request.model_dump(mode="json", exclude={"cursor"}))
        prior = None
        reserved = False
        started = time.monotonic()
        wall_started = time.time()
        try:
            with self._attachment_lock:
                if self._attachment_closed:
                    raise _AttachmentGuardFailure()
                if request.cursor is not None:
                    prior = self._attachment_states.get(request.cursor)
                    if prior is None or prior.request_fingerprint != digest:
                        raise _AttachmentGuardFailure()
                    self._attachment_check(prior)
                self._attachment_states = {k:v for k,v in self._attachment_states.items()
                    if started < v.expires and wall_started < v.scope.expires_at.timestamp()}
                if self._attachment_active >= 4 or (prior is None and
                        len(self._attachment_states) + self._attachment_pending >= 16):
                    raise _AttachmentGuardFailure("capacity_exhausted")
                if request.cursor is not None:
                    del self._attachment_states[request.cursor]
                self._attachment_active += 1
                self._attachment_pending += 1
                reserved = True
            self._attachment_operation_state.prior = prior
            self._attachment_operation_state.generation = None
            raw = self._request("read_attachment_page", request.model_dump(mode="json"))
            response = ReadAttachmentPageResponse.model_validate_json(json.dumps(raw, allow_nan=False), strict=True)
            scope = response.scope
            with self._attachment_lock:
                self._attachment_check(prior)
                generation = self._attachment_operation_state.generation
                if not generation or generation != self._attachment_generation:
                    raise _AttachmentGuardFailure()
                if response.status not in {"page", "complete"}:
                    return response
                if (scope.artifact_id != request.artifact_id or scope.max_chars != request.max_chars
                        or scope.render_pages != request.render_pages or scope.broker_generation != generation
                        or scope.extractor_version != 1):
                    raise ValueError("broker extraction scope differs")
                if scope.kind == "pdf":
                    expected = request.pages if request.pages is not None else list(range(1, min(5, scope.total_pages) + 1))
                    if scope.selected_pages != expected:
                        raise ValueError("broker page selection differs")
                elif request.pages is not None:
                    raise ValueError("pages require PDF")
                if response.text_start != (prior.text_end if prior is not None else 0):
                    raise ValueError("broker text offset differs")
                # The broker starts its TTL after request transit. Validate its
                # upper bound at receipt, while our own lifetime begins earlier.
                received = time.monotonic()
                remaining = scope.expires_at.timestamp() - time.time()
                if not 0 < remaining <= 300:
                    raise ValueError("broker extraction expiry differs")
                if prior is not None and scope != prior.scope:
                    raise ValueError("broker extraction binding changed")
                expires = prior.expires if prior is not None else min(started + 300, received + remaining)
                if time.monotonic() >= expires or time.time() >= scope.expires_at.timestamp():
                    raise _AttachmentGuardFailure("expired")
                seen = prior.seen_cursors if prior is not None else frozenset()
                if response.next_cursor is not None:
                    if len(seen) >= 255:
                        return terminal_attachment_page("limit_reached", "extraction call limit reached; use a larger text window in a fresh extraction")
                    if response.next_cursor in seen or response.next_cursor in self._attachment_states:
                        raise ValueError("broker repeated continuation")
                    self._attachment_states[response.next_cursor] = _AttachmentContinuation(digest,
                        scope.model_copy(deep=True), response.text_end, expires, generation,
                        seen | {response.next_cursor})
                return response
        except _AttachmentGuardFailure as error:
            if request.cursor is not None:
                with self._attachment_lock:
                    self._attachment_states.pop(request.cursor, None)
            return terminal_attachment_page(error.status)
        except (BrokerUnavailable, ValueError, TypeError, OverflowError):
            return terminal_attachment_page("error")
        finally:
            self._attachment_operation_state.prior = None
            if reserved:
                with self._attachment_lock:
                    self._attachment_active -= 1
                    self._attachment_pending -= 1

    def _require_spreadsheets(self) -> None:
        try:
            require_current(self._policy)
            contract_descriptor(self._policy)
            if 'spreadsheets' not in self._policy.enabled_capabilities:
                raise CompatibilityError('capability_disabled')
        except CompatibilityError as error:
            raise BrokerCompatibilityError(error.code) from None

    def _spreadsheet_check(self, prior: _SpreadsheetContinuation | None) -> None:
        # Caller owns registry lock. Only locally remembered issuance is authority.
        if self._spreadsheet_closed:
            raise _SpreadsheetGuardFailure()
        if prior is not None:
            if time.monotonic() >= prior.expires or time.time() >= prior.scope.expires_at.timestamp():
                raise _SpreadsheetGuardFailure('expired')
            if prior.generation != self._spreadsheet_generation:
                raise _SpreadsheetGuardFailure()

    @staticmethod
    def _spreadsheet_positions(selections):
        # Independent of extraction: derive every addressed position from the
        # strict public request, without trusting worksheet bodies or dimensions.
        for index, selection in enumerate(selections, 1):
            ends = selection.range.split(':')
            bounds = []
            for address in (ends[0], ends[-1]):
                letters = address.rstrip('0123456789')
                column = 0
                for letter in letters:
                    column = column * 26 + ord(letter) - ord('A') + 1
                bounds.append((int(address[len(letters):]), column))
            (row1, col1), (row2, col2) = bounds
            for row in range(row1, row2 + 1):
                for column in range(col1, col2 + 1):
                    value = column
                    label = ''
                    while value:
                        value, digit = divmod(value - 1, 26)
                        label = chr(ord('A') + digit) + label
                    yield index, label + str(row), row, column

    def read_spreadsheet(self, request: ReadSpreadsheetRequest) -> ReadSpreadsheetResponse:
        self._require_spreadsheets()
        try:
            request = ReadSpreadsheetRequest.model_validate(request.model_dump())
        except (ValueError, TypeError):
            return terminal_spreadsheet('invalid_cursor')
        digest = fingerprint(request.model_dump(mode='json', exclude={'cursor'}))
        prior = None
        reserved = False
        started = time.monotonic()
        wall_started = time.time()
        try:
            with self._spreadsheet_lock:
                if self._spreadsheet_closed:
                    raise _SpreadsheetGuardFailure()
                if request.cursor is not None:
                    prior = self._spreadsheet_states.pop(request.cursor, None)
                    if prior is None or prior.request_fingerprint != digest:
                        raise _SpreadsheetGuardFailure()
                    self._spreadsheet_check(prior)
                self._spreadsheet_states = {k:v for k,v in self._spreadsheet_states.items()
                    if started < v.expires and wall_started < v.scope.expires_at.timestamp()}
                if self._spreadsheet_active >= 4 or len(self._spreadsheet_states) + self._spreadsheet_pending >= 16:
                    raise _SpreadsheetGuardFailure('capacity_exhausted')
                self._spreadsheet_active += 1
                self._spreadsheet_pending += 1
                reserved = True
            self._spreadsheet_operation_state.prior = prior
            self._spreadsheet_operation_state.generation = None
            raw = self._request('read_spreadsheet', request.model_dump(mode='json'))
            response = ReadSpreadsheetResponse.model_validate_json(json.dumps(raw, allow_nan=False), strict=True)
            scope = response.scope
            with self._spreadsheet_lock:
                self._spreadsheet_check(prior)
                generation = self._spreadsheet_operation_state.generation
                if not generation or generation != self._spreadsheet_generation:
                    raise _SpreadsheetGuardFailure()
                if response.status not in {'catalog', 'page', 'complete'}:
                    return response
                if (scope.artifact_id != request.artifact_id or scope.max_cells != request.max_cells or
                        scope.selections != request.selections or scope.broker_generation != generation or
                        scope.extractor_version != 1):
                    raise ValueError('broker spreadsheet scope differs')
                parts = request.artifact_id.split('_')
                if scope.artifact_sha256 != parts[2] or scope.artifact_bytes != int(parts[3]):
                    raise ValueError('broker spreadsheet artifact differs')
                if [s.index for s in scope.catalog] != list(range(1, len(scope.catalog) + 1)):
                    raise ValueError('broker spreadsheet catalog differs')
                if response.status == 'catalog':
                    if request.selections is not None or prior is not None:
                        raise ValueError('broker spreadsheet catalog selection differs')
                else:
                    if request.selections is None or any(s.sheet_index > len(scope.catalog) for s in request.selections):
                        raise ValueError('broker spreadsheet selected sheet differs')
                    positions = list(self._spreadsheet_positions(request.selections))
                    if scope.total_cells != len(positions):
                        raise ValueError('broker spreadsheet selected count differs')
                    offset = prior.cell_end if prior is not None else 0
                    if response.cell_start != offset or not 0 < response.cell_end - offset <= request.max_cells:
                        raise ValueError('broker spreadsheet offsets differ')
                    expected = positions[offset:response.cell_end]
                    actual = [(c.selection_index, c.address, c.row, c.column) for c in response.cells]
                    if actual != expected or response.cell_end - offset != len(response.cells):
                        raise ValueError('broker spreadsheet cell ordering differs')
                    if sum(len(value) for c in response.cells for value in
                            (c.address, c.value_type, c.value, c.formula, c.formula_kind, c.formula_ref) if value is not None) > 20_000:
                        raise ValueError('broker spreadsheet response budget exceeded')
                    if response.has_more != (response.cell_end < scope.total_cells):
                        raise ValueError('broker spreadsheet completion differs')
                received = time.monotonic()
                remaining = scope.expires_at.timestamp() - time.time()
                if not 0 < remaining <= 300:
                    raise ValueError('broker spreadsheet expiry differs')
                if prior is not None and scope != prior.scope:
                    raise ValueError('broker spreadsheet binding changed')
                expires = prior.expires if prior is not None else min(started + 300, received + remaining)
                if time.monotonic() >= expires or time.time() >= scope.expires_at.timestamp():
                    raise _SpreadsheetGuardFailure('expired')
                calls = (prior.calls if prior is not None else 0) + 1
                seen = prior.seen_cursors if prior is not None else frozenset()
                if calls > 256 or (response.next_cursor is not None and (calls >= 256 or len(seen) >= 255)):
                    return terminal_spreadsheet('limit_reached')
                if response.next_cursor is not None:
                    if response.next_cursor in seen or response.next_cursor in self._spreadsheet_states:
                        raise ValueError('broker repeated spreadsheet continuation')
                    self._spreadsheet_states[response.next_cursor] = _SpreadsheetContinuation(digest,
                        scope.model_copy(deep=True), response.cell_end, expires, generation, seen | {response.next_cursor}, calls)
                return response
        except _SpreadsheetGuardFailure as error:
            return terminal_spreadsheet(error.status)
        except (BrokerUnavailable, ValueError, TypeError, OverflowError, AttributeError):
            return terminal_spreadsheet('error')
        finally:
            self._spreadsheet_operation_state.prior = None
            if reserved:
                with self._spreadsheet_lock:
                    self._spreadsheet_active -= 1
                    self._spreadsheet_pending -= 1

    def read_presentation(self, request: ReadPresentationRequest) -> ReadPresentationResponse:
        policy_snapshot = self._policy
        try:
            require_current(policy_snapshot)
            contract_descriptor(self._policy)
            if 'presentations' not in self._policy.enabled_capabilities:
                raise CompatibilityError('capability_disabled')
        except CompatibilityError as error:
            raise BrokerCompatibilityError(error.code) from None
        try:
            request = ReadPresentationRequest.model_validate(request.model_dump())
        except (ValueError, TypeError, AttributeError):
            return terminal_presentation('invalid_selection')
        reserved = False
        started = time.monotonic()
        try:
            with self._presentation_lock:
                if self._presentation_closed:
                    return terminal_presentation('expired')
                if self._presentation_active >= 4:
                    return terminal_presentation('capacity_exhausted')
                self._presentation_active += 1
                reserved = True
            self._presentation_operation_state.generation = None
            raw = self._request('read_presentation', request.model_dump(mode='json'))
            require_current(policy_snapshot)
            require_current(self._policy)
            if self._policy != policy_snapshot:
                raise CompatibilityError('config_stale')
            response = ReadPresentationResponse.model_validate_json(json.dumps(raw, allow_nan=False), strict=True)
            with self._presentation_lock:
                generation = self._presentation_operation_state.generation
                if self._presentation_closed or not generation or generation != self._presentation_generation:
                    return terminal_presentation('expired')
                if time.monotonic() - started >= 30:
                    return terminal_presentation('limit_reached')
                if response.status not in {'catalog', 'complete'}:
                    return terminal_presentation(response.status)
                scope = response.scope
                if (scope.artifact_id != request.artifact_id or scope.slides != request.slides or
                        scope.include_notes != request.include_notes or scope.broker_generation != generation or
                        scope.extractor_version != 1):
                    raise ValueError('broker presentation scope differs')
                parts = request.artifact_id.split('_')
                if scope.artifact_sha256 != parts[2] or scope.artifact_bytes != int(parts[3]):
                    raise ValueError('broker presentation artifact differs')
                if response.status == 'catalog':
                    if request.slides is not None or response.slides:
                        raise ValueError('broker presentation catalogue differs')
                elif request.slides is None or [slide.index for slide in response.slides] != request.slides:
                    raise ValueError('broker presentation selected order differs')
                # Request-bound checks supplement the strict response model. No
                # returned body is retained after this operation.
                if not request.include_notes and any(slide.notes is not None or any(
                        obj.source == 'notes' for obj in slide.unsupported_objects) for slide in response.slides):
                    raise ValueError('broker returned unrequested notes')
                return response.model_copy(update={'detail': PRESENTATION_DETAIL})
        except CompatibilityError:
            return terminal_presentation('blocked')
        except (BrokerUnavailable, ValueError, TypeError, OverflowError, AttributeError):
            return terminal_presentation('error')
        finally:
            self._presentation_operation_state.generation = None
            if reserved:
                with self._presentation_lock:
                    self._presentation_active -= 1

    def read_attachment(self, request: ReadAttachmentRequest) -> ReadAttachmentResponse:
        try:
            result = self._request("read_attachment", request.model_dump(mode="json"))
            return ReadAttachmentResponse.model_validate(result)
        except (BrokerUnavailable, ValidationError):
            return ReadAttachmentResponse(
                status="error", coverage_complete=False,
                detail="attachment broker is unavailable",
            )

    def analyze_media(self, request: AnalyzeMediaRequest) -> AnalyzeMediaResponse:
        try:
            return AnalyzeMediaResponse.model_validate(self._request("analyze_media", request.model_dump(mode="json")))
        except (BrokerUnavailable, ValidationError):
            return AnalyzeMediaResponse(status="error", detail="media broker is unavailable")

    def create_local_artifact(self, request: CreateLocalArtifactRequest) -> CreateLocalArtifactResponse:
        try:
            return CreateLocalArtifactResponse.model_validate(self._request("create_local_artifact", request.model_dump(mode="json")))
        except (BrokerUnavailable, ValidationError):
            return CreateLocalArtifactResponse(status="error", detail="artifact broker is unavailable")

    def begin_local_upload(self, request: BeginLocalUploadRequest) -> BeginLocalUploadResponse:
        try:
            return BeginLocalUploadResponse.model_validate(self._request("begin_local_upload", request.model_dump(mode="json")))
        except (BrokerUnavailable, ValidationError):
            return BeginLocalUploadResponse(status="error", detail="upload broker is unavailable")

    def append_local_upload(self, request: AppendLocalUploadRequest) -> AppendLocalUploadResponse:
        try:
            return AppendLocalUploadResponse.model_validate(self._request("append_local_upload", request.model_dump(mode="json")))
        except (BrokerUnavailable, ValidationError):
            return AppendLocalUploadResponse(status="error", upload_id=request.upload_id,
                                             detail="upload broker is unavailable; chunk outcome is unknown")

    def finish_local_upload(self, request: FinishLocalUploadRequest) -> FinishLocalUploadResponse:
        try:
            return FinishLocalUploadResponse.model_validate(self._request("finish_local_upload", request.model_dump(mode="json")))
        except (BrokerUnavailable, ValidationError):
            return FinishLocalUploadResponse(status="error", detail="upload broker is unavailable; finish outcome is unknown")

    def _revise_draft(self, operation: str, request: UpdateDraftRequest | RefreshDraftRequest) -> ReviseDraftResponse:
        try:
            response = ReviseDraftResponse.model_validate(self._request(operation, request.model_dump(mode="json")))
            if response.previous_draft_id is not None and response.previous_draft_id != request.draft_id:
                raise BrokerUnavailable("revision response does not match request")
            return response
        except (BrokerUnavailable, ValidationError):
            return ReviseDraftResponse(status="unavailable", detail="draft is unavailable")

    def update_draft(self, request: UpdateDraftRequest) -> ReviseDraftResponse:
        return self._revise_draft("update_draft", request)

    def refresh_draft(self, request: RefreshDraftRequest) -> ReviseDraftResponse:
        return self._revise_draft("refresh_draft", request)

    def list_drafts(self, request: ListDraftsRequest) -> ListDraftsResponse:
        try:
            response = ListDraftsResponse.model_validate(self._request("list_drafts", request.model_dump(mode="json")))
            if len(response.drafts) > request.limit or (
                request.after_draft_id is not None and any(
                    draft.draft_id <= request.after_draft_id for draft in response.drafts
                )
            ):
                raise BrokerUnavailable("draft list response does not match request")
            return response
        except (BrokerUnavailable, ValidationError):
            return ListDraftsResponse(status="unavailable", detail="drafts are unavailable")

    def get_send_status(self, request: GetSendStatusRequest) -> GetSendStatusResponse:
        try:
            response = GetSendStatusResponse.model_validate(
                self._request("get_send_status", request.model_dump(mode="json")))
            if response.draft_id != request.draft_id:
                raise BrokerUnavailable("send status response does not match request")
            return response
        except (BrokerUnavailable, ValidationError):
            return send_status(request.draft_id)

    def get_draft(self, request: GetDraftRequest) -> GetDraftResponse:
        try:
            response = GetDraftResponse.model_validate(self._request("get_draft", request.model_dump(mode="json")))
            if response.draft is not None and response.draft.draft_id != request.draft_id:
                raise BrokerUnavailable("draft response does not match request")
            return response
        except (BrokerUnavailable, ValidationError):
            return GetDraftResponse(status="unavailable", detail="draft is unavailable")

    def cancel_draft(self, request: CancelDraftRequest) -> CancelDraftResponse:
        try:
            response = CancelDraftResponse.model_validate(self._request("cancel_draft", request.model_dump(mode="json")))
            if response.draft_id is not None and response.draft_id != request.draft_id:
                raise BrokerUnavailable("cancellation response does not match request")
            return response
        except (BrokerUnavailable, ValidationError):
            return CancelDraftResponse(status="unavailable", detail="draft is unavailable")

    def prepare_artifact_send(self, request: PrepareArtifactSendRequest) -> PrepareArtifactSendResponse:
        try:
            return PrepareArtifactSendResponse.model_validate(self._request("prepare_artifact_send", request.model_dump(mode="json")))
        except (BrokerUnavailable, ValidationError):
            return PrepareArtifactSendResponse(status="error", detail="draft broker is unavailable")

    def prepare_reply_artifact_send(self,request: PrepareReplyArtifactSendRequest) -> PrepareReplyArtifactSendResponse:
        from .outgoing_drafts import _safe_caption,_safe_display_name
        try:
            response=PrepareReplyArtifactSendResponse.model_validate(self._request('prepare_reply_artifact_send',request.model_dump(mode='json')))
            if response.reply is not None:
                draft=response.reply.draft
                if (draft.recipient!=request.recipient or response.reply.reply_target.anchor!=request.reply_to
                    or draft.kind!=request.kind or draft.caption!=_safe_caption(request.caption)):
                    raise BrokerUnavailable('artifact reply scope differs')
                if request.kind=='voice_note':
                    if draft.source_sha256!=request.artifact_id.split('_')[2] or draft.source_display_name!=_safe_display_name(request.display_name):
                        raise BrokerUnavailable('voice reply provenance differs')
                elif (draft.artifact_id!=request.artifact_id or draft.display_name!=_safe_display_name(request.display_name)
                    or draft.mime_type!=request.mime_type):
                    raise BrokerUnavailable('artifact reply content differs')
            return response
        except (BrokerUnavailable,ValueError,CompatibilityError):
            return PrepareReplyArtifactSendResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def get_reply_artifact_draft(self,request: GetReplyArtifactDraftRequest) -> GetReplyArtifactDraftResponse:
        try:
            response=GetReplyArtifactDraftResponse.model_validate(self._request('get_reply_artifact_draft',request.model_dump(mode='json')))
            if response.reply is not None and response.reply.draft.draft_id!=request.draft_id:
                raise BrokerUnavailable('artifact reply handle differs')
            return response
        except (BrokerUnavailable,ValueError,CompatibilityError):
            return GetReplyArtifactDraftResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def _revise_reply_artifact_draft(self,operation,request) -> ReviseReplyArtifactDraftResponse:
        from .outgoing_drafts import _safe_caption
        try:
            response=ReviseReplyArtifactDraftResponse.model_validate(self._request(operation,request.model_dump(mode='json')))
            if response.reply is not None:
                if response.previous_draft_id!=request.draft_id:
                    raise BrokerUnavailable('artifact reply revision differs')
                if isinstance(request,UpdateReplyArtifactDraftRequest) and response.reply.draft.caption!=_safe_caption(request.caption):
                    raise BrokerUnavailable('artifact reply caption differs')
            return response
        except (BrokerUnavailable,ValueError,CompatibilityError):
            return ReviseReplyArtifactDraftResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def update_reply_artifact_draft(self,request: UpdateReplyArtifactDraftRequest) -> ReviseReplyArtifactDraftResponse:
        return self._revise_reply_artifact_draft('update_reply_artifact_draft',request)

    def refresh_reply_artifact_draft(self,request: RefreshReplyArtifactDraftRequest) -> ReviseReplyArtifactDraftResponse:
        return self._revise_reply_artifact_draft('refresh_reply_artifact_draft',request)

    def prepare_reply_text_send(self, request: PrepareReplyTextSendRequest) -> PrepareReplyTextSendResponse:
        try:
            response=PrepareReplyTextSendResponse.model_validate(self._request("prepare_reply_text_send",request.model_dump(mode="json")))
            if response.reply is not None:
                from .outgoing_drafts import _safe_message_text
                if (response.reply.draft.recipient!=request.recipient or response.reply.reply_target.anchor!=request.reply_to
                        or response.reply.draft.text!=_safe_message_text(request.text)):
                    raise BrokerUnavailable("reply response does not match request")
            return response
        except (BrokerUnavailable,ValidationError,ValueError):
            return PrepareReplyTextSendResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def get_reply_draft(self, request: GetReplyDraftRequest) -> GetReplyDraftResponse:
        try:
            response=GetReplyDraftResponse.model_validate(self._request("get_reply_draft",request.model_dump(mode="json")))
            if response.reply is not None and response.reply.draft.draft_id!=request.draft_id:
                raise BrokerUnavailable("reply response does not match request")
            return response
        except (BrokerUnavailable,ValidationError):
            return GetReplyDraftResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def _revise_reply_draft(self, operation: str, request: UpdateReplyDraftRequest | RefreshReplyDraftRequest) -> ReviseReplyDraftResponse:
        try:
            response=ReviseReplyDraftResponse.model_validate(self._request(operation,request.model_dump(mode="json")))
            if response.reply is not None:
                if response.previous_draft_id!=request.draft_id:
                    raise BrokerUnavailable("reply response does not match request")
                if isinstance(request,UpdateReplyDraftRequest):
                    from .outgoing_drafts import _safe_message_text
                    if response.reply.draft.text!=_safe_message_text(request.text):
                        raise BrokerUnavailable("reply response does not match request")
            return response
        except (BrokerUnavailable,ValidationError,ValueError):
            return ReviseReplyDraftResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def update_reply_draft(self, request: UpdateReplyDraftRequest) -> ReviseReplyDraftResponse:
        return self._revise_reply_draft("update_reply_draft",request)

    def refresh_reply_draft(self, request: RefreshReplyDraftRequest) -> ReviseReplyDraftResponse:
        return self._revise_reply_draft("refresh_reply_draft",request)

    def prepare_text_send(self, request: PrepareTextSendRequest) -> PrepareTextSendResponse:
        try:
            return PrepareTextSendResponse.model_validate(self._request("prepare_text_send", request.model_dump(mode="json")))
        except (BrokerUnavailable, ValidationError):
            return PrepareTextSendResponse(status="error", detail="draft broker is unavailable")

    def send_prepared_text(self, request: SendPreparedTextRequest) -> SendPreparedTextResponse:
        try:
            return SendPreparedTextResponse.model_validate(self._request("send_prepared_text", request.model_dump(mode="json")))
        except BrokerCompatibilityError as error:
            return SendPreparedTextResponse(status="error", draft_id=request.draft_id,
                                                detail="request rejected before dispatch: " + error.code)
        except (BrokerUnavailable, ValidationError):
            return SendPreparedTextResponse(status="outcome_unknown", draft_id=request.draft_id,
                                            detail="broker response is unavailable; do not retry automatically")

    def send_prepared_artifact(self, request: SendPreparedArtifactRequest) -> SendPreparedArtifactResponse:
        try:
            return SendPreparedArtifactResponse.model_validate(self._request("send_prepared_artifact", request.model_dump(mode="json")))
        except BrokerCompatibilityError as error:
            return SendPreparedArtifactResponse(status="error", draft_id=request.draft_id,
                                                detail="request rejected before dispatch: " + error.code)
        except (BrokerUnavailable, ValidationError):
            return SendPreparedArtifactResponse(status="outcome_unknown", draft_id=request.draft_id,
                                                detail="broker response is unavailable; do not retry automatically")

    def check(self) -> str:
        result = self._request("check", {})
        if set(result) != {"status"} or result["status"] not in {
            "ready",
            "blocked",
            "error",
        }:
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        return result["status"]

    def health(self) -> dict[str, Any]:
        result = self._request("health", {})
        expected = {
            "uptime_seconds",
            "queue_depth",
            "active_clients",
            "completed_requests",
            "error_count",
            "peak_overlap",
            "serialization_wait_count",
        }
        if set(result) != expected:
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        if type(result["uptime_seconds"]) not in (int, float) or result[
            "uptime_seconds"
        ] < 0:
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        if any(
            type(result[name]) is not int or result[name] < 0
            for name in expected - {"uptime_seconds"}
        ):
            raise BrokerUnavailable("TelegramSearch broker returned an invalid response")
        return result

    def close(self) -> None:
        with self._spreadsheet_lock:
            self._spreadsheet_closed = True
            self._spreadsheet_states.clear()
        with self._presentation_lock:
            self._presentation_closed = True
        with self._attachment_lock:
            self._attachment_closed = True
            self._attachment_states.clear()
        with self._target_lock:
            self._target_closed = True
            self._target_states.clear()
        with self._history_lock:
            if self._closed:
                return
            self._closed = True
            self._history_states.clear()
        with self._topics_lock:
            self._topics_states.clear()
        with self._topic_history_lock:
            self._topic_history_states.clear()
        with self._search_lock:
            self._search_states.clear()
        with self._chat_list_lock:
            self._chat_list_states.clear()
        with self._selected_search_lock:
            self._selected_search_states.clear()
        if not self._has_client_state:
            return
        try:
            self._request("release_client", {})
        except BrokerUnavailable:
            pass
