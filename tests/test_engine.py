from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from photo_cleaner.engine import Engine


def write_jpeg(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (80, 60)) -> None:
    Image.new("RGB", size, color).save(path, "JPEG", quality=90)


def write_pattern(path: Path, size: tuple[int, int] = (120, 80), quality: int = 95) -> None:
    img = Image.new("RGB", size, (0, 0, 0))
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = ((x * 3) % 256, (y * 5) % 256, (x + y) % 256)
    img.save(path, "JPEG", quality=quality)


class EngineTests(unittest.TestCase):
    def test_exact_and_similar_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = root / "holiday.jpg"
            dup = root / "holiday copy.jpg"
            write_jpeg(keep, (220, 30, 30))
            write_jpeg(dup, (220, 30, 30))

            original = root / "walk.jpg"
            similar = root / "walk-small.jpg"
            shot = root / "Screenshot walk.png"
            write_pattern(original, (160, 100), quality=95)
            write_pattern(similar, (80, 50), quality=40)
            Image.open(original).resize((80, 50)).save(shot, "PNG")

            engine = Engine(root=root, threshold=10, cache_dir=root / "_photo_cleaner")
            engine.run()

            self.assertTrue(engine.exact_ready)
            self.assertTrue(engine.similar_ready)
            self.assertGreaterEqual(len(engine.exact_groups), 1)
            names = {p.name for g in engine.exact_groups for p in g.files}
            self.assertIn("holiday.jpg", names)
            self.assertIn("holiday copy.jpg", names)
            keeper_names = [next(f.name for f in g.files if f.id == g.keeper_id) for g in engine.exact_groups]
            self.assertIn("holiday.jpg", keeper_names)

            similar_names = {p.name for g in engine.similar_groups for p in g.files}
            clustered = similar_names | {p.name for g in engine.other_groups for p in g.files}
            self.assertTrue(
                {"walk.jpg", "walk-small.jpg"} <= clustered,
                f"expected similar pair, got similar={similar_names!r} other="
                f"{ {p.name for g in engine.other_groups for p in g.files}!r}",
            )
            self.assertIn("Screenshot walk.png", clustered)


if __name__ == "__main__":
    unittest.main()
