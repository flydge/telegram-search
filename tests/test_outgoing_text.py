from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.broker import Broker
from telegram_search_mcp.schemas import PrepareTextSendRequest, SendPreparedTextRequest, SendPreparedArtifactRequest
from telegram_search_mcp.outgoing_drafts import OutgoingDraftRegistry


class TextSender:
    def __init__(self) -> None:
        self.send_observation_epoch = object()
        self.calls: list[tuple[int, str]] = []

    def get_account_id(self) -> int:
        return 7

    def ensure_ready(self) -> None:
        pass

    def resolve_target(self, target: int) -> dict[str, object]:
        return {"@type": "chat", "id": target, "title": "Recipient", "type": {"@type": "chatTypePrivate"}}

    def send_text_message(self, chat_id: int, text: str, *, attempt_id=None) -> int:
        self.calls.append((chat_id, text))
        return 701


class OutgoingTextTests(unittest.TestCase):
    def test_expired_text_recipient_titles_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ArtifactStore(cache_dir=root / "cache")
            now = [1000.0]
            broker = Broker(socket_path=root / "broker.sock", artifact_store=store,
                            client_factory=TextSender, verify_peer_uid=False)
            broker._drafts = OutgoingDraftRegistry(store, clock=lambda: now[0])
            first = broker._prepare_text_send(PrepareTextSendRequest(recipient=123, text="First"), client_id="client_" + "a" * 24)
            now[0] += 901
            second = broker._prepare_text_send(PrepareTextSendRequest(recipient=123, text="Second"), client_id="client_" + "a" * 24)
            self.assertNotIn(first.draft_id, broker._draft_recipient_titles)
            self.assertIn(second.draft_id, broker._draft_recipient_titles)
            broker.shutdown()

    def test_text_preview_is_complete_and_one_approved_attempt_is_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sender = TextSender()
            broker = Broker(socket_path=root / "broker.sock", artifact_store=ArtifactStore(cache_dir=root / "cache"),
                            client_factory=lambda: sender, verify_peer_uid=False, approval_prompt=lambda **_: True)
            preview = broker._prepare_text_send(PrepareTextSendRequest(recipient=123, text="Hello\nTelegram"), client_id="client_" + "a" * 24)
            self.assertEqual((preview.status, preview.recipient, preview.recipient_title, preview.text),
                             ("prepared", 123, "Recipient", "Hello\nTelegram"))
            self.assertEqual(sender.calls, [])
            denied = broker._send_prepared_text(SendPreparedTextRequest(draft_id=preview.draft_id, approved=False), client_id="client_" + "a" * 24)
            self.assertEqual(denied.status, "not_approved")
            self.assertEqual(sender.calls, [])
            sent = broker._send_prepared_text(SendPreparedTextRequest(draft_id=preview.draft_id, approved=True), client_id="client_" + "a" * 24)
            self.assertEqual((sent.status, sent.message_id), ("sent", 701))
            repeated = broker._send_prepared_text(SendPreparedTextRequest(draft_id=preview.draft_id, approved=True), client_id="client_" + "a" * 24)
            self.assertEqual((repeated.status, repeated.message_id), ("sent", 701))
            wrong_tool = broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=preview.draft_id, approved=True), client_id="client_" + "a" * 24)
            self.assertEqual(wrong_tool.status, "error")
            self.assertEqual(sender.calls, [(123, "Hello\nTelegram")])
            broker.shutdown()


if __name__ == "__main__":
    unittest.main()
