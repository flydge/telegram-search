from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from telegram_search_mcp.attachment_service import get_attachment, get_message_context, select_attachment
from telegram_search_mcp.schemas import AttachmentRequest, EvidenceAnchor, MessageContextRequest
from telegram_search_mcp.sanitize import render_evidence


class AttachmentSelectionTests(unittest.TestCase):
    def test_voice_note_selects_original_voice_file(self) -> None:
        message = {
            "content": {
                "@type": "messageVoiceNote",
                "voice_note": {
                    "mime_type": "audio/ogg",
                    "voice": {"@type": "file", "id": 42, "size": 123},
                },
            }
        }

        selected = select_attachment(message)

        self.assertEqual(selected.file_id, 42)
        self.assertEqual(selected.kind, "voice_note")
        self.assertEqual(selected.mime_type, "audio/ogg")

    def test_photo_chooses_largest_original_available_size(self) -> None:
        message = {
            "content": {
                "@type": "messagePhoto",
                "photo": {"sizes": [
                    {"photo": {"@type": "file", "id": 1, "size": 100}},
                    {"photo": {"@type": "file", "id": 2, "size": 200}},
                ]},
            }
        }

        selected = select_attachment(message)

        self.assertEqual(selected.file_id, 2)
        self.assertEqual(selected.kind, "photo")

    def test_rejects_unsupported_or_invalid_file(self) -> None:
        with self.assertRaises(ValueError):
            select_attachment({"content": {"@type": "messageText"}})
        with self.assertRaises(ValueError):
            select_attachment({"content": {"@type": "messageDocument", "document": {"document": {"id": True}}}})

    def test_zero_size_uses_expected_size_for_preflight(self) -> None:
        selected = select_attachment({"content": {"@type": "messageDocument", "document": {
            "file_name": "notes.txt", "document": {"@type": "file", "id": 3, "size": 0,
                                                   "expected_size": 70 * 1024 * 1024},
        }}})
        self.assertEqual(selected.size_bytes, 70 * 1024 * 1024)


class AttachmentRetrievalTests(unittest.TestCase):
    def test_short_history_page_reports_partial_coverage(self) -> None:
        class FakeClient:
            def ensure_ready(self) -> None: pass
            def resolve_target(self, chat_id: int) -> dict:
                return {"id": chat_id, "type": {"@type": "chatTypePrivate"}}
            def get_message(self, chat_id: int, message_id: int) -> dict:
                return {"@type": "message", "chat_id": chat_id, "id": message_id,
                        "date": 1700000000, "content": {"@type": "messageText", "text": {"text": "anchor"}}}
            def get_context_messages(self, *_: object) -> list[dict]:
                return []
            def get_sender_name(self, _: dict) -> str:
                return "Synthetic"

        response = get_message_context(
            FakeClient(), MessageContextRequest(anchor=EvidenceAnchor(chat_id=7, message_id=8), radius=2)
        )
        self.assertEqual(response.status, "partial")
        self.assertFalse(response.coverage_complete)

    def test_context_rechecks_anchor_and_returns_bounded_sanitized_messages(self) -> None:
        class FakeClient:
            def ensure_ready(self) -> None: pass
            def resolve_target(self, chat_id: int) -> dict:
                return {"id": chat_id, "type": {"@type": "chatTypePrivate"}}
            def get_message(self, chat_id: int, message_id: int) -> dict:
                return {"@type": "message", "chat_id": chat_id, "id": message_id,
                        "date": 1700000000, "content": {"@type": "messageText", "text": {"text": "anchor"}}}
            def get_context_messages(self, chat_id: int, message_id: int, radius: int) -> list[dict]:
                assert (chat_id, message_id, radius) == (7, 8, 1)
                return [{"@type": "message", "chat_id": 7, "id": 7, "date": 1699999999,
                         "content": {"@type": "messageText", "text": {"text": "previous"}}},
                        {"@type": "message", "chat_id": 7, "id": 9, "date": 1700000001,
                         "content": {"@type": "messageText", "text": {"text": "next"}}}]
            def get_sender_name(self, _: dict) -> str:
                return "Synthetic"

        response = get_message_context(
            FakeClient(), MessageContextRequest(anchor=EvidenceAnchor(chat_id=7, message_id=8), radius=1)
        )

        self.assertEqual(response.status, "complete")
        self.assertEqual([item.message_id for item in response.messages], [7, 8, 9])
        self.assertEqual(response.messages[1].snippet, render_evidence("anchor"))

    def test_oversized_document_is_rejected_before_download(self) -> None:
        class FakeClient:
            def ensure_ready(self) -> None: pass
            def resolve_target(self, chat_id: int) -> dict:
                return {"@type": "chat", "id": chat_id, "type": {"@type": "chatTypePrivate"}}
            def get_message(self, chat_id: int, message_id: int) -> dict:
                return {"@type": "message", "chat_id": chat_id, "id": message_id, "content": {
                    "@type": "messageDocument", "document": {
                        "file_name": "large.pdf", "mime_type": "application/pdf",
                        "document": {"@type": "file", "id": 1, "size": 65 * 1024 * 1024},
                    },
                }}
            def download_file(self, *_: object, **__: object) -> None:
                raise AssertionError("oversized file was downloaded")

        request = AttachmentRequest(anchor=EvidenceAnchor(chat_id=7, message_id=8))
        response = get_attachment(FakeClient(), object(), request)

        self.assertEqual(response.status, "too_large")
        self.assertEqual(response.coverage, "none")

    def test_exact_anchor_binds_downloaded_file_to_artifact(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source_path = Path(temporary.name) / "tdlib-file"
        source_path.write_bytes(b"data")
        class FakeClient:
            def ensure_ready(self) -> None: pass
            def resolve_target(self, chat_id: int) -> dict:
                return {"@type": "chat", "id": chat_id, "type": {"@type": "chatTypePrivate"}}
            def get_message(self, chat_id: int, message_id: int) -> dict:
                return {"@type": "message", "chat_id": chat_id, "id": message_id, "content": {
                    "@type": "messageDocument", "document": {
                        "file_name": "notes.txt", "mime_type": "text/plain",
                        "document": {"@type": "file", "id": 71, "size": 4},
                    },
                }}
            def download_file(self, file_id: int, **_: object) -> Path:
                assert file_id == 71
                return source_path
        class FakeStore:
            def store(self, source_path: Path, *, kind: str) -> object:
                assert source_path == Path(temporary.name) / "tdlib-file"
                assert kind == "document"
                return type("Artifact", (), {
                    "artifact_id": "artifact_123", "path": Path("/tmp/synthetic-artifact"),
                    "sha256": "a" * 64, "size_bytes": 4,
                    "expires_at": datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp(),
                })()

        request = AttachmentRequest(anchor=EvidenceAnchor(chat_id=7, message_id=8))
        response = get_attachment(FakeClient(), FakeStore(), request, source_root=Path(temporary.name))

        self.assertEqual(response.status, "complete")
        self.assertEqual(response.anchor, request.anchor)
        self.assertEqual(response.artifact_id, "artifact_123")
