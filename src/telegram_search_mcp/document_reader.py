"""Bounded, non-executing inspection of a broker-owned local document artifact.

The caller must resolve artifact IDs and authorize the path before calling this
module. PDF page previews are temporary private PNGs; callers own their cleanup.
Image files are returned as paths for Codex to display, without OCR.
"""

from __future__ import annotations

import stat
import shutil
import tempfile
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import fitz
from docx import Document
from docx.document import Document as DocumentType
from docx.table import Table
from PIL import Image


Status = Literal["complete", "partial", "unsupported", "error"]
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_RENDER_EDGE = 768

_TEXT_SUFFIXES = {".txt", ".text", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".sql", ".log", ".toml"}
_IMAGE_SUFFIXES = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
_IMAGE_MIMES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
_TEXT_MIMES = {"application/json", "application/xml", "application/javascript", "application/x-python", "application/yaml"}


@dataclass(frozen=True)
class DocumentReadResult:
    status: Status
    text: str = ""
    processed_bytes: int = 0
    total_bytes: int | None = None
    processed_pages: int = 0
    total_pages: int | None = None
    image_paths: tuple[Path, ...] = ()
    error: str | None = None


def _kind(name: str, mime_type: str | None) -> str | None:
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime == "application/pdf":
        return "pdf"
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if mime in _IMAGE_MIMES:
        return "image"
    if mime.startswith("text/") or mime in _TEXT_MIMES:
        return "text"
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    return None


def _limited_text(value: str, max_chars: int) -> tuple[str, bool]:
    return value[:max_chars], len(value) > max_chars


def _read_text(path: Path, total: int, max_chars: int) -> DocumentReadResult:
    with path.open("r", encoding="utf-8", errors="strict") as source:
        value = source.read(max_chars + 1)
    text, partial = _limited_text(value, max_chars)
    return DocumentReadResult(
        status="partial" if partial else "complete",
        text=text,
        processed_bytes=len(text.encode("utf-8")),
        total_bytes=total,
    )


def _read_docx(path: Path, total: int, max_chars: int) -> DocumentReadResult:
    from .docx_safety import read_docx_prefix

    result = read_docx_prefix(path, max_chars=max_chars)
    if result.total_bytes != total:
        return DocumentReadResult(status="error", total_bytes=total, error="artifact_changed")
    return DocumentReadResult(status=result.status, text=result.text,
                              processed_bytes=result.processed_bytes, total_bytes=result.total_bytes,
                              error=result.error)


def _render_page(page: fitz.Page, directory: Path, index: int) -> Path:
    rectangle = page.rect
    scale = min(1.0, MAX_RENDER_EDGE / max(rectangle.width, rectangle.height, 1))
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    destination = directory / f"page-{index + 1}.png"
    destination.write_bytes(pixmap.tobytes("png"))
    destination.chmod(0o600)
    return destination


def _read_pdf(path: Path, total: int, max_chars: int, max_pages: int) -> DocumentReadResult:
    with fitz.open(path) as document:
        if document.needs_pass:
            raise ValueError("password-protected PDF")
        total_pages = len(document)
        count = min(total_pages, max_pages)
        directory = Path(tempfile.mkdtemp(prefix="telegram-document-pages-")) if count else None
        texts: list[str] = []
        images: list[Path] = []
        length = 0
        truncated = False
        try:
            for index in range(count):
                page = document[index]
                value = page.get_text("text")
                separator = "\n" if texts else ""
                segment = separator + value
                remaining = max_chars - length
                if len(segment) > remaining:
                    truncated = True
                selected = segment[:remaining]
                texts.append(selected)
                length += len(selected)
                images.append(_render_page(page, directory, index))
                if truncated:
                    break
        except BaseException:
            if directory is not None:
                shutil.rmtree(directory)
            raise
        return DocumentReadResult(
            status="partial" if truncated or len(images) < total_pages else "complete",
            text="".join(texts),
            processed_bytes=total,
            total_bytes=total,
            processed_pages=len(images),
            total_pages=total_pages,
            image_paths=tuple(images),
        )


def _read_image(path: Path, total: int, name: str, mime_type: str | None) -> DocumentReadResult:
    expected = _IMAGE_MIMES.get((mime_type or "").split(";", 1)[0].strip().lower())
    if expected is None:
        expected = _IMAGE_SUFFIXES.get(Path(name).suffix.lower())
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.format != expected or image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("unsupported or oversized image")
            image.verify()
    return DocumentReadResult(
        status="complete", processed_bytes=total, total_bytes=total, image_paths=(path,)
    )


def read_document(
    path: Path,
    *,
    name: str | None = None,
    mime_type: str | None = None,
    max_chars: int = 20_000,
    max_pages: int = 5,
) -> DocumentReadResult:
    """Read one previously authorized artifact with explicit truncation and errors."""
    if not 1 <= max_chars <= 100_000 or not 1 <= max_pages <= 20:
        raise ValueError("max_chars must be 1..100000 and max_pages must be 1..20")
    path = Path(path)
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return DocumentReadResult(status="error", error="artifact is not a regular file")
        total = metadata.st_size
        if total > MAX_INPUT_BYTES:
            return DocumentReadResult(status="error", total_bytes=total, error="artifact exceeds reader size limit")
        kind = _kind(name or path.name, mime_type)
        if kind is None:
            return DocumentReadResult(status="unsupported", total_bytes=total)
        if kind == "text":
            return _read_text(path, total, max_chars)
        if kind == "docx":
            return _read_docx(path, total, max_chars)
        if kind == "pdf":
            return _read_pdf(path, total, max_chars, max_pages)
        return _read_image(path, total, name or path.name, mime_type)
    except (OSError, UnicodeError, ValueError, RuntimeError, zipfile.BadZipFile, fitz.FileDataError) as exc:
        return DocumentReadResult(status="error", error=type(exc).__name__)
