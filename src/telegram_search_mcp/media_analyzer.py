"""Bounded local analysis of a previously authorized media artifact.

The caller resolves the artifact ID and authorizes its owner-owned path. This
module never accepts Telegram identifiers, downloads content, or calls a cloud
service. Generated WAV and image files remain in a private temporary directory
until the caller calls ``MediaAnalysis.cleanup`` after transport/cache storage.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import APP_SUPPORT_ROOT


MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_WINDOW_SECONDS = 300.0
MAX_FRAMES = 12
DEFAULT_MODEL_PATH = APP_SUPPORT_ROOT / "models" / "ggml-small.bin"
FFMPEG = Path("/opt/homebrew/bin/ffmpeg")
FFPROBE = Path("/opt/homebrew/bin/ffprobe")
WHISPER_CLI = Path("/opt/homebrew/bin/whisper-cli")
MediaKind = Literal["audio", "voice_note", "video", "video_note"]
AnalysisStatus = Literal[
    "complete", "partial", "transcription_unavailable", "unsupported", "error"
]
TranscriptionStatus = Literal["complete", "partial", "unavailable", "no_audio", "error", "not_attempted"]
FrameStatus = Literal["complete", "partial", "unavailable", "not_applicable"]

_SHOWINFO_TIME = re.compile(r"\bpts_time:([0-9]+(?:\.[0-9]+)?)")
_UNCERTAIN = re.compile(r"\b(?:unintelligible|inaudible|unclear)\b|\?{3,}", re.I)


@dataclass(frozen=True)
class MediaStream:
    index: int
    codec_type: str
    codec_name: str | None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str
    uncertain: bool = False


@dataclass(frozen=True)
class MediaFrame:
    time_seconds: float
    path: Path
    source: Literal["time", "scene"]


@dataclass(frozen=True)
class MediaAnalysis:
    status: AnalysisStatus
    duration_seconds: float | None = None
    streams: tuple[MediaStream, ...] = ()
    window_start_seconds: float | None = None
    window_end_seconds: float | None = None
    transcription_status: TranscriptionStatus = "not_attempted"
    transcribed_start_seconds: float | None = None
    transcribed_end_seconds: float | None = None
    language: str | None = None
    segments: tuple[TranscriptSegment, ...] = ()
    frame_status: FrameStatus = "not_applicable"
    frames: tuple[MediaFrame, ...] = ()
    temp_directory: Path | None = None
    detail: str | None = None

    def cleanup(self) -> None:
        """Remove this analysis's generated files after the caller has used them."""
        if self.temp_directory is not None:
            try:
                shutil.rmtree(self.temp_directory)
            except FileNotFoundError:
                pass


def _positive_seconds(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _detect_format(path: Path) -> str:
    with path.open("rb") as source:
        header = source.read(16)
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "wav"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return "mp3"
    if header[4:8] == b"ftyp":
        return "mov"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    raise ValueError("unsupported media container")


def _input_options(format_name: str) -> list[str]:
    options = ["-protocol_whitelist", "file", "-f", format_name]
    if format_name == "mov":
        options.extend(["-enable_drefs", "0", "-use_absolute_path", "0"])
    return options


def _capture_bounded(command: list[str], *, timeout: int, max_bytes: int) -> bytes:
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    captured = bytearray()
    overflow = False
    def drain() -> None:
        nonlocal overflow
        assert process.stdout is not None
        with process.stdout:
            while chunk := process.stdout.read(8192):
                remaining = max_bytes - len(captured)
                if len(chunk) > remaining:
                    overflow = True
                if remaining > 0:
                    captured.extend(chunk[:remaining])
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        reader.join(timeout=5)
    if code != 0:
        raise subprocess.CalledProcessError(code, command)
    if overflow:
        raise ValueError("provider output exceeds limit")
    return bytes(captured)


def _probe(path: Path, format_name: str) -> tuple[float, tuple[MediaStream, ...]]:
    command = [
        str(FFPROBE), "-v", "error", "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,duration,width,height",
        "-of", "json", *_input_options(format_name), str(path),
    ]
    data = json.loads(_capture_bounded(command, timeout=20, max_bytes=128 * 1024))
    raw_streams = data.get("streams", [])
    if not isinstance(raw_streams, list) or len(raw_streams) > 64:
        raise ValueError("invalid media streams")
    streams = tuple(
        MediaStream(
            index=int(item["index"]), codec_type=item["codec_type"],
            codec_name=item.get("codec_name"), duration_seconds=_positive_seconds(item.get("duration")),
            width=item.get("width") if type(item.get("width")) is int else None,
            height=item.get("height") if type(item.get("height")) is int else None,
        )
        for item in raw_streams
        if isinstance(item, dict) and type(item.get("index")) is int
        and item.get("codec_type") in {"audio", "video", "subtitle", "data", "attachment"}
    )
    duration = _positive_seconds(data.get("format", {}).get("duration"))
    if duration is None:
        duration = max((stream.duration_seconds or 0 for stream in streams), default=0)
    if duration <= 0:
        raise ValueError("media duration unavailable")
    return duration, streams


def _private_directory(temp_root: Path | None) -> Path:
    if temp_root is not None:
        root = Path(temp_root)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("temporary root is not a private directory")
        worktree = Path(__file__).resolve().parents[2]
        if root.resolve().is_relative_to(worktree):
            raise OSError("temporary root must be outside the worktree")
    result = Path(tempfile.mkdtemp(prefix="telegram-media-", dir=temp_root))
    result.chmod(0o700)
    return result


def _available_file(path: Path | None, *, allow_symlink: bool = False) -> bool:
    if path is None:
        return False
    try:
        info = path.resolve(strict=True).stat() if allow_symlink else path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_size > 0


def _decode_wav(path: Path, destination: Path, start: float, length: float, format_name: str) -> None:
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{start:.3f}",
        *_input_options(format_name), "-i", str(path), "-t", f"{length:.3f}", "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "wav", "-y", str(destination),
    ]
    subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=90, check=True)
    if not destination.is_file() or not 44 < destination.stat().st_size <= 10 * 1024 * 1024:
        raise ValueError("decoded audio is empty or oversized")
    destination.chmod(0o600)


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})", value)
    if match is None:
        return None
    hours, minutes, seconds, millis = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _transcribe(wav: Path, directory: Path, *, cli: Path, model: Path,
                start: float, end: float) -> tuple[str | None, tuple[TranscriptSegment, ...]]:
    output = directory / "transcript"
    command = [str(cli), "-m", str(model), "-f", str(wav), "-l", "auto",
               "-ojf", "-of", str(output), "-t", "4"]
    subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=240, check=True)
    result_path = output.with_suffix(".json")
    if result_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("transcription output exceeds limit")
    data = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("transcription"), list):
        raise ValueError("invalid transcription output")
    raw_result = data.get("result")
    if not isinstance(raw_result, dict):
        raise ValueError("invalid transcription result")
    raw_language = raw_result.get("language")
    language = raw_language if isinstance(raw_language, str) and len(raw_language) <= 32 else None
    segments: list[TranscriptSegment] = []
    length = end - start
    for item in data["transcription"]:
        if not isinstance(item, dict):
            continue
        stamps = item.get("timestamps")
        if not isinstance(stamps, dict):
            continue
        begin = _timestamp(stamps.get("from"))
        finish = _timestamp(stamps.get("to"))
        value = item.get("text")
        if begin is None or finish is None or not isinstance(value, str):
            continue
        if begin < 0 or finish < begin or begin >= length:
            continue
        text = value.strip()[:2000]
        if not text:
            continue
        tokens = item.get("tokens")
        low_confidence = isinstance(tokens, list) and any(
            isinstance(token, dict)
            and isinstance(token.get("text"), str)
            and not token["text"].startswith("[_")
            and type(token.get("p")) in (int, float)
            and math.isfinite(token["p"])
            and token["p"] < 0.3
            for token in tokens
        )
        segments.append(TranscriptSegment(
            start_seconds=round(start + begin, 3),
            end_seconds=round(min(end, start + finish), 3),
            text=text, uncertain=bool(_UNCERTAIN.search(text)) or low_confidence,
        ))
        if len(segments) >= 500:
            break
    return language, tuple(segments)


def _transcribe_windows(
    source: Path, directory: Path, *, cli: Path, model: Path,
    start: float, end: float, format_name: str,
) -> tuple[str | None, tuple[TranscriptSegment, ...], bool]:
    """Detect language per short window so code switching is not hidden by one dominant language."""
    length = end - start
    if length <= 4:
        windows = [(start, end)]
    elif length <= 8:
        middle = start + length / 2
        windows = [(start, middle + 0.25), (middle - 0.25, end)]
    else:
        windows = []
        cursor = start
        while cursor < end:
            window_end = min(end, cursor + 8)
            windows.append((cursor, window_end))
            if window_end >= end:
                break
            cursor = window_end - 0.5
    languages: set[str] = set()
    segments: list[TranscriptSegment] = []
    complete = True
    for index, (begin, finish) in enumerate(windows):
        part = directory / f"window-{index:03d}"
        part.mkdir(mode=0o700)
        wav = part / "audio.wav"
        try:
            _decode_wav(source, wav, begin, finish - begin, format_name)
            language, found = _transcribe(wav, part, cli=cli, model=model,
                                          start=begin, end=finish)
            if language:
                languages.add(language)
            for segment in found:
                if len(segments) >= 500:
                    complete = False
                    break
                duplicate = any(
                    segment.text.casefold() == previous.text.casefold()
                    and abs(segment.start_seconds - previous.start_seconds) <= 1.5
                    for previous in segments[-4:]
                )
                if not duplicate:
                    segments.append(segment)
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError, TypeError):
            complete = False
        finally:
            wav.unlink(missing_ok=True)
            (part / "transcript.json").unlink(missing_ok=True)
    segments.sort(key=lambda item: (item.start_seconds, item.end_seconds))
    language = "multi" if len(languages) > 1 else next(iter(languages), None)
    return language, tuple(segments), complete


def _fixed_frame(path: Path, destination: Path, at: float, format_name: str) -> bool:
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{at:.3f}",
        *_input_options(format_name), "-i", str(path), "-frames:v", "1", "-an", "-vf",
        "scale=768:768:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-q:v", "4", "-y", str(destination),
    ]
    subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=20, check=True)
    if not destination.is_file() or destination.stat().st_size == 0:
        return False
    destination.chmod(0o600)
    return True


def _scene_frames(path: Path, directory: Path, start: float, length: float, format_name: str) -> tuple[MediaFrame, ...]:
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "info", "-nostdin", "-ss", f"{start:.3f}",
        *_input_options(format_name), "-i", str(path), "-t", f"{length:.3f}", "-an", "-vf",
        "select='gt(scene,0.3)',showinfo", "-frames:v", "4", "-f", "null", "-",
    ]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
    captured = bytearray()
    def drain() -> None:
        assert process.stderr is not None
        with process.stderr:
            while chunk := process.stderr.read(8192):
                remaining = 1_000_000 - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        reader.join(timeout=5)
    if code != 0:
        raise subprocess.CalledProcessError(code, command)
    times = [float(value) for value in _SHOWINFO_TIME.findall(captured.decode("utf-8", "replace"))]
    frames: list[MediaFrame] = []
    for index, relative_time in enumerate(times[:4]):
        at = start + relative_time
        if at >= start + length:
            continue
        image = directory / f"scene-{index + 1:02d}.jpg"
        if _fixed_frame(path, image, at, format_name):
            frames.append(MediaFrame(round(at, 3), image, "scene"))
    return tuple(frames)


def _extract_frames(path: Path, directory: Path, start: float, end: float, format_name: str) -> tuple[FrameStatus, tuple[MediaFrame, ...]]:
    length = end - start
    count = min(8, max(1, math.ceil(length / 15)))
    frames: list[MediaFrame] = []
    failed = False
    for index in range(count):
        at = start + length * (index + 0.5) / count
        destination = directory / f"time-{index + 1:02d}.jpg"
        try:
            if _fixed_frame(path, destination, at, format_name):
                frames.append(MediaFrame(round(at, 3), destination, "time"))
            else:
                failed = True
        except (OSError, subprocess.SubprocessError, ValueError):
            failed = True
    try:
        for frame in _scene_frames(path, directory, start, length, format_name):
            if all(abs(frame.time_seconds - previous.time_seconds) >= 0.5 for previous in frames):
                frames.append(frame)
    except (OSError, subprocess.SubprocessError, ValueError):
        failed = True
    frames.sort(key=lambda frame: frame.time_seconds)
    return ("partial" if failed and frames else "unavailable" if failed else "complete"), tuple(frames[:MAX_FRAMES])


def analyze_media(
    path: Path,
    *,
    kind: MediaKind,
    start_seconds: float = 0.0,
    whisper_cli: Path | None = None,
    model_path: Path | None = None,
    temp_root: Path | None = None,
) -> MediaAnalysis:
    """Probe and inspect at most five minutes of a broker-authorized artifact.

    Frame timestamps are the requested seek times for time samples, and decoder
    presentation times for scene samples. Transcription coverage describes the
    entire audio window passed to Whisper, even when it contains silence.
    """
    if kind not in {"audio", "voice_note", "video", "video_note"}:
        raise ValueError("unsupported media kind")
    if not isinstance(start_seconds, (int, float)) or not math.isfinite(start_seconds) or start_seconds < 0:
        raise ValueError("start_seconds must be finite and nonnegative")
    path = Path(path)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size <= 0 or info.st_size > MAX_SOURCE_BYTES):
            return MediaAnalysis(status="error", detail="artifact is unsafe, empty, or oversized")
        format_name = _detect_format(path)
        duration, streams = _probe(path, format_name)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError, KeyError, TypeError):
        return MediaAnalysis(status="error", detail="media probe failed")
    if start_seconds >= duration:
        return MediaAnalysis(status="unsupported", duration_seconds=duration, streams=streams,
                             detail="start is outside the media duration")
    start = float(start_seconds)
    end = min(duration, start + MAX_WINDOW_SECONDS)
    has_audio = any(stream.codec_type == "audio" for stream in streams)
    has_video = any(stream.codec_type == "video" for stream in streams)
    if (kind in {"audio", "voice_note"} and not has_audio) or (kind in {"video", "video_note"} and not has_video):
        return MediaAnalysis(status="unsupported", duration_seconds=duration, streams=streams,
                             window_start_seconds=start, window_end_seconds=end,
                             detail="media has no stream matching its selected kind")

    cli = Path(whisper_cli) if whisper_cli is not None else WHISPER_CLI
    model = Path(model_path) if model_path is not None else DEFAULT_MODEL_PATH
    can_transcribe = has_audio and _available_file(cli, allow_symlink=True) and _available_file(model)
    needs_directory = has_video or can_transcribe
    try:
        directory = _private_directory(temp_root) if needs_directory else None
    except OSError:
        return MediaAnalysis(status="error", duration_seconds=duration, streams=streams,
                             window_start_seconds=start, window_end_seconds=end,
                             detail="private analysis directory unavailable")

    frame_status: FrameStatus = "not_applicable"
    frames: tuple[MediaFrame, ...] = ()
    if has_video and directory is not None:
        frame_status, frames = _extract_frames(path, directory, start, end, format_name)

    transcription_status: TranscriptionStatus = "no_audio" if not has_audio else "unavailable"
    transcription_start = transcription_end = None
    language = None
    segments: tuple[TranscriptSegment, ...] = ()
    if can_transcribe and directory is not None:
        try:
            language, segments, all_windows = _transcribe_windows(
                path, directory, cli=cli, model=model,
                start=start, end=end, format_name=format_name,
            )
            transcription_status = "complete" if all_windows else "partial" if segments else "error"
            if all_windows:
                transcription_start, transcription_end = start, end
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError, TypeError):
            transcription_status = "error"

    if transcription_status == "unavailable":
        status: AnalysisStatus = "transcription_unavailable"
    elif transcription_status in {"error", "partial"} or frame_status in {"partial", "unavailable"}:
        status = "partial" if frames or segments or transcription_status == "complete" else "error"
    elif any(segment.uncertain for segment in segments):
        status = "partial"
    elif end < duration:
        status = "partial"
    else:
        status = "complete"
    return MediaAnalysis(
        status=status, duration_seconds=duration, streams=streams,
        window_start_seconds=start, window_end_seconds=end,
        transcription_status=transcription_status,
        transcribed_start_seconds=transcription_start, transcribed_end_seconds=transcription_end,
        language=language, segments=segments, frame_status=frame_status, frames=frames,
        temp_directory=directory,
        detail=("transcript includes uncertain segments" if any(segment.uncertain for segment in segments)
                else "only the selected window was analyzed" if end < duration else None),
    )
