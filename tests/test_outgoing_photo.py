from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import telegram_search_mcp.broker as broker_module
from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.schemas import PrepareArtifactSendRequest, SendPreparedArtifactRequest


class PhotoSender:
    def __init__(self) -> None:
        self.send_observation_epoch = object()
        self.sent: list[tuple[int, bytes, str]] = []

    def get_account_id(self) -> int:
        return 7

    def ensure_ready(self) -> None:
        pass

    def resolve_target(self, target: int) -> dict[str, object]:
        return {"@type": "chat", "id": target, "title": "Recipient", "type": {"@type": "chatTypePrivate"}}

    def send_photo_message(self, chat_id: int, path: Path, caption: str, *, attempt_id=None) -> int:
        self.sent.append((chat_id, path.read_bytes(), caption))
        return 811


class OutgoingPhotoTests(unittest.TestCase):
    def test_real_png_is_prepared_as_photo_and_one_attempt_is_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = Image.new("RGB", (20, 10), "red")
            payload = io.BytesIO()
            image.save(payload, format="PNG")
            source = root / "picture.png"
            source.write_bytes(payload.getvalue())
            store = ArtifactStore(cache_dir=root / "cache")
            artifact = store.store(source)
            sender = PhotoSender()
            broker = broker_module.Broker(socket_path=root / "broker.sock", artifact_store=store,
                                          client_factory=lambda: sender, verify_peer_uid=False,
                                          approval_prompt=lambda **_: True)
            request = PrepareArtifactSendRequest(artifact_id=artifact.artifact_id, recipient=123,
                                                display_name="picture.png", mime_type="image/png",
                                                caption="Picture", kind="photo")
            preview = broker._prepare_artifact_send(request, client_id="client_" + "a" * 24)
            self.assertEqual((preview.status, preview.kind), ("prepared", "photo"))
            self.assertEqual(sender.sent, [])
            # The real staging helper is exercised against a private temporary root.
            from functools import partial
            real_stage = broker_module.stage_approved_document
            real_retire = broker_module.retire_staged_document
            with patch.object(broker_module, "stage_approved_document", side_effect=partial(real_stage, root=root / "staging")), \
                 patch.object(broker_module, "retire_staged_document", side_effect=partial(real_retire, root=root / "staging")):
                result = broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=preview.draft_id, approved=True), client_id="client_" + "a" * 24)
            self.assertEqual((result.status, result.message_id), ("sent", 811))
            self.assertEqual(sender.sent, [(123, payload.getvalue(), "Picture")])
            broker.shutdown()

    def test_corrupt_image_and_wrong_mime_are_rejected_before_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "picture.png"
            source.write_bytes(b"not a png")
            store = ArtifactStore(cache_dir=root / "cache")
            artifact = store.store(source)
            broker = broker_module.Broker(socket_path=root / "broker.sock", artifact_store=store,
                                          client_factory=PhotoSender, verify_peer_uid=False)
            for mime in ("image/png", "image/jpeg"):
                response = broker._prepare_artifact_send(PrepareArtifactSendRequest(
                    artifact_id=artifact.artifact_id, recipient=123, display_name="picture.png",
                    mime_type=mime, kind="photo"), client_id="client_" + "a" * 24)
                self.assertEqual(response.status, "error")
            broker.shutdown()
