"""Single-owner Unix-socket broker for the local TDLib session."""

from __future__ import annotations

import ctypes
import base64
import binascii
import errno
import fcntl
import os
import secrets
import socket
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .artifact_store import ArtifactStore, ArtifactStoreError
from .config import ConfigurationError
from .approval_prompt import confirm_approved_send
from .attachment_service import get_attachment, get_message_context
from .message_reader import read_messages
from .reply_reader import read_reply_chain
from .broker_protocol import (
    FRAME_READ_TIMEOUT_SECONDS,
    MAX_DEADLINE_SECONDS,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    BrokerProtocolError,
    receive_request,
    send_frame,
)
from .config import TDLIB_SESSION_DIRECTORY, ensure_private_directory, RuntimePolicy, load_runtime_policy
from .contract import CompatibilityError, contract_descriptor, require_compatible, require_current, OPERATION_CAPABILITIES
from .document_reader import read_document
from .media_analyzer import analyze_media
from .local_upload import LocalUploadRegistry, UploadError
from .outgoing_drafts import DraftError, DraftOwner, OutgoingDraftRegistry
from .outgoing_media import validate_photo
from .outgoing_voice import prepare_voice_note, VoiceError
from .outgoing_stage import retire_staged_document, stage_approved_document
from .sanitize import sanitize_telegram_text
from .schemas import (
    AnalyzeMediaRequest, AnalyzeMediaResponse, AttachmentImage, AttachmentRequest,
    CreateLocalArtifactRequest, CreateLocalArtifactResponse, DiscoverTargetsRequest,
    MediaFrameResult, MediaSegment, MessageContextRequest, ReadAttachmentRequest, ReadMessagesRequest, ReadHistoryRequest, ReadReplyChainRequest, ListTopicsRequest, ReadTopicHistoryRequest, ListChatsRequest,
    PrepareArtifactSendRequest, PrepareArtifactSendResponse, ReadAttachmentResponse,
    PrepareTextSendRequest, PrepareTextSendResponse, SendPreparedTextRequest, SendPreparedTextResponse,
    BeginLocalUploadRequest, BeginLocalUploadResponse, AppendLocalUploadRequest, AppendLocalUploadResponse,
    FinishLocalUploadRequest, FinishLocalUploadResponse,
    VerifyTargetRequest, ReadTargetMessagesRequest,
    ResolveTargetRequest, SearchRequest, SearchChatsRequest, SearchMessagesRequest, SendPreparedArtifactRequest, SendPreparedArtifactResponse,
)
from .send_status_models import GetSendStatusRequest, GetSendStatusResponse, send_status
from .reply_artifact_drafts import (PrepareReplyArtifactSendRequest, PrepareReplyArtifactSendResponse,
    GetReplyArtifactDraftRequest, GetReplyArtifactDraftResponse, UpdateReplyArtifactDraftRequest,
    RefreshReplyArtifactDraftRequest, ReviseReplyArtifactDraftResponse, ReplyArtifactDraftPreview)
from .reply_drafts import (ReplyDraftPreview, PrepareReplyTextSendRequest, PrepareReplyTextSendResponse,
    GetReplyDraftRequest, GetReplyDraftResponse, UpdateReplyDraftRequest, RefreshReplyDraftRequest,
    ReviseReplyDraftResponse, PREPARED as REPLY_PREPARED, PENDING as REPLY_PENDING,
    REVISED as REPLY_REVISED, UNAVAILABLE as REPLY_UNAVAILABLE)

from .draft_models import (ListDraftsRequest, ListDraftsResponse, GetDraftRequest, GetDraftResponse,
                           CancelDraftRequest, CancelDraftResponse, DraftPreview, DraftSummary,
                           UpdateDraftRequest, RefreshDraftRequest, ReviseDraftResponse)
from .search_service import ReadOnlyTelegramClient, SearchService
from .tdjson import AuthorizationBlocked, MessageSendFailed, MessageSendNotAttempted, MessageSendOutcomeUnknown, TDLibClient, TDLibError


class BrokerStartupError(RuntimeError):
    """Raised when exclusive, private broker startup cannot be established."""


ClientFactory = Callable[[], ReadOnlyTelegramClient]
PeerUidLoader = Callable[[socket.socket], int]


@dataclass
class _ClientContext:
    service: SearchService
    touched_at: float
    active_requests: int = 0
    release_requested: bool = False
    attachment_pages: object | None = None
    spreadsheets: object | None = None
    presentations: object | None = None

    def close(self) -> None:
        self.service.close()
        if self.attachment_pages is not None:
            self.attachment_pages.close()
        if self.spreadsheets is not None:
            self.spreadsheets.close()
        if self.presentations is not None:
            self.presentations.close()

    def invalidate_active_handles(self) -> None:
        self.service.close_verified_targets()
        if self.attachment_pages is not None:
            self.attachment_pages.close()
        if self.spreadsheets is not None:
            self.spreadsheets.close()
        if self.presentations is not None:
            self.presentations.close()


def _peer_uid(connection: socket.socket) -> int:
    """Return the Darwin peer UID without widening the Python socket API."""
    libc = ctypes.CDLL(None, use_errno=True)
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    getpeereid = libc.getpeereid
    getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    getpeereid.restype = ctypes.c_int
    if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), "unable to verify broker peer")
    return int(uid.value)


_LOCAL_MIME_TYPES = {
    ".txt": "text/plain", ".text": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".tsv": "text/tab-separated-values", ".json": "application/json", ".xml": "application/xml",
    ".yaml": "application/yaml", ".yml": "application/yaml", ".py": "application/x-python",
    ".js": "application/javascript", ".ts": "text/plain", ".tsx": "text/plain", ".jsx": "text/plain",
    ".html": "text/html", ".css": "text/css", ".sh": "text/plain", ".sql": "text/plain",
    ".log": "text/plain", ".toml": "text/plain",
    ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
}


def _local_artifact_classification(file_name: str, kind: str = "document") -> tuple[str, str]:
    suffix = Path(file_name).suffix.casefold()
    media_kind = ("voice_note" if kind == "voice_note" else
                  "video" if suffix in {".mp4", ".mov", ".webm"} else
                  "audio" if suffix in {".wav", ".mp3", ".ogg"} else "document")
    return _LOCAL_MIME_TYPES.get(suffix, "application/octet-stream"), media_kind


class Broker:
    """Own one lazy Telegram client and isolate discovery state per proxy client."""

    def __init__(
        self,
        *,
        socket_path: Path,
        lock_path: Path | None = None,
        client_factory: ClientFactory = TDLibClient.from_defaults,
        verify_peer_uid: bool = True,
        peer_uid_loader: PeerUidLoader = _peer_uid,
        context_clock: Callable[[], float] = time.monotonic,
        context_ttl_seconds: float = 300.0,
        max_pending: int = 64,
        max_clients: int = 64,
        max_workers: int = 8,
        artifact_store: ArtifactStore | None = None,
        download_source_root: Path = TDLIB_SESSION_DIRECTORY / "files",
        approval_prompt: Callable[..., bool] = confirm_approved_send,
        policy: RuntimePolicy | None = None,
    ) -> None:
        if min(max_pending, max_clients, max_workers) < 1:
            raise ValueError("broker bounds must be positive")
        if context_ttl_seconds <= 0:
            raise ValueError("context_ttl_seconds must be positive")
        self._policy = policy if policy is not None else load_runtime_policy()
        self._contract = contract_descriptor(self._policy)
        self._generation = "broker_" + secrets.token_hex(16)
        self._socket_path = socket_path
        self._lock_path = lock_path or socket_path.with_name("broker.lock")
        self._client_factory = client_factory
        self._artifact_store = artifact_store if artifact_store is not None else ArtifactStore()
        self._drafts = OutgoingDraftRegistry(self._artifact_store,
                                            max_ttl_seconds=self._policy.max_draft_ttl_seconds)
        self._uploads = LocalUploadRegistry(self._artifact_store)
        self._approval_prompt = approval_prompt
        self._draft_recipient_titles: dict[str, str] = {}
        self._artifact_metadata: dict[str, tuple[object, str | None, str | None, str]] = {}
        self._download_source_root = download_source_root
        self._verify_peer_uid = verify_peer_uid
        self._peer_uid_loader = peer_uid_loader
        self._max_clients = max_clients
        self._context_clock = context_clock
        self._context_ttl_seconds = float(context_ttl_seconds)
        self._pending_slots = threading.BoundedSemaphore(max_pending)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="telegram-search-broker",
        )
        self._state_lock = threading.RLock()
        self._client: ReadOnlyTelegramClient | None = None
        self._contexts: dict[str, _ClientContext] = {}
        self._listener: socket.socket | None = None
        self._connections: set[socket.socket] = set()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._started_at = time.monotonic()
        self._pending = 0
        self._active_handlers = 0
        self._peak_overlap = 0
        self._completed = 0
        self._errors = 0

    def _acquire_owner_lock(self) -> int:
        if self._lock_path.is_symlink():
            raise BrokerStartupError("broker owner lock is unsafe")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
        except OSError as error:
            raise BrokerStartupError("broker owner lock is unsafe") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise BrokerStartupError("broker owner lock is unsafe")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise BrokerStartupError("broker owner lock is already held") from error
                raise BrokerStartupError("broker owner lock is unavailable") from error
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _prepare_socket_path(self) -> None:
        try:
            metadata = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            self._socket_path.is_symlink()
            or not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise BrokerStartupError("broker socket path is unsafe")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self._socket_path))
        except OSError as error:
            if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise BrokerStartupError("broker socket ownership is ambiguous") from error
        else:
            raise BrokerStartupError("an active broker socket already exists")
        finally:
            probe.close()
        try:
            current = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            self._socket_path.is_symlink()
            or not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != os.getuid()
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise BrokerStartupError("broker socket changed during validation")
        self._socket_path.unlink()

    def wait_until_ready(self, *, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def _shared_client(self) -> ReadOnlyTelegramClient:
        with self._state_lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _expire_contexts(self) -> None:
        with self._state_lock:
            now = self._context_clock()
            expired = [
                client_id
                for client_id, context in self._contexts.items()
                if context.active_requests == 0
                and now - context.touched_at >= self._context_ttl_seconds
            ]
            for client_id in expired:
                self._contexts.pop(client_id).close()

    @contextmanager
    def _service_lease(self, client_id: str):
        with self._state_lock:
            self._expire_contexts()
            context = self._contexts.get(client_id)
            if context is None:
                if len(self._contexts) >= self._max_clients:
                    raise BrokerProtocolError("client capacity is exhausted")
                context = _ClientContext(
                    service=SearchService(
                        client=self._shared_client(),
                        owns_client=False,
                        client_id=client_id,
                        broker_generation=self._generation,
                    ),
                    touched_at=self._context_clock(),
                )
                self._contexts[client_id] = context
            context.touched_at = self._context_clock()
            context.active_requests += 1
        try:
            yield context.service
        finally:
            with self._state_lock:
                current = self._contexts.get(client_id)
                if current is context:
                    context.active_requests -= 1
                    context.touched_at = self._context_clock()
                    if context.active_requests == 0 and context.release_requested:
                        self._contexts.pop(client_id, None)
                        context.close()

    @staticmethod
    def _require_empty(payload: dict[str, Any]) -> None:
        if payload:
            raise BrokerProtocolError("operation payload must be empty")

    def _health(self) -> dict[str, Any]:
        self._expire_contexts()
        with self._state_lock:
            wait_count = getattr(self._client, "serialization_wait_count", 0)
            if type(wait_count) is not int or wait_count < 0:
                wait_count = 0
            return {
                "uptime_seconds": max(0.0, time.monotonic() - self._started_at),
                "queue_depth": self._pending,
                "active_clients": len(self._contexts),
                "completed_requests": self._completed,
                "error_count": self._errors,
                "peak_overlap": self._peak_overlap,
                "serialization_wait_count": wait_count,
            }

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        require_current(self._policy)
        if request.get("broker_generation") != self._generation:
            raise CompatibilityError("generation_stale")
        operation = request["operation"]
        capability = OPERATION_CAPABILITIES.get(operation)
        if capability is not None and (capability not in self._policy.enabled_capabilities or
                (capability in {"reply_text_send","reply_artifact_send"} and "send" not in self._policy.enabled_capabilities)):
            raise CompatibilityError("capability_disabled")
        self._expire_contexts()
        payload = request["payload"]
        client_id = request["client_id"]
        remaining = request["deadline"] - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline expired")
        if remaining > MAX_DEADLINE_SECONDS:
            raise BrokerProtocolError("request deadline exceeds the allowed bound")
        if operation == "resolve":
            model = ResolveTargetRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.resolve(model).model_dump(mode="json")
        if operation == "discover":
            model = DiscoverTargetsRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.discover(model).model_dump(mode="json")
        if operation == "search":
            model = SearchRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.search(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "get_attachment":
            model = AttachmentRequest.model_validate(payload)
            response = get_attachment(
                self._shared_client(), self._artifact_store, model,
                source_root=self._download_source_root,
            )
            if response.status == "complete" and response.artifact_id is not None:
                with self._state_lock:
                    self._artifact_metadata[response.artifact_id] = (
                        response.anchor, response.file_name, response.mime_type,
                        response.media_type or "document",
                    )
            return response.model_dump(mode="json")
        if operation == "read_attachment_page":
            from .attachment_page_models import ReadAttachmentPageRequest
            from .attachment_pages import AttachmentPageReader
            model = ReadAttachmentPageRequest.model_validate(payload)
            with self._service_lease(client_id):
                with self._state_lock:
                    context = self._contexts[client_id]
                    if context.attachment_pages is None:
                        context.attachment_pages = AttachmentPageReader(
                            client=self._shared_client(), store=self._artifact_store,
                            metadata_lookup=self._attachment_metadata, client_id=client_id,
                            broker_generation=self._generation,
                        )
                        if context.release_requested:
                            context.attachment_pages.close()
                    reader = context.attachment_pages
                return reader.read(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_spreadsheet":
            from .spreadsheet_models import ReadSpreadsheetRequest
            from .spreadsheets import SpreadsheetReader
            model = ReadSpreadsheetRequest.model_validate(payload)
            with self._service_lease(client_id):
                with self._state_lock:
                    context = self._contexts[client_id]
                    if context.spreadsheets is None:
                        context.spreadsheets = SpreadsheetReader(
                            client=self._shared_client(), store=self._artifact_store,
                            metadata_lookup=self._attachment_metadata, client_id=client_id,
                            broker_generation=self._generation,
                        )
                        if context.release_requested:
                            context.spreadsheets.close()
                    reader = context.spreadsheets
                return reader.read(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_presentation":
            from .presentation_models import ReadPresentationRequest
            from .presentations import PresentationReader
            policy_snapshot = self._policy
            model = ReadPresentationRequest.model_validate(payload)
            with self._service_lease(client_id):
                with self._state_lock:
                    context = self._contexts[client_id]
                    if context.presentations is None:
                        context.presentations = PresentationReader(
                            client=self._shared_client(), store=self._artifact_store,
                            metadata_lookup=self._attachment_metadata, client_id=client_id,
                            broker_generation=self._generation,
                        )
                        if context.release_requested:
                            context.presentations.close()
                    reader = context.presentations
                response = reader.read(model, deadline=request["deadline"])
                require_current(policy_snapshot)
                require_current(self._policy)
                if self._policy != policy_snapshot or 'presentations' not in self._policy.enabled_capabilities:
                    raise CompatibilityError("config_stale")
                if request["broker_generation"] != self._generation:
                    raise CompatibilityError("generation_stale")
                return response.model_dump(mode="json")
        if operation == "read_attachment":
            model = ReadAttachmentRequest.model_validate(payload)
            return self._read_attachment(model).model_dump(mode="json")
        if operation == "analyze_media":
            model = AnalyzeMediaRequest.model_validate(payload)
            return self._analyze_media(model).model_dump(mode="json")
        if operation == "create_local_artifact":
            model = CreateLocalArtifactRequest.model_validate(payload)
            return self._create_local_artifact(model).model_dump(mode="json")
        if operation == "begin_local_upload":
            return self._begin_local_upload(BeginLocalUploadRequest.model_validate(payload)).model_dump(mode="json")
        if operation == "append_local_upload":
            return self._append_local_upload(AppendLocalUploadRequest.model_validate(payload)).model_dump(mode="json")
        if operation == "finish_local_upload":
            return self._finish_local_upload(FinishLocalUploadRequest.model_validate(payload)).model_dump(mode="json")
        if operation == "prepare_artifact_send":
            model = PrepareArtifactSendRequest.model_validate(payload)
            return self._prepare_artifact_send(model, client_id=client_id).model_dump(mode="json")
        if operation in {"prepare_reply_artifact_send","get_reply_artifact_draft","update_reply_artifact_draft","refresh_reply_artifact_draft"}:
            models={"prepare_reply_artifact_send":PrepareReplyArtifactSendRequest,
                "get_reply_artifact_draft":GetReplyArtifactDraftRequest,
                "update_reply_artifact_draft":UpdateReplyArtifactDraftRequest,
                "refresh_reply_artifact_draft":RefreshReplyArtifactDraftRequest}
            model=models[operation].model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"],time.monotonic()+330)):
                return getattr(self,"_"+operation)(model,client_id=client_id).model_dump(mode="json")
        if operation == "prepare_reply_text_send":
            model=PrepareReplyTextSendRequest.model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"],time.monotonic()+30)):
                return self._prepare_reply_text_send(model,client_id=client_id).model_dump(mode="json")
        if operation == "get_reply_draft":
            model=GetReplyDraftRequest.model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"],time.monotonic()+30)):
                return self._get_reply_draft(model,client_id=client_id).model_dump(mode="json")
        if operation in {"update_reply_draft","refresh_reply_draft"}:
            model=(UpdateReplyDraftRequest if operation=="update_reply_draft" else RefreshReplyDraftRequest).model_validate(payload)
            with self._shared_client().request_budget(min(request["deadline"],time.monotonic()+30)):
                return self._revise_reply_draft(model,client_id=client_id).model_dump(mode="json")
        if operation == "prepare_text_send":
            model = PrepareTextSendRequest.model_validate(payload)
            return self._prepare_text_send(model, client_id=client_id).model_dump(mode="json")
        if operation == "send_prepared_artifact":
            model = SendPreparedArtifactRequest.model_validate(payload)
            return self._send_prepared_artifact(model, client_id=client_id, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "send_prepared_text":
            model = SendPreparedTextRequest.model_validate(payload)
            return self._send_prepared_text(model, client_id=client_id, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "list_drafts":
            return self._list_drafts(ListDraftsRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "get_send_status":
            return self._get_send_status(GetSendStatusRequest.model_validate(payload), client_id=client_id,
                                         deadline=request["deadline"]).model_dump(mode="json")
        if operation == "get_draft":
            return self._get_draft(GetDraftRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "cancel_draft":
            return self._cancel_draft(CancelDraftRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "update_draft":
            return self._revise_draft(UpdateDraftRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "refresh_draft":
            return self._revise_draft(RefreshDraftRequest.model_validate(payload), client_id=client_id).model_dump(mode="json")
        if operation == "read_reply_chain":
            model = ReadReplyChainRequest.model_validate(payload)
            return read_reply_chain(self._shared_client(), model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "verify_target":
            model = VerifyTargetRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.verify_target(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_target_messages":
            model = ReadTargetMessagesRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.read_target_messages(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_messages":
            model = ReadMessagesRequest.model_validate(payload)
            return read_messages(self._shared_client(), model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "search_chats":
            model = SearchChatsRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.search_chats(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "search_messages":
            model = SearchMessagesRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.search_messages(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_topic_history":
            model = ReadTopicHistoryRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.read_topic_history(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "list_chats":
            model = ListChatsRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.list_chats(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "list_topics":
            model = ListTopicsRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.list_topics(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "read_history":
            model = ReadHistoryRequest.model_validate(payload)
            with self._service_lease(client_id) as service:
                return service.read_history(model, deadline=request["deadline"]).model_dump(mode="json")
        if operation == "get_message_context":
            model = MessageContextRequest.model_validate(payload)
            return get_message_context(self._shared_client(), model).model_dump(mode="json")
        if operation == "check":
            self._require_empty(payload)
            try:
                self._shared_client().ensure_ready()
            except AuthorizationBlocked:
                return {"status": "blocked"}
            except TDLibError:
                return {"status": "error"}
            return {"status": "ready"}
        if operation == "health":
            self._require_empty(payload)
            return self._health()
        if operation == "release_client":
            self._require_empty(payload)
            with self._state_lock:
                context = self._contexts.get(client_id)
                if context is not None and context.active_requests > 0:
                    context.release_requested = True
                    context.invalidate_active_handles()
                else:
                    context = self._contexts.pop(client_id, None)
                    if context is not None:
                        context.close()
            return {"released": True}
        raise BrokerProtocolError("operation is not allowed")

    def _attachment_metadata(self, artifact_id: str):
        with self._state_lock:
            return self._artifact_metadata.get(artifact_id)

    def _read_attachment(self, request: ReadAttachmentRequest) -> ReadAttachmentResponse:
        with self._state_lock:
            metadata = self._artifact_metadata.get(request.artifact_id)
        if metadata is None:
            return ReadAttachmentResponse(
                status="expired", detail="artifact metadata is unavailable", coverage_complete=False,
            )
        anchor, name, mime_type, media_type = metadata
        if media_type in {"audio", "voice_note", "video", "video_note"}:
            return ReadAttachmentResponse(
                status="unsupported", source_anchor=anchor,
                detail="use analyze_media for audio or video", coverage_complete=False,
            )
        try:
            artifact = self._artifact_store.lookup(request.artifact_id)
            if artifact is None:
                with self._state_lock:
                    self._artifact_metadata.pop(request.artifact_id, None)
                return ReadAttachmentResponse(
                    status="expired", source_anchor=anchor,
                    detail="artifact has expired or changed", coverage_complete=False,
                )
            result = read_document(
                artifact.path, name=name, mime_type=mime_type,
                max_chars=request.max_chars, max_pages=request.max_pages,
            )
            images: list[AttachmentImage] = []
            temporary_pages = [path for path in result.image_paths if path != artifact.path]
            try:
                temporary_bytes = sum(path.stat().st_size for path in temporary_pages)
                if temporary_bytes > 10 * 1024 * 1024:
                    raise ArtifactStoreError("generated page cleanup exceeds the safe bound")
                for index, image_path in enumerate(result.image_paths):
                    if image_path == artifact.path:
                        image_artifact = artifact
                        image_mime = mime_type if mime_type in {"image/jpeg", "image/png", "image/webp"} else "image/png"
                    else:
                        image_artifact = self._artifact_store.store(image_path, kind="image")
                        image_mime = "image/png"
                    images.append(AttachmentImage(
                        artifact_id=image_artifact.artifact_id,
                        artifact_path=str(image_artifact.path),
                        mime_type=image_mime,
                        page_number=index + 1 if result.total_pages is not None else None,
                    ))
            finally:
                for image_path in temporary_pages:
                    image_path.unlink(missing_ok=True)
                if temporary_pages:
                    temporary_pages[0].parent.rmdir()
            return ReadAttachmentResponse(
                status=result.status, source_anchor=anchor, text=result.text,
                processed_bytes=result.processed_bytes, total_bytes=result.total_bytes,
                processed_pages=result.processed_pages, total_pages=result.total_pages,
                images=images, coverage_complete=result.status == "complete",
                detail=result.error or "bounded local document read",
            )
        except (ArtifactStoreError, OSError, ValueError):
            return ReadAttachmentResponse(
                status="error", source_anchor=anchor, detail="artifact read failed safely",
                coverage_complete=False,
            )

    def _analyze_media(self, request: AnalyzeMediaRequest) -> AnalyzeMediaResponse:
        with self._state_lock:
            metadata = self._artifact_metadata.get(request.artifact_id)
        if metadata is None:
            return AnalyzeMediaResponse(status="expired", detail="artifact metadata is unavailable")
        anchor, _, _, kind = metadata
        if kind not in {"audio", "voice_note", "video", "video_note"}:
            return AnalyzeMediaResponse(status="unsupported", source_anchor=anchor, detail="artifact is not audio or video")
        try:
            artifact = self._artifact_store.lookup(request.artifact_id)
            if artifact is None:
                return AnalyzeMediaResponse(status="expired", source_anchor=anchor, detail="artifact has expired")
            result = analyze_media(artifact.path, kind=kind, start_seconds=request.start_seconds)
            try:
                frames: list[MediaFrameResult] = []
                for item in result.frames:
                    stored = self._artifact_store.store(item.path, kind="image")
                    frames.append(MediaFrameResult(
                        time_seconds=item.time_seconds, source=item.source,
                        artifact_id=stored.artifact_id, artifact_path=str(stored.path),
                    ))
                return AnalyzeMediaResponse(
                    status=result.status, source_anchor=anchor,
                    duration_seconds=result.duration_seconds,
                    window_start_seconds=result.window_start_seconds,
                    window_end_seconds=result.window_end_seconds,
                    transcription_status=result.transcription_status,
                    transcribed_start_seconds=result.transcribed_start_seconds,
                    transcribed_end_seconds=result.transcribed_end_seconds,
                    language=result.language,
                    segments=[MediaSegment(**vars(item)) for item in result.segments],
                    frame_status=result.frame_status, frames=frames,
                    coverage_complete=(result.status == "complete"
                                       and result.transcription_status in {"complete", "no_audio"}
                                       and result.frame_status in {"complete", "not_applicable"}
                                       and not any(item.uncertain for item in result.segments)),
                    detail=result.detail or "bounded local media analysis",
                )
            finally:
                result.cleanup()
        except (ArtifactStoreError, OSError, ValueError):
            return AnalyzeMediaResponse(status="error", source_anchor=anchor, detail="media analysis failed safely")

    def _create_local_artifact(self, request: CreateLocalArtifactRequest) -> CreateLocalArtifactResponse:
        suffix = Path(request.file_name).suffix.casefold()
        if suffix not in _LOCAL_MIME_TYPES:
            return CreateLocalArtifactResponse(status="error", detail="file format is unsupported")
        import tempfile
        if request.content is not None:
            if suffix not in {".txt", ".text", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".sql", ".log", ".toml"}:
                return CreateLocalArtifactResponse(status="error", detail="binary format requires base64 content")
            data = request.content.encode("utf-8")
        else:
            try:
                data = base64.b64decode(request.content_base64 or "", validate=True)
            except binascii.Error:
                return CreateLocalArtifactResponse(status="error", detail="base64 content is invalid")
        if len(data) > 512 * 1024:
            return CreateLocalArtifactResponse(status="error", detail="content exceeds creation limit")
        try:
            with tempfile.TemporaryDirectory(prefix="telegram-artifact-") as directory:
                path = Path(directory) / request.file_name
                path.write_bytes(data)
                mime_type, media_kind = _local_artifact_classification(request.file_name)
                artifact = self._artifact_store.store(path, kind="media" if media_kind != "document" else "document")
            with self._state_lock:
                self._artifact_metadata[artifact.artifact_id] = (
                    None, request.file_name, mime_type, media_kind,
                )
            return CreateLocalArtifactResponse(
                status="complete", artifact_id=artifact.artifact_id,
                artifact_path=str(artifact.path), sha256=artifact.sha256,
                size_bytes=artifact.size_bytes, file_name=request.file_name,
                detail="local artifact created in private cache",
            )
        except (ArtifactStoreError, OSError):
            return CreateLocalArtifactResponse(status="error", detail="local artifact creation failed safely")

    def _begin_local_upload(self, request: BeginLocalUploadRequest) -> BeginLocalUploadResponse:
        try:
            upload_id = self._uploads.begin(file_name=request.file_name, kind=request.kind,
                                            size_bytes=request.size_bytes, sha256=request.sha256)
            return BeginLocalUploadResponse(status="ready", upload_id=upload_id,
                                            expires_at=datetime.fromtimestamp(time.time() + 900, tz=timezone.utc),
                                            detail="ordered local upload started")
        except (UploadError, OSError, ValueError):
            return BeginLocalUploadResponse(status="error", detail="local upload could not be started safely")

    def _append_local_upload(self, request: AppendLocalUploadRequest) -> AppendLocalUploadResponse:
        try:
            next_index, received = self._uploads.append(
                request.upload_id, index=request.index,
                content_base64=request.content_base64, sha256=request.sha256,
            )
            return AppendLocalUploadResponse(status="accepted", upload_id=request.upload_id,
                                             next_index=next_index, received_bytes=received,
                                             detail="chunk accepted")
        except (UploadError, OSError, ValueError):
            return AppendLocalUploadResponse(status="error", upload_id=request.upload_id,
                                             detail="chunk was rejected; no bytes from this chunk were accepted")

    def _finish_local_upload(self, request: FinishLocalUploadRequest) -> FinishLocalUploadResponse:
        try:
            completed = self._uploads.finish_with_metadata(request.upload_id)
            artifact = completed.artifact
            mime_type, media_kind = _local_artifact_classification(completed.file_name, completed.kind)
            with self._state_lock:
                self._artifact_metadata[artifact.artifact_id] = (
                    None, completed.file_name, mime_type, media_kind,
                )
            return FinishLocalUploadResponse(status="complete", artifact_id=artifact.artifact_id,
                                             artifact_path=str(artifact.path), sha256=artifact.sha256,
                                             size_bytes=artifact.size_bytes, file_name=completed.file_name,
                                             detail="complete local upload stored in private artifact cache")
        except (UploadError, ArtifactStoreError, OSError, ValueError):
            return FinishLocalUploadResponse(status="error", detail="local upload could not be completed safely")

    def _draft_owner(self, client_id: str) -> DraftOwner:
        require_current(self._policy)
        if "send" not in self._policy.enabled_capabilities:
            raise CompatibilityError("capability_disabled")
        client = self._shared_client()
        client.ensure_ready()
        try:
            account_id = client.get_account_id()
        except AttributeError:
            raise DraftError("account is unavailable") from None
        return DraftOwner(client_id=client_id, account_id=account_id)

    @staticmethod
    def _draft_summary(draft, owner: DraftOwner) -> DraftSummary:
        return DraftSummary(
            draft_id=draft.draft_id, account_id=owner.account_id, recipient=draft.recipient,
            recipient_title=draft.recipient_title, kind=draft.kind,
            expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
            approval_required=draft.approval_required,
        )

    def _list_drafts(self, request: ListDraftsRequest, *, client_id: str) -> ListDraftsResponse:
        try:
            owner = self._draft_owner(client_id)
            pending, has_more = self._drafts.list_pending(owner=owner, limit=request.limit,
                                                        after_draft_id=request.after_draft_id)
            summaries = [self._draft_summary(draft, owner) for draft in pending]
            return ListDraftsResponse(status="listed", drafts=summaries, has_more=has_more,
                                      next_after_draft_id=pending[-1].draft_id if has_more else None,
                                      detail="owned pending drafts; live view, no recipient verification or approval")
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return ListDraftsResponse(status="unavailable", detail="drafts are unavailable")

    def _draft_preview(self, draft, owner: DraftOwner) -> DraftPreview:
        if draft.reply_source is not None:
            raise DraftError("draft is unavailable")
        return self._plain_draft_preview(draft,owner)

    def _plain_draft_preview(self, draft, owner: DraftOwner) -> DraftPreview:
        content = dict(sha256=draft.sha256, size_bytes=draft.size_bytes)
        if draft.kind == "text":
            content["text"] = draft.text
        else:
            content.update(artifact_id=draft.artifact_id, display_name=draft.display_name,
                           mime_type=draft.mime_type, caption=draft.caption,
                           duration_seconds=draft.duration_seconds, waveform_base64=draft.waveform_base64,
                           source_sha256=draft.source_sha256, source_display_name=draft.source_display_name,
                           converted=draft.converted)
        return DraftPreview(**self._draft_summary(draft, owner).model_dump(), **content)

    def _get_send_status(self, request: GetSendStatusRequest, *, client_id: str,
                         deadline: float | None = None) -> GetSendStatusResponse:
        budget = min(deadline if deadline is not None else float("inf"), time.monotonic() + 30)
        try:
            require_current(self._policy)
            if "send" not in self._policy.enabled_capabilities:
                raise CompatibilityError("capability_disabled")
            client = self._shared_client()
            with client.request_budget(budget):
                owner = self._draft_owner(client_id)
                state, receipt, provider, attempted_at, provider_epoch = self._drafts.status_snapshot(request.draft_id, owner=owner)
                result = send_status(request.draft_id)
                if state == "pending":
                    result = send_status(request.draft_id, "local_pending")
                elif provider is client and provider_epoch is client.send_observation_epoch:
                    observation = client.get_send_observation(request.draft_id)
                    if receipt is not None and receipt.evidence == "local_failed":
                        result = send_status(request.draft_id, "local_failed")
                    elif observation is not None:
                        if observation.status in {"sent", "failed"}:
                            receipt = self._drafts.reconcile(request.draft_id, owner=owner, provider=client, provider_epoch=provider_epoch,
                                attempted_at=attempted_at, status=observation.status, message_id=observation.message_id)
                            # A conflicting terminal event never changes the immutable receipt.
                            if receipt.status == observation.status and receipt.message_id == observation.message_id:
                                result = send_status(request.draft_id, receipt.evidence, receipt.message_id)
                        elif observation.status == "pending" and receipt is not None and receipt.status == "outcome_unknown":
                            result = send_status(request.draft_id, "provider_pending", status_override="outcome_unknown")
                        elif state == "claimed":
                            result = send_status(request.draft_id,
                                "provider_pending" if observation.status == "pending" else "local_claimed")
                    elif state == "claimed":
                        result = send_status(request.draft_id, "local_claimed")
                if self._draft_owner(client_id) != owner or time.monotonic() >= budget:
                    return send_status(request.draft_id)
                # Recheck local expiry and provider retention after account/auth reads.
                current_state, current_receipt, current_provider, current_attempt, current_epoch = self._drafts.status_snapshot(
                    request.draft_id, owner=owner)
                if current_provider is not provider or current_attempt != attempted_at or current_epoch is not provider_epoch:
                    return send_status(request.draft_id)
                if result.status == "pending" and (current_state not in {"pending", "claimed"} or current_receipt is not None):
                    if (result.evidence == "provider_pending" and current_receipt is not None
                            and current_receipt.status == "outcome_unknown"):
                        result = send_status(request.draft_id, "provider_pending", status_override="outcome_unknown")
                    else:
                        return send_status(request.draft_id)
                if self._shared_client() is not client or (provider is client and provider_epoch is not client.send_observation_epoch):
                    return send_status(request.draft_id)
                if result.evidence in {"provider_confirmed", "provider_failed", "provider_pending"}:
                    current = client.get_send_observation(request.draft_id)
                    if current is None or current != observation:
                        return send_status(request.draft_id)
                require_current(self._policy)
                if "send" not in self._policy.enabled_capabilities or time.monotonic() >= budget:
                    return send_status(request.draft_id)
                return result
        except (DraftError, TDLibError, ValueError, CompatibilityError, AttributeError, TimeoutError):
            return send_status(request.draft_id)

    def _get_draft(self, request: GetDraftRequest, *, client_id: str) -> GetDraftResponse:
        try:
            owner = self._draft_owner(client_id)
            draft = self._drafts.peek(request.draft_id, owner=owner)
            preview = self._draft_preview(draft, owner)
            return GetDraftResponse(status="pending", draft=preview,
                                    detail="exact pending draft; no recipient verification or approval")
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return GetDraftResponse(status="unavailable", detail="draft is unavailable")

    def _revise_draft(self, request: UpdateDraftRequest | RefreshDraftRequest,
                      *, client_id: str) -> ReviseDraftResponse:
        try:
            owner = self._draft_owner(client_id)
            old = self._drafts.peek(request.draft_id, owner=owner)
            self._draft_preview(old, owner)
            chat = self._shared_client().resolve_target(old.recipient)
            title = sanitize_telegram_text(chat.get("title"), max_length=255) or str(old.recipient)
            if self._draft_owner(client_id) != owner:
                raise DraftError("draft is unavailable")
            revised = self._drafts.revise(
                request.draft_id, owner=owner, recipient_title=title,
                text=request.text if isinstance(request, UpdateDraftRequest) else None,
                caption=request.caption if isinstance(request, UpdateDraftRequest) else None,
            )
            with self._state_lock:
                known = self._drafts.known_ids()
                for old_id in list(self._draft_recipient_titles):
                    if old_id not in known:
                        self._draft_recipient_titles.pop(old_id, None)
                self._draft_recipient_titles[revised.draft_id] = title
            return ReviseDraftResponse(
                status="revised", previous_draft_id=request.draft_id,
                draft=self._draft_preview(revised, owner),
                detail="new immutable revision; prior draft invalidated; fresh approval required",
            )
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return ReviseDraftResponse(status="unavailable", detail="draft is unavailable")

    def _cancel_draft(self, request: CancelDraftRequest, *, client_id: str) -> CancelDraftResponse:
        try:
            owner = self._draft_owner(client_id)
            self._drafts.cancel(request.draft_id, owner=owner)
            return CancelDraftResponse(status="cancelled", draft_id=request.draft_id,
                                       detail="pending draft cancelled; cached artifacts retained")
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return CancelDraftResponse(status="unavailable", detail="draft is unavailable")

    def _prepare_artifact_send(self, request: PrepareArtifactSendRequest, *, client_id: str) -> PrepareArtifactSendResponse:
        try:
            client = self._shared_client()
            client.ensure_ready()
            owner = self._draft_owner(client_id)
            chat = client.resolve_target(request.recipient)
            title = sanitize_telegram_text(chat.get("title"), max_length=255) or str(request.recipient)
            if request.kind == "photo":
                artifact = self._artifact_store.lookup(request.artifact_id)
                if artifact is None:
                    raise DraftError("artifact is unavailable")
                validate_photo(artifact.path, size_bytes=artifact.size_bytes,
                               display_name=request.display_name, mime_type=request.mime_type)
            artifact_id = request.artifact_id
            display_name = request.display_name
            mime_type = request.mime_type
            duration_seconds = None
            waveform_base64 = None
            source_sha256 = None
            source_display_name = None
            converted = False
            if request.kind == "voice_note":
                source = self._artifact_store.lookup(request.artifact_id)
                if source is None:
                    raise DraftError("artifact is unavailable")
                voice = prepare_voice_note(source, self._artifact_store,
                                           input_mime=request.mime_type,
                                           input_name=request.display_name)
                artifact_id = voice.artifact.artifact_id
                display_name = Path(request.display_name).stem + ".ogg"
                mime_type = "audio/ogg"
                duration_seconds = voice.duration_seconds
                waveform_base64 = voice.waveform_base64
                source_sha256 = voice.source_sha256
                source_display_name = request.display_name
                converted = voice.converted
            draft = self._drafts.prepare(
                owner=owner, artifact_id=artifact_id, recipient=request.recipient, recipient_title=title,
                display_name=display_name, mime_type=mime_type,
                caption=request.caption, kind=request.kind,
                duration_seconds=duration_seconds, waveform_base64=waveform_base64,
                source_sha256=source_sha256, source_display_name=source_display_name,
                converted=converted,
            )
            with self._state_lock:
                known = self._drafts.known_ids()
                for old_id in list(self._draft_recipient_titles):
                    if old_id not in known:
                        self._draft_recipient_titles.pop(old_id, None)
                self._draft_recipient_titles[draft.draft_id] = title
            return PrepareArtifactSendResponse(
                status="prepared", draft_id=draft.draft_id, recipient=draft.recipient,
                recipient_title=title, display_name=draft.display_name,
                mime_type=draft.mime_type, caption=draft.caption,
                kind=draft.kind, duration_seconds=draft.duration_seconds,
                waveform_base64=draft.waveform_base64,
                source_sha256=draft.source_sha256,
                source_display_name=draft.source_display_name,
                converted=draft.converted,
                sha256=draft.sha256, size_bytes=draft.size_bytes,
                expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
                detail="immutable local draft prepared; explicit approval is required before sending",
            )
        except AuthorizationBlocked:
            return PrepareArtifactSendResponse(status="blocked", detail="TDLib authorization is not ready")
        except (DraftError, TDLibError, VoiceError, ValueError, CompatibilityError):
            return PrepareArtifactSendResponse(status="error", detail="draft could not be prepared safely")

    def _reply_artifact_owner(self, client_id: str) -> DraftOwner:
        owner=self._draft_owner(client_id)
        if "reply_artifact_send" not in self._policy.enabled_capabilities:
            raise CompatibilityError("capability_disabled")
        return owner

    def _require_reply_source_policy(self, source):
        require_current(self._policy)
        if any(capability not in self._policy.enabled_capabilities for capability in source.required_capabilities):
            raise CompatibilityError('capability_disabled')

    def _reply_artifact_preview(self,draft,owner):
        if draft.reply_source is None or draft.kind=='text':raise DraftError(REPLY_UNAVAILABLE)
        self._require_reply_source_policy(draft.reply_source)
        return ReplyArtifactDraftPreview.create(self._plain_draft_preview(draft,owner),draft.reply_source)

    @staticmethod
    def _reply_budget_check(provider):
        deadline=getattr(provider._request_context,"deadline",None)
        if deadline is not None and time.monotonic()>=deadline:raise TimeoutError('reply request expired')

    def _prepare_reply_artifact_send(self,request: PrepareReplyArtifactSendRequest,*,client_id: str):
        from .outgoing_voice import prepare_reply_voice_note
        from .outgoing_drafts import _safe_display_name
        try:
            owner=self._reply_artifact_owner(client_id);provider=self._shared_client()
            request.caption.encode('utf-8');request.display_name.encode('utf-8')
            if request.reply_to.chat_id!=request.recipient:raise DraftError(REPLY_UNAVAILABLE)
            chat=provider.resolve_target(request.recipient)
            title=sanitize_telegram_text(chat.get('title'),max_length=255) or str(request.recipient)
            source=provider.read_reply_source(request.reply_to.chat_id,request.reply_to.message_id)
            self._require_reply_source_policy(source)
            artifact=self._artifact_store.lookup(request.artifact_id)
            if artifact is None:raise DraftError(REPLY_UNAVAILABLE)
            kwargs=dict(artifact_id=request.artifact_id,display_name=request.display_name,mime_type=request.mime_type)
            if request.kind=='photo':
                validate_photo(artifact.path,size_bytes=artifact.size_bytes,display_name=request.display_name,mime_type=request.mime_type)
            elif request.kind=='voice_note':
                voice=prepare_reply_voice_note(artifact,self._artifact_store,input_mime=request.mime_type,
                    input_name=request.display_name,budget_check=lambda:self._reply_budget_check(provider))
                kwargs.update(artifact_id=voice.artifact.artifact_id,display_name=_safe_display_name(Path(_safe_display_name(request.display_name)).stem+'.ogg'),mime_type='audio/ogg',
                    duration_seconds=voice.duration_seconds,waveform_base64=voice.waveform_base64,
                    source_sha256=voice.source_sha256,source_display_name=_safe_display_name(request.display_name),converted=voice.converted)
            self._reply_budget_check(provider)
            if self._reply_artifact_owner(client_id)!=owner or self._shared_client() is not provider:raise DraftError(REPLY_UNAVAILABLE)
            self._require_reply_source_policy(source)
            draft=self._drafts.prepare(owner=owner,recipient=request.recipient,recipient_title=title,
                caption=request.caption,kind=request.kind,reply_source=source,
                local_admission_guard=lambda:self._reply_budget_check(provider),**kwargs)
            preview=self._reply_artifact_preview(draft,owner);self._remember_draft_title(draft)
            return PrepareReplyArtifactSendResponse(status='prepared',reply=preview,detail=REPLY_PREPARED)
        except (DraftError,TDLibError,VoiceError,ValueError,CompatibilityError,AttributeError,TimeoutError,OSError):
            return PrepareReplyArtifactSendResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def _get_reply_artifact_draft(self,request: GetReplyArtifactDraftRequest,*,client_id: str):
        try:
            owner=self._reply_artifact_owner(client_id)
            draft=self._drafts.peek(request.draft_id,owner=owner)
            self._reply_budget_check(self._shared_client())
            return GetReplyArtifactDraftResponse(status='pending',reply=self._reply_artifact_preview(draft,owner),detail=REPLY_PENDING)
        except (DraftError,TDLibError,ValueError,CompatibilityError,TimeoutError):
            return GetReplyArtifactDraftResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def _revise_reply_artifact_draft(self,request,*,client_id: str):
        try:
            owner=self._reply_artifact_owner(client_id);old=self._drafts.peek(request.draft_id,owner=owner)
            self._reply_artifact_preview(old,owner);provider=self._shared_client()
            if isinstance(request,RefreshReplyArtifactDraftRequest):
                chat=provider.resolve_target(old.recipient)
                title=sanitize_telegram_text(chat.get('title'),max_length=255) or str(old.recipient)
                source=provider.read_reply_source(*old.reply_source.anchor)
            else:title=old.recipient_title;source=old.reply_source
            self._reply_budget_check(provider)
            if self._reply_artifact_owner(client_id)!=owner or self._shared_client() is not provider:raise DraftError(REPLY_UNAVAILABLE)
            self._require_reply_source_policy(source)
            revised=self._drafts.revise(request.draft_id,owner=owner,recipient_title=title,reply=True,
                reply_source=source,caption=request.caption if isinstance(request,UpdateReplyArtifactDraftRequest) else None,
                local_admission_guard=lambda:self._reply_budget_check(provider))
            self._remember_draft_title(revised)
            return ReviseReplyArtifactDraftResponse(status='revised',previous_draft_id=request.draft_id,
                reply=self._reply_artifact_preview(revised,owner),detail=REPLY_REVISED)
        except (DraftError,TDLibError,ValueError,CompatibilityError,AttributeError,TimeoutError):
            return ReviseReplyArtifactDraftResponse(status='unavailable',detail=REPLY_UNAVAILABLE)

    def _update_reply_artifact_draft(self,request,*,client_id: str):
        return self._revise_reply_artifact_draft(request,client_id=client_id)

    def _refresh_reply_artifact_draft(self,request,*,client_id: str):
        return self._revise_reply_artifact_draft(request,client_id=client_id)

    def _reply_owner(self, client_id: str) -> DraftOwner:
        owner=self._draft_owner(client_id)
        if "reply_text_send" not in self._policy.enabled_capabilities:
            raise CompatibilityError("capability_disabled")
        return owner

    def _reply_preview(self, draft, owner: DraftOwner) -> ReplyDraftPreview:
        if draft.reply_source is None or draft.kind != "text":
            raise DraftError("reply draft is unavailable")
        self._require_reply_source_policy(draft.reply_source)
        return ReplyDraftPreview.create(self._plain_draft_preview(draft,owner),draft.reply_source)

    def _remember_draft_title(self, draft):
        with self._state_lock:
            known=self._drafts.known_ids()
            for old_id in list(self._draft_recipient_titles):
                if old_id not in known:self._draft_recipient_titles.pop(old_id,None)
            self._draft_recipient_titles[draft.draft_id]=draft.recipient_title

    def _prepare_reply_text_send(self, request: PrepareReplyTextSendRequest, *, client_id: str) -> PrepareReplyTextSendResponse:
        try:
            owner=self._reply_owner(client_id)
            if request.reply_to.chat_id!=request.recipient:
                raise DraftError("reply draft is unavailable")
            provider=self._shared_client()
            chat=provider.resolve_target(request.recipient)
            title=sanitize_telegram_text(chat.get("title"),max_length=255) or str(request.recipient)
            source=provider.read_reply_source(request.reply_to.chat_id,request.reply_to.message_id)
            self._require_reply_source_policy(source)
            if self._reply_owner(client_id)!=owner or self._shared_client() is not provider:
                raise DraftError("reply draft is unavailable")
            draft=self._drafts.prepare_text(owner=owner,recipient=request.recipient,text=request.text,
                                           recipient_title=title,reply_source=source)
            preview=self._reply_preview(draft,owner)
            self._remember_draft_title(draft)
            return PrepareReplyTextSendResponse(status="prepared",reply=preview,detail=REPLY_PREPARED)
        except (DraftError,TDLibError,ValueError,CompatibilityError,AttributeError,TimeoutError):
            return PrepareReplyTextSendResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def _get_reply_draft(self, request: GetReplyDraftRequest, *, client_id: str) -> GetReplyDraftResponse:
        try:
            owner=self._reply_owner(client_id)
            draft=self._drafts.peek(request.draft_id,owner=owner)
            return GetReplyDraftResponse(status="pending",reply=self._reply_preview(draft,owner),detail=REPLY_PENDING)
        except (DraftError,TDLibError,ValueError,CompatibilityError):
            return GetReplyDraftResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def _revise_reply_draft(self, request: UpdateReplyDraftRequest | RefreshReplyDraftRequest, *, client_id: str) -> ReviseReplyDraftResponse:
        try:
            owner=self._reply_owner(client_id)
            old=self._drafts.peek(request.draft_id,owner=owner)
            self._reply_preview(old,owner)
            provider=self._shared_client()
            if isinstance(request,RefreshReplyDraftRequest):
                chat=provider.resolve_target(old.recipient)
                title=sanitize_telegram_text(chat.get("title"),max_length=255) or str(old.recipient)
                source=provider.read_reply_source(*old.reply_source.anchor)
            else:
                title=old.recipient_title
                source=old.reply_source
            if self._reply_owner(client_id)!=owner or self._shared_client() is not provider:
                raise DraftError("reply draft is unavailable")
            self._require_reply_source_policy(source)
            revised=self._drafts.revise(request.draft_id,owner=owner,recipient_title=title,reply=True,
                reply_source=source,text=request.text if isinstance(request,UpdateReplyDraftRequest) else None)
            self._remember_draft_title(revised)
            return ReviseReplyDraftResponse(status="revised",previous_draft_id=request.draft_id,
                reply=self._reply_preview(revised,owner),detail=REPLY_REVISED)
        except (DraftError,TDLibError,ValueError,CompatibilityError,AttributeError,TimeoutError):
            return ReviseReplyDraftResponse(status="unavailable",detail=REPLY_UNAVAILABLE)

    def _prepare_text_send(self, request: PrepareTextSendRequest, *, client_id: str) -> PrepareTextSendResponse:
        try:
            client = self._shared_client()
            client.ensure_ready()
            owner = self._draft_owner(client_id)
            chat = client.resolve_target(request.recipient)
            title = sanitize_telegram_text(chat.get("title"), max_length=255) or str(request.recipient)
            draft = self._drafts.prepare_text(owner=owner, recipient=request.recipient, text=request.text, recipient_title=title)
            with self._state_lock:
                known = self._drafts.known_ids()
                for old_id in list(self._draft_recipient_titles):
                    if old_id not in known:
                        self._draft_recipient_titles.pop(old_id, None)
                self._draft_recipient_titles[draft.draft_id] = title
            return PrepareTextSendResponse(
                status="prepared", draft_id=draft.draft_id, recipient=draft.recipient,
                recipient_title=title, text=draft.text, sha256=draft.sha256,
                expires_at=datetime.fromtimestamp(draft.expires_at, tz=timezone.utc),
                detail="immutable text draft prepared; explicit approval is required before sending",
            )
        except AuthorizationBlocked:
            return PrepareTextSendResponse(status="blocked", detail="TDLib authorization is not ready")
        except (DraftError, TDLibError, ValueError, CompatibilityError):
            return PrepareTextSendResponse(status="error", detail="text draft could not be prepared safely")

    def _send_prepared_text(self, request: SendPreparedTextRequest, *, client_id: str, deadline: float | None = None) -> SendPreparedTextResponse:
        result = self._send_prepared_artifact(
            SendPreparedArtifactRequest(draft_id=request.draft_id, approved=request.approved), client_id=client_id, text_only=True, deadline=deadline
        )
        return SendPreparedTextResponse.model_validate(result.model_dump())

    def _send_prepared_artifact(self, request: SendPreparedArtifactRequest, *, client_id: str, text_only: bool = False, deadline: float | None = None) -> SendPreparedArtifactResponse:
        if not text_only and self._drafts.is_owned_artifact_reply(request.draft_id,client_id=client_id):
            provider=self._shared_client()
            # Capture once: nested provider budgets must not renew time consumed
            # by account reads, owner confirmation or local staging.
            bounded_deadline=min(deadline if deadline is not None else float("inf"),time.monotonic()+330)
            with provider.request_budget(bounded_deadline):
                return self._send_prepared_artifact_impl(request,client_id=client_id,text_only=text_only,deadline=bounded_deadline)
        return self._send_prepared_artifact_impl(request,client_id=client_id,text_only=text_only,deadline=deadline)

    def _send_prepared_artifact_impl(self, request: SendPreparedArtifactRequest, *, client_id: str, text_only: bool = False, deadline: float | None = None) -> SendPreparedArtifactResponse:
        if request.approved is not True:
            return SendPreparedArtifactResponse(status="not_approved", draft_id=request.draft_id,
                                                detail="explicit approval was not given")
        try:
            owner = self._draft_owner(client_id)
            if (self._drafts.kind(request.draft_id, owner=owner) == "text") != text_only:
                return SendPreparedArtifactResponse(status="error", draft_id=request.draft_id,
                                                    detail="draft kind does not match send tool")
            # Receipt replay remains bound to the reply capability as well.
            if self._drafts.is_reply(request.draft_id,owner=owner):
                (self._reply_owner if text_only else self._reply_artifact_owner)(client_id)
                provider=self._shared_client()
                capabilities=self._drafts.source_required_capabilities(request.draft_id,owner=owner,
                    provider=provider,provider_epoch=provider.send_observation_epoch)
                if any(capability not in self._policy.enabled_capabilities for capability in capabilities):
                    raise CompatibilityError('capability_disabled')
            previous = self._drafts.receipt(request.draft_id, owner=owner)
            if previous is not None:
                return SendPreparedArtifactResponse(
                    status=previous.status, draft_id=previous.draft_id,
                    message_id=previous.message_id,
                    detail="previous provider attempt receipt; no new send was attempted",
                )
            with self._state_lock:
                expected_title = self._draft_recipient_titles.get(request.draft_id)
            if expected_title is None:
                return SendPreparedArtifactResponse(status="expired", draft_id=request.draft_id,
                                                    detail="draft is unavailable after expiry or restart")
            # The recipient is resolved again immediately before the single provider attempt.
            entry = self._drafts.peek(request.draft_id, owner=owner)
            if (entry.kind == "text") != text_only:
                return SendPreparedArtifactResponse(status="error", draft_id=request.draft_id,
                                                    detail="draft kind does not match send tool")
            chat = self._shared_client().resolve_target(entry.recipient)
            current_title = sanitize_telegram_text(chat.get("title"), max_length=255) or str(entry.recipient)
            if current_title != expected_title:
                return SendPreparedArtifactResponse(status="recipient_changed", draft_id=request.draft_id,
                                                    recipient=entry.recipient, detail="recipient title changed; prepare a new draft")
            provider_before_dialog=self._shared_client()
            epoch_before_dialog=provider_before_dialog.send_observation_epoch
            reply_preview=((self._reply_preview if text_only else self._reply_artifact_preview)(entry,owner).model_dump(mode="json") if entry.reply_source is not None else None)
            reply_confirmation={("reply_preview" if text_only else "reply_artifact_preview"):reply_preview} if reply_preview is not None else {}
            if not self._approval_prompt(
                recipient_title=expected_title, recipient=entry.recipient,
                display_name=entry.display_name, size_bytes=entry.size_bytes,
                sha256=entry.sha256, caption=entry.caption, kind=entry.kind, text=entry.text,
                duration_seconds=entry.duration_seconds, source_sha256=entry.source_sha256,
                source_display_name=entry.source_display_name, **reply_confirmation,
            ):
                return SendPreparedArtifactResponse(status="not_approved", draft_id=request.draft_id,
                                                    recipient=entry.recipient,
                                                    detail="local owner confirmation was declined or unavailable")
            # The local dialog may remain open while cancellation/account/policy changes.
            if self._draft_owner(client_id) != owner:
                raise DraftError("draft is unavailable")
            provider = self._shared_client()
            if entry.reply_source is not None:
                (self._reply_owner if text_only else self._reply_artifact_owner)(client_id)
                self._require_reply_source_policy(entry.reply_source)
                if provider is not provider_before_dialog or provider.send_observation_epoch is not epoch_before_dialog:
                    raise DraftError("draft is unavailable")
            claim = self._drafts.claim(request.draft_id, owner=owner, approved=True, provider=provider,
                                       provider_epoch=provider.send_observation_epoch)
            if claim.draft.kind == "text":
                failure_detail = "TDLib rejected the send"
                try:
                    if claim.draft.reply_source is not None:
                        def reply_guard():
                            require_current(self._policy)
                            return ("send" in self._policy.enabled_capabilities and "reply_text_send" in self._policy.enabled_capabilities
                                    and all(capability in self._policy.enabled_capabilities for capability in claim.draft.reply_source.required_capabilities)
                                    and self._shared_client() is provider and provider.send_observation_epoch is epoch_before_dialog)
                        reply_sender=getattr(provider,"send_reply_text_message",None)
                        if reply_sender is None:
                            raise MessageSendNotAttempted("reply provider is unavailable")
                        with provider.request_budget(min(deadline if deadline is not None else float("inf"),time.monotonic()+330)):
                            message_id=reply_sender(claim.draft.recipient,claim.draft.text,
                                reply_source=claim.draft.reply_source,expected_account_id=owner.account_id,
                                expected_recipient_title=expected_title,attempt_id=request.draft_id,pre_send_guard=reply_guard)
                    else:
                        message_id = provider.send_text_message(claim.draft.recipient, claim.draft.text, attempt_id=request.draft_id)
                    receipt = self._drafts.finish(request.draft_id, owner=owner, status="sent", message_id=message_id)
                except MessageSendNotAttempted:
                    failure_detail = "local preparation failed; no provider send was attempted"
                    receipt = self._drafts.finish(request.draft_id, owner=owner, status="failed", evidence="local_failed")
                except MessageSendFailed as error:
                    failure_detail = error.public_detail()
                    receipt = self._drafts.finish(request.draft_id, owner=owner, status="failed")
                except Exception:
                    receipt = self._drafts.finish(request.draft_id, owner=owner, status="outcome_unknown")
                return SendPreparedArtifactResponse(
                    status=receipt.status, draft_id=receipt.draft_id, recipient=claim.draft.recipient,
                    message_id=receipt.message_id,
                    detail=("TDLib confirmed delivery" if receipt.status == "sent" else
                            failure_detail if receipt.status == "failed" else
                            "delivery is unconfirmed; do not retry automatically"),
                )
            failure_detail = "TDLib rejected the send"
            try:
                staged_path = stage_approved_document(claim)
            except (OSError,ValueError,ConfigurationError) as error:
                if isinstance(error,ConfigurationError) and claim.draft.reply_source is None:
                    raise  # Preserve the ordinary-send exception boundary.
                self._drafts.finish(request.draft_id, owner=owner, status="failed", evidence="local_failed")
                return SendPreparedArtifactResponse(status="failed", draft_id=request.draft_id,
                                                    recipient=claim.draft.recipient,
                                                    detail="local staged file could not be prepared; no provider send was attempted")
            try:
                if claim.draft.reply_source is not None:
                    def artifact_reply_guard():
                        require_current(self._policy)
                        return ("send" in self._policy.enabled_capabilities and "reply_artifact_send" in self._policy.enabled_capabilities
                            and all(capability in self._policy.enabled_capabilities for capability in claim.draft.reply_source.required_capabilities)
                            and self._shared_client() is provider and provider.send_observation_epoch is epoch_before_dialog)
                    sender=getattr(provider,"send_reply_artifact_message",None)
                    if not callable(sender):raise MessageSendNotAttempted("reply provider is unavailable")
                    with provider.request_budget(min(deadline if deadline is not None else float("inf"),time.monotonic()+330)):
                        message_id=sender(claim.draft.recipient,staged_path,claim.draft.caption,kind=claim.draft.kind,
                            duration_seconds=claim.draft.duration_seconds,waveform_base64=claim.draft.waveform_base64,
                            reply_source=claim.draft.reply_source,expected_account_id=owner.account_id,
                            expected_recipient_title=expected_title,attempt_id=request.draft_id,pre_send_guard=artifact_reply_guard)
                elif claim.draft.kind == "photo":
                    message_id = self._shared_client().send_photo_message(
                        claim.draft.recipient, staged_path, claim.draft.caption, attempt_id=request.draft_id,
                    )
                elif claim.draft.kind == "voice_note":
                    message_id = self._shared_client().send_voice_note_message(
                        claim.draft.recipient, staged_path, claim.draft.caption,
                        claim.draft.duration_seconds, claim.draft.waveform_base64, attempt_id=request.draft_id,
                    )
                else:
                    message_id = self._shared_client().send_document_message(
                        claim.draft.recipient, staged_path, claim.draft.caption, attempt_id=request.draft_id,
                    )
                receipt = self._drafts.finish(request.draft_id, owner=owner, status="sent", message_id=message_id)
            except MessageSendNotAttempted:
                failure_detail = "local preparation failed; no provider send was attempted"
                receipt = self._drafts.finish(request.draft_id, owner=owner, status="failed", evidence="local_failed")
            except MessageSendFailed as error:
                failure_detail = error.public_detail()
                receipt = self._drafts.finish(request.draft_id, owner=owner, status="failed")
            except Exception:
                receipt = self._drafts.finish(request.draft_id, owner=owner, status="outcome_unknown")
            if receipt.status in {"sent", "failed"}:
                try:
                    retire_staged_document(staged_path)
                except (OSError, ValueError):
                    pass  # The receipt still owns the provider outcome; staging is TTL-bounded.
            return SendPreparedArtifactResponse(
                status=receipt.status, draft_id=receipt.draft_id,
                recipient=claim.draft.recipient, message_id=receipt.message_id,
                detail=("TDLib confirmed delivery" if receipt.status == "sent" else
                        failure_detail if receipt.status == "failed" else
                        "delivery is unconfirmed; do not retry automatically"),
            )
        except (DraftError, TDLibError, ValueError, CompatibilityError, TimeoutError):
            return SendPreparedArtifactResponse(status="expired", draft_id=request.draft_id,
                                                detail="draft is unavailable or no longer valid")

    @staticmethod
    def _error_code(error: BaseException) -> str:
        if isinstance(error, CompatibilityError):
            return error.code
        if isinstance(error, TimeoutError):
            return "expired"
        if isinstance(error, (BrokerProtocolError, ValidationError)):
            return "invalid_request"
        return "unavailable"

    def _handle_connection(self, connection: socket.socket) -> None:
        request: dict[str, Any] | None = None
        with self._state_lock:
            self._active_handlers += 1
            self._peak_overlap = max(self._peak_overlap, self._active_handlers)
        try:
            with connection:
                connection.settimeout(FRAME_READ_TIMEOUT_SECONDS)
                if (
                    self._verify_peer_uid
                    and self._peer_uid_loader(connection) != os.getuid()
                ):
                    return
                request = receive_request(connection)
                try:
                    require_current(self._policy)
                    if request["operation"] != "handshake":
                        raise CompatibilityError("handshake_required")
                    if request["broker_generation"] is not None:
                        raise CompatibilityError("handshake_invalid")
                    remaining = request["deadline"] - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("request deadline expired")
                    if remaining > MAX_DEADLINE_SECONDS:
                        raise BrokerProtocolError("request deadline exceeds the allowed bound")
                    require_compatible(request["payload"], self._contract)
                    descriptor = {**self._contract, "broker_generation": self._generation}
                    send_frame(connection, {"version": PROTOCOL_VERSION, "request_id": request["request_id"],
                                            "ok": True, "result": descriptor}, max_bytes=MAX_RESPONSE_BYTES)
                    # Exactly one operation may follow on this authenticated connection.
                    hello = request
                    if not connection.recv(1, socket.MSG_PEEK):
                        return  # A diagnostic-only handshake ends with a clean EOF.
                    request = receive_request(connection)
                    if (request["client_id"] != hello["client_id"] or request["deadline"] != hello["deadline"]
                            or request["request_id"] == hello["request_id"]):
                        raise CompatibilityError("handshake_invalid")
                    result = self._dispatch(request)
                    response = {
                        "version": PROTOCOL_VERSION,
                        "request_id": request["request_id"],
                        "ok": True,
                        "result": result,
                    }
                    with self._state_lock:
                        self._completed += 1
                except Exception as error:
                    response = {
                        "version": PROTOCOL_VERSION,
                        "request_id": request["request_id"],
                        "ok": False,
                        "error": self._error_code(error),
                    }
                    with self._state_lock:
                        self._errors += 1
                send_frame(connection, response, max_bytes=MAX_RESPONSE_BYTES)
        except (OSError, BrokerProtocolError):
            with self._state_lock:
                self._errors += 1
        finally:
            with self._state_lock:
                self._connections.discard(connection)
                self._active_handlers -= 1
                self._pending -= 1
            self._pending_slots.release()

    def _reject_overloaded(self, connection: socket.socket) -> None:
        """Return a correlated terminal response without entering the worker queue."""
        try:
            with connection:
                connection.settimeout(FRAME_READ_TIMEOUT_SECONDS)
                if (
                    self._verify_peer_uid
                    and self._peer_uid_loader(connection) != os.getuid()
                ):
                    return
                request = receive_request(connection)
                send_frame(
                    connection,
                    {
                        "version": PROTOCOL_VERSION,
                        "request_id": request["request_id"],
                        "ok": False,
                        "error": "overloaded",
                    },
                    max_bytes=MAX_RESPONSE_BYTES,
                )
        except (OSError, BrokerProtocolError):
            pass
        finally:
            with self._state_lock:
                self._connections.discard(connection)
                self._errors += 1

    def serve_forever(self) -> None:
        ensure_private_directory(self._socket_path.parent)
        lock_descriptor = self._acquire_owner_lock()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_identity: tuple[int, int] | None = None
        try:
            self._prepare_socket_path()
            listener.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o600)
            bound = self._socket_path.lstat()
            bound_identity = (bound.st_dev, bound.st_ino)
            listener.listen()
            listener.settimeout(0.1)
            self._listener = listener
            self._ready.set()
            while not self._stop.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                with self._state_lock:
                    self._connections.add(connection)
                if self._stop.is_set():
                    connection.close()
                    with self._state_lock:
                        self._connections.discard(connection)
                    break
                if not self._pending_slots.acquire(blocking=False):
                    self._reject_overloaded(connection)
                    continue
                with self._state_lock:
                    self._pending += 1
                self._executor.submit(self._handle_connection, connection)
        finally:
            self._ready.set()
            listener.close()
            self._executor.shutdown(wait=True, cancel_futures=False)
            with self._state_lock:
                contexts = list(self._contexts.values())
                self._contexts.clear()
                client = self._client
                self._client = None
            for service in contexts:
                service.close()
            if client is not None:
                client.close()
            try:
                metadata = self._socket_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    bound_identity is not None
                    and stat.S_ISSOCK(metadata.st_mode)
                    and not self._socket_path.is_symlink()
                    and metadata.st_uid == os.getuid()
                    and (metadata.st_dev, metadata.st_ino) == bound_identity
                ):
                    self._socket_path.unlink()
            os.close(lock_descriptor)

    def shutdown(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            listener.close()
        with self._state_lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
