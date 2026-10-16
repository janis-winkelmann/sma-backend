import os
import tempfile
import time
import unittest
from unittest.mock import patch

import image_cache


JPEG = b"\xff\xd8\xff" + (b"x" * 16)


class ImageCacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"SMA_IMAGE_CACHE": self.directory})
        self.env.start()
        image_cache.clear_image_cache()

    def tearDown(self):
        image_cache.clear_image_cache()
        self.env.stop()

    def test_a_signed_url_reuses_the_saved_image(self):
        stored = "https://cdn.discordapp.com/attachments/1/2/face.jpg?ex=aaa"
        refreshed = "https://cdn.discordapp.com/attachments/1/2/face.jpg?ex=bbb"
        image_cache.store_cached_image(stored, JPEG, "image/jpeg")
        self.assertEqual(image_cache.cached_image(refreshed), (JPEG, "image/jpeg"))

    def test_an_old_file_is_read_again(self):
        url = "https://cdn.example/old.jpg"
        image_cache.store_cached_image(url, JPEG, "image/jpeg")
        path = image_cache._path_for(url)
        old = time.time() - image_cache.CACHE_TTL - 10
        os.utime(path, (old, old))
        self.assertIsNone(image_cache.cached_image(url))

    def test_a_file_that_is_not_an_image_is_ignored(self):
        url = "https://cdn.example/note.txt"
        image_cache.store_cached_image(url, b"hello", "text/plain")
        self.assertIsNone(image_cache.cached_image(url))


if __name__ == "__main__":
    unittest.main()
