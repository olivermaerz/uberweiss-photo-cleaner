"""File types, walk rules, classification, and keeper scoring."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STILL_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".gif",
    ".tif",
    ".tiff",
    ".webp",
    ".bmp",
    ".raw",
    ".cr2",
    ".nef",
    ".dng",
    ".arw",
    ".orf",
    ".rw2",
    ".nrw",
    ".psd",
    ".avif",
    ".jpf",
}

VIDEO_EXTS = {
    ".mov",
    ".mp4",
    ".avi",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".mkv",
    ".360",
}

MEDIA_EXTS = STILL_EXTS | VIDEO_EXTS

SKIP_DIR_NAMES = {
    "_photo_cleaner",
    "_duplicate_reports",
    "_import_reports",
    "_date_reports",
    "_gopro_segment_reports",
    ".Trash",
    ".Trashes",
    "Photo Booth Library",
    "Photos Library.photoslibrary",
    "twenty.photoslibrary",
}

PENALTY_SUBSTRINGS = (
    " copy",
    " copy.",
    " (1)",
    " (2)",
    " (3)",
    "__2.",
    "__3.",
    "__4.",
)

SCREENSHOT_NAME_RE = re.compile(
    r"(?i)(^|[^a-z])(screenshot|screen.?shot|screen.?recording)([^a-z]|$)"
)
IOS_SCREENSHOT_STEM_RE = re.compile(r"(?i)^IMG_\d+$")
WHATSAPP_RE = re.compile(r"(?i)^IMG-\d{8}-WA\d+")

KIND_PHOTO = "photo"
KIND_SCREENSHOT = "screenshot"
KIND_OTHER = "other"
KIND_MOVIE = "movie"
KIND_LIVE_VIDEO = "live_video"

KIND_RANK = {
    KIND_PHOTO: 4,
    KIND_OTHER: 3,
    KIND_SCREENSHOT: 2,
    KIND_LIVE_VIDEO: 1,
    KIND_MOVIE: 0,
}


@dataclass
class MediaFile:
    id: int
    path: Path
    relpath: str
    size: int
    mtime: float
    ext: str
    kind: str = KIND_OTHER
    sha256: str | None = None
    phash: int | None = None
    shot_time: str | None = None
    make: str | None = None
    model: str | None = None
    software: str | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None
    meta_checked: bool = False
    extras: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def is_still(self) -> bool:
        return self.kind in {KIND_PHOTO, KIND_SCREENSHOT, KIND_OTHER}

    @property
    def is_video(self) -> bool:
        return self.kind in {KIND_MOVIE, KIND_LIVE_VIDEO}


def is_media(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTS and not path.name.startswith(".")


def should_skip_dir(name: str) -> bool:
    if name.startswith("."):
        return True
    if name in SKIP_DIR_NAMES:
        return True
    if name.endswith(".photoslibrary"):
        return True
    return False


def iter_media(root: Path):
    """Yield media files under root, skipping cache/trash/library bundles."""
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not should_skip_dir(d))
        for name in filenames:
            path = Path(dirpath) / name
            if not is_media(path):
                continue
            yield path


def keep_score(path: Path, root: Path) -> tuple:
    """Higher is more preferred to KEEP."""
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = path.name
    name = path.name.lower()
    rel_l = rel.lower()
    penalties = sum(1 for s in PENALTY_SUBSTRINGS if s in name or s in rel_l)
    if "__" in path.stem:
        penalties += 1
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = float("inf")
    return (
        -penalties,
        -len(Path(rel).parts),
        -len(path.name),
        -mtime,
        -len(rel),
        rel,
    )


def similar_keep_score(item: MediaFile, root: Path) -> tuple:
    return (KIND_RANK.get(item.kind, 0),) + keep_score(item.path, root)


def looks_like_screenshot_name(name: str) -> bool:
    return bool(SCREENSHOT_NAME_RE.search(name))


def classify_kind(
    *,
    ext: str,
    name: str,
    make: str | None,
    model: str | None,
    software: str | None,
    shot_time: str | None,
) -> str:
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return KIND_MOVIE
    if looks_like_screenshot_name(name):
        return KIND_SCREENSHOT
    if WHATSAPP_RE.match(Path(name).stem):
        return KIND_OTHER
    software_l = (software or "").lower()
    if "screenshot" in software_l or "screen shot" in software_l:
        return KIND_SCREENSHOT
    make_s = (make or "").strip()
    model_s = (model or "").strip()
    if make_s and model_s and shot_time:
        return KIND_PHOTO
    if make_s and shot_time:
        return KIND_PHOTO
    # iOS camera-roll screenshots are often IMG_1234.PNG with no camera make.
    if ext in {".png", ".heic", ".heif"} and not make_s:
        if IOS_SCREENSHOT_STEM_RE.match(Path(name).stem):
            return KIND_SCREENSHOT
        return KIND_SCREENSHOT if ext == ".png" else KIND_OTHER
    if not make_s:
        return KIND_OTHER
    return KIND_PHOTO if shot_time else KIND_OTHER


def mark_live_photos(files: list[MediaFile]) -> None:
    """IMG_1234.HEIC + IMG_1234.MOV in the same folder is a Live Photo pair."""
    by_key: dict[tuple[str, str], list[MediaFile]] = {}
    for item in files:
        key = (str(item.path.parent).lower(), item.path.stem.lower())
        by_key.setdefault(key, []).append(item)
    for group in by_key.values():
        stills = [f for f in group if f.ext in STILL_EXTS]
        videos = [f for f in group if f.ext in VIDEO_EXTS]
        if stills and videos:
            for vid in videos:
                vid.kind = KIND_LIVE_VIDEO


def parse_shot_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("-", ":")
    for fmt, size in (("%Y:%m:%d %H:%M:%S", 19), ("%Y:%m:%d %H:%M:%S", 19)):
        chunk = text[:size]
        try:
            return datetime.strptime(chunk, fmt)
        except ValueError:
            continue
    return None


def shot_times_close(a: str | None, b: str | None, seconds: int = 2) -> bool:
    da, db = parse_shot_time(a), parse_shot_time(b)
    if da is None or db is None:
        return False
    return abs((da - db).total_seconds()) <= seconds


def format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"
