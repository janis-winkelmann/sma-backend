import threading
import unittest
from unittest.mock import patch

import scraper_client
from app import app, account_ready, client_address


class Store(object):
    def __init__(self, user=None, removed=False):
        self.user = user
        self.removed = removed
        self.added_seen = []

    def is_removed(self, username):
        return self.removed

    def get_user(self, username):
        return self.user

    def posts_page(self, sec_uid, offset, limit, order, kind, status, query, visible_or=None):
        return {"posts": [], "total": 0}

    def post_counts(self, sec_uid, visible_or=None):
        return {"all": 0, "video": 0, "images": 0, "live": 0, "story": 0, "archived": 0, "deleted": 0}

    def locked_video_count(self, sec_uid, cutoff):
        return 0

    def archive_stats(self, sec_uid, cutoff):
        return self.post_counts(sec_uid), 0


SNAPSHOT = {"state": "running", "phase": "listing", "action": "Scanning", "found": 2, "saved": 1, "seq": 4}


class AddAccountTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_an_account_needs_a_sec_uid_or_a_scraped_visibility_to_count_as_saved(self):
        self.assertFalse(account_ready(None))
        self.assertFalse(account_ready({"username": "x"}))
        self.assertFalse(account_ready({"username": "x", "visibility": "missing"}))
        self.assertTrue(account_ready({"username": "x", "sec_uid": "sec"}))
        self.assertTrue(account_ready({"username": "x", "visibility": "private"}))

    def test_a_username_that_is_not_on_tiktok_is_reported_and_not_added(self):
        with patch("app.database", return_value=Store()), patch(
            "app.scraper_client.add_account", return_value=(404, {"status": "missing"})
        ):
            response = self.client.get("/api/lookup?user=zzznobody")
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertTrue(body["notFound"])
        self.assertIn("doesn't exist on TikTok", body["error"])

    def test_a_check_that_could_not_run_asks_the_visitor_to_retry(self):
        with patch("app.database", return_value=Store()), patch(
            "app.scraper_client.add_account", side_effect=scraper_client.ScraperUnavailable("down")
        ):
            response = self.client.post("/api/add?user=nasa")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.get_json()["retry"])

    def test_too_many_new_accounts_are_refused(self):
        with patch("app.database", return_value=Store()), patch(
            "app.scraper_client.add_account", return_value=(429, {"status": "rate_limited"})
        ):
            response = self.client.post("/api/add?user=nasa")
        self.assertEqual(response.status_code, 429)

    def test_a_new_account_comes_back_with_its_profile_already_filled_in(self):
        sent = {}

        def add(username, address=""):
            sent["username"] = username
            sent["address"] = address
            return 200, {"status": "added", "username": username, "name": "NASA", "bio": "Explore", "visibility": "public", "scrape": SNAPSHOT}

        with patch("app.database", return_value=Store(None)), patch("app.scraper_client.add_account", side_effect=add):
            response = self.client.post("/api/add?user=@NASA", headers={"X-Real-IP": "9.9.9.9"})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "added")
        self.assertEqual(body["name"], "NASA")
        self.assertEqual(body["bio"], "Explore")
        self.assertEqual(body["firstScrape"]["state"], "running")
        self.assertEqual(sent, {"username": "nasa", "address": "9.9.9.9"})

    def test_lookup_of_a_new_account_is_flagged_added_with_no_posts_yet(self):
        payload = {"status": "added", "username": "nasa", "name": "NASA", "bio": "Explore", "visibility": "public", "scrape": SNAPSHOT}
        with patch("app.database", return_value=Store(None)), patch(
            "app.scraper_client.add_account", return_value=(200, payload)
        ):
            response = self.client.get("/api/lookup?user=nasa")
        body = response.get_json()
        self.assertTrue(body["added"])
        self.assertEqual(body["name"], "NASA")
        self.assertEqual(body["posts"], [])
        self.assertEqual(body["firstScrape"]["found"], 2)

    def test_an_account_saved_before_sign_in_is_not_added_again(self):
        user = {"username": "nasa", "name": "NASA", "bio": "Explore", "sec_uid": "sec", "visibility": "public"}
        with patch("app.database", return_value=Store(user)), patch(
            "app.scraper_client.add_account", side_effect=AssertionError("must not add twice")
        ), patch("app.scraper_client.scrape_state", return_value=SNAPSHOT):
            response = self.client.get("/api/lookup?user=nasa")
        body = response.get_json()
        self.assertFalse(body["added"])
        self.assertEqual(body["firstScrape"]["state"], "running")

    def test_a_saved_account_with_no_first_scrape_has_none(self):
        user = {"username": "nasa", "sec_uid": "sec", "visibility": "public"}
        with patch("app.database", return_value=Store(user)), patch("app.scraper_client.scrape_state", return_value=None):
            response = self.client.get("/api/lookup?user=nasa")
        self.assertIsNone(response.get_json()["firstScrape"])

    def test_a_removed_account_is_never_added(self):
        with patch("app.database", return_value=Store(removed=True)), patch(
            "app.scraper_client.add_account", side_effect=AssertionError("removed accounts stay gone")
        ):
            response = self.client.post("/api/add?user=gone")
        self.assertEqual(response.status_code, 410)

    def test_the_visitor_address_comes_from_the_proxy_header(self):
        with app.test_request_context("/", headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}):
            self.assertEqual(client_address(), "1.1.1.1")
        with app.test_request_context("/", headers={"X-Real-IP": "3.3.3.3", "X-Forwarded-For": "1.1.1.1"}):
            self.assertEqual(client_address(), "3.3.3.3")


class LiveStreamTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_the_stream_passes_scraper_events_through(self):
        lines = [b"event: snapshot\n", b"data: {}\n", b"\n"]
        with patch("app.scraper_client.stream_events", return_value=iter(lines)):
            response = self.client.get("/api/live/nasa")
            body = response.get_data()
        self.assertEqual(response.mimetype, "text/event-stream")
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        self.assertEqual(body, b"".join(lines))

    def test_the_stream_reports_idle_when_the_scraper_is_down(self):
        def down(username):
            raise scraper_client.ScraperUnavailable("down")
            yield b""

        with patch("app.scraper_client.stream_events", side_effect=down):
            body = self.client.get("/api/live/nasa").get_data()
        self.assertIn(b"event: idle", body)

    def test_a_bad_username_is_refused(self):
        self.assertEqual(self.client.get("/api/live/").status_code, 404)


class ClientTest(unittest.TestCase):
    def test_events_are_read_line_by_line_from_the_scraper(self):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"event: idle\ndata: {}\n\n")
                self.wfile.flush()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.dict("os.environ", {"SMA_SCRAPER_URL": "http://127.0.0.1:%s" % server.server_address[1]}):
                lines = list(scraper_client.stream_events("nasa"))
        finally:
            server.shutdown()
        self.assertEqual(lines, [b"event: idle\n", b"data: {}\n", b"\n"])

    def test_state_is_none_when_the_scraper_is_unreachable(self):
        with patch.dict("os.environ", {"SMA_SCRAPER_URL": "http://127.0.0.1:1"}):
            self.assertIsNone(scraper_client.scrape_state("nasa"))
            with self.assertRaises(scraper_client.ScraperUnavailable):
                scraper_client.add_account("nasa")


if __name__ == "__main__":
    unittest.main()
