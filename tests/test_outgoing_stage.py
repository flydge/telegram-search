from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.outgoing_drafts import DraftOwner, OutgoingDraftRegistry
from telegram_search_mcp.outgoing_stage import retire_staged_document, stage_approved_document


OWNER = DraftOwner(client_id="client_" + "a" * 24, account_id=7)

class OutgoingStageTests(unittest.TestCase):
    def test_large_finished_copy_moves_to_private_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            source = root / "source"
            source.write_bytes(b"12345")
            store = ArtifactStore(cache_dir=root / "artifacts")
            artifact = store.store(source)
            registry = OutgoingDraftRegistry(store)
            draft = registry.prepare(owner=OWNER,
                artifact_id=artifact.artifact_id, recipient=123,
                display_name="large.pdf", mime_type="application/pdf", caption="",
            )
            with patch("telegram_search_mcp.outgoing_stage.LARGE_FILE_THRESHOLD", 4):
                staged = stage_approved_document(registry.claim(draft.draft_id, owner=OWNER, approved=True),
                                                 root=root / "outgoing")
                retire_staged_document(staged, root=root / "outgoing")
            self.assertFalse(staged.exists())
            quarantined = list((root / "outgoing-quarantine").iterdir())
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].stat().st_size, 5)

    def test_staged_copy_has_reviewed_name_and_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            source = root / "source"
            source.write_bytes(b"local generated content")
            store = ArtifactStore(cache_dir=root / "artifacts")
            artifact = store.store(source)
            registry = OutgoingDraftRegistry(store)
            draft = registry.prepare(owner=OWNER,
                artifact_id=artifact.artifact_id, recipient=123,
                display_name="report.txt", mime_type="text/plain", caption="caption",
            )
            claim = registry.claim(draft.draft_id, owner=OWNER, approved=True)
            staged = stage_approved_document(claim, root=root / "outgoing")
            self.assertEqual(staged.name, "report.txt")
            self.assertEqual(staged.read_bytes(), b"local generated content")
            self.assertNotEqual(staged, artifact.path)
            retire_staged_document(staged, root=root / "outgoing")
            self.assertFalse(staged.exists())


if __name__ == "__main__":
    unittest.main()
