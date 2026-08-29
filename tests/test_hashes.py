from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image

from photo_cleaner.hashes import hamming, phash_from_image


def solid(color: tuple[int, int, int], size: int = 64) -> Image.Image:
    return Image.new("RGB", (size, size), color)


class HashTests(unittest.TestCase):
    def test_identical_images_have_zero_distance(self) -> None:
        a = solid((200, 40, 40))
        b = solid((200, 40, 40))
        self.assertEqual(hamming(phash_from_image(a), phash_from_image(b)), 0)

    def test_different_images_are_far(self) -> None:
        a = Image.new("RGB", (64, 64), (0, 0, 0))
        b = Image.new("RGB", (64, 64), (0, 0, 0))
        pa, pb = a.load(), b.load()
        for x in range(64):
            for y in range(64):
                pa[x, y] = (x * 4, y * 3, 40)
                pb[x, y] = (255 - x * 4, 80, y * 3)
        self.assertGreater(hamming(phash_from_image(a), phash_from_image(b)), 8)

    def test_resized_copy_is_close(self) -> None:
        src = Image.new("RGB", (120, 80), (20, 80, 160))
        for x in range(120):
            for y in range(80):
                src.putpixel((x, y), ((x * 2) % 256, y, 90))
        small = src.resize((60, 40))
        dist = hamming(phash_from_image(src), phash_from_image(small))
        self.assertLessEqual(dist, 8)

    def test_jpeg_roundtrip_is_close(self) -> None:
        src = Image.new("RGB", (80, 80), (12, 140, 90))
        buf = BytesIO()
        src.save(buf, "JPEG", quality=40)
        buf.seek(0)
        jpeg = Image.open(buf)
        dist = hamming(phash_from_image(src), phash_from_image(jpeg))
        self.assertLessEqual(dist, 8)


if __name__ == "__main__":
    unittest.main()
