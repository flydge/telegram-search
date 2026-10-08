"""Hash-pinned attachment parsing in a short-lived, resource-bounded worker.

Paths must already be authorized by the caller. Offsets count Unicode codepoints
in the selected text scope. Nothing extracted is retained between calls.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_DOCX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_RENDER_EDGE = 768
MAX_PAGE_EDGE = 1_000_000
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGES_BYTES = 6 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 9 * 1024 * 1024
MAX_WORKER_MEMORY_BYTES = 768 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024

_TEXT_SUFFIXES = {".txt", ".text", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".sql", ".log", ".toml"}
_IMAGE_SUFFIXES = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
_IMAGE_MIMES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
_TEXT_MIMES = {"application/json", "application/xml", "application/javascript", "application/x-python", "application/yaml"}
_STATUSES = {"complete", "page", "unsupported", "invalid_selection", "limit_reached", "error"}
_KINDS = {"pdf", "docx", "text", "image", None}
_DETAILS = {"", "invalid_arguments", "invalid_pages", "offset_out_of_range", "artifact_changed", "source_invalid", "input_limit", "extraction_limit", "docx_expansion_limit", "image_limit", "password_protected", "parser_error", "worker_timeout", "worker_limit", "worker_error"}


@dataclass(frozen=True)
class PageImage:
    page_number: int | None
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class PageReadResult:
    status: str
    kind: str | None
    text: str = ""
    text_start: int = 0
    text_end: int = 0
    total_pages: int | None = None
    selected_pages: tuple[int, ...] = ()
    images: tuple[PageImage, ...] = ()
    has_more: bool = False
    detail: str = ""


class _ReadFailure(Exception):
    def __init__(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = detail


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
    return "text" if suffix in _TEXT_SUFFIXES else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _check_source(path: Path, fd: int, initial: os.stat_result) -> None:
    try:
        if _fingerprint(os.fstat(fd)) != _fingerprint(initial) or _fingerprint(path.lstat()) != _fingerprint(initial):
            raise _ReadFailure("error", "artifact_changed")
    except OSError:
        raise _ReadFailure("error", "artifact_changed") from None


def _read_pinned(path: Path, sha256: str, size_bytes: int) -> tuple[bytes, int, os.stat_result]:
    fd = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            raise _ReadFailure("error", "source_invalid")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        initial = os.fstat(fd)
        if _fingerprint(initial) != _fingerprint(before):
            raise _ReadFailure("error", "artifact_changed")
        if initial.st_size != size_bytes:
            raise _ReadFailure("error", "artifact_changed")
        if initial.st_size > MAX_INPUT_BYTES:
            raise _ReadFailure("limit_reached", "input_limit")
        chunks: list[bytes] = []
        length = 0
        digest = hashlib.sha256()
        while True:
            value = os.read(fd, min(1024 * 1024, size_bytes - length + 1))
            if not value:
                break
            length += len(value)
            if length > size_bytes:
                raise _ReadFailure("error", "artifact_changed")
            digest.update(value)
            chunks.append(value)
        _check_source(path, fd, initial)
        if length != size_bytes or digest.hexdigest() != sha256:
            raise _ReadFailure("error", "artifact_changed")
        return b"".join(chunks), fd, initial
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


class _TextScope:
    def __init__(self) -> None:
        self.segments: list[str] = []
        self.length = 0

    def append(self, value: str) -> None:
        separator = "\n" if self.segments else ""
        if self.length + len(separator) + len(value) > MAX_TEXT_CHARS:
            raise _ReadFailure("limit_reached", "extraction_limit")
        self.segments.append(separator + value)
        self.length += len(separator) + len(value)

    def text(self) -> str:
        return "".join(self.segments)


def _page_result(kind: str, text: str, request: dict[str, Any], *, total_pages: int | None = None,
                 selected_pages: tuple[int, ...] = (), images: tuple[PageImage, ...] = ()) -> PageReadResult:
    if len(text) > MAX_TEXT_CHARS:
        raise _ReadFailure("limit_reached", "extraction_limit")
    offset = request["offset"]
    if offset > len(text):
        return PageReadResult("invalid_selection", kind, detail="offset_out_of_range")
    end = min(offset + request["max_chars"], len(text))
    has_more = end < len(text)
    return PageReadResult("page" if has_more else "complete", kind, text[offset:end], offset, end,
                          total_pages, selected_pages, images, has_more)


def _check_preview(data: bytes, images: list[PageImage], page_number: int | None) -> None:
    if len(data) > MAX_IMAGE_BYTES or sum(len(image.data) for image in images) + len(data) > MAX_IMAGES_BYTES:
        raise _ReadFailure("limit_reached", "image_limit")
    images.append(PageImage(page_number, "image/png", data))


def _read_pdf(data: bytes, request: dict[str, Any]) -> PageReadResult:
    import fitz

    with fitz.open(stream=data, filetype="pdf") as document:
        if document.needs_pass:
            raise _ReadFailure("error", "password_protected")
        total_pages = len(document)
        chosen = request["pages"]
        selected = tuple(chosen) if chosen is not None else tuple(range(1, min(5, total_pages) + 1))
        # Validate the entire selection before loading, extracting or rendering any page.
        if any(number > total_pages for number in selected):
            return PageReadResult("invalid_selection", "pdf", total_pages=total_pages, detail="invalid_pages")
        scope = _TextScope()
        for number in selected:
            scope.append(document[number - 1].get_text("text"))
        images: list[PageImage] = []
        if request["render_pages"] and request["offset"] == 0:
            for number in selected:
                page = document[number - 1]
                rect = page.rect
                dimensions = (rect.width, rect.height)
                if any(not math.isfinite(edge) or edge <= 0 or edge > MAX_PAGE_EDGE for edge in dimensions):
                    raise _ReadFailure("limit_reached", "image_limit")
                scale = min(1.0, (MAX_RENDER_EDGE - 1) / max(dimensions))
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                if pixmap.width <= 0 or pixmap.height <= 0 or max(pixmap.width, pixmap.height) > MAX_RENDER_EDGE or pixmap.width * pixmap.height > MAX_RENDER_EDGE ** 2:
                    raise _ReadFailure("limit_reached", "image_limit")
                _check_preview(pixmap.tobytes("png"), images, number)
        return _page_result("pdf", scope.text(), request, total_pages=total_pages,
                            selected_pages=selected, images=tuple(images))


def _read_docx(data: bytes, request: dict[str, Any]) -> PageReadResult:
    # The isolated script loads only its exact installed sibling.
    import importlib.util

    identity = "_telegram_document_docx_safety"
    specification = importlib.util.spec_from_file_location(identity, Path(__file__).with_name("docx_safety.py"))
    if specification is None or specification.loader is None:
        raise _ReadFailure("error", "worker_error")
    safety = importlib.util.module_from_spec(specification)
    sys.modules[identity] = safety
    specification.loader.exec_module(safety)
    try:
        safety.validate_docx(data)
    except safety.DocxSafetyError as error:
        raise _ReadFailure("limit_reached" if error.budget else "error",
                           "docx_expansion_limit" if error.budget else "parser_error") from None
    from docx import Document
    from docx.table import Table

    document = Document(io.BytesIO(data))
    scope = _TextScope()
    for block in document.iter_inner_content():
        values = (cell.text for row in block.rows for cell in row.cells) if isinstance(block, Table) else (block.text,)
        for value in values:
            if value:
                scope.append(value)
    return _page_result("docx", scope.text(), request)


def _read_image(data: bytes, request: dict[str, Any]) -> PageReadResult:
    from PIL import Image

    expected = _IMAGE_MIMES.get((request["mime_type"] or "").split(";", 1)[0].strip().lower())
    if expected is None:
        expected = _IMAGE_SUFFIXES.get(Path(request["name"]).suffix.lower())
    images: list[PageImage] = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != expected:
                    raise _ReadFailure("error", "parser_error")
                if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise _ReadFailure("limit_reached", "image_limit")
                image.verify()
            if request["render_pages"] and request["offset"] == 0:
                with Image.open(io.BytesIO(data)) as image:
                    image.thumbnail((MAX_RENDER_EDGE, MAX_RENDER_EDGE))
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    if max(image.size) > MAX_RENDER_EDGE or image.width * image.height > MAX_RENDER_EDGE ** 2:
                        raise _ReadFailure("limit_reached", "image_limit")
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    _check_preview(output.getvalue(), images, None)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise _ReadFailure("limit_reached", "image_limit") from None
    return _page_result("image", "", request, images=tuple(images))


def _parse_request(request: dict[str, Any]) -> PageReadResult:
    path = Path(request["path"])
    kind = _kind(request["name"], request["mime_type"])
    fd = -1
    try:
        data, fd, initial = _read_pinned(path, request["sha256"], request["size_bytes"])
        try:
            if request["pages"] is not None and kind != "pdf":
                result = PageReadResult("invalid_selection", kind, detail="invalid_pages")
            elif kind is None:
                result = PageReadResult("unsupported", None)
            elif kind == "pdf":
                result = _read_pdf(data, request)
            elif kind == "docx":
                result = _read_docx(data, request)
            elif kind == "image":
                result = _read_image(data, request)
            else:
                text = data.decode("utf-8", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
                result = _page_result("text", text, request)
        finally:
            _check_source(path, fd, initial)
        return result
    except _ReadFailure as exc:
        return PageReadResult(exc.status, kind, detail=exc.detail)
    except MemoryError:
        return PageReadResult("limit_reached", kind, detail="worker_limit")
    except Exception:
        return PageReadResult("error", kind, detail="parser_error")
    finally:
        if fd >= 0:
            os.close(fd)


def _darwin_virtual_size() -> int:
    import ctypes

    # Apple proc_info.h: six uint64 fields followed by twelve int32 fields.
    # Read only this worker's initial mappings; no document data is involved.
    class TaskInfo(ctypes.Structure):
        _fields_ = [("values", ctypes.c_uint64 * 6), ("counters", ctypes.c_int32 * 12)]

    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    info = TaskInfo()
    if function(os.getpid(), 4, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        raise OSError("worker memory measurement unavailable")
    virtual_size, resident_size = info.values[0], info.values[1]
    if virtual_size < resident_size or virtual_size <= 0:
        raise OSError("worker memory measurement unavailable")
    return virtual_size


def _set_limits(timeout: float) -> None:
    import resource

    # These limits precede every third-party import and every source-byte read.
    # Darwin includes a very large initial shared virtual map. Modern Mach VM
    # rejects limits below that map, so cap *additional* mappings there. This is
    # an incremental virtual allocation budget, never an absolute RSS claim.
    memory_limit = MAX_WORKER_MEMORY_BYTES
    if sys.platform == "darwin":
        memory_limit += _darwin_virtual_size()
    for limit in (resource.RLIMIT_AS, resource.RLIMIT_DATA):
        resource.setrlimit(limit, (memory_limit, memory_limit))
    cpu = max(1, math.ceil(timeout))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_WORKER_OUTPUT_BYTES, MAX_WORKER_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _encode_result(result: PageReadResult) -> dict[str, Any]:
    return {"status": result.status, "kind": result.kind, "text": result.text,
            "text_start": result.text_start, "text_end": result.text_end,
            "total_pages": result.total_pages, "selected_pages": list(result.selected_pages),
            "images": [{"page_number": image.page_number, "mime_type": image.mime_type,
                        "data": base64.b64encode(image.data).decode("ascii")} for image in result.images],
            "has_more": result.has_more, "detail": result.detail}


def _decode_result(value: Any, request: dict[str, Any]) -> PageReadResult:
    if not isinstance(value, dict) or set(value) != {"status", "kind", "text", "text_start", "text_end", "total_pages", "selected_pages", "images", "has_more", "detail"}:
        raise ValueError
    if value["status"] not in _STATUSES or value["kind"] not in _KINDS or value["detail"] not in _DETAILS:
        raise ValueError
    if not isinstance(value["text"], str) or len(value["text"]) > request["max_chars"]:
        raise ValueError
    if any(not _is_int(value[field]) or not 0 <= value[field] <= MAX_TEXT_CHARS for field in ("text_start", "text_end")) or value["text_end"] != value["text_start"] + len(value["text"]):
        raise ValueError
    if value["total_pages"] is not None and (not _is_int(value["total_pages"]) or not 0 <= value["total_pages"] < 2 ** 53):
        raise ValueError
    selected = value["selected_pages"]
    if not isinstance(selected, list) or len(selected) > 5 or len(set(selected)) != len(selected) or any(not _is_int(page) or not 0 < page < 2 ** 53 for page in selected):
        raise ValueError
    if not isinstance(value["has_more"], bool) or not isinstance(value["images"], list) or len(value["images"]) > 5:
        raise ValueError
    images: list[PageImage] = []
    for image in value["images"]:
        if not isinstance(image, dict) or set(image) != {"page_number", "mime_type", "data"} or image["mime_type"] != "image/png" or not isinstance(image["data"], str) or len(image["data"]) > 4 * math.ceil(MAX_IMAGE_BYTES / 3):
            raise ValueError
        number = image["page_number"]
        if number is not None and (not _is_int(number) or number not in selected):
            raise ValueError
        _check_preview(base64.b64decode(image["data"], validate=True), images, number)
    if value["status"] not in {"complete", "page"} and (value["text"] or images or value["has_more"]):
        raise ValueError
    if images and (not request["render_pages"] or request["offset"] != 0):
        raise ValueError
    return PageReadResult(value["status"], value["kind"], value["text"], value["text_start"], value["text_end"], value["total_pages"], tuple(selected), tuple(images), value["has_more"], value["detail"])


def _worker_main(output_path: str, timeout: float) -> None:
    _set_limits(timeout)
    encoded = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(encoded) > MAX_REQUEST_BYTES:
        return
    request = json.loads(encoded)
    result = _parse_request(request)
    with open(output_path, "x", encoding="utf-8") as output:
        json.dump(_encode_result(result), output, ensure_ascii=False, separators=(",", ":"))


def read_document_page(
    path: Path, *, sha256: str, size_bytes: int, name: str | None,
    mime_type: str | None, pages: tuple[int, ...] | None = None,
    offset: int = 0, max_chars: int = 20_000, render_pages: bool = True,
    timeout: float = 15.0,
) -> PageReadResult:
    """Parse a single immutable authorized artifact, returning one text window."""
    if (not _is_int(size_bytes) or size_bytes < 0 or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or not _is_int(offset) or not 0 <= offset <= MAX_TEXT_CHARS
            or not _is_int(max_chars) or not 1 <= max_chars <= 20_000
            or not isinstance(render_pages, bool)
            or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0
            or (name is not None and (not isinstance(name, str) or len(name) > 1024))
            or (mime_type is not None and (not isinstance(mime_type, str) or len(mime_type) > 256))):
        return PageReadResult("invalid_selection", None, detail="invalid_arguments")
    if pages is not None and (not isinstance(pages, tuple) or not 1 <= len(pages) <= 5
                              or any(not _is_int(page) or not 0 < page < 2 ** 53 for page in pages)
                              or len(set(pages)) != len(pages)):
        return PageReadResult("invalid_selection", None, detail="invalid_pages")
    if size_bytes > MAX_INPUT_BYTES:
        return PageReadResult("limit_reached", None, detail="input_limit")
    try:
        path = Path(path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return PageReadResult("error", None, detail="source_invalid")
        if metadata.st_size != size_bytes:
            return PageReadResult("error", None, detail="artifact_changed")
        request = {"path": str(path.absolute()), "sha256": sha256, "size_bytes": size_bytes,
                   "name": name or path.name, "mime_type": mime_type, "pages": pages,
                   "offset": offset, "max_chars": max_chars, "render_pages": render_pages}
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            return PageReadResult("invalid_selection", None, detail="invalid_arguments")
        timeout = min(float(timeout), 15.0)
        with tempfile.TemporaryDirectory(prefix="telegram-document-page-") as directory:
            output_path = Path(directory) / "result.json"
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(Path(__file__).absolute()), "--worker", str(output_path), str(timeout)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
            try:
                process.communicate(input=encoded, timeout=timeout)
            except subprocess.TimeoutExpired:
                return PageReadResult("limit_reached", None, detail="worker_timeout")
            finally:
                # Also clean up on caller interruption or a failed pipe write.
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
                if process.stdin is not None:
                    process.stdin.close()
            if process.returncode != 0:
                return PageReadResult("limit_reached" if process.returncode < 0 else "error", None,
                                      detail="worker_limit" if process.returncode < 0 else "worker_error")
            with output_path.open("rb") as source:
                output = source.read(MAX_WORKER_OUTPUT_BYTES + 1)
            if len(output) > MAX_WORKER_OUTPUT_BYTES:
                return PageReadResult("limit_reached", None, detail="worker_limit")
            result = _decode_result(json.loads(output), request)
            if _fingerprint(path.lstat()) != _fingerprint(metadata):
                return PageReadResult("error", None, detail="artifact_changed")
            return result
    except (OSError, ValueError, TypeError, _ReadFailure):
        return PageReadResult("error", None, detail="worker_error")


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
    _worker_main(sys.argv[2], float(sys.argv[3]))
