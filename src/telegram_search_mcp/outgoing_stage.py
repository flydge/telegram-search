"""Bounded private file names for a single approved TDLib document send."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

from .config import APP_SUPPORT_ROOT, ensure_private_directory
from .outgoing_drafts import ClaimedDraft

STAGING_ROOT = APP_SUPPORT_ROOT / "outgoing-staging"
MAX_STAGING_BYTES = 512 * 1024 * 1024
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024
_DRAFT_NAME = re.compile(r"draft_[0-9a-f]{32}\Z")
_LOCK = threading.Lock()


def stage_approved_document(claim: ClaimedDraft, *, root: Path = STAGING_ROOT) -> Path:
    """Copy verified bytes to the exact reviewed file name before provider use."""
    draft = claim.draft
    name = draft.display_name
    if (not _DRAFT_NAME.fullmatch(draft.draft_id) or not name or name in {".", ".."}
        or "/" in name or "\\" in name or "\x00" in name
        or len(name.encode("utf-8")) > 240):
        raise ValueError("unsafe outgoing file name")
    with _LOCK:
        ensure_private_directory(root)
        quarantine = root.with_name(root.name + "-quarantine")
        ensure_private_directory(quarantine)
        occupied = 0
        now = time.time()
        for item in quarantine.iterdir():
            info = item.lstat()
            if (not item.name.startswith("quarantine_") or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_nlink != 1):
                raise ValueError("unsafe outgoing quarantine entry")
            if info.st_mtime + 12 * 3600 <= now:
                item.unlink()
            else:
                occupied += info.st_size
        for entry in root.iterdir():
            info = entry.lstat()
            if not _DRAFT_NAME.fullmatch(entry.name) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("unsafe outgoing staging entry")
            files = list(entry.iterdir())
            if not files and info.st_mtime + 15 * 60 <= now:
                entry.rmdir()
                continue
            if len(files) != 1:
                raise ValueError("unsafe outgoing staging contents")
            file_info = files[0].lstat()
            if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.geteuid() or file_info.st_nlink != 1:
                raise ValueError("unsafe outgoing staging file")
            if info.st_mtime + 12 * 3600 <= now:
                retire_staged_document(files[0], root=root)
                if file_info.st_size > LARGE_FILE_THRESHOLD:
                    occupied += file_info.st_size
            else:
                occupied += file_info.st_size
        if occupied + draft.size_bytes > MAX_STAGING_BYTES:
            raise ValueError("outgoing staging capacity exhausted")
        directory = root / draft.draft_id
        directory.mkdir(mode=0o700)
        target = directory / name
        digest = hashlib.sha256()
        copied = 0
        source_fd = -1
        target_fd = -1
        try:
            source_fd = os.open(claim.path, os.O_RDONLY | os.O_NOFOLLOW)
            source_info = os.fstat(source_fd)
            if (not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != os.geteuid()
                or source_info.st_size != draft.size_bytes):
                raise ValueError("source changed before staging")
            target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            while chunk := os.read(source_fd, 1024 * 1024):
                copied += len(chunk)
                if copied > draft.size_bytes:
                    raise ValueError("source grew while staging")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(target_fd, view):]
            if copied != draft.size_bytes or digest.hexdigest() != draft.sha256:
                raise ValueError("source hash changed before staging")
            os.fsync(target_fd)
            return target
        except BaseException:
            if target_fd >= 0:
                os.close(target_fd)
                target_fd = -1
            target.unlink(missing_ok=True)
            directory.rmdir()
            raise
        finally:
            if target_fd >= 0:
                os.close(target_fd)
            if source_fd >= 0:
                os.close(source_fd)


def retire_staged_document(path: Path, *, root: Path = STAGING_ROOT) -> None:
    """Retire a finished copy, quarantining large files outside the worktree."""
    if path.parent.parent != root or not _DRAFT_NAME.fullmatch(path.parent.name):
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ValueError("unsafe outgoing staging file")
    if info.st_size > LARGE_FILE_THRESHOLD:
        quarantine = root.with_name(root.name + "-quarantine")
        ensure_private_directory(quarantine)
        target = quarantine / f"quarantine_{uuid.uuid4().hex}"
        os.replace(path, target)
        os.utime(target, (info.st_mtime, info.st_mtime))
    else:
        path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass
