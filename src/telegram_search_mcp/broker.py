"""Single-owner Unix-socket broker for the local TDLib session."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import socket
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .broker_protocol import (
    FRAME_READ_TIMEOUT_SECONDS,
    MAX_DEADLINE_SECONDS,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    BrokerProtocolError,
    receive_request,
    send_frame,
)
from .config import ensure_private_directory
from .schemas import DiscoverTargetsRequest, ResolveTargetRequest, SearchRequest
from .search_service import ReadOnlyTelegramClient, SearchService
from .tdjson import AuthorizationBlocked, TDLibClient, TDLibError


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
    ) -> None:
        if min(max_pending, max_clients, max_workers) < 1:
            raise ValueError("broker bounds must be positive")
        if context_ttl_seconds <= 0:
            raise ValueError("context_ttl_seconds must be positive")
        self._socket_path = socket_path
        self._lock_path = lock_path or socket_path.with_name("broker.lock")
        self._client_factory = client_factory
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
                self._contexts.pop(client_id).service.close()

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
                        context.service.close()

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
        self._expire_contexts()
        operation = request["operation"]
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
                return service.search(model).model_dump(mode="json")
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
                else:
                    context = self._contexts.pop(client_id, None)
                    if context is not None:
                        context.service.close()
            return {"released": True}
        raise BrokerProtocolError("operation is not allowed")

    @staticmethod
    def _error_code(error: BaseException) -> str:
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
                contexts = [context.service for context in self._contexts.values()]
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
