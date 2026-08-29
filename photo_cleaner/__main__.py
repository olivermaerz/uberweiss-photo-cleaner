"""CLI: python3 -m photo_cleaner --root /path/to/Pictures"""

from __future__ import annotations

import argparse
import socket
import sys
import webbrowser
from pathlib import Path

from photo_cleaner import __version__
from photo_cleaner.engine import Engine
from photo_cleaner.server import serve, start_scan


def _pick_port(host: str, port: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return port
        except OSError:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review exact and similar photo duplicates in a local web UI."
    )
    parser.add_argument(
        "--root",
        default="/Volumes/Twenty/Pictures",
        help="Folder to scan (default: /Volumes/Twenty/Pictures)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument(
        "--threshold",
        type=int,
        default=8,
        help="Perceptual-hash Hamming distance for similar matches (default: 8)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Index/thumbnail cache (default: <root>/_photo_cleaner)",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser")
    parser.add_argument("--version", action="version", version=f"photo-cleaner {__version__}")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Root not found: {root}", file=sys.stderr)
        return 1
    if args.threshold < 0 or args.threshold > 32:
        print("threshold must be between 0 and 32", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir).expanduser().resolve() if args.cache_dir else None
    engine = Engine(root=root, threshold=args.threshold, cache_dir=cache_dir)
    start_scan(engine)

    port = _pick_port(args.host, args.port)
    httpd = serve(engine, args.host, port)
    url = f"http://{args.host}:{port}/"
    print(f"Scanning: {root}")
    print(f"Review UI: {url}")
    print("Stop with Ctrl+C. Files are moved to Trash only after you confirm in the UI.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
        engine.stop_event.set()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
