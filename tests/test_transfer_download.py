"""Selected cached snapshots export through real local files and broker handshakes."""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.broker_client import BrokerUnavailable
from telegram_search_mcp.config import RuntimePolicy, load_runtime_policy
from test_uploaded_artifact_read import running_broker


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("telegram_search_mcp.transfer_cli")
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp", prefix="f10b-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination_parent = self.root / "exports"
        self.destination_parent.mkdir(mode=0o700)
        self.destination = self.destination_parent / "result.bin"
        self.source = self.root / "selected.bin"

    @contextlib.contextmanager
    def selected(self, content=b"abc", *, policy=None):
        self.source.write_bytes(content)
        with running_broker(self.root, policy=policy) as (broker, client, store):
            selected = store.store(self.source)
            with patch.object(self.module, "CACHE_DIRECTORY", store.cache_dir, create=True):
                yield broker, client, store, selected

    def invoke(self, client, artifact, *, destination=None, arguments=None, output=None):
        output = output if output is not None else io.StringIO()
        error = io.StringIO()
        args = arguments if arguments is not None else [
            "download", "--artifact-id", artifact.artifact_id,
            "--destination", str(destination or self.destination),
        ]
        with patch.object(self.module, "_make_client", return_value=client), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = self.module.main(args)
        return result, output.getvalue(), error.getvalue()

    def failed(self, result, *, code=None):
        exit_code, output, error = result
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(output, "")
        self.assertEqual(len(error.splitlines()), 1)
        self.assertLess(len(error), 450)
        value = json.loads(error)
        self.assertEqual(value["status"], "error")
        if code:
            self.assertEqual(value["code"], code)
        self.assertNotIn(str(self.root), error)
        self.assertNotIn("sensitive", error)
        self.assertNotIn("Traceback", error)
        return value

    def partials(self, parent=None):
        return list((parent or self.destination_parent).glob(".telegram-search-transfer-partial_*"))

    def assert_prelink_retained(self):
        self.assertFalse(self.destination.exists())
        partials = self.partials()
        self.assertEqual(len(partials), 1)
        self.assertEqual(stat.S_IMODE(partials[0].stat().st_mode), 0o600)
        self.assertEqual(partials[0].stat().st_nlink, 1)
        return partials[0]

    def policy_file(self):
        parent = self.root / "policy"
        parent.mkdir(mode=0o700, exist_ok=True)
        path = parent / "runtime.toml"
        path.write_text('config_version = 1\nenabled_capabilities = ["artifacts", "read"]\n')
        path.chmod(0o600)
        return path, load_runtime_policy(path)

    def test_real_store_export_and_zero_snapshot_have_exact_independent_private_bytes(self):
        # Linking the cache inode, omitting EOF/hash verification, or leaking paths breaks this.
        for content in (b"abc", b"", b"0123456789abcdef" * 70000):
            with self.subTest(size=len(content)), self.selected(content) as (_, client, store, artifact):
                cache_before = artifact.path.stat()
                reads, writes = [], []
                real_read, real_write = os.read, os.write
                def bounded_read(fd, size):
                    reads.append(size)
                    return real_read(fd, size)
                def bounded_write(fd, value):
                    writes.append(len(value))
                    return real_write(fd, value)
                with patch.object(self.module.os, "read", side_effect=bounded_read), \
                     patch.object(self.module.os, "write", side_effect=bounded_write):
                    result, output, error = self.invoke(client, artifact)
                self.assertEqual(result, 0, error)
                self.assertEqual(error, "")
                self.assertEqual(len(output.splitlines()), 1)
                self.assertLess(len(output), 350)
                self.assertNotIn(str(self.root), output)
                self.assertEqual(json.loads(output), {
                    "status": "complete", "artifact_id": artifact.artifact_id,
                    "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                })
                self.assertEqual(self.destination.read_bytes(), content)
                final = self.destination.stat()
                self.assertNotEqual((final.st_dev, final.st_ino), (cache_before.st_dev, cache_before.st_ino))
                self.assertEqual((final.st_nlink, stat.S_IMODE(final.st_mode)), (1, 0o600))
                self.assertEqual(artifact.path.stat(), cache_before)
                self.assertEqual(self.partials(), [])
                self.assertLessEqual(max(reads), 512 * 1024)
                self.assertLessEqual(max(writes, default=0), 512 * 1024)
                self.destination.unlink()

    def test_default_cli_uses_fixed_socket_policy_and_only_handshakes(self):
        with self.selected() as (broker, client, store, artifact):
            output, error = io.StringIO(), io.StringIO()
            with patch.object(self.module, "BROKER_SOCKET_PATH", broker._socket_path), \
                 patch.object(self.module, "load_runtime_policy", return_value=broker._policy), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                result = self.module.main(["download", "--artifact-id", artifact.artifact_id,
                                           "--destination", str(self.destination)])
            self.assertEqual(result, 0, error.getvalue())
            self.assertEqual(self.destination.read_bytes(), b"abc")
            self.assertEqual(broker._artifact_metadata, {})
            self.assertEqual(broker._uploads._entries, {})

    def test_malformed_noncanonical_and_oversized_handles_are_bounded_failures(self):
        with self.selected() as (_, client, _, artifact):
            bad = ["../sensitive", artifact.artifact_id.upper(), artifact.artifact_id + "/file",
                   artifact.artifact_id[:-1] + "03", artifact.artifact_id[:-1] + "268435457",
                   "artifact_" + "a" * 32 + "_" + "b" * 64 + "_" + "9" * 10000]
            for handle in bad:
                with self.subTest(handle=handle[:40]):
                    args = ["download", "--artifact-id", handle, "--destination", str(self.destination)]
                    self.failed(self.invoke(client, artifact, arguments=args), code="invalid_artifact")
                    self.assertFalse(self.destination.exists())
                    self.assertEqual(self.partials(), [])
            maximum = "artifact_" + "a" * 32 + "_" + "b" * 64 + "_268435456"
            self.failed(self.invoke(client, artifact, arguments=["download", "--artifact-id", maximum,
                        "--destination", str(self.destination)]), code="artifact_unverified")

    def test_unsafe_destination_paths_and_existing_leaf_types_are_unchanged(self):
        with self.selected() as (_, client, store, artifact):
            before = artifact.path.stat()
            bad = ["relative.bin", str(self.destination) + "/", str(self.root) + "/exports/../result.bin",
                   str(self.root) + "/exports/./result.bin", str(self.destination) + "\x00sensitive",
                   str(self.root / "missing" / "file"), str(store.cache_dir / "new.bin")]
            nested = store.cache_dir / "child"
            nested.mkdir(mode=0o700)
            quarantine = store.cache_dir.with_name(store.cache_dir.name + "-quarantine")
            quarantine.mkdir(mode=0o700, exist_ok=True)
            child = quarantine / "child"
            child.mkdir(mode=0o700)
            bad.extend([str(nested / "new.bin"), str(quarantine / "new.bin"), str(child / "new.bin")])
            alias = self.root / "exports-alias"
            alias.symlink_to(self.destination_parent, target_is_directory=True)
            bad.append(str(alias / "new.bin"))
            for destination in bad:
                with self.subTest(destination=destination):
                    self.failed(self.invoke(client, artifact, destination=destination), code="invalid_destination")
                    self.assertEqual(self.partials(), [])
            for kind in ("file", "directory", "symlink", "fifo"):
                with self.subTest(kind=kind):
                    if kind == "file":
                        self.destination.write_bytes(b"prior destination")
                    elif kind == "directory":
                        self.destination.mkdir()
                    elif kind == "symlink":
                        self.destination.symlink_to(self.root / "missing-sensitive")
                    else:
                        os.mkfifo(self.destination)
                    try:
                        old = self.destination.lstat()
                        self.failed(self.invoke(client, artifact), code="invalid_destination")
                        self.assertEqual(self.destination.lstat(), old)
                        if kind == "file":
                            self.assertEqual(self.destination.read_bytes(), b"prior destination")
                    finally:
                        if kind == "directory":
                            self.destination.rmdir()
                        else:
                            self.destination.unlink()
            self.assertEqual(artifact.path.stat(), before)

    def test_group_writable_destination_parent_is_rejected_without_chmod(self):
        with self.selected() as (_, client, _, artifact):
            self.destination_parent.chmod(0o720)
            before = self.destination_parent.stat()
            self.failed(self.invoke(client, artifact), code="invalid_destination")
            self.assertEqual(self.destination_parent.stat(), before)
            self.assertEqual(self.partials(), [])

    def test_owner_and_cache_quarantine_identity_alias_checks_reject_before_staging(self):
        # A normal user cannot chown test files or hard-link directories. Inject
        # only those external metadata observations; all opening/copying remains real.
        for change in ("cache_owner", "leaf_owner", "destination_owner", "cache_alias", "quarantine_alias"):
            with self.subTest(change=change), self.selected() as (_, client, store, artifact):
                quarantine = store.quarantine_dir
                quarantine.mkdir(mode=0o700, exist_ok=True)
                real_fstat = os.fstat
                selected_info = artifact.path.stat()
                cache_info = store.cache_dir.stat()
                destination_info = self.destination_parent.stat()
                quarantine_info = quarantine.stat()
                wanted = selected_info if change == "leaf_owner" else cache_info if change == "cache_owner" else destination_info
                def observed(fd):
                    info = real_fstat(fd)
                    if (info.st_dev, info.st_ino) != (wanted.st_dev, wanted.st_ino):
                        return info
                    values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                    if change.endswith("owner"):
                        values["st_uid"] = info.st_uid + 1
                    else:
                        alias = cache_info if change == "cache_alias" else quarantine_info
                        values.update(st_dev=alias.st_dev, st_ino=alias.st_ino)
                    return SimpleNamespace(**values)
                with patch.object(self.module.os, "fstat", side_effect=observed):
                    code = "artifact_unverified" if change in {"cache_owner", "leaf_owner"} else "invalid_destination"
                    self.failed(self.invoke(client, artifact), code=code)
                self.assertEqual(artifact.path.stat(), selected_info)
                self.assertFalse(self.destination.exists())
                self.assertEqual(self.partials(), [])

    def test_unsafe_missing_and_damaged_cache_entries_are_not_pruned_or_changed(self):
        for change in ("missing", "root_mode", "root_symlink", "leaf_symlink", "directory", "fifo",
                       "multilink", "leaf_mode", "size", "hash", "expired"):
            with self.subTest(change=change), self.selected() as (_, client, store, artifact):
                extra = store.cache_dir / "unrelated"
                extra.write_bytes(b"unchanged unrelated bytes")
                if change == "missing":
                    artifact.path.unlink()
                elif change == "root_mode":
                    store.cache_dir.chmod(0o750)
                elif change == "root_symlink":
                    store.cache_dir.rename(self.root / "old-cache")
                    store.cache_dir.symlink_to(self.root / "old-cache", target_is_directory=True)
                elif change in {"leaf_symlink", "directory", "fifo"}:
                    artifact.path.unlink()
                    if change == "leaf_symlink":
                        artifact.path.symlink_to(self.source)
                    elif change == "directory":
                        artifact.path.mkdir()
                    else:
                        os.mkfifo(artifact.path)
                elif change == "multilink":
                    os.link(artifact.path, self.root / "cache-hardlink")
                elif change == "leaf_mode":
                    artifact.path.chmod(0o640)
                elif change == "size":
                    artifact.path.write_bytes(b"abcd")
                elif change == "hash":
                    artifact.path.write_bytes(b"xyz")
                else:
                    expired = time.time() - 43201
                    os.utime(artifact.path, (expired, expired))
                cache_info = store.cache_dir.lstat()
                leaf_info = artifact.path.lstat() if change != "missing" else None
                extra_info = extra.stat()
                self.failed(self.invoke(client, artifact), code="artifact_unverified")
                self.assertFalse(self.destination.exists())
                self.assertEqual(store.cache_dir.lstat(), cache_info)
                if leaf_info is not None:
                    self.assertEqual(artifact.path.lstat(), leaf_info)
                self.assertEqual(extra.stat(), extra_info)
                self.assertEqual(extra.read_bytes(), b"unchanged unrelated bytes")
            # Fresh store fixtures must not pass through deliberately poisoned caches.
            if change == "root_symlink":
                (self.root / "cache").unlink()
                (self.root / "old-cache").rename(self.root / "cache")
            (self.root / "cache").chmod(0o700)
            for path in (self.root / "cache").iterdir():
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()
            if (self.root / "cache-hardlink").exists():
                (self.root / "cache-hardlink").unlink()
            for partial in self.partials():
                partial.unlink()

    def test_source_changes_during_copy_leave_partial_and_no_final(self):
        for change in ("bytes", "growth", "shrink", "leaf", "parent", "expire"):
            with self.subTest(change=change), self.selected(b"abc" * 200000) as (_, client, store, artifact):
                real_read = os.read
                changed = False
                source_identity = (artifact.path.stat().st_dev, artifact.path.stat().st_ino)
                def mutate_after_read(fd, size):
                    nonlocal changed
                    data = real_read(fd, size)
                    info = os.fstat(fd)
                    if data and not changed and (info.st_dev, info.st_ino) == source_identity:
                        changed = True
                        if change == "bytes":
                            with artifact.path.open("r+b") as output:
                                output.write(b"xyz")
                        elif change == "growth":
                            with artifact.path.open("ab") as output:
                                output.write(b"x")
                        elif change == "shrink":
                            with artifact.path.open("r+b") as output:
                                output.truncate(2)
                        elif change == "leaf":
                            replacement = self.root / "replacement"
                            replacement.write_bytes(b"abc" * 200000)
                            replacement.chmod(0o600)
                            os.replace(replacement, artifact.path)
                        elif change == "parent":
                            store.cache_dir.rename(self.root / "old-cache")
                            store.cache_dir.mkdir(mode=0o700)
                            artifact.path.write_bytes(b"abc" * 200000)
                            artifact.path.chmod(0o600)
                        else:
                            expired = time.time() - 43201
                            os.utime(artifact.path, (expired, expired))
                    return data
                with patch.object(self.module.os, "read", side_effect=mutate_after_read):
                    self.failed(self.invoke(client, artifact), code="artifact_unverified")
                self.assert_prelink_retained()
            for partial in self.partials():
                partial.unlink()
            if change == "parent" and (self.root / "old-cache").exists():
                for path in (self.root / "old-cache").iterdir():
                    path.unlink()
                (self.root / "old-cache").rmdir()

    def test_ttl_expiring_during_copy_rejects_without_any_source_metadata_change(self):
        with self.selected() as (_, client, _, artifact):
            before = artifact.path.stat()
            now = time.time()
            real_write = os.write
            def boundary(fd, data):
                nonlocal now
                result = real_write(fd, data)
                now = before.st_mtime + 43200
                return result
            with patch.object(self.module.time, "time", side_effect=lambda: now), \
                 patch.object(self.module.os, "write", side_effect=boundary):
                self.failed(self.invoke(client, artifact), code="artifact_unverified")
            self.assertEqual(artifact.path.stat(), before)
            self.assertEqual(self.assert_prelink_retained().read_bytes(), b"abc")

    def test_staging_corruption_is_rehashed_and_rejected_before_any_link(self):
        with self.selected() as (_, client, _, artifact):
            real_write = os.write
            def boundary(fd, data):
                return real_write(fd, b"xyz")
            with patch.object(self.module.os, "write", side_effect=boundary):
                self.failed(self.invoke(client, artifact), code="download_failed")
            self.assertEqual(self.assert_prelink_retained().read_bytes(), b"xyz")
            self.assertEqual(artifact.path.read_bytes(), b"abc")

    def test_disabled_stale_and_mismatched_policy_reject_before_staging(self):
        for change in ("disabled", "stale", "mismatch"):
            with self.subTest(change=change):
                path, policy = self.policy_file() if change == "stale" else (None, RuntimePolicy(
                    enabled_capabilities=("read",)) if change == "disabled" else RuntimePolicy())
                with self.selected(policy=policy) as (broker, client, _, artifact):
                    before = artifact.path.stat()
                    if change == "stale":
                        path.write_text('config_version = 1\nenabled_capabilities = ["read"]\n')
                    elif change == "mismatch":
                        client._policy = RuntimePolicy(expected_package_version="not-installed")
                    self.failed(self.invoke(client, artifact), code="download_authorization")
                    self.assertEqual(artifact.path.stat(), before)
                    self.assertEqual(self.partials(), [])
                    self.assertFalse(self.destination.exists())
                if path is not None:
                    path.unlink()
                    path.parent.rmdir()

    def test_policy_and_generation_changes_during_copy_prevent_publication(self):
        for change in ("policy", "generation", "policy_after_second"):
            with self.subTest(change=change):
                path, policy = self.policy_file()
                with self.selected(policy=policy) as (broker, client, _, artifact):
                    real_write, real_handshake = os.write, client.handshake
                    changed = False
                    handshakes = 0
                    def change_settings():
                        if change == "generation":
                            broker._generation = "broker_" + "f" * 32
                        else:
                            path.write_text('config_version = 1\nenabled_capabilities = ["read"]\n')
                    def boundary_write(fd, value):
                        nonlocal changed
                        result = real_write(fd, value)
                        if not changed and change != "policy_after_second":
                            changed = True
                            change_settings()
                        return result
                    def boundary_handshake():
                        nonlocal handshakes
                        response = real_handshake()
                        handshakes += 1
                        if handshakes == 2 and change == "policy_after_second":
                            change_settings()
                        return response
                    with patch.object(self.module.os, "write", side_effect=boundary_write), \
                         patch.object(client, "handshake", side_effect=boundary_handshake):
                        self.failed(self.invoke(client, artifact), code="download_authorization")
                    self.assert_prelink_retained()
                path.unlink()
                path.parent.rmdir()
                for partial in self.partials():
                    partial.unlink()

    def test_policy_change_after_first_handshake_rejects_before_cache_or_staging(self):
        path, policy = self.policy_file()
        with self.selected(policy=policy) as (_, client, _, artifact):
            real_handshake = client.handshake
            def boundary():
                response = real_handshake()
                path.write_text('config_version = 1\nenabled_capabilities = ["read"]\n')
                return response
            with patch.object(client, "handshake", side_effect=boundary):
                self.failed(self.invoke(client, artifact), code="download_authorization")
            self.assertFalse(self.destination.exists())
            self.assertEqual(self.partials(), [])

    def test_second_handshake_lost_malformed_or_changed_descriptor_retains_verified_partial(self):
        for change in ("lost", "malformed", "descriptor"):
            with self.subTest(change=change), self.selected() as (_, client, _, artifact):
                real_handshake = client.handshake
                calls = 0
                def boundary():
                    nonlocal calls
                    response = real_handshake()
                    calls += 1
                    if calls == 2:
                        if change == "lost":
                            raise BrokerUnavailable("sensitive transport failure")
                        if change == "malformed":
                            response["broker_generation"] = "bad"
                        else:
                            response["enabled_capabilities"] = ["read"]
                    return response
                with patch.object(client, "handshake", side_effect=boundary):
                    self.failed(self.invoke(client, artifact), code="download_authorization")
                self.assertEqual(self.assert_prelink_retained().read_bytes(), b"abc")
                self.assertEqual(calls, 2)
            for partial in self.partials():
                partial.unlink()

    def test_valid_snapshot_survives_prior_broker_restart_without_reader_metadata(self):
        with self.selected() as (_, _, store, artifact):
            before = artifact.path.stat()
        with running_broker(self.root) as (broker, client, _), \
             patch.object(self.module, "CACHE_DIRECTORY", store.cache_dir, create=True):
            result, _, error = self.invoke(client, artifact)
            self.assertEqual(result, 0, error)
            self.assertEqual(self.destination.read_bytes(), b"abc")
            self.assertEqual(artifact.path.stat(), before)
            self.assertEqual(broker._artifact_metadata, {})

    def test_short_writes_are_completed_and_zero_or_disk_full_writes_retain_partial(self):
        for change in ("short", "zero", "full"):
            with self.subTest(change=change), self.selected(b"abc" * 100) as (_, client, _, artifact):
                real_write = os.write
                calls = 0
                def boundary(fd, value):
                    nonlocal calls
                    calls += 1
                    if change == "short":
                        return real_write(fd, value[:7])
                    if calls == 1:
                        return real_write(fd, value[:7])
                    if change == "zero":
                        return 0
                    raise OSError(28, "sensitive disk full")
                with patch.object(self.module.os, "write", side_effect=boundary):
                    result = self.invoke(client, artifact)
                if change == "short":
                    self.assertEqual(result[0], 0, result[2])
                    self.assertEqual(self.destination.read_bytes(), b"abc" * 100)
                    self.destination.unlink()
                else:
                    self.failed(result, code="download_failed")
                    self.assertEqual(self.assert_prelink_retained().read_bytes(), b"abcabca")
            for partial in self.partials():
                partial.unlink()

    def test_prelink_fsync_and_interrupt_failures_keep_partial_without_final(self):
        for change in ("fsync", "keyboard", "sigterm"):
            with self.subTest(change=change), self.selected() as (_, client, _, artifact):
                real_fsync = os.fsync
                def boundary(fd):
                    if change == "fsync":
                        raise OSError("sensitive fsync failure")
                    if change == "sigterm":
                        signal.raise_signal(signal.SIGTERM)
                    raise KeyboardInterrupt
                previous = signal.getsignal(signal.SIGTERM)
                with patch.object(self.module.os, "fsync", side_effect=boundary):
                    result = self.invoke(client, artifact)
                self.failed(result, code="download_failed" if change == "fsync" else "download_interrupted")
                self.assertEqual(self.assert_prelink_retained().read_bytes(), b"abc")
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
            for partial in self.partials():
                partial.unlink()

    def test_stage_mutation_or_substitution_before_link_is_not_published_or_unlinked(self):
        for change in ("bytes", "substitute"):
            with self.subTest(change=change), self.selected() as (_, client, _, artifact):
                real_handshake = client.handshake
                calls = 0
                def boundary():
                    nonlocal calls
                    response = real_handshake()
                    calls += 1
                    if calls == 2:
                        partial = self.partials()[0]
                        if change == "substitute":
                            partial.rename(self.destination_parent / "own-recovery")
                            partial.write_bytes(b"abc")
                            partial.chmod(0o600)
                        else:
                            partial.write_bytes(b"xyz")
                    return response
                with patch.object(client, "handshake", side_effect=boundary):
                    self.failed(self.invoke(client, artifact), code="download_failed")
                partial = self.assert_prelink_retained()
                self.assertEqual(partial.read_bytes(), b"abc" if change == "substitute" else b"xyz")
                if change == "substitute":
                    self.assertEqual((self.destination_parent / "own-recovery").read_bytes(), b"abc")
                    (self.destination_parent / "own-recovery").unlink()
            for partial in self.partials():
                partial.unlink()

    def test_destination_parent_replacement_before_link_keeps_recovery_in_pinned_directory(self):
        with self.selected() as (_, client, _, artifact):
            real_handshake = client.handshake
            calls = 0
            old_parent = self.root / "old-exports"
            def boundary():
                nonlocal calls
                response = real_handshake()
                calls += 1
                if calls == 2:
                    self.destination_parent.rename(old_parent)
                    self.destination_parent.mkdir(mode=0o700)
                return response
            with patch.object(client, "handshake", side_effect=boundary):
                self.failed(self.invoke(client, artifact), code="invalid_destination")
            self.assertFalse(self.destination.exists())
            self.assertFalse((old_parent / self.destination.name).exists())
            self.assertEqual(len(self.partials(old_parent)), 1)
            self.assertEqual(self.partials(old_parent)[0].read_bytes(), b"abc")

    def test_raced_existing_destination_is_never_overwritten_and_is_uncertain(self):
        with self.selected() as (_, client, _, artifact):
            real_link = os.link
            calls = 0
            def boundary(source, destination, **kwargs):
                nonlocal calls
                calls += 1
                self.destination.write_bytes(b"raced prior bytes")
                return real_link(source, destination, **kwargs)
            with patch.object(self.module.os, "link", side_effect=boundary):
                error = self.failed(self.invoke(client, artifact), code="download_uncertain")
            self.assertIn("inspect", error["detail"])
            self.assertEqual(calls, 1)
            self.assertEqual(self.destination.read_bytes(), b"raced prior bytes")
            self.assertEqual(self.partials()[0].read_bytes(), b"abc")

    def test_errors_and_interruptions_after_actual_link_preserve_final_and_never_replay(self):
        for change in ("link_error", "keyboard", "sigterm", "directory_fsync", "unlink", "unlink_after",
                       "cleanup_fsync", "stage_substitute", "parent_replace"):
            with self.subTest(change=change), self.selected() as (_, client, _, artifact):
                real_link, real_fsync, real_unlink = os.link, os.fsync, os.unlink
                links, directory_syncs = 0, 0
                old_parent = self.root / "old-exports"
                def link_boundary(source, destination, **kwargs):
                    nonlocal links
                    links += 1
                    result = real_link(source, destination, **kwargs)
                    if change == "link_error":
                        raise OSError("sensitive lost link return")
                    if change == "keyboard":
                        raise KeyboardInterrupt
                    if change == "sigterm":
                        signal.raise_signal(signal.SIGTERM)
                    return result
                def sync_boundary(fd):
                    nonlocal directory_syncs
                    result = real_fsync(fd)
                    if stat.S_ISDIR(os.fstat(fd).st_mode):
                        directory_syncs += 1
                        if (change == "directory_fsync" and directory_syncs == 1
                                or change == "cleanup_fsync" and directory_syncs == 2):
                            raise OSError("sensitive directory sync failure")
                        if change == "stage_substitute" and directory_syncs == 1:
                            partial = self.partials()[0]
                            partial.rename(self.destination_parent / "own-recovery")
                            partial.write_bytes(b"foreign recovery bytes")
                            partial.chmod(0o600)
                        if change == "parent_replace" and directory_syncs == 1:
                            self.destination_parent.rename(old_parent)
                            self.destination_parent.mkdir(mode=0o700)
                    return result
                def unlink_boundary(name, **kwargs):
                    if str(name).startswith(".telegram-search-transfer-partial_") and change == "unlink":
                        raise OSError("sensitive cleanup failure")
                    result = real_unlink(name, **kwargs)
                    if str(name).startswith(".telegram-search-transfer-partial_") and change == "unlink_after":
                        raise OSError("sensitive lost cleanup return")
                    return result
                with patch.object(self.module.os, "link", side_effect=link_boundary), \
                     patch.object(self.module.os, "fsync", side_effect=sync_boundary), \
                     patch.object(self.module.os, "unlink", side_effect=unlink_boundary):
                    self.failed(self.invoke(client, artifact), code="download_uncertain")
                final = old_parent / self.destination.name if change == "parent_replace" else self.destination
                self.assertEqual(final.read_bytes(), b"abc")
                self.assertEqual(links, 1)
                if change == "stage_substitute":
                    self.assertEqual(self.partials()[0].read_bytes(), b"foreign recovery bytes")
                    self.assertEqual((self.destination_parent / "own-recovery").read_bytes(), b"abc")
                    (self.destination_parent / "own-recovery").unlink()
                final.unlink()
                for partial in self.partials(old_parent if change == "parent_replace" else None):
                    partial.unlink()
                if change == "parent_replace":
                    old_parent.rmdir()

    def test_success_output_write_flush_and_signal_failures_are_uncertain_with_final_preserved(self):
        for change in ("write", "flush", "sigterm"):
            with self.subTest(change=change), self.selected() as (_, client, _, artifact):
                class FailedOutput(io.StringIO):
                    def write(inner, value):
                        if change == "write":
                            raise OSError("sensitive output failure")
                        if change == "sigterm":
                            signal.raise_signal(signal.SIGTERM)
                        return super().write(value)
                    def flush(inner):
                        if change == "flush":
                            raise OSError("sensitive flush failure")
                        return super().flush()
                result, output, error = self.invoke(client, artifact, output=FailedOutput())
                self.assertNotEqual(result, 0)
                self.assertEqual(json.loads(error)["code"], "download_uncertain")
                self.assertNotIn("sensitive", error)
                self.assertEqual(self.destination.read_bytes(), b"abc")
                self.assertEqual(self.destination.stat().st_nlink, 1)
                self.assertEqual(self.partials(), [])
                self.destination.unlink()

    def test_postlink_same_size_mutation_with_restored_mtime_never_receives_success(self):
        # Link changes ctime itself; checking only prelink mtime and size misses this mutation.
        with self.selected() as (_, client, _, artifact):
            real_link = os.link
            def boundary(source, destination, **kwargs):
                result = real_link(source, destination, **kwargs)
                before = self.destination.stat()
                self.destination.write_bytes(b"xyz")
                os.utime(self.destination, ns=(before.st_atime_ns, before.st_mtime_ns))
                return result
            with patch.object(self.module.os, "link", side_effect=boundary):
                self.failed(self.invoke(client, artifact), code="download_uncertain")
            self.assertEqual(self.destination.read_bytes(), b"xyz")

    def test_postpublication_fd_close_serialization_and_handler_restore_faults_are_uncertain(self):
        for change in ("stage_close", "source_close", "serialization", "restore"):
            with self.subTest(change=change), self.selected() as (_, client, _, artifact):
                real_close, real_dumps, real_signal = os.close, json.dumps, signal.signal
                source_identity = (artifact.path.stat().st_dev, artifact.path.stat().st_ino)
                raised = False
                previous = signal.getsignal(signal.SIGTERM)
                def close_boundary(fd):
                    nonlocal raised
                    info = os.fstat(fd)
                    result = real_close(fd)
                    if self.destination.exists() and not raised and stat.S_ISREG(info.st_mode):
                        matches_source = (info.st_dev, info.st_ino) == source_identity
                        if change == "source_close" and matches_source or change == "stage_close" and not matches_source:
                            raised = True
                            raise OSError("sensitive lost close return")
                    return result
                def dumps_boundary(value, **kwargs):
                    if change == "serialization" and isinstance(value, dict) and value.get("status") == "complete":
                        raise KeyboardInterrupt
                    return real_dumps(value, **kwargs)
                def signal_boundary(signum, handler):
                    result = real_signal(signum, handler)
                    if change == "restore" and signum == signal.SIGTERM and handler == previous:
                        raise OSError("sensitive signal restore failure")
                    return result
                with patch.object(self.module.os, "close", side_effect=close_boundary), \
                     patch.object(self.module.json, "dumps", side_effect=dumps_boundary), \
                     patch.object(self.module.signal, "signal", side_effect=signal_boundary):
                    result, output, error = self.invoke(client, artifact)
                self.assertNotEqual(result, 0)
                self.assertEqual(json.loads(error)["code"], "download_uncertain")
                self.assertNotIn("sensitive", error)
                self.assertEqual(self.destination.read_bytes(), b"abc")
                self.assertEqual(self.destination.stat().st_nlink, 1)
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
                if change != "restore":
                    self.assertEqual(output, "")
                self.destination.unlink()

    def test_real_closed_stdout_pipe_emits_one_uncertainty_line_and_preserves_final(self):
        # Buffer flush during interpreter shutdown must not add a second diagnostic/traceback.
        program = '''
import sys
from pathlib import Path
from telegram_search_mcp import transfer_cli as transfer
from telegram_search_mcp.config import RuntimePolicy
transfer.CACHE_DIRECTORY = Path(sys.argv[1])
transfer.BROKER_SOCKET_PATH = Path(sys.argv[2])
policy = RuntimePolicy(enabled_capabilities=("artifacts", "read", "spreadsheets", "presentations"))
transfer.load_runtime_policy = lambda: policy
raise SystemExit(transfer.main(sys.argv[3:]))
'''
        with self.selected() as (broker, _, store, artifact):
            command = [sys.executable, "-c", program, str(store.cache_dir), str(broker._socket_path),
                       "download", "--artifact-id", artifact.artifact_id, "--destination", str(self.destination)]
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                process.stdout.close()
                error = process.stderr.read(2048).decode("utf-8")
                process.wait(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(len(error.splitlines()), 1, error)
            self.assertEqual(json.loads(error)["code"], "download_uncertain")
            self.assertEqual(self.destination.read_bytes(), b"abc")


if __name__ == "__main__":
    unittest.main()
