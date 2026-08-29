"""Move files to the macOS Trash (or platform equivalent)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def move_to_trash(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    try:
        from send2trash import send2trash

        send2trash(str(path))
        if not path.exists():
            return
    except Exception:
        pass
    _finder_delete(path)
    if path.exists():
        raise OSError(f"Failed to trash {path}")


def _finder_delete(path: Path) -> None:
    posix = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Finder" to delete POSIX file "{posix}"'
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=120)
