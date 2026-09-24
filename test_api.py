import unittest

from app import app, present_post
from media import content_type


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
            }
        )
        self.assertEqual(payload["title"], "Hello")
        self.assertEqual(payload["status"], "Deleted")
        self.assertIn("Removed from the profile", payload["detail"])
        self.assertEqual(payload["mediaUrl"], "/api/media/123")

    def test_content_type(self):
        self.assertEqual(content_type("video", [{"filename": "1.part000"}]), "video/mp4")
        self.assertEqual(content_type("images", [{"filename": "1.jpg"}]), "image/jpeg")


if __name__ == "__main__":
    unittest.main()
