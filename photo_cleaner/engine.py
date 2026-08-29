"""Scan a library, cache hashes, and build exact / similar / other groups."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from photo_cleaner.hashes import file_sha256, hamming, phash_from_hex, phash_from_path, phash_to_hex
from photo_cleaner.media import (
    KIND_LIVE_VIDEO,
    KIND_MOVIE,
    KIND_PHOTO,
    MediaFile,
    VIDEO_EXTS,
    classify_kind,
    format_bytes,
    iter_media,
    keep_score,
    mark_live_photos,
    similar_keep_score,
    shot_times_close,
)
from photo_cleaner.thumbs import ensure_thumb, thumb_cache_key, thumb_path_for


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    last: sqlite3.OperationalError | None = None
    for attempt in range(8):
        conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=20.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=20000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    sha256 TEXT,
                    phash TEXT,
                    shot_time TEXT,
                    make TEXT,
                    model TEXT,
                    software TEXT,
                    width INTEGER,
                    height INTEGER,
                    meta_checked INTEGER DEFAULT 0
                )
                """
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            if "meta_checked" not in cols:
                conn.execute("ALTER TABLE files ADD COLUMN meta_checked INTEGER DEFAULT 0")
            try:
                conn.execute("PRAGMA journal_mode=WAL").fetchone()
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.OperationalError:
                pass
            conn.commit()
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            if not _locked(exc):
                raise
            last = exc
            time.sleep(min(0.25 * (2**attempt), 2.0))
    if last is not None:
        raise last
    raise sqlite3.OperationalError("could not open index")


def _locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        parent = self.parent
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


@dataclass
class FileGroup:
    id: str
    tab: str  # exact | similar | other
    reason: str
    waste_bytes: int
    distance: int
    files: list[MediaFile]
    keeper_id: int
    suggested_delete_ids: list[int]


@dataclass
class Engine:
    root: Path
    threshold: int = 8
    cache_dir: Path | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve()
        if self.cache_dir is None:
            self.cache_dir = self.root / "_photo_cleaner"
        else:
            self.cache_dir = Path(self.cache_dir).expanduser().resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "thumbs").mkdir(parents=True, exist_ok=True)

        self.lock = threading.Lock()
        self.phase = "starting"
        self.message = "Waiting to scan…"
        self.progress_current = 0
        self.progress_total = 0
        self.error: str | None = None
        self.exact_ready = False
        self.similar_ready = False

        self.files: dict[int, MediaFile] = {}
        self.path_to_id: dict[str, int] = {}
        self.exact_groups: list[FileGroup] = []
        self.similar_groups: list[FileGroup] = []
        self.other_groups: list[FileGroup] = []
        self.dismissed_ids: set[int] = set()
        self._conn = _connect(self.cache_dir / "index.sqlite")
        self._db_lock = threading.Lock()

    def cache_key(self, item: MediaFile) -> str:
        return thumb_cache_key(item.path)

    def thumb_path(self, item: MediaFile) -> Path:
        return thumb_path_for(self.cache_dir, self.cache_key(item))

    def _db_run(self, fn, retries: int = 8):
        delay = 0.05
        last: sqlite3.OperationalError | None = None
        for _ in range(retries):
            try:
                with self._db_lock:
                    return fn()
            except sqlite3.OperationalError as exc:
                if not _locked(exc):
                    raise
                last = exc
                time.sleep(delay)
                delay = min(delay * 2, 0.8)
        if last is not None:
            raise last
        return None

    def _db_commit(self) -> None:
        self._db_run(lambda: self._conn.commit())

    def set_status(self, phase: str, message: str, current: int = 0, total: int = 0) -> None:
        with self.lock:
            self.phase = phase
            self.message = message
            self.progress_current = current
            self.progress_total = total

    def snapshot(self) -> dict:
        with self.lock:
            exact_waste = sum(g.waste_bytes for g in self.exact_groups)
            similar_waste = sum(g.waste_bytes for g in self.similar_groups)
            other_waste = sum(g.waste_bytes for g in self.other_groups)
            kinds = defaultdict(int)
            for item in self.files.values():
                kinds[item.kind] += 1
            return {
                "root": str(self.root),
                "phase": self.phase,
                "message": self.message,
                "progress_current": self.progress_current,
                "progress_total": self.progress_total,
                "error": self.error,
                "exact_ready": self.exact_ready,
                "similar_ready": self.similar_ready,
                "file_count": len(self.files),
                "exact_groups": len(self.exact_groups),
                "similar_groups": len(self.similar_groups),
                "other_groups": len(self.other_groups),
                "exact_waste_bytes": exact_waste,
                "similar_waste_bytes": similar_waste,
                "other_waste_bytes": other_waste,
                "kinds": dict(kinds),
                "threshold": self.threshold,
            }

    def run(self) -> None:
        try:
            self._walk_and_load()
            if self.stop_event.is_set():
                return
            self._classify_exif()
            if self.stop_event.is_set():
                return
            mark_live_photos(list(self.files.values()))
            self._exact_duplicates()
            with self.lock:
                self.exact_ready = True
            if self.stop_event.is_set():
                return
            self._thumbs_and_phash()
            if self.stop_event.is_set():
                return
            self._similar_groups()
            self.set_status("ready", "Ready to review.", len(self.files), len(self.files))
            with self.lock:
                self.similar_ready = True
        except Exception as exc:
            self.error = str(exc)
            self.set_status("error", f"Failed: {exc}")
            raise

    def _walk_and_load(self) -> None:
        self.set_status("scanning", "Walking files…")
        cached = self._load_cache()
        items: list[MediaFile] = []
        seen_paths: set[str] = set()
        total = 0
        for path in iter_media(self.root):
            if self.stop_event.is_set():
                return
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size <= 0:
                continue
            total += 1
            abs_path = str(path.resolve())
            seen_paths.add(abs_path)
            row = cached.get(abs_path)
            fresh = (
                row is not None
                and row["size"] == stat.st_size
                and abs(row["mtime"] - stat.st_mtime) < 0.001
            )
            try:
                rel = str(path.resolve().relative_to(self.root))
            except ValueError:
                rel = path.name
            item = MediaFile(
                id=0,
                path=path.resolve(),
                relpath=rel,
                size=stat.st_size,
                mtime=stat.st_mtime,
                ext=path.suffix.lower(),
                sha256=row["sha256"] if fresh else None,
                phash=phash_from_hex(row["phash"]) if fresh else None,
                shot_time=row["shot_time"] if fresh else None,
                make=row["make"] if fresh else None,
                model=row["model"] if fresh else None,
                software=row["software"] if fresh else None,
                width=row["width"] if fresh else None,
                height=row["height"] if fresh else None,
                meta_checked=_row_flag(row, "meta_checked") if fresh else False,
            )
            item.kind = classify_kind(
                ext=item.ext,
                name=item.name,
                make=item.make,
                model=item.model,
                software=item.software,
                shot_time=item.shot_time,
            )
            if item.ext in VIDEO_EXTS:
                item.kind = KIND_MOVIE
            items.append(item)
            if total % 1000 == 0:
                self.set_status("scanning", f"Found {total} media files…", total, total)

        stale = [p for p in cached if p not in seen_paths]
        if stale:
            def drop_stale() -> None:
                self._conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in stale])
                self._conn.commit()

            self._db_run(drop_stale)

        items.sort(key=lambda f: f.relpath.lower())
        files: dict[int, MediaFile] = {}
        path_to_id: dict[str, int] = {}
        for i, item in enumerate(items, start=1):
            item.id = i
            files[i] = item
            path_to_id[str(item.path)] = i

        with self.lock:
            self.files = files
            self.path_to_id = path_to_id
        self.set_status("scanning", f"Indexed {len(files)} media files.", len(files), len(files))

    def _load_cache(self) -> dict[str, sqlite3.Row]:
        with self._db_lock:
            rows = self._conn.execute("SELECT * FROM files").fetchall()
        return {row["path"]: row for row in rows}

    def _upsert(self, item: MediaFile) -> None:
        row = (
            str(item.path),
            item.size,
            item.mtime,
            item.sha256,
            phash_to_hex(item.phash) if item.phash is not None else None,
            item.shot_time,
            item.make,
            item.model,
            item.software,
            item.width,
            item.height,
            1 if item.meta_checked else 0,
        )

        def write() -> None:
            self._conn.execute(
                """
                INSERT INTO files (
                    path, size, mtime, sha256, phash, shot_time, make, model,
                    software, width, height, meta_checked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    sha256=excluded.sha256,
                    phash=excluded.phash,
                    shot_time=excluded.shot_time,
                    make=excluded.make,
                    model=excluded.model,
                    software=excluded.software,
                    width=excluded.width,
                    height=excluded.height,
                    meta_checked=excluded.meta_checked
                """,
                row,
            )
            self._conn.commit()

        self._db_run(write)

    def _classify_exif(self) -> None:
        stills = [
            f
            for f in self.files.values()
            if f.ext not in VIDEO_EXTS and not f.meta_checked
        ]
        if not stills:
            self.set_status("metadata", "No new metadata to read.")
            return
        if shutil.which("exiftool") is None:
            self.set_status("metadata", "exiftool not found — classifying by name/extension.")
            return

        self.set_status("metadata", f"Reading EXIF for {len(stills)} files…", 0, len(stills))
        chunk = 400
        done = 0
        for i in range(0, len(stills), chunk):
            if self.stop_event.is_set():
                return
            batch = stills[i : i + chunk]
            meta = _exiftool_batch([f.path for f in batch])
            for item in batch:
                row = meta.get(str(item.path)) or meta.get(item.path.as_posix())
                if not row:
                    continue
                item.make = _exif_str(row.get("Make"))
                item.model = _exif_str(row.get("Model"))
                item.software = _exif_str(row.get("Software"))
                item.shot_time = _exif_str(row.get("DateTimeOriginal")) or _exif_str(
                    row.get("CreateDate")
                )
                item.width = _exif_int(row.get("ImageWidth"))
                item.height = _exif_int(row.get("ImageHeight"))
                item.kind = classify_kind(
                    ext=item.ext,
                    name=item.name,
                    make=item.make,
                    model=item.model,
                    software=item.software,
                    shot_time=item.shot_time,
                )
                item.meta_checked = True
                self._upsert(item)
            for item in batch:
                if not item.meta_checked:
                    item.meta_checked = True
                    self._upsert(item)
            done += len(batch)
            self._db_commit()
            self.set_status("metadata", f"Read EXIF {done}/{len(stills)}…", done, len(stills))

    def _exact_duplicates(self) -> None:
        by_size: dict[int, list[MediaFile]] = defaultdict(list)
        for item in self.files.values():
            by_size[item.size].append(item)
        candidates = {size: group for size, group in by_size.items() if len(group) > 1}
        to_hash = [f for group in candidates.values() for f in group if not f.sha256]
        self.set_status(
            "hashing",
            f"SHA-256 hashing {len(to_hash)} possible exact duplicates…",
            0,
            max(len(to_hash), 1),
        )
        hashed = 0
        for item in to_hash:
            if self.stop_event.is_set():
                return
            try:
                item.sha256 = file_sha256(item.path)
                self._upsert(item)
            except OSError as exc:
                item.error = str(exc)
            hashed += 1
            if hashed % 50 == 0:
                self._db_commit()
                self.set_status(
                    "hashing",
                    f"Hashed {hashed}/{len(to_hash)}…",
                    hashed,
                    len(to_hash),
                )
        self._db_commit()

        by_hash: dict[str, list[MediaFile]] = defaultdict(list)
        for group in candidates.values():
            for item in group:
                if item.sha256:
                    by_hash[item.sha256].append(item)

        groups: list[FileGroup] = []
        for digest, members in by_hash.items():
            unique = []
            seen = set()
            for item in members:
                key = str(item.path)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(item)
            if len(unique) < 2:
                continue
            keeper = max(unique, key=lambda f: keep_score(f.path, self.root))
            deletes = [f for f in unique if f.id != keeper.id]
            waste = keeper.size * len(deletes)
            groups.append(
                FileGroup(
                    id=f"exact:{digest[:16]}",
                    tab="exact",
                    reason="Byte-identical (SHA-256)",
                    waste_bytes=waste,
                    distance=0,
                    files=sorted(unique, key=lambda f: (f.id != keeper.id, f.relpath.lower())),
                    keeper_id=keeper.id,
                    suggested_delete_ids=[f.id for f in deletes],
                )
            )
        groups.sort(key=lambda g: g.waste_bytes, reverse=True)
        with self.lock:
            self.exact_groups = groups
        self.set_status(
            "hashing",
            f"Found {len(groups)} exact duplicate groups ({format_bytes(sum(g.waste_bytes for g in groups))}).",
            len(to_hash),
            max(len(to_hash), 1),
        )

    def ensure_thumb(self, item: MediaFile) -> Path | None:
        dest = self.thumb_path(item)
        if ensure_thumb(item.path, dest):
            return dest
        return None

    def ensure_phash(self, item: MediaFile) -> int | None:
        if item.phash is not None:
            return item.phash
        if not item.is_still and item.kind != KIND_LIVE_VIDEO:
            # Try stills only; movies rarely hash well from a frame via PIL.
            if item.kind in {KIND_MOVIE, KIND_LIVE_VIDEO}:
                return None
        thumb = self.ensure_thumb(item)
        source = thumb if thumb is not None else item.path
        try:
            item.phash = phash_from_path(source)
            self._upsert(item)
            return item.phash
        except Exception as exc:
            item.error = str(exc)
            return None

    def _thumbs_and_phash(self) -> None:
        stills = [f for f in self.files.values() if f.is_still]
        missing = [f for f in stills if f.phash is None]
        self.set_status(
            "similar",
            f"Computing perceptual hashes for {len(missing)} stills…",
            0,
            max(len(missing), 1),
        )
        done = 0
        for item in missing:
            if self.stop_event.is_set():
                return
            self.ensure_phash(item)
            done += 1
            if done % 25 == 0:
                self._db_commit()
                self.set_status(
                    "similar",
                    f"Hashed stills {done}/{len(missing)}…",
                    done,
                    len(missing),
                )
        self._db_commit()

    def _similar_groups(self) -> None:
        exact_extra_ids = {
            fid
            for group in self.exact_groups
            for fid in group.suggested_delete_ids
        }
        stills = [
            f
            for f in self.files.values()
            if f.is_still and f.phash is not None and f.id not in exact_extra_ids
        ]
        self.set_status("grouping", f"Clustering {len(stills)} stills by similarity…")
        n = len(stills)
        uf = UnionFind(n)
        distances: dict[tuple[int, int], int] = {}

        if n <= 9000:
            for i in range(n):
                if self.stop_event.is_set():
                    return
                hi = stills[i].phash
                if hi is None:
                    continue
                for j in range(i + 1, n):
                    hj = stills[j].phash
                    if hj is None:
                        continue
                    dist = hamming(hi, hj)
                    if dist <= self.threshold:
                        uf.union(i, j)
                        distances[(i, j)] = dist
                if i % 500 == 0:
                    self.set_status("grouping", f"Compared {i}/{n}…", i, n)
        else:
            buckets: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(4)]
            for i, item in enumerate(stills):
                h = int(item.phash)
                for b in range(4):
                    buckets[b][(h >> (b * 16)) & 0xFFFF].append(i)
            seen: set[tuple[int, int]] = set()
            for b in range(4):
                for idxs in buckets[b].values():
                    if len(idxs) < 2:
                        continue
                    if len(idxs) > 800:
                        continue
                    for a in range(len(idxs)):
                        for c in range(a + 1, len(idxs)):
                            i, j = idxs[a], idxs[c]
                            key = (i, j) if i < j else (j, i)
                            if key in seen:
                                continue
                            seen.add(key)
                            dist = hamming(int(stills[i].phash), int(stills[j].phash))
                            if dist <= self.threshold:
                                uf.union(i, j)
                                distances[key] = dist

        clusters: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            clusters[uf.find(i)].append(i)

        similar: list[FileGroup] = []
        other: list[FileGroup] = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            files = [stills[i] for i in members]
            keeper = max(files, key=lambda f: similar_keep_score(f, self.root))
            max_dist = 0
            file_dist: dict[int, int] = {keeper.id: 0}
            for item in files:
                if item.id == keeper.id or keeper.phash is None or item.phash is None:
                    continue
                dist = hamming(keeper.phash, item.phash)
                file_dist[item.id] = dist
                max_dist = max(max_dist, dist)
                item.extras["distance"] = dist
            keeper.extras["distance"] = 0
            deletes = []
            for item in files:
                if item.id == keeper.id:
                    continue
                dist = file_dist.get(item.id, max_dist)
                if (
                    dist <= 4
                    and shot_times_close(keeper.shot_time, item.shot_time)
                    and item.kind != KIND_PHOTO
                ):
                    deletes.append(item.id)
                elif dist <= 2 and shot_times_close(keeper.shot_time, item.shot_time):
                    deletes.append(item.id)
            waste = sum(f.size for f in files if f.id != keeper.id)
            kinds = {f.kind for f in files}
            has_photo = KIND_PHOTO in kinds
            mixed = len(kinds) > 1
            tab = "similar" if has_photo or mixed else "other"
            group = FileGroup(
                id=f"{tab}:{keeper.id}",
                tab=tab,
                reason="Perceptual hash (visually similar)",
                waste_bytes=waste,
                distance=max_dist,
                files=sorted(files, key=lambda f: (f.id != keeper.id, f.relpath.lower())),
                keeper_id=keeper.id,
                suggested_delete_ids=deletes,
            )
            (similar if tab == "similar" else other).append(group)

        similar.sort(key=lambda g: g.waste_bytes, reverse=True)
        other.sort(key=lambda g: g.waste_bytes, reverse=True)
        with self.lock:
            self.similar_groups = similar
            self.other_groups = other
        self.set_status(
            "grouping",
            f"Similar photo groups: {len(similar)}; other groups: {len(other)}.",
        )

    def groups_for(self, tab: str) -> list[FileGroup]:
        with self.lock:
            if tab == "exact":
                return list(self.exact_groups)
            if tab == "similar":
                return list(self.similar_groups)
            if tab == "other":
                return list(self.other_groups)
            return []

    def browse_other(self, kind: str | None, offset: int, limit: int) -> tuple[list[MediaFile], int]:
        grouped_ids = set()
        with self.lock:
            for group in self.similar_groups + self.other_groups:
                for item in group.files:
                    grouped_ids.add(item.id)
            files = list(self.files.values())
            dismissed = set(self.dismissed_ids)
        wanted = []
        for item in files:
            if item.kind in {KIND_MOVIE, KIND_LIVE_VIDEO, KIND_PHOTO}:
                continue
            if item.id in grouped_ids or item.id in dismissed:
                continue
            if kind and item.kind != kind:
                continue
            wanted.append(item)
        wanted.sort(key=lambda f: f.relpath.lower())
        return wanted[offset : offset + limit], len(wanted)

    def file_by_id(self, file_id: int) -> MediaFile | None:
        with self.lock:
            return self.files.get(file_id)

    def remove_files(self, ids: list[int]) -> list[MediaFile]:
        id_set = set(ids)
        removed: list[MediaFile] = []
        with self.lock:
            for fid in id_set:
                item = self.files.get(fid)
                if item is not None:
                    removed.append(item)
            paths = [str(item.path) for item in removed]
            for item in removed:
                self.files.pop(item.id, None)
                self.path_to_id.pop(str(item.path), None)
                thumb = self.thumb_path(item)
                if thumb.exists():
                    thumb.unlink(missing_ok=True)
            exact, exact_left = _strip_groups(self.exact_groups, id_set)
            similar, similar_left = _strip_groups(self.similar_groups, id_set)
            other, other_left = _strip_groups(self.other_groups, id_set)
            self.exact_groups = exact
            self.similar_groups = similar
            self.other_groups = other
            self.dismissed_ids.update(exact_left | similar_left | other_left)
            self.dismissed_ids -= id_set
        if paths:
            def drop() -> None:
                self._conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])
                self._conn.commit()

            try:
                self._db_run(drop)
            except sqlite3.OperationalError:
                pass
        return removed

    def dismiss_review(self, ids: list[int]) -> None:
        """Keep files on disk but hide them from later review pages this session."""
        id_set = {int(i) for i in ids}
        with self.lock:
            self.dismissed_ids.update(i for i in id_set if i in self.files)


def _strip_groups(groups: list[FileGroup], removed: set[int]) -> tuple[list[FileGroup], set[int]]:
    """Drop groups that lost any member. Leftovers stay on disk but leave the review queue."""
    kept_groups = []
    leftovers: set[int] = set()
    for group in groups:
        remaining = [f for f in group.files if f.id not in removed]
        if len(remaining) != len(group.files):
            leftovers.update(f.id for f in remaining)
            continue
        kept_groups.append(group)
    return kept_groups, leftovers


def _exif_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _exif_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_flag(row: sqlite3.Row, key: str) -> bool:
    try:
        return bool(row[key])
    except (KeyError, IndexError):
        return False


def _exiftool_batch(paths: list[Path]) -> dict[str, dict]:
    if not paths:
        return {}
    try:
        proc = subprocess.run(
            [
                "exiftool",
                "-j",
                "-n",
                "-fast2",
                "-Make",
                "-Model",
                "-Software",
                "-DateTimeOriginal",
                "-CreateDate",
                "-ImageWidth",
                "-ImageHeight",
                *map(str, paths),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if not proc.stdout.strip():
        return {}
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    out = {}
    for row in rows:
        src = row.get("SourceFile")
        if src:
            out[src] = row
            try:
                out[str(Path(src).resolve())] = row
            except OSError:
                pass
    return out
