import os
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import requests
import time

from app import IMAGE_CACHE, app, free_visible_filter, lookup_payload, post_is_locked, present_post
from db import Database
from media import (
    audio_header_patch,
    chunks_are_playable,
    content_type,
    file_plan,
    iter_file,
    link_is_live,
    live_is_recording,
    looks_like_image,
    media_plan,
    read_image_bytes,
    replace_urls,
    take_bytes,
)
from playback import (
    assemble_source,
    build_playable_chunks,
    cached_choice,
    discard_live_cache,
    ffmpeg_remux_args,
    live_should_remux,
    live_signature,
)
import playback
import tempfile
import shutil


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()


    def test_archive_counts_run_together(self):
        class Counting(Database):
            def __init__(self):
                self.calls = 0

            def _count(self, sec_uid, extra):
                self.calls += 1
                time.sleep(0.15)
                return 4

        store = Counting()
        started = time.perf_counter()
        counts = store.post_counts("sec")
        elapsed = time.perf_counter() - started
        self.assertEqual(store.calls, 7)
        self.assertEqual(counts["all"], 4)
        self.assertEqual(counts["deleted"], 4)
        self.assertLess(elapsed, 0.6)
        self.assertEqual(IMAGE_CACHE, "public, max-age=86400")

    def test_platforms_are_tiktok_only(self):
        response = self.client.get("/api/platforms")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["platforms"], [{"id": "tiktok", "label": "TikTok"}])

    def test_profile_reads_an_existing_account_without_creating_one(self):
        class Store(object):
            def get_user(self, username):
                self.username = username
                return {
                    "username": username,
                    "name": "Khaby Lame",
                    "bio": "hello",
                    "visibility": "public",
                    "sec_uid": "sec",
                    "pfp": "https://cdn.example/a.jpg",
                }

            def posts_page(self, sec_uid, offset, limit, order, kind, status, query):
                return {
                    "posts": [{
                        "post_id": "1",
                        "type": "video",
                        "caption": "Clip",
                        "is_deleted": True,
                        "posted_at": "2026-01-01T00:00:00Z",
                        "chunks": [{"url": "https://cdn.example/a"}],
                    }],
                    "total": 40,
                }

            def post_counts(self, sec_uid):
                return {"all": 40, "video": 40, "images": 0, "live": 0, "story": 0, "archived": 10, "deleted": 30}

            def locked_video_count(self, sec_uid, cutoff):
                return 3

            def ensure_user(self, username):
                raise AssertionError("profile lookup must not add an account")

        store = Store()
        with patch("app.database", return_value=store):
            response = self.client.get("/api/profile/Khaby.Lame", headers={"X-Sma-Plan": "free"})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(store.username, "khaby.lame")
        self.assertEqual(body["name"], "Khaby Lame")
        self.assertEqual(body["total"], 40)
        self.assertEqual(body["posts"][0]["title"], "Clip")
        self.assertTrue(body["posts"][0]["locked"])
        self.assertIsNone(body["posts"][0]["mediaUrl"])

    def test_profile_is_missing_when_the_account_was_never_added(self):
        class Store(object):
            def get_user(self, username):
                return None

            def ensure_user(self, username):
                raise AssertionError("profile lookup must not add an account")

        with patch("app.database", return_value=Store()):
            response = self.client.get("/api/profile/nobody")
        self.assertEqual(response.status_code, 404)

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
                "slides": [{"url": "https://cdn.discordapp.com/attachments/1/2/a.jpg", "index": 0}],
                "thumbnail": "https://cdn.discordapp.com/attachments/1/2/thumb.jpg",
            },
            True,
        )
        self.assertTrue(payload["locked"])
        self.assertIsNone(payload["mediaUrl"])
        self.assertEqual(payload["imageUrls"], [])
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
        recent_photo = {"type": "images", "posted_at": "2026-09-10T00:00:00Z"}
        undated = {"type": "live", "posted_at": None}
        recent_live = {"type": "live", "posted_at": "2026-09-24T00:00:00Z"}
        story = {"type": "story", "posted_at": "2020-01-01T00:00:00Z"}
        self.assertTrue(post_is_locked(old, False, now))
        self.assertFalse(post_is_locked(recent, False, now))
        self.assertTrue(post_is_locked(photo, False, now))
        self.assertFalse(post_is_locked(recent_photo, False, now))
        self.assertTrue(post_is_locked(undated, False, now))
        self.assertTrue(post_is_locked(recent_live, False, now))
        self.assertFalse(post_is_locked(recent_live, True, now))
        self.assertTrue(post_is_locked(story, False, now))
        self.assertFalse(post_is_locked(old, True, now))
        self.assertEqual(free_visible_filter("2026-08-26T00:00:00Z"), "posted_at.gte.2026-08-26T00:00:00Z")

    def test_a_live_stays_recording_until_it_is_closed_or_goes_quiet(self):
        now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        fresh = [{"index": 0, "uploaded_at": "2026-09-25T11:58:00Z"}]
        stale = [{"index": 0, "uploaded_at": "2026-09-25T11:00:00Z"}]
        closed = [{"index": 0, "uploaded_at": "2026-09-25T11:59:00Z", "closed": True}]
        self.assertTrue(live_is_recording(fresh, now))
        self.assertFalse(live_is_recording(stale, now))
        self.assertFalse(live_is_recording(closed, now))
        self.assertFalse(live_is_recording([], now))

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

    def test_a_recording_live_changes_its_media_url_as_chunks_arrive(self):
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = present_post(
            {
                "post_id": "55",
                "type": "live",
                "caption": "Live",
                "is_deleted": False,
                "posted_at": recent,
                "chunks": [
                    {"index": 0, "size": 8, "uploaded_at": recent},
                    {"index": 1, "size": 8, "uploaded_at": recent},
                ],
            }
        )
        self.assertEqual(payload["mediaUrl"], "/api/media/55?n=2")
        self.assertTrue(payload["recording"])

    def test_a_live_without_an_audio_config_is_playable_and_ranges_stay_aligned(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "live_moov.bin")
        with open(fixture, "rb") as handle:
            header = handle.read()
        patch = audio_header_patch(header)
        self.assertIsNotNone(patch)
        prefix, replaced = patch
        self.assertGreater(len(prefix), replaced)
        self.assertIn(bytes.fromhex("05808080021310"), prefix)
        restored = prefix + header[replaced:]
        self.assertIsNone(audio_header_patch(restored))

        chunks = [
            {"index": 0, "size": 8388608, "url": "https://cdn.example/a"},
            {"index": 1, "size": 100, "url": "https://cdn.example/b"},
        ]
        delta = len(prefix) - replaced
        full = media_plan(chunks, None, patch)
        self.assertEqual(full["headers"]["Content-Length"], str(8388608 + 100 + delta))

        inside = media_plan(chunks, "bytes=0-7", patch)
        piece, start, end = inside["slices"][0]
        self.assertEqual(bytes(piece["inline"][start : end + 1]), prefix[:8])

        tail = media_plan(chunks, "bytes=%s-%s" % (len(prefix), len(prefix) + 9), patch)
        self.assertEqual(len(tail["slices"]), 1)
        chunk, start, end = tail["slices"][0]
        self.assertEqual(chunk["byte_offset"], replaced)
        self.assertEqual((start, end), (0, 9))
        self.assertEqual(chunk["url"], "https://cdn.example/a")

    def test_a_stalled_thumbnail_is_retried(self):
        jpeg = b"\xff\xd8\xff" + (b"x" * 32)
        self.assertTrue(looks_like_image(jpeg, ""))
        self.assertFalse(looks_like_image(b"not-an-image", "text/html"))
        calls = {"count": 0}

        def flaky(_url):
            calls["count"] += 1
            if calls["count"] < 3:
                raise requests.ConnectionError("stalled")
            return jpeg, "image/jpeg"

        data, content_type = read_image_bytes("https://cdn.example/a.jpg?ex=1", get=flaky, pause=lambda _seconds: None)
        self.assertEqual(calls["count"], 3)
        self.assertEqual(data, jpeg)
        self.assertEqual(content_type, "image/jpeg")

    def test_take_bytes_skips_a_prefix(self):
        pieces = [b"abcdef", b"ghijkl"]
        self.assertEqual(b"".join(take_bytes(pieces, 4, 4)), b"efgh")

    def test_a_local_file_range_matches_the_video_plan(self):
        full = file_plan(150, None)
        self.assertEqual(full["status"], 200)
        self.assertEqual((full["start"], full["end"]), (0, 149))
        self.assertEqual(full["headers"]["Content-Length"], "150")
        self.assertNotIn("Content-Range", full["headers"])
        opened = file_plan(150, "bytes=0-")
        self.assertEqual(opened["status"], 206)
        self.assertEqual(opened["headers"]["Content-Range"], "bytes 0-149/150")
        middle = file_plan(150, "bytes=2-5")
        self.assertEqual((middle["start"], middle["end"]), (2, 5))
        self.assertEqual(file_plan(150, "bytes=500-")["status"], 416)
        self.assertEqual(file_plan(0, None)["headers"]["Content-Length"], "0")
        directory = tempfile.mkdtemp()
        try:
            path = os.path.join(directory, "clip.bin")
            with open(path, "wb") as handle:
                handle.write(b"0123456789")
            self.assertEqual(b"".join(iter_file(path, 2, 5)), b"2345")
        finally:
            shutil.rmtree(directory)

    def test_fragmented_live_audio_is_remuxed_to_faststart_aac(self):
        self.assertTrue(live_should_remux((b"abc", 1), b"iso5"))
        self.assertTrue(live_should_remux(None, b"iso5"))
        self.assertFalse(live_should_remux(None, b"isom"))
        self.assertFalse(live_should_remux(None, b""))
        args = ffmpeg_remux_args("/tmp/in.mp4", "/tmp/out.mp4")
        self.assertIn("-c:v", args)
        self.assertEqual(args[args.index("-c:v") + 1], "copy")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertEqual(args[args.index("-b:a") + 1], "96k")
        self.assertIn("+faststart", args)
        self.assertEqual(assemble_source([b"HELLO-WORLD", b"TAIL"], b"HEY", 5), b"HEY-WORLDTAIL")
        self.assertEqual(assemble_source([b"abc", b"de"], None, 0), b"abcde")
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "live_moov.bin")
        derived = playback._patch_from_chunk(fixture)
        self.assertIsNotNone(derived)
        self.assertGreater(len(derived[0]), derived[1])

    def test_a_live_snapshot_keeps_serving_the_file_it_started_with(self):
        root = tempfile.mkdtemp()
        previous = playback.CACHE_ROOT
        playback.CACHE_ROOT = root
        try:
            directory = os.path.join(root, "post")
            os.makedirs(directory)
            chunks = [{"index": 0, "size": 8, "message_id": "m", "filename": "a.part000"}]
            signature = live_signature("post", chunks)
            older = os.path.join(directory, "older.mp4")
            exact = os.path.join(directory, signature + ".mp4")
            with open(older, "wb") as handle:
                handle.write(b"old")
            grown = list(chunks) + [{"index": 1, "size": 8, "message_id": "n", "filename": "a.part001"}]
            self.assertNotEqual(live_signature("post", grown), signature)
            self.assertEqual(os.path.realpath(cached_choice(directory, signature)), os.path.realpath(older))
            with open(exact, "wb") as handle:
                handle.write(b"exact-bytes")
            self.assertEqual(os.path.realpath(cached_choice(directory, signature)), os.path.realpath(exact))
            pin = os.path.join(directory, signature + ".pin")
            with open(pin, "w") as handle:
                handle.write(os.path.realpath(older))
            self.assertEqual(os.path.realpath(cached_choice(directory, signature)), os.path.realpath(older))
        finally:
            playback.CACHE_ROOT = previous
            shutil.rmtree(root)

    def test_a_finished_live_is_split_for_discord_and_removed_from_disk(self):
        self.assertFalse(chunks_are_playable([{"index": 0, "url": "https://cdn.example/a"}]))
        self.assertTrue(chunks_are_playable([{"index": 0, "playable": True}, {"index": 1, "playable": True}]))
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "live.mp4")
        original = b"0123456789abcdef"
        with open(path, "wb") as handle:
            handle.write(original)
        saved = []

        def upload(filename, blob):
            saved.append((filename, blob))
            return {"filename": filename, "url": "https://cdn.example/%s" % filename, "channel_id": "9", "message_id": str(len(saved))}

        chunks = build_playable_chunks(path, "post 1", upload, now="2026-09-26T00:00:00Z", piece_size=6)
        self.assertEqual(b"".join(blob for _name, blob in saved), original)
        self.assertEqual([chunk["index"] for chunk in chunks], [0, 1, 2])
        self.assertTrue(all(chunk["playable"] and chunk["closed"] for chunk in chunks))
        self.assertEqual(chunks[0]["filename"], "post1.play.part00000")
        self.assertEqual(sum(chunk["size"] for chunk in chunks), len(original))
        self.assertEqual(
            playback._old_messages(
                [
                    {"channel_id": "9", "message_id": "1"},
                    {"channel_id": "9", "message_id": "1"},
                    {"channel_id": "9", "message_id": "2", "playable": True},
                ]
            ),
            [{"channel_id": "9", "message_id": "1"}],
        )
        root = tempfile.mkdtemp()
        previous = playback.CACHE_ROOT
        playback.CACHE_ROOT = root
        try:
            kept = os.path.join(root, "post1")
            os.makedirs(kept)
            with open(os.path.join(kept, "clip.mp4"), "wb") as handle:
                handle.write(b"video")
            discard_live_cache("post 1")
            self.assertFalse(os.path.exists(kept))
            running = os.path.join(root, "busy")
            os.makedirs(running)
            with open(os.path.join(running, "job.pid"), "w") as handle:
                handle.write(str(os.getpid()))
            discard_live_cache("busy")
            self.assertTrue(os.path.isdir(running))
        finally:
            playback.CACHE_ROOT = previous
            shutil.rmtree(root)
            shutil.rmtree(directory)


    def test_thumbnail_batch_returns_several_images_from_one_lookup(self):
        import app as appmod
        import base64

        jpeg = b"\xff\xd8\xff" + (b"x" * 16)
        appmod._THUMB_CACHE.clear()
        del appmod._THUMB_ORDER[:]
        appmod._THUMB_INFLIGHT.clear()
        appmod._THUMB_BYTES = 0

        class Store(object):
            def __init__(self):
                self.lookups = 0
                self.patches = []

            def thumbnails(self, post_ids):
                self.lookups += 1
                self.asked = list(post_ids)
                return {"123456": "https://cdn.example/a.jpg", "654321": "https://cdn.example/gone.jpg"}

            def patch_post(self, post_id, fields):
                self.patches.append(post_id)

        class Files(object):
            def prepare(self, urls):
                return {url: url + "?fresh=1" for url in urls}, {}

        store = Store()

        def fake_read(url, attempts=3):
            if "gone" in url:
                raise requests.RequestException("missing")
            if attempts != 2:
                raise AssertionError(attempts)
            return jpeg, "image/jpeg"

        with patch("app.database", return_value=store), patch("app.discord_files", return_value=Files()), patch(
            "app.read_image_bytes", side_effect=fake_read
        ):
            empty = self.client.get("/api/thumbs")
            self.assertEqual(empty.get_json(), {"thumbs": []})
            response = self.client.get("/api/thumbs?ids=123456,654321,123456,nope,12")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.lookups, 1)
        self.assertEqual(store.asked, ["123456", "654321"])
        self.assertEqual(store.patches, ["123456", "654321"])
        body = response.get_json()["thumbs"]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["id"], "123456")
        self.assertEqual(body[0]["type"], "image/jpeg")
        self.assertEqual(base64.b64decode(body[0]["data"]), jpeg)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=60")

        with patch("app.database", return_value=store), patch("app.discord_files", return_value=Files()):
            cached = self.client.get("/api/thumbs?ids=123456")
        self.assertEqual(store.lookups, 1)
        self.assertEqual(base64.b64decode(cached.get_json()["thumbs"][0]["data"]), jpeg)
        self.assertEqual(cached.headers["Cache-Control"], IMAGE_CACHE)
        appmod._THUMB_CACHE.clear()
        del appmod._THUMB_ORDER[:]
        appmod._THUMB_BYTES = 0



if __name__ == "__main__":
    unittest.main()
