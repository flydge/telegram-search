"""Exact-message attachment selection and retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sanitize import render_evidence, sanitize_telegram_text
from .schemas import AttachmentRequest, AttachmentResponse, MessageContextRequest, MessageContextResponse, ContextEvidence
from .config import TDLIB_SESSION_DIRECTORY
from .artifact_store import ArtifactStoreError
from .tdjson import AuthorizationBlocked, DownloadTooLarge, MessageNotFound, SecretChatRejected, TDLibError

_DOCUMENT_SUFFIXES = frozenset({
    ".pdf", ".docx", ".txt", ".text", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json",
    ".csv", ".tsv", ".html", ".css", ".xml", ".yaml", ".yml", ".sh", ".sql", ".log", ".toml",
    ".jpg", ".jpeg", ".png", ".webp",
})
_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_MEDIA_KINDS = frozenset({"audio", "voice_note", "video", "video_note"})


@dataclass(frozen=True)
class SelectedAttachment:
    file_id: int
    kind: str
    mime_type: str | None
    name: str | None
    size_bytes: int | None


def select_attachment(message: dict[str, Any]) -> SelectedAttachment:
    content = message.get("content")
    if not isinstance(content, dict):
        raise ValueError("message has no supported attachment")
    content_type = content.get("@type")
    mapping = {
        "messageDocument": ("document", "document", "document", None),
        "messageAudio": ("audio", "audio", "audio", None),
        "messageVoiceNote": ("voice_note", "voice_note", "voice", "audio/ogg"),
        "messageVideo": ("video", "video", "video", None),
        "messageVideoNote": ("video_note", "video_note", "video", "video/mp4"),
    }
    if content_type == "messagePhoto":
        photo = content.get("photo")
        sizes = photo.get("sizes") if isinstance(photo, dict) else None
        if not isinstance(sizes, list):
            raise ValueError("photo has no supported file")
        files = [size.get("photo") for size in sizes if isinstance(size, dict)]
        candidates = [file for file in files if isinstance(file, dict)]
        if not candidates:
            raise ValueError("photo has no supported file")
        file = max(candidates, key=lambda item: item.get("size") if type(item.get("size")) is int else 0)
        kind, default_mime, name = "photo", "image/jpeg", None
    elif content_type in mapping:
        kind, content_key, file_key, default_mime = mapping[content_type]
        media = content.get(content_key)
        if not isinstance(media, dict):
            raise ValueError("message has no supported file")
        file = media.get(file_key)
        name = media.get("file_name")
    else:
        raise ValueError("message has no supported attachment")
    if not isinstance(file, dict) or file.get("@type") not in (None, "file"):
        raise ValueError("message has an invalid file")
    file_id = file.get("id")
    if type(file_id) is not int or not 0 < file_id <= (1 << 31) - 1:
        raise ValueError("message has an invalid file ID")
    size = file.get("size")
    if type(size) is not int or size <= 0:
        size = file.get("expected_size")
    if type(size) is not int or size <= 0:
        size = None
    if content_type == "messagePhoto":
        mime = default_mime
    else:
        mime = media.get("mime_type") or default_mime
    return SelectedAttachment(
        file_id=file_id,
        kind=kind,
        mime_type=sanitize_telegram_text(mime, max_length=127) or None,
        name=sanitize_telegram_text(name, max_length=255) or None,
        size_bytes=size,
    )


def get_attachment(
    client: Any,
    store: Any,
    request: AttachmentRequest,
    *,
    source_root: Path = TDLIB_SESSION_DIRECTORY / "files",
) -> AttachmentResponse:
    """Resolve one exact message, download its selected original, and snapshot it."""
    anchor = request.anchor

    def incomplete(status: str, detail: str, selected: SelectedAttachment | None = None) -> AttachmentResponse:
        return AttachmentResponse(
            status=status, anchor=anchor, coverage="none", detail=detail,
            media_type=selected.kind if selected else None,
            file_name=selected.name if selected else None,
            mime_type=selected.mime_type if selected else None,
            size_bytes=selected.size_bytes if selected else None,
        )

    try:
        client.ensure_ready()
        chat = client.resolve_target(anchor.chat_id)
        if chat.get("id") != anchor.chat_id or (chat.get("type") or {}).get("@type") == "chatTypeSecret":
            return incomplete("unsupported", "chat is unavailable for attachment transfer")
        message = client.get_message(anchor.chat_id, anchor.message_id)
        if message.get("chat_id") != anchor.chat_id or message.get("id") != anchor.message_id:
            return incomplete("error", "Telegram returned a different message")
        if message.get("can_be_saved") is False or message.get("self_destruct_type") is not None:
            return incomplete("unsupported", "message disallows persistent transfer")
        selected = select_attachment(message)
        if selected.kind == "document" and Path(selected.name or "").suffix.casefold() not in _DOCUMENT_SUFFIXES:
            return incomplete("unsupported", "document format is unsupported", selected)
        if selected.kind == "photo" and selected.mime_type not in _IMAGE_MIMES:
            return incomplete("unsupported", "image format is unsupported", selected)
        size_limit = (256 if selected.kind in _MEDIA_KINDS else 64) * 1024 * 1024
        if selected.size_bytes is not None and selected.size_bytes > size_limit:
            return incomplete("too_large", "attachment exceeds the transfer limit", selected)
        source_path = client.download_file(selected.file_id, max_bytes=size_limit, timeout=300)
        if not source_path.is_absolute() or not source_path.resolve(strict=True).is_relative_to(source_root.resolve(strict=True)):
            return incomplete("error", "TDLib file path is outside its private cache", selected)
        storage_kind = "media" if selected.kind in _MEDIA_KINDS else ("image" if selected.kind == "photo" else "document")
        artifact = store.store(source_path, kind=storage_kind)
        return AttachmentResponse(
            status="complete", anchor=anchor, media_type=selected.kind,
            file_name=selected.name, mime_type=selected.mime_type,
            size_bytes=artifact.size_bytes, sha256=artifact.sha256,
            artifact_id=artifact.artifact_id, artifact_path=str(artifact.path),
            expires_at=datetime.fromtimestamp(artifact.expires_at, tz=timezone.utc),
            coverage="complete", detail="original bytes are available in the private cache",
        )
    except AuthorizationBlocked:
        return incomplete("blocked", "TDLib authorization is not ready")
    except MessageNotFound:
        return incomplete("not_found", "message is no longer available")
    except (ValueError, SecretChatRejected):
        return incomplete("unsupported", "message has no supported attachment")
    except DownloadTooLarge:
        return incomplete("too_large", "attachment exceeds the transfer limit")
    except TimeoutError:
        return incomplete("error", "attachment download expired")
    except (TDLibError, OSError, ArtifactStoreError):
        return incomplete("error", "attachment transfer failed safely")


def get_message_context(client: Any, request: MessageContextRequest) -> MessageContextResponse:
    anchor = request.anchor

    def terminal(status: str, detail: str) -> MessageContextResponse:
        return MessageContextResponse(
            status=status, anchor=anchor, coverage_complete=False, detail=detail,
        )

    try:
        client.ensure_ready()
        chat = client.resolve_target(anchor.chat_id)
        if chat.get("id") != anchor.chat_id or (chat.get("type") or {}).get("@type") == "chatTypeSecret":
            return terminal("error", "chat is unavailable")
        center = client.get_message(anchor.chat_id, anchor.message_id)
        if center.get("chat_id") != anchor.chat_id or center.get("id") != anchor.message_id:
            return terminal("error", "Telegram returned a different message")
        candidates = (
            client.get_context_messages(anchor.chat_id, anchor.message_id, request.radius)
            if request.radius else []
        )
        valid: list[dict[str, Any]] = []
        partial = len(candidates) < request.radius * 2
        for item in [center, *candidates]:
            if (not isinstance(item, dict) or item.get("chat_id") != anchor.chat_id
                or type(item.get("id")) is not int or type(item.get("date")) is not int
                or item["date"] < 0):
                partial = True
                continue
            valid.append(item)
        center_key = (center.get("date"), center.get("id"))
        before = sorted((item for item in valid if (item["date"], item["id"]) < center_key),
                        key=lambda item: (item["date"], item["id"]))[-request.radius:]
        after = sorted((item for item in valid if (item["date"], item["id"]) > center_key),
                       key=lambda item: (item["date"], item["id"]))[:request.radius]
        selected = [*before, center, *after]
        rendered: list[ContextEvidence] = []
        for item in selected:
            content = item.get("content") or {}
            text = content.get("text") if content.get("@type") == "messageText" else content.get("caption")
            snippet = text.get("text") if isinstance(text, dict) else None
            rendered.append(ContextEvidence(
                message_id=item["id"],
                date_utc=datetime.fromtimestamp(item["date"], tz=timezone.utc),
                sender=sanitize_telegram_text(client.get_sender_name(item), max_length=256),
                snippet=render_evidence(snippet or "message"),
            ))
        return MessageContextResponse(
            status="partial" if partial else "complete", anchor=anchor,
            messages=rendered, coverage_complete=not partial,
            detail="bounded context around exact anchor" if not partial else "some context messages were invalid",
        )
    except AuthorizationBlocked:
        return terminal("blocked", "TDLib authorization is not ready")
    except MessageNotFound:
        return terminal("not_found", "message is no longer available")
    except (SecretChatRejected, TDLibError, ValueError):
        return terminal("error", "message context failed safely")
