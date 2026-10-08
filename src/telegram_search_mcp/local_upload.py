"""Bounded, ordered local upload into the existing private artifact store."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .artifact_store import ArtifactStore, StoredArtifact
from .config import APP_SUPPORT_ROOT, ensure_private_directory

UPLOAD_ROOT = APP_SUPPORT_ROOT / "incoming-uploads"
UPLOAD_TTL_SECONDS = 15 * 60
MAX_CHUNK_BYTES = 512 * 1024
MAX_ACTIVE_UPLOADS = 8
MAX_QUARANTINE_BYTES = 256 * 1024 * 1024
QUARANTINE_TTL_SECONDS = 12 * 60 * 60
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_UPLOAD_ID = re.compile(r"upload_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LIMITS = {"document": 64 * 1024 * 1024, "photo": 10_000_000, "voice_note": 64 * 1024 * 1024}


class UploadError(RuntimeError):
    """The upload failed a size, integrity, order, or lifecycle check."""


@dataclass(frozen=True)
class CompletedUpload:
    artifact: StoredArtifact
    file_name: str
    kind: str


@dataclass
class _Upload:
    path: Path
    file_name: str
    kind: str
    declared_size: int
    declared_sha256: str
    expires_at: float
    fd: int
    digest: object
    received: int = 0
    next_index: int = 0


class LocalUploadRegistry:
    def __init__(self, store: ArtifactStore, *, root: Path = UPLOAD_ROOT,
                 clock: Callable[[], float] = time.time, capacity: int = MAX_ACTIVE_UPLOADS,
                 quarantine_limit_bytes: int = MAX_QUARANTINE_BYTES) -> None:
        self._store = store
        self._root = Path(root)
        self._clock = clock
        self._capacity = capacity
        self._quarantine_limit_bytes = quarantine_limit_bytes
        self._entries: dict[str, _Upload] = {}
        self._lock = threading.RLock()
        if capacity < 1 or quarantine_limit_bytes < 1:
            raise ValueError("upload capacity must be positive")

    def _ensure_root(self) -> None:
        ensure_private_directory(self._root)
        for path in self._root.iterdir():
            if path.name in self._entries:
                continue
            if _UPLOAD_ID.fullmatch(path.name) is None:
                raise UploadError("unknown upload entry")
            self._discard_path(path)
        self._prune_quarantine()

    def _prune_quarantine(self) -> None:
        quarantine = self._root.with_name(self._root.name + "-quarantine")
        ensure_private_directory(quarantine)
        retained: list[tuple[float, Path, int]] = []
        occupied = 0
        for path in quarantine.iterdir():
            info = path.lstat()
            if (not re.fullmatch(r"quarantine_[0-9a-f]{32}", path.name)
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1):
                raise UploadError("unsafe upload quarantine entry")
            if info.st_mtime + QUARANTINE_TTL_SECONDS <= self._clock():
                path.unlink()
            else:
                occupied += info.st_size
                retained.append((info.st_mtime, path, info.st_size))
        for _, path, size in sorted(retained):
            if occupied <= self._quarantine_limit_bytes:
                break
            path.unlink()
            occupied -= size

    def _discard_path(self, path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise UploadError("unsafe upload entry")
        if info.st_size > 10 * 1024 * 1024:
            quarantine = self._root.with_name(self._root.name + "-quarantine")
            ensure_private_directory(quarantine)
            os.replace(path, quarantine / f"quarantine_{uuid.uuid4().hex}")
            self._prune_quarantine()
        else:
            path.unlink()

    def _retire(self, entry: _Upload) -> None:
        os.close(entry.fd)
        self._discard_path(entry.path)

    def _prune(self) -> None:
        now = self._clock()
        for upload_id, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                self._entries.pop(upload_id)
                self._retire(entry)

    def _get(self, upload_id: str) -> _Upload:
        self._prune()
        if not isinstance(upload_id, str) or _UPLOAD_ID.fullmatch(upload_id) is None:
            raise UploadError("upload ID is invalid")
        try:
            return self._entries[upload_id]
        except KeyError:
            raise UploadError("upload is unavailable") from None

    def begin(self, *, file_name: str, kind: str, size_bytes: int, sha256: str) -> str:
        if not isinstance(file_name, str) or _NAME.fullmatch(file_name) is None or file_name in {".", ".."}:
            raise UploadError("file name is invalid")
        if kind not in _LIMITS or type(size_bytes) is not int or not 0 < size_bytes <= _LIMITS[kind]:
            raise UploadError("upload kind or size is invalid")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise UploadError("declared hash is invalid")
        with self._lock:
            self._prune()
            if len(self._entries) >= self._capacity:
                raise UploadError("upload capacity is exhausted")
            self._ensure_root()
            upload_id = f"upload_{uuid.uuid4().hex}"
            path = self._root / upload_id
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            self._entries[upload_id] = _Upload(
                path=path, file_name=file_name, kind=kind, declared_size=size_bytes,
                declared_sha256=sha256, expires_at=self._clock() + UPLOAD_TTL_SECONDS,
                fd=fd, digest=hashlib.sha256(),
            )
            return upload_id

    def append(self, upload_id: str, *, index: int, content_base64: str, sha256: str) -> tuple[int, int]:
        if type(index) is not int or index < 0 or not isinstance(content_base64, str) or len(content_base64) > 700_000:
            raise UploadError("chunk input is invalid")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise UploadError("chunk hash is invalid")
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise UploadError("chunk base64 is invalid") from error
        if not 0 < len(data) <= MAX_CHUNK_BYTES or hashlib.sha256(data).hexdigest() != sha256:
            raise UploadError("chunk size or hash is invalid")
        with self._lock:
            entry = self._get(upload_id)
            if index != entry.next_index or entry.received + len(data) > entry.declared_size:
                raise UploadError("chunk order or total size is invalid")
            view = memoryview(data)
            while view:
                view = view[os.write(entry.fd, view):]
            entry.digest.update(data)
            entry.received += len(data)
            entry.next_index += 1
            return entry.next_index, entry.received

    def finish(self, upload_id: str) -> StoredArtifact:
        return self.finish_with_metadata(upload_id).artifact

    def finish_with_metadata(self, upload_id: str) -> CompletedUpload:
        """Consume one completed entry and return its coherent immutable metadata."""
        with self._lock:
            entry = self._get(upload_id)
            if entry.received != entry.declared_size:
                raise UploadError("upload is incomplete")
            self._entries.pop(upload_id)
            if entry.digest.hexdigest() != entry.declared_sha256:
                self._retire(entry)
                raise UploadError("upload hash does not match")
            os.fsync(entry.fd)
            os.close(entry.fd)
            try:
                stored = self._store.store(entry.path, kind="media" if entry.kind == "voice_note" else "document")
                if stored.sha256 != entry.declared_sha256 or stored.size_bytes != entry.declared_size:
                    raise UploadError("stored artifact does not match upload")
                return CompletedUpload(stored, entry.file_name, entry.kind)
            finally:
                self._discard_path(entry.path)
