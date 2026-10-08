"""Turn an owner-owned audio artifact into a verified Telegram voice note."""

from __future__ import annotations

import base64
import json
import math
import subprocess
import tempfile
from array import array
from dataclasses import dataclass
from pathlib import Path

from .artifact_store import ArtifactStore, StoredArtifact
from .media_analyzer import FFMPEG, FFPROBE

MAX_VOICE_SECONDS = 600
MAX_VOICE_BYTES = 64 * 1024 * 1024


class VoiceError(RuntimeError):
    """The audio cannot be safely prepared as a voice note."""


@dataclass(frozen=True)
class VoicePrepared:
    artifact: StoredArtifact
    source_sha256: str
    duration_seconds: int
    waveform_base64: str
    converted: bool


def _probe(path: Path) -> dict[str, object]:
    try:
        result = subprocess.run(
            [str(FFPROBE), "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_name,channels,duration:format=duration,format_name", "-of", "json", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=True,
        )
        if len(result.stdout) > 64 * 1024:
            raise VoiceError("audio metadata is oversized")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise VoiceError("audio metadata is invalid")
        return value
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise VoiceError("audio metadata could not be verified") from error


def _duration(metadata: dict[str, object]) -> float:
    streams = metadata.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        raise VoiceError("exactly one audio stream is required")
    container = metadata.get("format")
    if not isinstance(container, dict):
        raise VoiceError("audio container is invalid")
    try:
        seconds = float(container.get("duration") or streams[0].get("duration"))
    except (TypeError, ValueError) as error:
        raise VoiceError("audio duration is invalid") from error
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_VOICE_SECONDS:
        raise VoiceError("audio duration is out of bounds")
    return seconds


def _waveform(path: Path, duration: float) -> str:
    try:
        result = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-vn", "-t", str(min(MAX_VOICE_SECONDS, duration + 0.1)), "-ac", "1", "-ar", "8000",
             "-f", "s16le", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=120, check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise VoiceError("voice waveform could not be analyzed") from error
    if not result.stdout or len(result.stdout) > MAX_VOICE_SECONDS * 8000 * 2 + 3200:
        raise VoiceError("voice waveform is invalid")
    samples = array("h")
    samples.frombytes(result.stdout[:len(result.stdout) - len(result.stdout) % 2])
    peaks = [0] * 100
    for index, sample in enumerate(samples):
        bucket = min(99, index * 100 // len(samples))
        peaks[bucket] = max(peaks[bucket], abs(sample))
    maximum = max(peaks)
    values = [min(31, round(31 * math.sqrt(peak / maximum))) if maximum else 0 for peak in peaks]
    packed = bytearray(63)
    for index, value in enumerate(values):
        position = index * 5
        packed[position // 8] |= (value << (position % 8)) & 0xff
        if position % 8 > 3:
            packed[position // 8 + 1] |= value >> (8 - position % 8)
    return base64.b64encode(packed).decode("ascii")


def prepare_voice_note(source: StoredArtifact, store: ArtifactStore, *,
                       input_mime: str | None = None, input_name: str | None = None) -> VoicePrepared:
    if not 0 < source.size_bytes <= MAX_VOICE_BYTES:
        raise VoiceError("audio exceeds voice size limit")
    source_metadata = _probe(source.path)
    _duration(source_metadata)
    if input_mime is not None or input_name is not None:
        expected = {
            ".wav": ("audio/wav", "wav"), ".mp3": ("audio/mpeg", "mp3"),
            ".ogg": ("audio/ogg", "ogg"), ".m4a": ("audio/mp4", "mov,mp4"),
            ".flac": ("audio/flac", "flac"),
        }.get(Path(input_name or "").suffix.casefold())
        if expected is None or input_mime != expected[0] or expected[1] not in str(source_metadata.get("format", {}).get("format_name", "")):
            raise VoiceError("audio MIME or extension does not match bytes")
    try:
        with tempfile.TemporaryDirectory(prefix="telegram-voice-") as temporary:
            output = Path(temporary) / "voice.ogg"
            subprocess.run(
                [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-i", str(source.path), "-vn", "-ac", "1", "-c:a", "libopus",
                 "-b:a", "32k", "-application", "voip", "-f", "ogg", str(output)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=180, check=True,
            )
            if not output.is_file() or not 0 < output.stat().st_size <= MAX_VOICE_BYTES:
                raise VoiceError("converted voice size is invalid")
            with output.open("rb") as handle:
                if handle.read(4) != b"OggS":
                    raise VoiceError("converted voice container is invalid")
            metadata = _probe(output)
            duration = _duration(metadata)
            stream = metadata["streams"][0]
            container = metadata["format"]
            if stream.get("codec_name") != "opus" or stream.get("channels") != 1 or "ogg" not in str(container.get("format_name", "")):
                raise VoiceError("converted voice is not OGG/Opus mono")
            waveform = _waveform(output, duration)
            artifact = store.store(output, kind="media")
            return VoicePrepared(artifact=artifact, source_sha256=source.sha256,
                                 duration_seconds=max(1, round(duration)),
                                 waveform_base64=waveform, converted=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise VoiceError("voice conversion failed safely") from error


def prepare_reply_voice_note(source: StoredArtifact, store: ArtifactStore, *,
                             input_mime: str, input_name: str, budget_check) -> VoicePrepared:
    """Convert only bytes verified against the admitted immutable source identity."""
    import os
    import stat
    import hashlib
    import time
    from dataclasses import replace
    budget_check()
    if not 0 < source.size_bytes <= MAX_VOICE_BYTES or time.time() >= source.expires_at:
        raise VoiceError('source audio is unavailable')
    # Metadata inspection precedes streaming, including large sources.
    info=source.path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size!=source.size_bytes:
        raise VoiceError('source audio is unavailable')
    with tempfile.TemporaryDirectory(prefix='telegram-reply-source-') as temporary:
        snapshot=Path(temporary)/'source'
        fd=os.open(source.path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            actual=os.fstat(fd)
            if not stat.S_ISREG(actual.st_mode) or actual.st_size!=source.size_bytes:
                raise VoiceError('source audio changed')
            digest=hashlib.sha256();copied=0
            with snapshot.open('xb') as output:
                os.chmod(snapshot,0o600)
                while chunk:=os.read(fd,1024*1024):
                    copied+=len(chunk)
                    if copied>source.size_bytes:raise VoiceError('source audio changed')
                    digest.update(chunk);output.write(chunk)
            if copied!=source.size_bytes or digest.hexdigest()!=source.sha256:
                raise VoiceError('source audio changed')
        finally:
            os.close(fd)
        budget_check()
        if time.time() >= source.expires_at:raise VoiceError('source audio expired')
        prepared=prepare_voice_note(replace(source,path=snapshot),store,input_mime=input_mime,input_name=input_name)
        budget_check()
        return prepared
