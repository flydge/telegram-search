from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from telegram_search_mcp.artifact_store import ArtifactStore
from telegram_search_mcp.outgoing_voice import prepare_voice_note, VoiceError
import telegram_search_mcp.broker as broker_module
from telegram_search_mcp.schemas import PrepareArtifactSendRequest, SendPreparedArtifactRequest
from functools import partial
from unittest.mock import patch


class OutgoingVoiceTests(unittest.TestCase):
    def test_voice_preview_and_send_use_converted_bytes(self) -> None:
        class VoiceSender:
            def __init__(self) -> None:
                self.send_observation_epoch = object()
                self.sent: list[tuple[int, bytes, int, str]] = []

            def get_account_id(self) -> int:
                return 7

            def ensure_ready(self) -> None:
                pass

            def resolve_target(self, target: int) -> dict[str, object]:
                return {"@type": "chat", "id": target, "title": "Recipient", "type": {"@type": "chatTypePrivate"}}

            def send_voice_note_message(self, chat_id: int, path: Path, caption: str,
                                        duration_seconds: int, waveform_base64: str, *, attempt_id=None) -> int:
                self.sent.append((chat_id, path.read_bytes(), duration_seconds, waveform_base64))
                return 901

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "speech.wav"
            subprocess.run(["/opt/homebrew/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=1", str(source)], check=True)
            store = ArtifactStore(cache_dir=root / "cache")
            original = store.store(source, kind="media")
            sender = VoiceSender()
            broker = broker_module.Broker(socket_path=root / "broker.sock", artifact_store=store,
                                          client_factory=lambda: sender, verify_peer_uid=False,
                                          approval_prompt=lambda **_: True)
            preview = broker._prepare_artifact_send(PrepareArtifactSendRequest(
                artifact_id=original.artifact_id, recipient=123, display_name="speech.wav",
                mime_type="audio/wav", kind="voice_note"), client_id="client_" + "a" * 24)
            self.assertEqual((preview.status, preview.kind, preview.mime_type, preview.display_name),
                             ("prepared", "voice_note", "audio/ogg", "speech.ogg"))
            self.assertEqual(preview.source_sha256, original.sha256)
            self.assertGreaterEqual(preview.duration_seconds, 1)
            self.assertEqual(sender.sent, [])
            real_stage = broker_module.stage_approved_document
            real_retire = broker_module.retire_staged_document
            with patch.object(broker_module, "stage_approved_document", side_effect=partial(real_stage, root=root / "staging")), \
                 patch.object(broker_module, "retire_staged_document", side_effect=partial(real_retire, root=root / "staging")):
                receipt = broker._send_prepared_artifact(SendPreparedArtifactRequest(draft_id=preview.draft_id, approved=True), client_id="client_" + "a" * 24)
            self.assertEqual((receipt.status, receipt.message_id), ("sent", 901))
            self.assertEqual(len(sender.sent), 1)
            self.assertEqual((sender.sent[0][0], sender.sent[0][2], sender.sent[0][3]),
                             (123, preview.duration_seconds, preview.waveform_base64))
            self.assertEqual(sender.sent[0][1][:4], b"OggS")
            broker.shutdown()

    def test_wav_is_converted_to_valid_mono_ogg_opus_with_waveform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "speech.wav"
            subprocess.run(["/opt/homebrew/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=1", "-ac", "2", str(source)], check=True)
            store = ArtifactStore(cache_dir=root / "cache")
            original = store.store(source, kind="media")
            prepared = prepare_voice_note(original, store)
            self.assertNotEqual(prepared.artifact.sha256, original.sha256)
            self.assertEqual(prepared.duration_seconds, 1)
            self.assertTrue(prepared.waveform_base64)
            probe = subprocess.run(["/opt/homebrew/bin/ffprobe", "-v", "error", "-select_streams", "a:0",
                                    "-show_entries", "stream=codec_name,channels", "-of", "default=nw=1",
                                    str(prepared.artifact.path)], capture_output=True, text=True, check=True)
            self.assertIn("codec_name=opus", probe.stdout)
            self.assertIn("channels=1", probe.stdout)

    def test_corrupt_audio_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "bad.wav"
            source.write_bytes(b"not audio")
            store = ArtifactStore(cache_dir=root / "cache")
            artifact = store.store(source, kind="media")
            with self.assertRaises(VoiceError):
                prepare_voice_note(artifact, store)
