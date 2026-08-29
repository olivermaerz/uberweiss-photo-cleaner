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

    def test_partial_trash_dismisses_leftover_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jpeg(root / "shot.jpg", (40, 80, 180))
            write_jpeg(root / "shot copy.jpg", (40, 80, 180))
            write_jpeg(root / "shot (1).jpg", (40, 80, 180))
            write_jpeg(root / "solo.jpg", (10, 200, 10))
            write_jpeg(root / "solo copy.jpg", (10, 200, 10))

            engine = Engine(root=root, threshold=8, cache_dir=root / "_photo_cleaner")
            engine.run()
            self.assertEqual(len(engine.exact_groups), 2)

            triple = next(g for g in engine.exact_groups if len(g.files) == 3)
            extra = next(f for f in triple.files if f.id != triple.keeper_id)
            leftover_ids = {f.id for f in triple.files if f.id != extra.id}

            engine.remove_files([extra.id])

            remaining_names = {f.name for g in engine.exact_groups for f in g.files}
            self.assertEqual(remaining_names, {"solo.jpg", "solo copy.jpg"})
            self.assertEqual(engine.dismissed_ids, leftover_ids)
            self.assertTrue(leftover_ids.issubset(engine.files))
            browsed, _total = engine.browse_other(None, 0, 60)
            self.assertTrue(leftover_ids.isdisjoint({f.id for f in browsed}))
            self.assertEqual(engine.snapshot()["kept_count"], len(leftover_ids))

            self.assertEqual(engine.reset_kept(), len(leftover_ids))
            self.assertEqual(engine.snapshot()["kept_count"], 0)
            self.assertEqual(engine.dismissed_ids, set())
            restored = {f.id for g in engine.exact_groups for f in g.files}
            self.assertTrue(leftover_ids <= restored)
            self.assertEqual(len(engine.exact_groups), 2)

    def test_partial_trash_dismisses_other_screenshot_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "Screenshot walk.png"
            similar = root / "Screenshot walk-small.png"
            write_pattern(original, (160, 100), quality=95)
            write_pattern(similar, (80, 50), quality=40)

            engine = Engine(root=root, threshold=10, cache_dir=root / "_photo_cleaner")
            engine.run()
            self.assertEqual(len(engine.other_groups), 1)
            group = engine.other_groups[0]
            extra = next(f for f in group.files if f.id != group.keeper_id)
            leftover_ids = {f.id for f in group.files if f.id != extra.id}

            engine.remove_files([extra.id])

            self.assertEqual(engine.other_groups, [])
            self.assertEqual(engine.dismissed_ids, leftover_ids)
            browsed, _total = engine.browse_other("screenshot", 0, 60)
            self.assertTrue(leftover_ids.isdisjoint({f.id for f in browsed}))

    def test_partial_trash_dismisses_ungrouped_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jpeg(root / "Screenshot red.png", (220, 20, 20), (200, 120))
            write_pattern(root / "Screenshot noise.png", (160, 100))

            engine = Engine(root=root, threshold=0, cache_dir=root / "_photo_cleaner")
            engine.run()
            self.assertEqual(engine.other_groups, [])
            browsed, total = engine.browse_other("screenshot", 0, 60)
            self.assertEqual(total, 2)
            self.assertEqual(len(browsed), 2)

            trash = browsed[0]
            keep = browsed[1]
            trash.path.unlink()
            engine.remove_files([trash.id])
            engine.dismiss_review([keep.id])

            again, again_total = engine.browse_other("screenshot", 0, 60)
            self.assertEqual(again_total, 0)
            self.assertEqual(again, [])
            self.assertIn(keep.id, engine.files)
            self.assertIn(keep.id, engine.dismissed_ids)
            self.assertEqual(engine.reset_kept(), 1)
            restored, restored_total = engine.browse_other("screenshot", 0, 60)
            self.assertEqual(restored_total, 1)
            self.assertEqual(restored[0].id, keep.id)
            engine.dismiss_review([keep.id])
            engine.close()

            engine2 = Engine(root=root, threshold=0, cache_dir=root / "_photo_cleaner")
            engine2.run()
            try:
                again, again_total = engine2.browse_other("screenshot", 0, 60)
                self.assertEqual(again_total, 0)
                self.assertEqual(again, [])
                self.assertIn(keep.name, {f.name for f in engine2.files.values()})
            finally:
                engine2.close()

    def test_restart_reuses_hashes_and_hides_dismissed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "_photo_cleaner"
            write_jpeg(root / "shot.jpg", (40, 80, 180))
            write_jpeg(root / "shot copy.jpg", (40, 80, 180))
            write_jpeg(root / "shot (1).jpg", (40, 80, 180))
            write_jpeg(root / "solo.jpg", (10, 200, 10))
            write_jpeg(root / "solo copy.jpg", (10, 200, 10))

            engine = Engine(root=root, threshold=8, cache_dir=cache)
            engine.run()
            sha_before = {str(f.path): f.sha256 for f in engine.files.values() if f.sha256}
            phash_before = {str(f.path): f.phash for f in engine.files.values() if f.phash is not None}
            self.assertEqual(len(sha_before), 5)
            self.assertEqual(len(phash_before), 5)

            triple = next(g for g in engine.exact_groups if len(g.files) == 3)
            extra = next(f for f in triple.files if f.id != triple.keeper_id)
            leftover_names = {f.name for f in triple.files if f.id != extra.id}
            extra.path.unlink()
            engine.remove_files([extra.id])
            engine.close()

            engine2 = Engine(root=root, threshold=8, cache_dir=cache)
            engine2.run()
            try:
                sha_after = {str(f.path): f.sha256 for f in engine2.files.values()}
                for path, digest in sha_before.items():
                    if path.endswith(extra.name):
                        continue
                    self.assertEqual(sha_after.get(path), digest)
                for path, phash in phash_before.items():
                    if path.endswith(extra.name):
                        continue
                    self.assertEqual(engine2.files[engine2.path_to_id[path]].phash, phash)

                exact_names = {f.name for g in engine2.exact_groups for f in g.files}
                self.assertEqual(exact_names, {"solo.jpg", "solo copy.jpg"})
                self.assertTrue(leftover_names.isdisjoint(exact_names))
            finally:
                engine2.close()


if __name__ == "__main__":
    unittest.main()
