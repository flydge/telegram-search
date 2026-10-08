from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from telegram_search_mcp.artifact_store import ArtifactStore, StoredArtifact
from telegram_search_mcp.outgoing_drafts import DraftError, DraftOwner, OutgoingDraftRegistry


OWNER = DraftOwner(client_id="client_" + "a" * 24, account_id=7)

class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class OutgoingDraftTests(unittest.TestCase):
    def test_media_cached_over_document_limit_cannot_be_prepared_as_document(self) -> None:
        class OversizedStore:
            def lookup(self, artifact_id: str) -> StoredArtifact:
                return StoredArtifact(artifact_id=artifact_id, path=Path("/unused"),
                                      sha256="a" * 64, size_bytes=64 * 1024 * 1024 + 1,
                                      expires_at=1_700_000_900.0)

        registry = OutgoingDraftRegistry(OversizedStore(), clock=lambda: 1_700_000_000.0)
        with self.assertRaises(DraftError):
            registry.prepare(owner=OWNER, artifact_id="artifact-test", recipient=123, display_name="large.bin",
                             mime_type="application/octet-stream", caption="", kind="document")

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.clock = Clock()
        self.store = ArtifactStore(cache_dir=self.base / "cache", clock=self.clock)
        self.source = self.base / "source"
        self.source.write_bytes(b"outgoing bytes")
        self.artifact = self.store.store(self.source)
        self.registry = OutgoingDraftRegistry(self.store, clock=self.clock)

    def prepare(self):
        return self.registry.prepare(owner=OWNER,
            artifact_id=self.artifact.artifact_id,
            recipient=-100123,
            display_name="folder\\report.txt",
            mime_type="text/plain",
            caption="Please review\nthis file",
        )

    def test_prepare_records_exact_artifact_snapshot_and_safe_metadata(self) -> None:
        draft = self.prepare()
        self.assertEqual(draft.artifact_id, self.artifact.artifact_id)
        self.assertEqual(draft.sha256, self.artifact.sha256)
        self.assertEqual(draft.size_bytes, self.artifact.size_bytes)
        self.assertEqual(draft.display_name, "report.txt")
        self.assertEqual(draft.mime_type, "text/plain")
        self.assertEqual(draft.caption, "Please review\nthis file")
        self.assertEqual(draft.recipient, -100123)
        self.assertEqual(draft.expires_at, self.clock.now + 15 * 60)
        self.assertTrue(draft.approval_required)
        self.assertFalse(hasattr(draft, "path"))

    def test_unknown_artifact_cannot_be_prepared(self) -> None:
        with self.assertRaises(DraftError):
            self.registry.prepare(owner=OWNER,
                artifact_id="artifact_" + "0" * 32,
                recipient=-100123,
                display_name="report.txt",
                mime_type="text/plain",
                caption="",
            )

    def test_claim_requires_approval_and_is_single_use(self) -> None:
        draft = self.prepare()
        with self.assertRaises(DraftError):
            self.registry.claim(draft.draft_id, owner=OWNER, approved=False)
        claimed = self.registry.claim(draft.draft_id, owner=OWNER, approved=True)
        self.assertEqual(claimed.draft, draft)
        self.assertEqual(claimed.path, self.artifact.path)
        with self.assertRaises(DraftError):
            self.registry.claim(draft.draft_id, owner=OWNER, approved=True)

    def test_expired_draft_cannot_be_claimed(self) -> None:
        draft = self.prepare()
        self.clock.now += 15 * 60
        with self.assertRaises(DraftError):
            self.registry.claim(draft.draft_id, owner=OWNER, approved=True)

    def test_changed_artifact_cannot_be_claimed(self) -> None:
        draft = self.prepare()
        self.artifact.path.write_bytes(b"tampered bytes")
        with self.assertRaises(DraftError):
            self.registry.claim(draft.draft_id, owner=OWNER, approved=True)
        with self.assertRaises(DraftError):
            self.registry.claim(draft.draft_id, owner=OWNER, approved=True)

    def test_only_one_concurrent_claim_can_start_send(self) -> None:
        draft = self.prepare()
        start = threading.Barrier(3)

        def claim() -> bool:
            start.wait()
            try:
                self.registry.claim(draft.draft_id, owner=OWNER, approved=True)
            except DraftError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(claim)
            second = executor.submit(claim)
            start.wait()
            self.assertEqual([first.result(), second.result()].count(True), 1)

    def test_finish_records_outcome_unknown_and_repeats_same_receipt(self) -> None:
        draft = self.prepare()
        self.registry.claim(draft.draft_id, owner=OWNER, approved=True)
        receipt = self.registry.finish(draft.draft_id, owner=OWNER, status="outcome_unknown")
        self.assertEqual(receipt.status, "outcome_unknown")
        self.assertEqual(receipt.draft_id, draft.draft_id)
        self.assertIsNone(receipt.message_id)
        self.assertEqual(
            self.registry.finish(draft.draft_id, owner=OWNER, status="sent", message_id=10), receipt
        )
        with self.assertRaises(DraftError):
            self.registry.claim(draft.draft_id, owner=OWNER, approved=True)

    def test_finish_requires_claim_and_valid_receipt_status(self) -> None:
        draft = self.prepare()
        with self.assertRaises(DraftError):
            self.registry.finish(draft.draft_id, owner=OWNER, status="sent", message_id=10)
        self.registry.claim(draft.draft_id, owner=OWNER, approved=True)
        with self.assertRaises(ValueError):
            self.registry.finish(draft.draft_id, owner=OWNER, status="retry")
        receipt = self.registry.finish(draft.draft_id, owner=OWNER, status="sent", message_id=10)
        self.assertEqual((receipt.status, receipt.message_id), ("sent", 10))

    def test_capacity_refuses_new_draft_without_dropping_receipt(self) -> None:
        limited = OutgoingDraftRegistry(self.store, clock=self.clock, capacity=1)
        draft = limited.prepare(owner=OWNER,
            artifact_id=self.artifact.artifact_id,
            recipient=123,
            display_name="x",
            mime_type="text/plain",
            caption="",
        )
        limited.claim(draft.draft_id, owner=OWNER, approved=True)
        receipt = limited.finish(draft.draft_id, owner=OWNER, status="failed")
        with self.assertRaises(DraftError):
            limited.prepare(owner=OWNER,
                artifact_id=self.artifact.artifact_id,
                recipient=123,
                display_name="x",
                mime_type="text/plain",
                caption="",
            )
        self.assertEqual(limited.finish(draft.draft_id, owner=OWNER, status="failed"), receipt)


if __name__ == "__main__":
    unittest.main()
