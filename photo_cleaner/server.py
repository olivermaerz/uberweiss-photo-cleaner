"""Local review server. Binds to 127.0.0.1 by default."""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from photo_cleaner.engine import Engine, FileGroup
from photo_cleaner.media import MediaFile, format_bytes
from photo_cleaner.thumbs import thumb_cache_key
from photo_cleaner.trash import move_to_trash

STATIC_DIR = Path(__file__).resolve().parent / "static"
BROWSER_PREVIEW_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ID_RE = re.compile(r"^/?(thumb|file)/(\d+)$")
MAX_TRASH = 500
MAX_GROUP_PAGE = 180


def _rev(item: MediaFile) -> str:
    return f"{thumb_cache_key(item.path)}-{int(item.mtime)}-{item.size}"


def file_payload(item: MediaFile, group: FileGroup | None = None) -> dict:
    keep = group is not None and item.id == group.keeper_id
    suggested = group is not None and item.id in group.suggested_delete_ids
    distance = 0
    if group is not None:
        distance = int(item.extras.get("distance", 0 if keep else group.distance))
    return {
        "id": item.id,
        "name": item.name,
        "path": str(item.path),
        "relpath": item.relpath,
        "size": item.size,
        "size_label": format_bytes(item.size),
        "kind": item.kind,
        "shot_time": item.shot_time,
        "make": item.make,
        "model": item.model,
        "width": item.width,
        "height": item.height,
        "keep": keep,
        "suggested_delete": suggested,
        "distance": distance,
        "sha256": (item.sha256 or "")[:16],
        "thumb": f"/thumb/{item.id}?v={_rev(item)}",
        "file": f"/file/{item.id}?v={_rev(item)}",
        "browser_preview": item.ext in BROWSER_PREVIEW_EXTS,
        "ext": item.ext,
    }


def group_payload(group: FileGroup) -> dict:
    return {
        "id": group.id,
        "tab": group.tab,
        "reason": group.reason,
        "waste_bytes": group.waste_bytes,
        "waste_label": format_bytes(group.waste_bytes),
        "distance": group.distance,
        "keeper_id": group.keeper_id,
        "files": [file_payload(item, group) for item in group.files],
    }


class CleanerHandler(BaseHTTPRequestHandler):
    engine: Engine

    def log_message(self, format: str, *args) -> None:
        if self.path.startswith("/thumb/") or self.path.startswith("/api/status"):
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            ico = STATIC_DIR / "favicon.ico"
            if ico.is_file():
                self._send_file(ico, "image/x-icon")
                return
        if path.startswith("/static/"):
            name = Path(path[len("/static/") :]).name
            dest = (STATIC_DIR / name).resolve()
            if dest.parent != STATIC_DIR or not dest.is_file():
                self._send_json({"error": "not found"}, 404)
                return
            ctype = mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
            self._send_file(dest, ctype)
            return
        if path == "/api/status":
            self._send_json(self.engine.snapshot())
            return
        if path == "/api/groups":
            self._handle_groups(parse_qs(parsed.query))
            return
        if path == "/api/browse":
            self._handle_browse(parse_qs(parsed.query))
            return
        match = ID_RE.match(path)
        if match:
            kind, file_id = match.group(1), int(match.group(2))
            if kind == "thumb":
                self._handle_thumb(file_id)
            else:
                self._handle_original(file_id)
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            body = self._read_json()
            if parsed.path == "/api/trash":
                self._handle_trash(body)
                return
            if parsed.path == "/api/reveal":
                self._handle_reveal(body)
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_groups(self, query: dict) -> None:
        tab = (query.get("tab") or ["exact"])[0]
        if tab not in {"exact", "similar", "other"}:
            self._send_json({"error": "bad tab"}, 400)
            return
        try:
            offset = int((query.get("offset") or ["0"])[0])
            limit = min(MAX_GROUP_PAGE, max(1, int((query.get("limit") or ["30"])[0])))
            max_files = min(MAX_TRASH, max(1, int((query.get("max_files") or [str(MAX_TRASH)])[0])))
        except ValueError:
            self._send_json({"error": "bad paging"}, 400)
            return
        groups = self.engine.groups_for(tab)
        slice_ = []
        file_count = 0
        capped = False
        for group in groups[offset:]:
            n = len(group.files)
            if slice_ and file_count + n > max_files:
                capped = True
                break
            slice_.append(group)
            file_count += n
            if len(slice_) >= limit:
                break
            if file_count >= max_files:
                capped = offset + len(slice_) < len(groups)
                break
        self._send_json(
            {
                "tab": tab,
                "total": len(groups),
                "offset": offset,
                "file_count": file_count,
                "capped": capped,
                "groups": [group_payload(g) for g in slice_],
            }
        )

    def _handle_browse(self, query: dict) -> None:
        kind = (query.get("kind") or [""])[0] or None
        if kind == "all":
            kind = None
        try:
            offset = int((query.get("offset") or ["0"])[0])
            limit = min(120, max(1, int((query.get("limit") or ["60"])[0])))
        except ValueError:
            self._send_json({"error": "bad paging"}, 400)
            return
        items, total = self.engine.browse_other(kind, offset, limit)
        self._send_json(
            {
                "total": total,
                "offset": offset,
                "files": [file_payload(item) for item in items],
            }
        )

    def _handle_thumb(self, file_id: int) -> None:
        item = self.engine.file_by_id(file_id)
        if item is None:
            self._send_json({"error": "unknown file"}, 404)
            return
        dest = self.engine.ensure_thumb(item)
        if dest is None or not dest.is_file():
            self._send_json({"error": "no thumbnail"}, 404)
            return
        self._send_file(dest, "image/jpeg")

    def _handle_original(self, file_id: int) -> None:
        item = self.engine.file_by_id(file_id)
        if item is None or not item.path.is_file():
            self._send_json({"error": "unknown file"}, 404)
            return
        try:
            item.path.resolve().relative_to(self.engine.root)
        except ValueError:
            self._send_json({"error": "outside root"}, 403)
            return
        ctype = mimetypes.guess_type(item.path.name)[0] or "application/octet-stream"
        self._send_file(item.path, ctype)

    def _handle_trash(self, body: dict) -> None:
        if not body.get("confirm"):
            self._send_json({"error": "confirm required"}, 400)
            return
        raw_ids = body.get("ids") or []
        try:
            ids = [int(x) for x in raw_ids]
        except (TypeError, ValueError):
            self._send_json({"error": "ids must be integers"}, 400)
            return
        if not ids:
            self._send_json({"error": "nothing selected"}, 400)
            return
        if len(ids) > MAX_TRASH:
            self._send_json({"error": f"max {MAX_TRASH} files per request"}, 400)
            return
        trashed = []
        errors = []
        to_remove = []
        for fid in ids:
            item = self.engine.file_by_id(fid)
            if item is None:
                errors.append({"id": fid, "error": "unknown id"})
                continue
            try:
                item.path.resolve().relative_to(self.engine.root)
            except ValueError:
                errors.append({"id": fid, "error": "outside root"})
                continue
            try:
                move_to_trash(item.path)
                trashed.append(file_payload(item))
                to_remove.append(fid)
            except Exception as exc:
                errors.append({"id": fid, "path": str(item.path), "error": str(exc)})
        if to_remove:
            try:
                self.engine.remove_files(to_remove)
            except sqlite3.OperationalError:
                pass
        self._send_json({"trashed": len(trashed), "files": trashed, "errors": errors})

    def _handle_reveal(self, body: dict) -> None:
        try:
            file_id = int(body.get("id"))
        except (TypeError, ValueError):
            self._send_json({"error": "id required"}, 400)
            return
        item = self.engine.file_by_id(file_id)
        if item is None:
            self._send_json({"error": "unknown file"}, 404)
            return
        import subprocess

        subprocess.run(["open", "-R", str(item.path)], check=False)
        self._send_json({"ok": True})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            try:
                stat = path.stat()
                self.send_header("ETag", f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"')
            except OSError:
                pass
            self.send_header("Cache-Control", "private, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def serve(engine: Engine, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (CleanerHandler,), {"engine": engine})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd


def start_scan(engine: Engine) -> threading.Thread:
    thread = threading.Thread(target=engine.run, name="scan", daemon=True)
    thread.start()
    return thread
