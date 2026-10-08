"""Private, bounded copies of TDLib-downloaded attachments.

Only the broker should pass TDLib's selected local path to ``store``. Public MCP
handlers should accept and return generated artifact IDs, never source paths.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import APP_SUPPORT_ROOT

CACHE_DIRECTORY = APP_SUPPORT_ROOT / "artifacts"
DOCUMENT_LIMIT = 64 * 1024 * 1024
MEDIA_LIMIT = 256 * 1024 * 1024
GLOBAL_LIMIT = 1024 * 1024 * 1024
TTL_SECONDS = 12 * 60 * 60
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_ARTIFACT_NAME = re.compile(r"artifact_[0-9a-f]{32}_[0-9a-f]{64}_[0-9]+\Z")
_QUARANTINE_NAME = re.compile(r"quarantine_[0-9a-f]{32}\Z")


class ArtifactStoreError(RuntimeError):
    """The requested file cannot be safely copied or retained."""


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    path: Path
    sha256: str
    size_bytes: int
    expires_at: float


class ArtifactStore:
    def __init__(
        self,
        *,
        cache_dir: Path = CACHE_DIRECTORY,
        clock: Callable[[], float] = time.time,
        max_document_bytes: int = DOCUMENT_LIMIT,
        max_media_bytes: int = MEDIA_LIMIT,
        max_total_bytes: int = GLOBAL_LIMIT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.quarantine_dir = self.cache_dir.with_name(self.cache_dir.name + "-quarantine")
        self.clock = clock
        self.max_document_bytes = max_document_bytes
        self.max_media_bytes = max_media_bytes
        self.max_total_bytes = max_total_bytes
        if min(max_document_bytes, max_media_bytes, max_total_bytes) <= 0:
            raise ValueError("artifact limits must be positive")

    def store(self, source_path: Path, *, kind: str = "document") -> StoredArtifact:
        """Copy one regular owner-owned TDLib file; reject changing or oversized files."""
        if kind not in {"document", "image", "media"}:
            raise ValueError("kind must be document, image, or media")
        limit = self.max_media_bytes if kind == "media" else self.max_document_bytes
        source_fd = -1
        directory_fd = -1
        lock_fd = -1
        temporary_name: str | None = None
        try:
            source_fd = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
                raise ArtifactStoreError("source is not an owner-owned regular file")
            if before.st_size > limit or before.st_size > self.max_total_bytes:
                raise ArtifactStoreError("source exceeds artifact size limit")
            directory_fd, lock_fd = self._open_locked_cache()
            quarantine_occupied = self._prune(directory_fd)
            self._make_room(directory_fd, before.st_size, quarantine_occupied)
            token = uuid.uuid4().hex
            temporary_name = f"tmp_{token}"
            temp_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            digest = hashlib.sha256()
            copied = 0
            try:
                while chunk := os.read(source_fd, _CHUNK_SIZE):
                    copied += len(chunk)
                    if copied > before.st_size or copied > limit or copied > self.max_total_bytes:
                        raise ArtifactStoreError("source exceeds artifact size limit")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(temp_fd, view)
                        view = view[written:]
                after = os.fstat(source_fd)
                if (
                    copied != before.st_size
                    or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                    or after.st_ctime_ns != before.st_ctime_ns
                ):
                    raise ArtifactStoreError("source changed during copy")
                os.fsync(temp_fd)
                created_at = self.clock()
                os.utime(temp_fd, (created_at, created_at))
            finally:
                os.close(temp_fd)
            checksum = digest.hexdigest()
            artifact_id = f"artifact_{token}_{checksum}_{copied}"
            os.link(
                temporary_name,
                artifact_id,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=directory_fd)
            temporary_name = None
            return StoredArtifact(
                artifact_id=artifact_id,
                path=self.cache_dir / artifact_id,
                sha256=checksum,
                size_bytes=copied,
                expires_at=created_at + TTL_SECONDS,
            )
        except ArtifactStoreError:
            raise
        except (OSError, ValueError) as error:
            raise ArtifactStoreError("unable to safely store artifact") from error
        finally:
            if temporary_name is not None and directory_fd >= 0:
                try:
                    self._retire(directory_fd, temporary_name)
                except FileNotFoundError:
                    pass
            if lock_fd >= 0:
                os.close(lock_fd)
            if directory_fd >= 0:
                os.close(directory_fd)
            if source_fd >= 0:
                os.close(source_fd)

    def lookup(self, artifact_id: str) -> StoredArtifact | None:
        """Resolve only a generated ID, removing expired or damaged copies."""
        if not isinstance(artifact_id, str) or _ARTIFACT_NAME.fullmatch(artifact_id) is None:
            return None
        directory_fd = -1
        lock_fd = -1
        try:
            directory_fd, lock_fd = self._open_locked_cache()
            self._prune(directory_fd)
            try:
                fd = os.open(artifact_id, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                    raise ArtifactStoreError("artifact is not an owner-owned regular file")
                _, _, expected_hash, expected_size = artifact_id.split("_", 3)
                if info.st_size != int(expected_size):
                    self._retire(directory_fd, artifact_id)
                    return None
                digest = hashlib.sha256()
                while chunk := os.read(fd, _CHUNK_SIZE):
                    digest.update(chunk)
                if digest.hexdigest() != expected_hash:
                    self._retire(directory_fd, artifact_id)
                    return None
                return StoredArtifact(
                    artifact_id=artifact_id,
                    path=self.cache_dir / artifact_id,
                    sha256=expected_hash,
                    size_bytes=info.st_size,
                    expires_at=info.st_mtime + TTL_SECONDS,
                )
            finally:
                os.close(fd)
        except ArtifactStoreError:
            raise
        except OSError as error:
            raise ArtifactStoreError("unable to safely look up artifact") from error
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            if directory_fd >= 0:
                os.close(directory_fd)

    def _open_locked_cache(self) -> tuple[int, int]:
        if self.cache_dir.is_symlink():
            raise ArtifactStoreError("cache directory must not be a symlink")
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_fd = os.open(self.cache_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ArtifactStoreError("cache directory is not owner-owned")
            os.fchmod(directory_fd, 0o700)
            lock_fd = os.open(
                ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
            )
            try:
                lock_info = os.fstat(lock_fd)
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid() or lock_info.st_nlink != 1:
                    raise ArtifactStoreError("cache lock is unsafe")
                os.fchmod(lock_fd, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                return directory_fd, lock_fd
            except BaseException:
                os.close(lock_fd)
                raise
        except BaseException:
            os.close(directory_fd)
            raise

    def _prune(self, directory_fd: int) -> int:
        now = self.clock()
        for name in os.listdir(directory_fd):
            if name == ".lock":
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ArtifactStoreError("cache contains an unsafe entry")
            if name.startswith("tmp_"):
                self._retire(directory_fd, name)
                continue
            if _ARTIFACT_NAME.fullmatch(name) is None:
                raise ArtifactStoreError("cache contains an unknown entry")
            if info.st_mtime + TTL_SECONDS <= now:
                self._retire(directory_fd, name)
        return self._prune_quarantine(now)

    def _make_room(
        self, directory_fd: int, incoming_size: int, quarantine_occupied: int
    ) -> None:
        entries: list[tuple[float, str, int]] = []
        occupied = quarantine_occupied
        for name in os.listdir(directory_fd):
            if _ARTIFACT_NAME.fullmatch(name) is None:
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ArtifactStoreError("cache contains an unsafe entry")
            occupied += info.st_size
            entries.append((info.st_mtime, name, info.st_size))
        for _, name, size in sorted(entries):
            if occupied + incoming_size <= self.max_total_bytes:
                break
            if size <= LARGE_FILE_THRESHOLD:
                self._retire(directory_fd, name)
                occupied -= size
        if occupied + incoming_size > self.max_total_bytes:
            raise ArtifactStoreError("cache budget is exhausted")

    def _retire(self, directory_fd: int, name: str) -> None:
        """Remove small files; recoverably quarantine large files outside the cache."""
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ArtifactStoreError("cache contains an unsafe entry")
        if info.st_size <= LARGE_FILE_THRESHOLD:
            os.unlink(name, dir_fd=directory_fd)
            return
        quarantine_fd = self._open_quarantine()
        try:
            quarantine_name = f"quarantine_{uuid.uuid4().hex}"
            os.link(
                name,
                quarantine_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                os.unlink(quarantine_name, dir_fd=quarantine_fd)
                raise
            # Preserve the artifact creation time: the 12-hour retention clock
            # does not restart when a file leaves the active cache.
        finally:
            os.close(quarantine_fd)

    def _open_quarantine(self) -> int:
        if self.quarantine_dir.is_symlink():
            raise ArtifactStoreError("quarantine directory must not be a symlink")
        self.quarantine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        quarantine_fd = os.open(
            self.quarantine_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            info = os.fstat(quarantine_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ArtifactStoreError("quarantine directory is not owner-owned")
            os.fchmod(quarantine_fd, 0o700)
            return quarantine_fd
        except BaseException:
            os.close(quarantine_fd)
            raise

    def _prune_quarantine(self, now: float) -> int:
        """Keep recoverable bytes for 12h, then release their budget."""
        quarantine_fd = self._open_quarantine()
        occupied = 0
        try:
            for name in os.listdir(quarantine_fd):
                info = os.stat(name, dir_fd=quarantine_fd, follow_symlinks=False)
                if (
                    _QUARANTINE_NAME.fullmatch(name) is None
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                ):
                    raise ArtifactStoreError("quarantine contains an unsafe entry")
                if info.st_mtime + TTL_SECONDS <= now:
                    # The authorized retention window has elapsed. Metadata is
                    # checked above before permanently removing the file.
                    os.unlink(name, dir_fd=quarantine_fd)
                else:
                    occupied += info.st_size
            return occupied
        finally:
            os.close(quarantine_fd)
