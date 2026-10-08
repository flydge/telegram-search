from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import telegram_search_mcp.media_analyzer as media_analyzer
from telegram_search_mcp.media_analyzer import TranscriptSegment, analyze_media


class BoundedCaptureTests(unittest.TestCase):
    def test_capture_closes_stdout_after_success_failure_and_overflow(self) -> None:
        real_popen = subprocess.Popen
        processes = []

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        for code, maximum, error in (
            ("print('ok')", 32, None),
            ("raise SystemExit(2)", 32, subprocess.CalledProcessError),
            ("print('x' * 100)", 32, ValueError),
        ):
            with self.subTest(code=code), patch.object(media_analyzer.subprocess, "Popen", side_effect=spawn):
                if error is None:
                    self.assertEqual(media_analyzer._capture_bounded([sys.executable, "-c", code], timeout=5, max_bytes=maximum), b"ok\n")
                else:
                    with self.assertRaises(error):
                        media_analyzer._capture_bounded([sys.executable, "-c", code], timeout=5, max_bytes=maximum)
                self.assertTrue(processes[-1].stdout.closed)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required")
class MediaAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.base = Path(self.sandbox.name)

    def test_playlist_reference_is_rejected_before_ffmpeg(self) -> None:
        path = self.base / "fake.mp3"
        path.write_text("#EXTM3U\nfile:///etc/passwd\n")
        with patch.object(media_analyzer, "_probe", side_effect=AssertionError("probe must not run")):
            result = analyze_media(path, kind="audio")
        self.assertEqual(result.status, "error")

    def test_short_code_switch_uses_separate_language_windows(self) -> None:
        path = self._audio(duration=6)
        cli, model = self._whisper_fixture([])
        def transcribe(_wav: Path, _directory: Path, *, cli: Path, model: Path,
                       start: float, end: float):
            del cli, model
            language = "en" if start < 2 else "ru"
            text = "blue square" if language == "en" else "синий квадрат"
            return language, (TranscriptSegment(start, end, text),)
        with patch.object(media_analyzer, "_transcribe", side_effect=transcribe):
            result = analyze_media(path, kind="audio", whisper_cli=cli, model_path=model)
        self.addCleanup(result.cleanup)
        self.assertEqual(result.language, "multi")
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.transcription_status, "complete")

    def test_uncertain_segment_is_partial_coverage(self) -> None:
        cli, model = self._whisper_fixture([{
            "timestamps": {"from": "00:00:00,000", "to": "00:00:01,000"},
            "text": "unclear speech", "tokens": [{"text": "unclear", "p": 0.1}],
        }])
        result = analyze_media(self._audio(duration=4), kind="audio", whisper_cli=cli, model_path=model)
        self.addCleanup(result.cleanup)
        self.assertEqual(result.status, "partial")
        self.assertTrue(result.segments[0].uncertain)

    def _audio(self, *, duration: int = 8) -> Path:
        path = self.base / "generated.wav"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "sine=frequency=440:sample_rate=16000", "-t", str(duration), "-y", str(path)],
            check=True, timeout=20,
        )
        return path

    def _video(self) -> Path:
        path = self.base / "generated.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc2=size=320x240:rate=2:duration=4", "-f", "lavfi", "-i",
             "sine=frequency=440:sample_rate=16000:duration=4", "-c:v", "mpeg4",
             "-c:a", "aac", "-shortest", "-y", str(path)],
            check=True, timeout=20,
        )
        return path

    def _whisper_fixture(self, transcription: list[dict], *, result: object = None) -> tuple[Path, Path]:
        model = self.base / "synthetic-model.bin"
        model.write_bytes(b"generated fixture")
        cli = self.base / "synthetic-whisper-cli"
        cli.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "base = pathlib.Path(sys.argv[sys.argv.index('-of') + 1])\n"
            f"data = {{'result': {({'language': 'en'} if result is None else result)!r}, 'transcription': {transcription!r}}}\n"
            "base.with_suffix('.json').write_text(json.dumps(data))\n"
        )
        cli.chmod(0o700)
        return cli, model

    def test_missing_model_reports_probe_and_exact_window_without_transcription(self) -> None:
        path = self._audio()

        result = analyze_media(path, kind="audio", start_seconds=2.0,
                               model_path=self.base / "missing-model.bin")

        self.assertEqual(result.status, "transcription_unavailable")
        self.assertAlmostEqual(result.duration_seconds, 8.0, places=2)
        self.assertEqual((result.window_start_seconds, result.window_end_seconds), (2.0, 8.0))
        self.assertEqual(result.transcription_status, "unavailable")
        self.assertIsNone(result.transcribed_start_seconds)
        self.assertIsNone(result.transcribed_end_seconds)
        self.assertEqual(result.segments, ())
        self.assertEqual(result.frames, ())
        self.assertTrue(any(stream.codec_type == "audio" for stream in result.streams))

    def test_generated_video_samples_at_most_twelve_private_timestamped_frames(self) -> None:
        path = self._video()

        result = analyze_media(path, kind="video", model_path=self.base / "missing-model.bin")
        self.addCleanup(result.cleanup)

        self.assertEqual(result.status, "transcription_unavailable")
        self.assertEqual(result.frame_status, "complete")
        self.assertGreaterEqual(len(result.frames), 1)
        self.assertLessEqual(len(result.frames), 12)
        self.assertTrue(any(stream.codec_type == "video" for stream in result.streams))
        for frame in result.frames:
            self.assertTrue(0 <= frame.time_seconds < result.window_end_seconds)
            self.assertTrue(frame.path.is_file())
            self.assertNotEqual(frame.path.parent, path.parent)
            self.assertEqual(frame.path.stat().st_mode & 0o777, 0o600)
        private_dir = result.temp_directory
        result.cleanup()
        self.assertFalse(private_dir.exists())

    def test_scene_extraction_closes_its_diagnostic_pipe(self) -> None:
        path = self._video()
        real_popen = subprocess.Popen
        processes = []

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(media_analyzer.subprocess, "Popen", side_effect=spawn):
            media_analyzer._scene_frames(path, self.base, 0, 4, "mov")
        self.assertGreaterEqual(len(processes), 1)
        self.assertTrue(processes[0].stderr.closed)

    def test_timestamped_whisper_segments_are_offset_into_source_window(self) -> None:
        path = self._audio(duration=4)
        model = self.base / "synthetic-model.bin"
        model.write_bytes(b"generated fixture")
        cli = self.base / "synthetic-whisper-cli"
        cli.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "base = pathlib.Path(sys.argv[sys.argv.index('-of') + 1])\n"
            "data = {'result': {'language': 'en'}, 'transcription': ["
            "{'timestamps': {'from': '00:00:00,000', 'to': '00:00:01,250'}, 'text': ' hello '},"
            "{'timestamps': {'from': '00:00:01,250', 'to': '00:00:02,000'}, 'text': '[unintelligible]'}]}\n"
            "base.with_suffix('.json').write_text(json.dumps(data))\n"
        )
        cli.chmod(0o700)

        result = analyze_media(path, kind="voice_note", start_seconds=2.0,
                               whisper_cli=cli, model_path=model)
        self.addCleanup(result.cleanup)

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.transcription_status, "complete")
        self.assertEqual((result.transcribed_start_seconds, result.transcribed_end_seconds), (2.0, 4.0))
        self.assertEqual([(s.start_seconds, s.end_seconds) for s in result.segments],
                         [(2.0, 3.25), (3.25, 4.0)])
        self.assertEqual(result.segments[0].text, "hello")
        self.assertFalse(result.segments[0].uncertain)
        self.assertTrue(result.segments[1].uncertain)

    def test_symlink_and_oversized_input_are_rejected_before_probe(self) -> None:
        path = self.base / "oversized.bin"
        path.write_bytes(b"generated fixture")
        link = self.base / "link.wav"
        link.symlink_to(path)

        with patch.object(media_analyzer, "MAX_SOURCE_BYTES", 3):
            for candidate in (path, link):
                with self.subTest(candidate=candidate):
                    result = analyze_media(candidate, kind="audio", model_path=self.base / "missing")
                    self.assertEqual(result.status, "error")
                    self.assertIsNone(result.duration_seconds)
                    self.assertEqual(result.frames, ())

    def test_low_confidence_token_marks_segment_uncertain(self) -> None:
        cli, model = self._whisper_fixture([{
            "timestamps": {"from": "00:00:00,000", "to": "00:00:01,000"},
            "text": "hello", "tokens": [{"text": " hello", "p": 0.12}],
        }])

        result = analyze_media(self._audio(duration=4), kind="audio", whisper_cli=cli, model_path=model)
        self.addCleanup(result.cleanup)

        self.assertEqual(len(result.segments), 1)
        self.assertTrue(result.segments[0].uncertain)

    def test_out_of_window_whisper_segment_is_ignored(self) -> None:
        cli, model = self._whisper_fixture([{
            "timestamps": {"from": "00:00:06,100", "to": "00:00:06,200"},
            "text": "late", "tokens": [],
        }])

        result = analyze_media(self._audio(), kind="audio", start_seconds=2.0,
                               whisper_cli=cli, model_path=model)
        self.addCleanup(result.cleanup)

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.segments, ())

    def test_invalid_whisper_result_does_not_escape_as_exception(self) -> None:
        cli, model = self._whisper_fixture([], result="invalid")

        result = analyze_media(self._audio(), kind="audio", whisper_cli=cli, model_path=model)
        self.addCleanup(result.cleanup)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.transcription_status, "error")
        self.assertIsNone(result.transcribed_start_seconds)


if __name__ == "__main__":
    unittest.main()
