import unittest

from app import app, lookup_payload, present_post
from media import content_type, media_plan, take_bytes


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_platforms_are_tiktok_only(self):
        response = self.client.get("/api/platforms")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["platforms"], [{"id": "tiktok", "label": "TikTok"}])

    def test_other_platforms_are_rejected(self):
        response = self.client.get("/api/lookup?user=sma&platform=instagram")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Only TikTok is available.")

    def test_present_post(self):
        payload = present_post(
            {
                "post_id": "123",
                "type": "video",
                "caption": "Hello",
                "is_deleted": True,
                "posted_at": "2026-03-02T12:00:00Z",
                "chunks": [{"url": "https://cdn.discordapp.com/attachments/1/2/123.part000"}],
                "thumbnail": "https://cdn.discordapp.com/attachments/1/2/thumb.jpg",
            }
        )
        self.assertEqual(payload["title"], "Hello")
        self.assertEqual(payload["status"], "Deleted")
        self.assertIn("Removed from the profile", payload["detail"])
        self.assertEqual(payload["mediaUrl"], "/api/media/123")
        self.assertEqual(payload["thumbnailUrl"], "/api/thumb/123")
        self.assertEqual(payload["imageUrls"], [])

    def test_photo_post_lists_each_slide(self):
        payload = present_post(
            {
                "post_id": "9",
                "type": "images",
                "caption": "Trip",
                "is_deleted": False,
                "chunks": [],
                "slides": [
                    {"url": "https://cdn.discordapp.com/attachments/1/2/a.jpg", "index": 0},
                    {"url": "https://cdn.discordapp.com/attachments/1/2/b.jpg", "index": 1},
                ],
            }
        )
        self.assertEqual(payload["type"], "images")
        self.assertEqual(payload["imageUrls"], ["/api/slide/9/0", "/api/slide/9/1"])
        self.assertEqual(payload["imageCount"], 2)
        self.assertIsNone(payload["mediaUrl"])

    def test_new_username_is_flagged_and_hides_posts(self):
        payload = lookup_payload({"username": "newname"}, [{"post_id": "1", "caption": "x"}], True, "newname")
        self.assertTrue(payload["added"])
        self.assertEqual(payload["posts"], [])
        self.assertEqual(payload["total"], 0)

    def test_existing_username_includes_posts(self):
        payload = lookup_payload(
            {"username": "known", "sec_uid": "sec"},
            [{"post_id": "1", "type": "video", "caption": "Clip", "is_deleted": False, "chunks": []}],
            False,
            "known",
        )
        self.assertFalse(payload["added"])
        self.assertEqual(payload["posts"][0]["title"], "Clip")
        self.assertIsNone(payload["posts"][0]["thumbnailUrl"])

    def test_content_type(self):
        self.assertEqual(content_type("video", [{"filename": "1.part000"}]), "video/mp4")
        self.assertEqual(content_type("images", [{"filename": "1.jpg"}]), "image/jpeg")

    def test_video_range_covers_the_tail_without_the_whole_file(self):
        chunks = [
            {"index": 0, "size": 100, "url": "https://cdn.example/a"},
            {"index": 1, "size": 50, "url": "https://cdn.example/b"},
        ]
        full = media_plan(chunks, None)
        self.assertEqual(full["status"], 200)
        self.assertEqual(full["headers"]["Content-Length"], "150")
        self.assertEqual(full["headers"]["Accept-Ranges"], "bytes")
        self.assertNotIn("Content-Range", full["headers"])

        opened = media_plan(chunks, "bytes=0-")
        self.assertEqual(opened["status"], 206)
        self.assertEqual(opened["headers"]["Content-Range"], "bytes 0-149/150")

        tail = media_plan(chunks, "bytes=140-")
        self.assertEqual(tail["status"], 206)
        self.assertEqual(tail["headers"]["Content-Range"], "bytes 140-149/150")
        self.assertEqual(tail["headers"]["Content-Length"], "10")
        self.assertEqual([(item[1], item[2]) for item in tail["slices"]], [(40, 49)])

        across = media_plan(chunks, "bytes=90-109")
        self.assertEqual([(item[0]["index"], item[1], item[2]) for item in across["slices"]], [(0, 90, 99), (1, 0, 9)])

        self.assertEqual(media_plan(chunks, "bytes=500-")["status"], 416)
        self.assertIsNone(media_plan([{"index": 0, "url": "https://cdn.example/a"}], None)["slices"])

    def test_take_bytes_skips_a_prefix(self):
        pieces = [b"abcdef", b"ghijkl"]
        self.assertEqual(b"".join(take_bytes(pieces, 4, 4)), b"efgh")


if __name__ == "__main__":
    unittest.main()
