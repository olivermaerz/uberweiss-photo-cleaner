from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from photo_cleaner.media import (
    KIND_MOVIE,
    KIND_OTHER,
    KIND_PHOTO,
    KIND_SCREENSHOT,
    classify_kind,
    keep_score,
    looks_like_screenshot_name,
)


class ClassifyTests(unittest.TestCase):
    def test_screenshot_filename(self) -> None:
        self.assertTrue(looks_like_screenshot_name("Screenshot 2024-01-01 at 12.00.00.png"))
        self.assertTrue(looks_like_screenshot_name("Screen Shot 2020-03-04 at 9.41.00 AM.png"))
        self.assertEqual(
            classify_kind(
                ext=".png",
                name="Screenshot 2024-01-01.png",
                make=None,
                model=None,
                software=None,
                shot_time=None,
            ),
            KIND_SCREENSHOT,
        )

    def test_camera_photo(self) -> None:
        self.assertEqual(
            classify_kind(
                ext=".heic",
                name="IMG_1234.HEIC",
                make="Apple",
                model="iPhone 12",
                software=None,
                shot_time="2024:01:01 12:00:00",
            ),
            KIND_PHOTO,
        )

    def test_movie(self) -> None:
        self.assertEqual(
            classify_kind(
                ext=".mov",
                name="IMG_1234.MOV",
                make=None,
                model=None,
                software=None,
                shot_time=None,
            ),
            KIND_MOVIE,
        )

    def test_whatsapp_is_other(self) -> None:
        self.assertEqual(
            classify_kind(
                ext=".jpg",
                name="IMG-20240101-WA0001.jpg",
                make=None,
                model=None,
                software=None,
                shot_time=None,
            ),
            KIND_OTHER,
        )

    def test_keep_score_penalizes_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "event.jpg"
            copy = root / "event copy.jpg"
            original.write_bytes(b"a")
            copy.write_bytes(b"a")
            self.assertGreater(keep_score(original, root), keep_score(copy, root))


if __name__ == "__main__":
    unittest.main()
