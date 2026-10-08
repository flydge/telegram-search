from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker


class AttachmentBrokerTests(unittest.TestCase):
    def test_exact_anchor_returns_private_snapshot_over_broker_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tdlib" / "source"
            source.parent.mkdir()
            source.write_bytes(b"hello")

            class FakeClient:
                def ensure_ready(self) -> None: pass
                def resolve_target(self, chat_id: int) -> dict:
                    return {"@type": "chat", "id": chat_id, "type": {"@type": "chatTypePrivate"}}
                def get_message(self, chat_id: int, message_id: int) -> dict:
                    return {"@type": "message", "chat_id": chat_id, "id": message_id, "content": {
                        "@type": "messageDocument", "document": {
                            "file_name": "notes.txt", "mime_type": "text/plain",
                            "document": {"@type": "file", "id": 12, "size": 5},
                        },
                    }}
                def download_file(self, file_id: int, **_: object) -> Path:
                    assert file_id == 12
                    return source
                def close(self) -> None: pass

            broker = Broker(
                socket_path=root / "broker.sock", client_factory=FakeClient,
                artifact_store=ArtifactStore(cache_dir=root / "artifacts"),
                download_source_root=source.parent,
            )
            result = broker._dispatch({
                "operation": "get_attachment", "client_id": "client_" + "a" * 24,
                "payload": {"anchor": {"chat_id": 7, "message_id": 8}},
                "deadline": time.monotonic() + 10, "broker_generation": broker._generation,
            })

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["anchor"], {"chat_id": 7, "message_id": 8})
            self.assertEqual(Path(result["artifact_path"]).read_bytes(), b"hello")
            self.assertEqual(result["size_bytes"], 5)
            read = broker._dispatch({
                "operation": "read_attachment", "client_id": "client_" + "a" * 24,
                "payload": {"artifact_id": result["artifact_id"], "max_chars": 100, "max_pages": 1},
                "deadline": time.monotonic() + 10, "broker_generation": broker._generation,
            })
            self.assertEqual(read["status"], "complete")
            self.assertEqual(read["text"], "hello")
            self.assertEqual(read["processed_bytes"], 5)
