from __future__ import annotations

import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import telegram_search_mcp.broker as broker_module
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.schemas import PrepareArtifactSendRequest, SendPreparedArtifactRequest
from telegram_search_mcp.tdjson import MessageSendFailed


class FakeSender:
    def __init__(self) -> None:
        self.send_observation_epoch = object()
        self.sent: list[tuple[int, Path, str]] = []

    def get_account_id(self) -> int:
        return 7

    def ensure_ready(self) -> None:
        pass

    def resolve_target(self, target: int) -> dict[str, object]:
        return {"@type": "chat", "id": target, "title": "Synthetic Recipient",
                "type": {"@type": "chatTypePrivate"}}

    def send_document_message(self, recipient: int, path: Path, caption: str, *, attempt_id=None) -> int:
        self.sent.append((recipient, path, caption))
        if path.name != "report.txt" or path.read_bytes() != b"reviewed artifact":
            raise AssertionError("outgoing bytes or file name changed")
        return 555


class BrokerOutgoingTests(unittest.TestCase):
    def test_safe_provider_failure_reaches_receipt_without_second_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            source = root / "source.txt"
            source.write_bytes(b"reviewed artifact")
            store = ArtifactStore(cache_dir=root / "artifacts")
            artifact = store.store(source)
            sender = FakeSender()
            broker = broker_module.Broker(
                socket_path=root / "broker.sock", artifact_store=store,
                client_factory=lambda: sender, verify_peer_uid=False,
                approval_prompt=lambda **_: True,
            )
            preview = broker._prepare_artifact_send(PrepareArtifactSendRequest(
                artifact_id=artifact.artifact_id, recipient=123,
                display_name="report.txt", mime_type="text/plain", caption="",
            ), client_id="client_" + "a" * 24)
            failure = MessageSendFailed("rejected", provider_error={
                "code": 400, "message": "FILE_PARTS_INVALID",
            })
            request = SendPreparedArtifactRequest(draft_id=preview.draft_id, approved=True)
            with patch.object(broker_module, "stage_approved_document", return_value=source), \
                 patch.object(broker_module, "retire_staged_document"), \
                 patch.object(sender, "send_document_message", side_effect=failure) as send:
                result = broker._send_prepared_artifact(request, client_id="client_" + "a" * 24)
                repeated = broker._send_prepared_artifact(request, client_id="client_" + "a" * 24)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.detail, "TDLib rejected the send (code 400)")
            self.assertIsNone(result.message_id)
            self.assertEqual(repeated.status, "failed")
            self.assertEqual(send.call_count, 1)
            broker.shutdown()

    def test_prepare_has_no_provider_send_and_approval_consumes_draft_once(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            source = root / "source.txt"
            source.write_bytes(b"reviewed artifact")
            store = ArtifactStore(cache_dir=root / "artifacts")
            artifact = store.store(source)
            sender = FakeSender()
            broker = broker_module.Broker(
                socket_path=root / "broker.sock", artifact_store=store,
                client_factory=lambda: sender, verify_peer_uid=False,
                approval_prompt=lambda **_: True,
            )
            preview = broker._prepare_artifact_send(PrepareArtifactSendRequest(
                artifact_id=artifact.artifact_id, recipient=123,
                display_name="report.txt", mime_type="text/plain", caption="Reviewed",
            ), client_id="client_" + "a" * 24)
            self.assertEqual(preview.status, "prepared")
            self.assertEqual(preview.recipient_title, "Synthetic Recipient")
            self.assertEqual(preview.sha256, artifact.sha256)
            self.assertEqual(sender.sent, [])
            denied = broker._send_prepared_artifact(SendPreparedArtifactRequest(
                draft_id=preview.draft_id, approved=False,
            ), client_id="client_" + "a" * 24)
            self.assertEqual(denied.status, "not_approved")
            self.assertEqual(sender.sent, [])
            broker._approval_prompt = lambda **_: False
            locally_denied = broker._send_prepared_artifact(SendPreparedArtifactRequest(
                draft_id=preview.draft_id, approved=True,
            ), client_id="client_" + "a" * 24)
            self.assertEqual(locally_denied.status, "not_approved")
            self.assertEqual(sender.sent, [])
            broker._approval_prompt = lambda **_: True
            with patch.object(broker_module, "stage_approved_document",
                              side_effect=partial(broker_module.stage_approved_document,
                                                  root=root / "outgoing")), \
                 patch.object(broker_module, "retire_staged_document",
                              side_effect=partial(broker_module.retire_staged_document,
                                                  root=root / "outgoing")):
                sent = broker._send_prepared_artifact(SendPreparedArtifactRequest(
                    draft_id=preview.draft_id, approved=True,
                ), client_id="client_" + "a" * 24)
            self.assertEqual(sent.status, "sent")
            self.assertEqual(sent.message_id, 555)
            self.assertEqual(len(sender.sent), 1)
            repeated = broker._send_prepared_artifact(SendPreparedArtifactRequest(
                draft_id=preview.draft_id, approved=True,
            ), client_id="client_" + "a" * 24)
            self.assertEqual(repeated.status, "sent")
            self.assertIn("no new send", repeated.detail)
            self.assertEqual(len(sender.sent), 1)
            broker.shutdown()


if __name__ == "__main__":
    unittest.main()
