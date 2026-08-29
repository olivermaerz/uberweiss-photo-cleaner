#!/usr/bin/env python3
"""Knock out the studio white and write README + web logo sizes."""

from __future__ import annotations

import argparse
import shutil
from collections import deque
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "images" / "uberweiss-source.jpg"


def near_white(r: int, g: int, b: int, limit: int) -> bool:
    return (255 - r) <= limit and (255 - g) <= limit and (255 - b) <= limit


def knockout(rgb: Image.Image, limit: int = 22) -> Image.Image:
    """Flood-fill studio white from the border. Leaves whites on the box intact."""
    w, h = rgb.size
    src = rgb.convert("RGB")
    px = src.load()
    alpha = Image.new("L", (w, h), 255)
    a = alpha.load()
    seen = bytearray(w * h)
    queue: deque[tuple[int, int]] = deque()

    def push(x: int, y: int) -> None:
        i = y * w + x
        if seen[i]:
            return
        r, g, b = px[x, y]
        if not near_white(r, g, b, limit):
            return
        seen[i] = 1
        queue.append((x, y))

    for x in range(w):
        push(x, 0)
        push(x, h - 1)
    for y in range(h):
        push(0, y)
        push(w - 1, y)

    while queue:
        x, y = queue.popleft()
        a[x, y] = 0
        if x > 0:
            push(x - 1, y)
        if x + 1 < w:
            push(x + 1, y)
        if y > 0:
            push(x, y - 1)
        if y + 1 < h:
            push(x, y + 1)

    # Chew JPEG fringe: looser white that already touches transparency.
    fringe: deque[tuple[int, int]] = deque()
    for y in range(h):
        for x in range(w):
            if a[x, y] != 0:
                continue
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h and a[nx, ny] != 0:
                    r, g, b = px[nx, ny]
                    if near_white(r, g, b, 36):
                        fringe.append((nx, ny))
    chewed = set()
    while fringe:
        x, y = fringe.popleft()
        if (x, y) in chewed:
            continue
        chewed.add((x, y))
        a[x, y] = 0
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h and a[nx, ny] != 0:
                r, g, b = px[nx, ny]
                if near_white(r, g, b, 36):
                    fringe.append((nx, ny))

    # Soften remaining anti-aliased edge against dark UI backgrounds.
    for y in range(h):
        for x in range(w):
            if a[x, y] == 0:
                continue
            r, g, b = px[x, y]
            white = min(r, g, b)
            if white < 232:
                continue
            touches = False
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h and a[nx, ny] == 0:
                    touches = True
                    break
            if touches:
                a[x, y] = max(0, min(255, int((255 - white) * 12)))

    out = src.convert("RGBA")
    out.putalpha(alpha)
    return out


def crop_opaque(im: Image.Image, pad: int = 8) -> Image.Image:
    bbox = im.getbbox()
    if not bbox:
        return im
    left, top, right, bottom = bbox
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(im.width, right + pad)
    bottom = min(im.height, bottom + pad)
    return im.crop((left, top, right, bottom))


def fit_height(im: Image.Image, height: int) -> Image.Image:
    w = max(1, round(im.width * (height / im.height)))
    return im.resize((w, height), Image.Resampling.LANCZOS)


def fit_width(im: Image.Image, width: int) -> Image.Image:
    h = max(1, round(im.height * (width / im.width)))
    return im.resize((width, h), Image.Resampling.LANCZOS)


def fit_square(im: Image.Image, size: int, fill: tuple[int, int, int, int] = (0, 0, 0, 0)) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), fill)
    scale = min((size * 0.88) / im.width, (size * 0.88) / im.height)
    nw, nh = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    resized = im.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas.paste(resized, ((size - nw) // 2, (size - nh) // 2), resized)
    return canvas


def save_png(im: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build transparent Überweiss logos.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--full", action="store_true", help="Also write images/logo-full.png")
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Source not found: {source}")

    images = ROOT / "images"
    static = ROOT / "photo_cleaner" / "static"
    images.mkdir(parents=True, exist_ok=True)

    archived = images / "uberweiss-source.jpg"
    if source != archived.resolve():
        shutil.copy2(source, archived)

    rgba = crop_opaque(knockout(Image.open(source)))
    if args.full:
        save_png(rgba, images / "logo-full.png")
    save_png(fit_width(rgba, 640), images / "logo-readme.png")

    save_png(fit_height(rgba, 128), static / "logo.png")
    save_png(fit_height(rgba, 256), static / "logo@2x.png")
    save_png(fit_square(rgba, 16), static / "favicon-16.png")
    save_png(fit_square(rgba, 32), static / "favicon-32.png")
    save_png(fit_square(rgba, 180), static / "apple-touch-icon.png")

    ico_16 = fit_square(rgba, 16)
    ico_32 = fit_square(rgba, 32)
    ico_48 = fit_square(rgba, 48)
    ico_32.save(
        static / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
        append_images=[ico_16, ico_48],
    )

    print(f"cropped {rgba.size[0]}x{rgba.size[1]} from {source}")
    written = [
        images / "logo-readme.png",
        static / "logo.png",
        static / "logo@2x.png",
        static / "favicon-16.png",
        static / "favicon-32.png",
        static / "favicon.ico",
        static / "apple-touch-icon.png",
    ]
    if args.full:
        written.insert(0, images / "logo-full.png")
    for path in written:
        print(f"  {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
