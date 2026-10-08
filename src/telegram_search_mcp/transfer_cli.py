"""Trusted-local, bounded upload and selected cached-artifact download.

Only this local command accepts source paths. It does not start services and
never resumes or replays a dispatched upload operation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import stat
import sys
import time
import uuid
from collections.abc import Sequence

from pydantic import ValidationError

from .broker_client import BrokerClient, BrokerUnavailable
from .artifact_store import CACHE_DIRECTORY, MEDIA_LIMIT, TTL_SECONDS
from .config import BROKER_SOCKET_PATH, ConfigurationError, load_runtime_policy
from .contract import CompatibilityError, contract_descriptor, require_compatible, require_current
from .local_upload import MAX_CHUNK_BYTES, _LIMITS, _NAME
from .schemas import (
    AppendLocalUploadRequest, AppendLocalUploadResponse, BeginLocalUploadRequest,
    BeginLocalUploadResponse, FinishLocalUploadRequest, FinishLocalUploadResponse,
)

_UPLOAD_ID = re.compile(r"upload_[0-9a-f]{32}\Z")
_ARTIFACT_ID = re.compile(r"artifact_[0-9a-f]{32}_([0-9a-f]{64})_([0-9]+)\Z")
_DETAILS = {
    "invalid_arguments": "Use upload with an absolute source, supported kind and optional safe file name.",
    "invalid_source": "Source must be an owner-owned regular file with one link and no symlink components.",
    "invalid_name": "Supply a safe ASCII file basename of at most 128 characters.",
    "invalid_size": "Source is empty or exceeds the upload kind size limit.",
    "source_changed": "Source changed during verification; select a stable source and start from byte zero.",
    "upload_unverified": "Upload outcome is unverified; start a new upload from byte zero. No resume or automatic send.",
    "configuration_error": "Trusted runtime policy is unsafe or unavailable.",
    "interrupted": "Upload interrupted; start a new upload from byte zero. No resume or automatic send.",
    "internal_error": "Transfer failed internally; no success receipt was verified.",
    "invalid_artifact": "Select one canonical issued artifact ID within the cache size limit.",
    "invalid_destination": "Destination must be an absent leaf in an existing trusted absolute directory, outside the artifact cache and quarantine.",
    "artifact_unverified": "Selected cached artifact is unavailable, expired, unsafe or changed; no destination was published. Generated private partials are retained.",
    "download_authorization": "Artifacts capability or matching current broker policy is unavailable; no destination was published. Generated private partials are retained.",
    "download_failed": "Download failed before publication; no destination was published. Generated private partials are retained; start again from byte zero.",
    "download_interrupted": "Download interrupted before publication; no destination was published. Generated private partials are retained; start again from byte zero.",
    "download_uncertain": "Download completion is uncertain; inspect the selected destination and generated partials before starting again from byte zero.",
}


class TransferError(RuntimeError):
    """A fixed, sanitized transfer failure suitable for local command output."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(_DETAILS[code])


def _file_facts(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISDIR(info.st_mode):
        raise TransferError("invalid_source")
    return info.st_dev, info.st_ino


class _PinnedSource:
    """Hold root-anchored descriptors and compare the named chain at each gate."""
    def __init__(self, source: str):
        self._fds: list[int] = []
        self._directories: list[tuple[int, int]] = []
        self.fd = -1
        if (type(source) is not str or not source.startswith("/") or "\x00" in source
                or any(part in {".", ".."} for part in source.split("/"))):
            raise TransferError("invalid_source")
        self._parts = tuple(part for part in source.split("/") if part)
        if not self._parts or source.endswith("/"):
            raise TransferError("invalid_source")
        self.name = self._parts[-1]
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            parent = os.open("/", flags)
            self._fds.append(parent)
            self._directories.append(_directory_identity(os.fstat(parent)))
            for part in self._parts[:-1]:
                parent = os.open(part, flags, dir_fd=parent)
                self._fds.append(parent)
                self._directories.append(_directory_identity(os.fstat(parent)))
            self.fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                              | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
            self._fds.append(self.fd)
            self.info = os.fstat(self.fd)
            if (not stat.S_ISREG(self.info.st_mode) or self.info.st_uid != os.geteuid()
                    or self.info.st_nlink != 1):
                raise TransferError("invalid_source")
            self._facts = _file_facts(self.info)
        except (OSError, ValueError):
            self.close()
            raise TransferError("invalid_source") from None
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for descriptor in reversed(self._fds):
            os.close(descriptor)
        self._fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def recheck(self) -> None:
        fresh: list[int] = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            if _file_facts(os.fstat(self.fd)) != self._facts:
                raise TransferError("source_changed")
            parent = os.open("/", flags)
            fresh.append(parent)
            if _directory_identity(os.fstat(parent)) != self._directories[0]:
                raise TransferError("source_changed")
            for index, part in enumerate(self._parts[:-1], 1):
                parent = os.open(part, flags, dir_fd=parent)
                fresh.append(parent)
                if _directory_identity(os.fstat(parent)) != self._directories[index]:
                    raise TransferError("source_changed")
            leaf = os.stat(self.name, dir_fd=parent, follow_symlinks=False)
            if _file_facts(leaf) != self._facts:
                raise TransferError("source_changed")
            for descriptor, identity in zip(self._fds[:-1], self._directories):
                if _directory_identity(os.fstat(descriptor)) != identity:
                    raise TransferError("source_changed")
        except OSError:
            raise TransferError("source_changed") from None
        finally:
            for descriptor in reversed(fresh):
                os.close(descriptor)


def _read(fd: int, size: int) -> bytes:
    try:
        return os.read(fd, size)
    except OSError:
        raise TransferError("source_changed") from None


def _call(client: BrokerClient, operation: str, request):
    try:
        return getattr(client, operation)(request)
    except (BrokerUnavailable, ConfigurationError, ValidationError, OSError):
        raise TransferError("upload_unverified") from None


def upload_source(*, source: str, kind: str, client: BrokerClient,
                  file_name: str | None = None) -> dict[str, str | int]:
    """Verify and stream one explicit local source; return only a verified handle."""
    if kind not in _LIMITS:
        raise TransferError("invalid_arguments")
    with _PinnedSource(source) as pinned:
        name = pinned.name if file_name is None else file_name
        if type(name) is not str or _NAME.fullmatch(name) is None:
            raise TransferError("invalid_name")
        size = pinned.info.st_size
        if not 0 < size <= _LIMITS[kind]:
            raise TransferError("invalid_size")
        digest = hashlib.sha256()
        total = 0
        while total < size:
            data = _read(pinned.fd, min(MAX_CHUNK_BYTES, size - total))
            if not data:
                raise TransferError("source_changed")
            digest.update(data)
            total += len(data)
        if _read(pinned.fd, 1):
            raise TransferError("source_changed")
        sha256 = digest.hexdigest()
        try:
            os.lseek(pinned.fd, 0, os.SEEK_SET)
        except OSError:
            raise TransferError("source_changed") from None
        pinned.recheck()
        ready = _call(client, "begin_local_upload", BeginLocalUploadRequest(
            file_name=name, kind=kind, size_bytes=size, sha256=sha256))
        if (ready.status != "ready" or type(ready.upload_id) is not str
                or _UPLOAD_ID.fullmatch(ready.upload_id) is None):
            raise TransferError("upload_unverified")
        upload_id = ready.upload_id
        streamed = hashlib.sha256()
        total = 0
        index = 0
        while total < size:
            data = _read(pinned.fd, min(MAX_CHUNK_BYTES, size - total))
            if not data:
                raise TransferError("source_changed")
            streamed.update(data)
            total += len(data)
            accepted = _call(client, "append_local_upload", AppendLocalUploadRequest(
                upload_id=upload_id, index=index, content_base64=base64.b64encode(data).decode("ascii"),
                sha256=hashlib.sha256(data).hexdigest()))
            if (accepted.status != "accepted" or accepted.upload_id != upload_id
                    or type(accepted.next_index) is not int or accepted.next_index != index + 1
                    or type(accepted.received_bytes) is not int or accepted.received_bytes != total):
                raise TransferError("upload_unverified")
            index += 1
        if _read(pinned.fd, 1) or total != size or streamed.hexdigest() != sha256:
            raise TransferError("source_changed")
        pinned.recheck()
        finished = _call(client, "finish_local_upload", FinishLocalUploadRequest(upload_id=upload_id))
        artifact = _ARTIFACT_ID.fullmatch(finished.artifact_id) if type(finished.artifact_id) is str else None
        if (finished.status != "complete" or artifact is None or artifact[1] != sha256
                or artifact[2] != str(size) or finished.sha256 != sha256
                or type(finished.size_bytes) is not int or finished.size_bytes != size
                or finished.file_name != name):
            raise TransferError("upload_unverified")
        return {"status": "complete", "artifact_id": finished.artifact_id,
                "size_bytes": size, "sha256": sha256}


def _absolute_parts(path: str, code: str) -> tuple[str, ...]:
    if (type(path) is not str or not path.startswith("/") or "\x00" in path
            or path.endswith("/") and path != "/"
            or any(part in {".", ".."} for part in path.split("/"))):
        raise TransferError(code)
    return tuple(part for part in path.split("/") if part)


class _PinnedDirectory:
    """A read-only directory chain; creating and securing directories is out of scope."""
    def __init__(self, path: str, code: str):
        self.code = code
        self.parts = _absolute_parts(path, code)
        self.fds: list[int] = []
        self.identities: list[tuple[int, int]] = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            parent = os.open("/", flags)
            self.fds.append(parent)
            self.identities.append(_directory_identity(os.fstat(parent)))
            for part in self.parts:
                parent = os.open(part, flags, dir_fd=parent)
                self.fds.append(parent)
                self.identities.append(_directory_identity(os.fstat(parent)))
            self.fd = parent
        except (OSError, ValueError, TransferError):
            self.close()
            raise TransferError(code) from None
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        error = None
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError as failure:
                error = failure
        self.fds.clear()
        if error is not None:
            raise error

    def recheck(self) -> None:
        fresh: list[int] = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            parent = os.open("/", flags)
            fresh.append(parent)
            if _directory_identity(os.fstat(parent)) != self.identities[0]:
                raise TransferError(self.code)
            for index, part in enumerate(self.parts, 1):
                parent = os.open(part, flags, dir_fd=parent)
                fresh.append(parent)
                if _directory_identity(os.fstat(parent)) != self.identities[index]:
                    raise TransferError(self.code)
            for fd, identity in zip(self.fds, self.identities):
                if _directory_identity(os.fstat(fd)) != identity:
                    raise TransferError(self.code)
        except (OSError, ValueError, TransferError):
            raise TransferError(self.code) from None
        finally:
            for fd in reversed(fresh):
                os.close(fd)


class _PinnedArtifact:
    """Read one fixed-cache selection without lookup's creation, pruning or chmod."""
    def __init__(self, artifact_id: str, size: int):
        self.directory = _PinnedDirectory(str(CACHE_DIRECTORY), "artifact_unverified")
        self.fd = -1
        self.name = artifact_id
        try:
            self._check_directory()
            self.fd = os.open(artifact_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                              | getattr(os, "O_CLOEXEC", 0), dir_fd=self.directory.fd)
            info = os.fstat(self.fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size != size):
                raise TransferError("artifact_unverified")
            self.facts = _file_facts(info)
            self.recheck()
        except (OSError, ValueError):
            self.close()
            raise TransferError("artifact_unverified") from None
        except BaseException:
            self.close()
            raise

    def _check_directory(self) -> None:
        info = os.fstat(self.directory.fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise TransferError("artifact_unverified")

    def recheck(self) -> None:
        try:
            self.directory.recheck()
            self._check_directory()
            info = os.fstat(self.fd)
            named = os.stat(self.name, dir_fd=self.directory.fd, follow_symlinks=False)
            if (_file_facts(info) != self.facts or _file_facts(named) != self.facts
                    or info.st_mtime + TTL_SECONDS <= time.time()):
                raise TransferError("artifact_unverified")
        except (OSError, ValueError):
            raise TransferError("artifact_unverified") from None

    def close(self) -> None:
        try:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
        finally:
            self.directory.close()


class _PinnedDestination:
    """Keep an exclusive independent recovery inode until publication is verified."""
    def __init__(self, destination: str, source: _PinnedArtifact):
        parts = _absolute_parts(destination, "invalid_destination")
        if not parts:
            raise TransferError("invalid_destination")
        self.name = parts[-1]
        self.directory = _PinnedDirectory("/" + "/".join(parts[:-1]), "invalid_destination")
        self.stage_name = ".telegram-search-transfer-partial_" + uuid.uuid4().hex
        self.stage_fd = -1
        try:
            self.recheck(source)
            try:
                os.stat(self.name, dir_fd=self.directory.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise TransferError("invalid_destination")
        except (OSError, ValueError):
            self.close()
            raise TransferError("invalid_destination") from None
        except BaseException:
            self.close()
            raise

    def recheck(self, source: _PinnedArtifact) -> None:
        self.directory.recheck()
        info = os.fstat(self.directory.fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise TransferError("invalid_destination")
        cache = source.directory
        quarantine_parts = (*cache.parts[:-1], cache.parts[-1] + "-quarantine")
        for forbidden in (cache.parts, quarantine_parts):
            if self.directory.parts[:len(forbidden)] == forbidden:
                raise TransferError("invalid_destination")
        forbidden_identities = {cache.identities[-1]}
        try:
            quarantine = os.stat(quarantine_parts[-1], dir_fd=cache.fds[-2], follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISDIR(quarantine.st_mode):
                forbidden_identities.add((quarantine.st_dev, quarantine.st_ino))
        if any(identity in forbidden_identities for identity in self.directory.identities):
            raise TransferError("invalid_destination")

    def create_stage(self) -> None:
        self.stage_fd = os.open(self.stage_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                                | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=self.directory.fd)

    def stage_facts(self, size: int, links: int) -> tuple[int, ...]:
        info = os.fstat(self.stage_fd)
        named = os.stat(self.stage_name, dir_fd=self.directory.fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != links
                or info.st_size != size or _file_facts(named) != _file_facts(info)):
            raise TransferError("download_failed")
        return _file_facts(info)

    def final_facts(self, expected: tuple[int, ...], links: int) -> tuple[int, ...]:
        info = os.fstat(self.stage_fd)
        named = os.stat(self.name, dir_fd=self.directory.fd, follow_symlinks=False)
        actual = _file_facts(info)
        # Link/unlink legitimately change nlink and ctime, but no other pinned fact.
        if (actual[:4] != expected[:4] or actual[5:7] != expected[5:7]
                or info.st_nlink != links or _file_facts(named) != actual):
            raise TransferError("download_failed")
        return actual

    def close(self) -> None:
        # Recovery names are deliberately retained on every failure, including
        # incomplete writes and uncertain publication. Never remove the final.
        try:
            if self.stage_fd >= 0:
                os.close(self.stage_fd)
                self.stage_fd = -1
        finally:
            self.directory.close()


class _DownloadPublication:
    def __init__(self):
        self.attempted = False


def _download_read(fd: int, size: int) -> bytes:
    try:
        return os.read(fd, size)
    except OSError:
        raise TransferError("artifact_unverified") from None


def _download_handshake(client: BrokerClient, local: dict[str, object]) -> dict[str, object]:
    try:
        remote = client.handshake()
        require_compatible(remote, local, broker=True)
        # Retain a detached, fully validated descriptor rather than transport state.
        return json.loads(json.dumps(remote, allow_nan=False))
    except (BrokerUnavailable, ConfigurationError, CompatibilityError, ValidationError, OSError,
            TypeError, ValueError):
        raise TransferError("download_authorization") from None


def _require_artifacts(policy) -> None:
    try:
        require_current(policy)
        if "artifacts" not in policy.enabled_capabilities:
            raise CompatibilityError("capability_disabled")
    except (ConfigurationError, CompatibilityError):
        raise TransferError("download_authorization") from None


def _verify_download_bytes(fd: int, size: int, sha256: str) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    verified = 0
    while verified < size:
        data = os.read(fd, min(MAX_CHUNK_BYTES, size - verified))
        if not data:
            raise TransferError("download_failed")
        digest.update(data)
        verified += len(data)
    if os.read(fd, 1) or digest.hexdigest() != sha256:
        raise TransferError("download_failed")


def download_artifact(*, artifact_id: str, destination: str, client: BrokerClient,
                      _publication: _DownloadPublication | None = None) -> dict[str, str | int]:
    """Export one cache handle; publication errors preserve possible complete bytes."""
    publication = _publication if _publication is not None else _DownloadPublication()
    source = target = None
    try:
        # Bound before regex/int parsing, including adversarial decimal strings.
        match = _ARTIFACT_ID.fullmatch(artifact_id) if type(artifact_id) is str and len(artifact_id) <= 116 else None
        if match is None:
            raise TransferError("invalid_artifact")
        sha256, decimal_size = match.groups()
        size = int(decimal_size)
        if decimal_size != str(size) or size > MEDIA_LIMIT:
            raise TransferError("invalid_artifact")
        policy = client._policy
        _require_artifacts(policy)
        try:
            local = contract_descriptor(policy)
        except CompatibilityError:
            raise TransferError("download_authorization") from None
        first = _download_handshake(client, local)
        _require_artifacts(policy)
        try:
            source = _PinnedArtifact(artifact_id, size)
            target = _PinnedDestination(destination, source)
            target.create_stage()
            digest = hashlib.sha256()
            copied = 0
            while copied < size:
                data = _download_read(source.fd, min(MAX_CHUNK_BYTES, size - copied))
                if not data:
                    raise TransferError("artifact_unverified")
                digest.update(data)
                copied += len(data)
                view = memoryview(data)
                while view:
                    written = os.write(target.stage_fd, view)
                    if written <= 0 or written > len(view):
                        raise TransferError("download_failed")
                    view = view[written:]
            if _download_read(source.fd, 1) or digest.hexdigest() != sha256:
                raise TransferError("artifact_unverified")
            source.recheck()
            os.fsync(target.stage_fd)
            staged = target.stage_facts(size, 1)
            _verify_download_bytes(target.stage_fd, size, sha256)
            if target.stage_facts(size, 1) != staged:
                raise TransferError("download_failed")
            second = _download_handshake(client, local)
            if second != first:
                raise TransferError("download_authorization")
            source.recheck()
            target.recheck(source)
            if target.stage_facts(size, 1) != staged:
                raise TransferError("download_failed")
            _require_artifacts(policy)
            # Even a raised link can follow kernel completion. Set this before
            # the call and carry it through CLI output and signal restoration.
            publication.attempted = True
            os.link(target.stage_name, target.name, src_dir_fd=target.directory.fd,
                    dst_dir_fd=target.directory.fd, follow_symlinks=False)
            os.fsync(target.directory.fd)
            target.recheck(source)
            linked = target.final_facts(staged, 2)
            if target.stage_facts(size, 2) != linked:
                raise TransferError("download_failed")
            os.unlink(target.stage_name, dir_fd=target.directory.fd)
            os.fsync(target.directory.fd)
            target.recheck(source)
            finished = target.final_facts(linked, 1)
            # A link/unlink changes ctime itself. Rehash the final retained fd
            # so same-size edits with restored mtime cannot hide behind it.
            _verify_download_bytes(target.stage_fd, size, sha256)
            if target.final_facts(finished, 1) != finished:
                raise TransferError("download_failed")
            target.recheck(source)
            return {"status": "complete", "artifact_id": artifact_id,
                    "size_bytes": size, "sha256": sha256}
        finally:
            try:
                if target is not None:
                    target.close()
            finally:
                if source is not None:
                    source.close()
    except BaseException:
        if publication.attempted:
            raise TransferError("download_uncertain") from None
        raise


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages include user-controlled tokens by default.
        raise TransferError("invalid_arguments")


class _UploadBrokerClient(BrokerClient):
    """Keep the proxy's handshake, with strict and bounded wire upload receipts."""
    def _upload_call(self, operation, request, response_type):
        result = self._request(operation, request.model_dump(mode="json"))
        if set(result) - response_type.model_fields.keys():
            raise TransferError("upload_unverified")
        limits = {"detail": 512, "artifact_path": 4096, "artifact_id": 160,
                  "upload_id": 64, "sha256": 64, "file_name": 128, "expires_at": 64}
        for field, limit in limits.items():
            value = result.get(field)
            if value is not None and (type(value) is not str or len(value) > limit):
                raise TransferError("upload_unverified")
        # JSON strict validation retains ISO timestamp support while forbidding
        # Boolean/string coercion into acknowledgment indexes and byte counts.
        return response_type.model_validate_json(json.dumps(result, allow_nan=False), strict=True)

    def begin_local_upload(self, request: BeginLocalUploadRequest) -> BeginLocalUploadResponse:
        return self._upload_call("begin_local_upload", request, BeginLocalUploadResponse)

    def append_local_upload(self, request: AppendLocalUploadRequest) -> AppendLocalUploadResponse:
        return self._upload_call("append_local_upload", request, AppendLocalUploadResponse)

    def finish_local_upload(self, request: FinishLocalUploadRequest) -> FinishLocalUploadResponse:
        return self._upload_call("finish_local_upload", request, FinishLocalUploadResponse)


def _make_client() -> BrokerClient:
    return _UploadBrokerClient(socket_path=BROKER_SOCKET_PATH, policy=load_runtime_policy(),
                              restart_callback=lambda: None)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _silence_failed_stdout() -> None:
    """Prevent Python shutdown from retrying a failed buffered receipt flush."""
    try:
        output_fd = sys.stdout.fileno()
        sink_fd = os.open(os.devnull, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.dup2(sink_fd, output_fd)
        finally:
            os.close(sink_fd)
    except (AttributeError, OSError, ValueError):
        # In-memory/closed streams have no usable descriptor to silence.
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(prog="telegram-search-transfer", description="Upload a trusted local file or download one selected cached artifact.",
                     allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    upload = commands.add_parser("upload", help="Verify and upload one explicit local source", allow_abbrev=False)
    upload.add_argument("--source", required=True, metavar="ABSOLUTE_PATH")
    upload.add_argument("--kind", required=True, choices=tuple(_LIMITS))
    upload.add_argument("--file-name", metavar="SAFE_BASENAME")
    download = commands.add_parser("download", help="Verify and export one selected cached artifact", allow_abbrev=False)
    download.add_argument("--artifact-id", required=True, metavar="ISSUED_ID")
    download.add_argument("--destination", required=True, metavar="ABSOLUTE_PATH")
    publication = _DownloadPublication()
    command = None
    try:
        previous = signal.signal(signal.SIGTERM, _interrupt)
        try:
            args = parser.parse_args(argv)
            command = args.command
            if command == "download":
                receipt = download_artifact(artifact_id=args.artifact_id, destination=args.destination,
                                            client=_make_client(), _publication=publication)
            else:
                receipt = upload_source(source=args.source, kind=args.kind, file_name=args.file_name, client=_make_client())
            try:
                print(json.dumps(receipt, separators=(",", ":")), flush=command == "download")
            except BaseException:
                if publication.attempted:
                    _silence_failed_stdout()
                raise
            return 0
        finally:
            signal.signal(signal.SIGTERM, previous)
    except KeyboardInterrupt:
        error = TransferError("download_uncertain" if publication.attempted else
                              "download_interrupted" if command == "download" else "interrupted")
        exit_code = 130
    except TransferError as failure:
        error = TransferError("download_uncertain") if publication.attempted else failure
        exit_code = 1
    except ConfigurationError:
        error = TransferError("download_uncertain" if publication.attempted else
                              "download_authorization" if command == "download" else "configuration_error")
        exit_code = 1
    except Exception:
        error = TransferError("download_uncertain" if publication.attempted else
                              "download_failed" if command == "download" else "internal_error")
        exit_code = 1
    try:
        print(json.dumps({"status": "error", "code": error.code, "detail": str(error)}, separators=(",", ":")),
              file=sys.stderr, flush=True)
    except (OSError, KeyboardInterrupt):
        # A broken diagnostic stream cannot turn uncertainty into success or
        # justify replaying publication. Preserve the nonzero exit silently.
        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
