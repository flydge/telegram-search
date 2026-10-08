from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

import fitz

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.schemas import CreateLocalArtifactRequest, ReadAttachmentRequest


class CreatedArtifactReadTests(unittest.TestCase):
    def test_synthetic_pdf_round_trips_through_broker_artifact_and_reader(self) -> None:
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Synthetic local report")
        content = document.tobytes()
        document.close()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            broker = Broker(
                socket_path=root / "broker.sock",
                artifact_store=ArtifactStore(cache_dir=root / "artifacts"),
                verify_peer_uid=False,
            )
            created = broker._create_local_artifact(CreateLocalArtifactRequest(
                file_name="report.pdf", content_base64=base64.b64encode(content).decode("ascii"),
            ))
            self.assertEqual(created.status, "complete")
            read = broker._read_attachment(ReadAttachmentRequest(artifact_id=created.artifact_id))
            self.assertEqual(read.status, "complete")
            self.assertIn("Synthetic local report", read.text)
            self.assertEqual(len(read.images), 1)
            self.assertIsNone(read.source_anchor)
            broker.shutdown()


if __name__ == "__main__":
    unittest.main()
