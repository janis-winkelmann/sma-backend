import unittest
from datetime import datetime, timezone

from app import app, free_visible_filter, lookup_payload, post_is_locked, present_post
from media import content_type, link_is_live, media_plan, replace_urls, take_bytes


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
        self.assertEqual(payload["postedAt"], "2026-03-02T12:00:00Z")
        self.assertFalse(payload["locked"])

    def test_locked_post_keeps_its_thumbnail(self):
        payload = present_post(
            {
                "post_id": "123",
                "type": "video",
                "caption": "Old",
                "is_deleted": False,
                "posted_at": "2026-01-01T00:00:00Z",
                "chunks": [{"url": "https://cdn.discordapp.com/attachments/1/2/123.part000"}],
                "thumbnail": "https://cdn.discordapp.com/attachments/1/2/thumb.jpg",
            },
            True,
        )
        self.assertTrue(payload["locked"])
        self.assertIsNone(payload["mediaUrl"])
        self.assertEqual(payload["thumbnailUrl"], "/api/thumb/123")

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

    def test_old_videos_are_locked_for_free_accounts(self):
        now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        old = {"type": "video", "posted_at": "2026-08-01T00:00:00Z"}
        recent = {"type": "video", "posted_at": "2026-09-10T00:00:00Z"}
        photo = {"type": "images", "posted_at": "2020-01-01T00:00:00Z"}
        undated = {"type": "live", "posted_at": None}
        story = {"type": "story", "posted_at": "2020-01-01T00:00:00Z"}
        self.assertTrue(post_is_locked(old, False, now))
        self.assertFalse(post_is_locked(recent, False, now))
        self.assertFalse(post_is_locked(photo, False, now))
        self.assertTrue(post_is_locked(undated, False, now))
        self.assertFalse(post_is_locked(story, False, now))
        self.assertFalse(post_is_locked(old, True, now))
        self.assertIn("posted_at.gte.2026-08-26", free_visible_filter("2026-08-26T00:00:00Z"))

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

    def test_a_live_link_is_kept_and_an_expired_one_is_replaced(self):
        now = 1_700_000_000
        live = "https://cdn.discordapp.com/attachments/1/2/file.mp4?ex=%x&is=aa&hm=bb" % (now + 500)
        expired = "https://cdn.discordapp.com/attachments/1/2/old.mp4?ex=%x&is=aa&hm=bb" % (now - 10)
        unsigned = "https://cdn.discordapp.com/attachments/1/2/plain.mp4"
        self.assertTrue(link_is_live(live, now=now))
        self.assertFalse(link_is_live(expired, now=now))
        self.assertFalse(link_is_live(unsigned, now=now))
        self.assertFalse(link_is_live(live, now=now + 400))
        refreshed = "https://cdn.discordapp.com/attachments/1/2/old.mp4?ex=%x&is=cc&hm=dd" % (now + 80000)
        rewritten = replace_urls(
            [{"index": 0, "url": expired, "size": 3}, {"index": 1, "url": live, "size": 4}],
            {expired: refreshed},
        )
        self.assertEqual(rewritten[0]["url"], refreshed)
        self.assertEqual(rewritten[1]["url"], live)
        self.assertIsNone(replace_urls([{"url": live}], {}))

    def test_take_bytes_skips_a_prefix(self):
        pieces = [b"abcdef", b"ghijkl"]
        self.assertEqual(b"".join(take_bytes(pieces, 4, 4)), b"efgh")


if __name__ == "__main__":
    unittest.main()
