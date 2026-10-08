from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from telegram_search_mcp.artifact_store import ArtifactStore, ArtifactStoreError


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.base = Path(self.sandbox.name)
        self.cache = self.base / "cache"
        self.clock = Clock()
        self.store = ArtifactStore(cache_dir=self.cache, clock=self.clock)
        self.source = self.base / "tdlib-source"

    def test_store_copies_bytes_with_digest_and_private_permissions(self) -> None:
        self.source.write_bytes(b"Telegram attachment")

        result = self.store.store(self.source, kind="document")

        self.assertNotEqual(result.path, self.source)
        self.assertEqual(result.path.read_bytes(), b"Telegram attachment")
        self.assertEqual(result.sha256, hashlib.sha256(b"Telegram attachment").hexdigest())
        self.assertEqual(result.size_bytes, 19)
        self.assertEqual(result.expires_at, self.clock.now + 12 * 3600)
        self.assertEqual(result.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.lookup(result.artifact_id), result)

    def test_source_symlink_and_nonregular_file_are_refused(self) -> None:
        self.source.write_bytes(b"secret")
        symlink = self.base / "source-link"
        symlink.symlink_to(self.source)
        with self.assertRaises(ArtifactStoreError):
            self.store.store(symlink)
        with self.assertRaises(ArtifactStoreError):
            self.store.store(self.base)
        self.assertEqual(list(self.cache.glob("artifact_*")), [])

    def test_oversize_preflight_rejects_without_copy(self) -> None:
        self.source.write_bytes(b"12345")
        small = ArtifactStore(cache_dir=self.cache, clock=self.clock, max_document_bytes=4)
        with self.assertRaises(ArtifactStoreError):
            small.store(self.source, kind="image")
        self.assertEqual(list(self.cache.glob("artifact_*")), [])

    def test_source_growth_does_not_leave_unreserved_quarantine_bytes(self) -> None:
        self.source.write_bytes(b"1")
        budgeted = ArtifactStore(cache_dir=self.cache, clock=self.clock, max_total_bytes=7)
        with patch("telegram_search_mcp.artifact_store.LARGE_FILE_THRESHOLD", 2):
            with patch("telegram_search_mcp.artifact_store.os.read", side_effect=[b"123", b""]):
                with self.assertRaises(ArtifactStoreError):
                    budgeted.store(self.source)
        self.assertEqual(list(budgeted.quarantine_dir.iterdir()), [])

    def test_abandoned_temp_is_removed_before_budget_check(self) -> None:
        self.cache.mkdir()
        abandoned = self.cache / "tmp_abandoned"
        abandoned.write_bytes(b"1234567")
        self.source.write_bytes(b"1234")
        budgeted = ArtifactStore(cache_dir=self.cache, clock=self.clock, max_total_bytes=7)
        budgeted.store(self.source)
        self.assertFalse(abandoned.exists())

    def test_budget_is_reserved_before_copy_begins(self) -> None:
        budgeted = ArtifactStore(cache_dir=self.cache, clock=self.clock, max_total_bytes=7)
        self.source.write_bytes(b"1234")
        first = budgeted.store(self.source)
        self.clock.now += 1
        self.source.write_bytes(b"abcde")
        real_read = os.read
        observed = []

        def inspect_then_read(fd: int, count: int) -> bytes:
            if not observed:
                observed.append(first.path.exists())
            return real_read(fd, count)

        with patch("telegram_search_mcp.artifact_store.os.read", side_effect=inspect_then_read):
            budgeted.store(self.source)
        self.assertEqual(observed, [False])

    def test_expired_artifact_is_removed_on_lookup(self) -> None:
        self.source.write_bytes(b"old")
        result = self.store.store(self.source)
        self.clock.now += 12 * 3600
        self.assertIsNone(self.store.lookup(result.artifact_id))
        self.assertFalse(result.path.exists())

    def test_large_expired_artifact_moves_to_private_quarantine(self) -> None:
        self.source.write_bytes(b"old")
        result = self.store.store(self.source)
        self.clock.now += 12 * 3600
        with patch("telegram_search_mcp.artifact_store.LARGE_FILE_THRESHOLD", 2):
            self.assertIsNone(self.store.lookup(result.artifact_id))
        self.assertFalse(result.path.exists())
        quarantined = list(self.store.quarantine_dir.iterdir())
        self.assertEqual(quarantined, [])
        self.assertEqual(self.store.quarantine_dir.stat().st_mode & 0o777, 0o700)

    def test_quarantine_name_collision_preserves_existing_file(self) -> None:
        self.source.write_bytes(b"new")
        result = self.store.store(self.source)
        self.store.quarantine_dir.mkdir(exist_ok=True)
        existing = self.store.quarantine_dir / ("quarantine_" + "a" * 32)
        existing.write_bytes(b"old")
        self.clock.now += 12 * 3600
        with patch("telegram_search_mcp.artifact_store.LARGE_FILE_THRESHOLD", 2):
            with patch("telegram_search_mcp.artifact_store.uuid.uuid4", return_value=SimpleNamespace(hex="a" * 32)):
                with self.assertRaises(ArtifactStoreError):
                    self.store.lookup(result.artifact_id)
        self.assertEqual(existing.read_bytes(), b"old")

    def test_expired_large_artifact_releases_budget_after_original_ttl(self) -> None:
        budgeted = ArtifactStore(cache_dir=self.cache, clock=self.clock, max_total_bytes=7)
        self.source.write_bytes(b"1234")
        first = budgeted.store(self.source)
        self.clock.now += 12 * 3600
        with patch("telegram_search_mcp.artifact_store.LARGE_FILE_THRESHOLD", 2):
            self.assertIsNone(budgeted.lookup(first.artifact_id))
            self.source.write_bytes(b"abcd")
            second = budgeted.store(self.source)
        self.assertEqual(second.size_bytes, 4)
        self.assertEqual(list(budgeted.quarantine_dir.iterdir()), [])

    def test_global_budget_evicts_oldest_artifact(self) -> None:
        budgeted = ArtifactStore(cache_dir=self.cache, clock=self.clock, max_total_bytes=7)
        self.source.write_bytes(b"1234")
        first = budgeted.store(self.source)
        self.clock.now += 1
        self.source.write_bytes(b"abcde")
        second = budgeted.store(self.source)
        self.assertIsNone(budgeted.lookup(first.artifact_id))
        self.assertEqual(budgeted.lookup(second.artifact_id), second)

    def test_unknown_or_malformed_id_never_reads_arbitrary_path(self) -> None:
        self.source.write_bytes(b"private")
        self.assertIsNone(self.store.lookup("../tdlib-source"))
        self.assertIsNone(self.store.lookup("artifact_" + "0" * 32))
        self.assertEqual(self.source.read_bytes(), b"private")

    def test_symlink_cache_directory_is_refused(self) -> None:
        self.source.write_bytes(b"source")
        target = self.base / "other"
        target.mkdir()
        self.cache.symlink_to(target)
        with self.assertRaises(ArtifactStoreError):
            self.store.store(self.source)
        self.assertEqual(list(target.iterdir()), [])

    def test_tampered_cached_bytes_are_not_returned(self) -> None:
        self.source.write_bytes(b"original")
        result = self.store.store(self.source)
        result.path.write_bytes(b"altered!")
        self.assertIsNone(self.store.lookup(result.artifact_id))
        self.assertFalse(result.path.exists())

    def test_hardlinked_cache_lock_is_refused_without_changing_target(self) -> None:
        self.source.write_bytes(b"source")
        target = self.base / "other-file"
        target.write_bytes(b"private")
        target.chmod(0o644)
        self.cache.mkdir()
        os.link(target, self.cache / ".lock")
        with self.assertRaises(ArtifactStoreError):
            self.store.store(self.source)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(target.read_bytes(), b"private")

    def test_symlink_inside_cache_is_refused(self) -> None:
        self.source.write_bytes(b"source")
        self.cache.mkdir()
        (self.cache / "unexpected").symlink_to(self.source)
        with self.assertRaises(ArtifactStoreError):
            self.store.store(self.source)
        self.assertEqual(self.source.read_bytes(), b"source")

    def test_single_artifact_larger_than_global_budget_is_refused(self) -> None:
        self.source.write_bytes(b"12345")
        budgeted = ArtifactStore(cache_dir=self.cache, clock=self.clock, max_total_bytes=4)
        with self.assertRaises(ArtifactStoreError):
            budgeted.store(self.source)
        self.assertEqual(list(self.cache.glob("artifact_*")), [])


if __name__ == "__main__":
    unittest.main()
