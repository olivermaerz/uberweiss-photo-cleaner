"""JPEG thumbnails via macOS sips, with a Pillow fallback."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, UnidentifiedImageError

THUMB_MAX = 480
THUMB_QUALITY = 72


def thumb_cache_key(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:20]


def thumb_path_for(cache_dir: Path, key: str) -> Path:
    return cache_dir / "thumbs" / f"{key}.jpg"


def ensure_thumb(source: Path, dest: Path, max_edge: int = THUMB_MAX) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        try:
            if dest.stat().st_mtime >= source.stat().st_mtime:
                return True
        except OSError:
            pass
    tmp = dest.with_suffix(".tmp.jpg")
    try:
        if _sips_thumb(source, tmp, max_edge) or _pillow_thumb(source, tmp, max_edge):
            tmp.replace(dest)
            return True
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return False


def _sips_thumb(source: Path, dest: Path, max_edge: int) -> bool:
    if sys.platform != "darwin" or shutil.which("sips") is None:
        return False
    try:
        proc = subprocess.run(
            [
                "sips",
                "-Z",
                str(max_edge),
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                str(THUMB_QUALITY),
                str(source),
                "--out",
                str(dest),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0


def _pillow_thumb(source: Path, dest: Path, max_edge: int) -> bool:
    try:
        with Image.open(source) as image:
            image.load()
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            rgb = image.convert("RGB")
            rgb.save(dest, "JPEG", quality=THUMB_QUALITY, optimize=True)
        return dest.is_file() and dest.stat().st_size > 0
    except (OSError, UnidentifiedImageError, ValueError):
        return False
