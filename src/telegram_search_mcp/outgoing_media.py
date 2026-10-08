"""Validate media bytes before creating an immutable outgoing preview."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

PHOTO_LIMIT = 10_000_000
PHOTO_FORMATS = {
    "PNG": ("image/png", {".png"}),
    "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
    "WEBP": ("image/webp", {".webp"}),
}


def validate_photo(path: Path, *, size_bytes: int, display_name: str, mime_type: str) -> tuple[int, int]:
    if not 0 < size_bytes <= PHOTO_LIMIT:
        raise ValueError("photo exceeds the size limit")
    try:
        with Image.open(path) as image:
            actual_format = image.format
            width, height = image.size
            if actual_format not in PHOTO_FORMATS:
                raise ValueError("photo format is unsupported")
            expected_mime, extensions = PHOTO_FORMATS[actual_format]
            if mime_type != expected_mime or Path(display_name).suffix.casefold() not in extensions:
                raise ValueError("photo MIME or extension does not match bytes")
            if (min(width, height) < 1 or width + height > 10_000
                or max(width, height) > 20 * min(width, height)):
                raise ValueError("photo dimensions are unsupported")
            image.load()
            return width, height
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("photo bytes are invalid") from error
