import unittest

from app import app


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_platforms_are_tiktok_only(self):
        response = self.client.get("/api/platforms")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["platforms"], [{"id": "tiktok", "label": "TikTok"}])

    def test_lookup_returns_archived_posts(self):
        response = self.client.get("/api/lookup?user=sma.demo&platform=tiktok")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["username"], "sma.demo")
        self.assertEqual(payload["platform"], "tiktok")
        self.assertEqual(payload["posts"][0]["title"], "Video")
        self.assertIn("Removed from the profile", payload["posts"][0]["detail"])

    def test_other_platforms_are_rejected(self):
        response = self.client.get("/api/lookup?user=sma.demo&platform=instagram")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Only TikTok is available.")

    def test_missing_username(self):
        response = self.client.get("/api/lookup?platform=tiktok")
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
