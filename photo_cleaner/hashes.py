"""SHA-256 (exact) and difference-hash (similar). Pillow only — no numpy."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

HASH_SIZE = 8


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def phash_from_image(image: Image.Image) -> int:
    """64-bit difference hash (horizontal). Kept as phash_* for the rest of the app."""
    gray = image.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS)
    pixels = gray.tobytes()
    value = 0
    width = HASH_SIZE + 1
    for row in range(HASH_SIZE):
        start = row * width
        for col in range(HASH_SIZE):
            value = (value << 1) | int(pixels[start + col] > pixels[start + col + 1])
    return value


def phash_from_path(path: Path) -> int:
    with Image.open(path) as image:
        image.load()
        return phash_from_image(image)


def phash_to_hex(value: int) -> str:
    return f"{value:016x}"


def phash_from_hex(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None
