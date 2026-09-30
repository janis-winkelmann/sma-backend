import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import playback
from app import app

CHUNKS = [
    {"index": 0, "size": 1000, "message_id": "1", "filename": "a.mp4", "url": "u0"},
    {"index": 1, "size": 1000, "message_id": "2", "filename": "b.mp4", "url": "u1"},
]


def write(path, size):
    with open(path, "wb") as handle:
        handle.write(b"x" * size)


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.previous = playback.CACHE_ROOT
        playback.CACHE_ROOT = self.root
        self.directory = os.path.join(self.root, "live1")
        os.makedirs(self.directory)
        self.signature = playback.live_signature("live1", CHUNKS)
        pid = os.path.join(self.directory, self.signature + ".pid")
        with open(pid, "w") as handle:
            handle.write(str(os.getpid()))

    def tearDown(self):
        playback.CACHE_ROOT = self.previous
        shutil.rmtree(self.root, ignore_errors=True)

    def job(self, started):
        path = os.path.join(self.directory, self.signature + ".job.json")
        write(path, 2)
        os.utime(path, (started, started))

    def test_waiting_before_the_job_starts(self):
        os.remove(os.path.join(self.directory, self.signature + ".pid"))
        self.assertEqual(playback.prepare_progress("live1", CHUNKS)["state"], "waiting")

    def test_download_counts_finished_and_partial_chunks(self):
        self.job(1000)
        write(os.path.join(self.directory, "chunk-000.bin"), 1000)
        write(os.path.join(self.directory, "chunk-001.bin.partial"), 500)
        result = playback.prepare_progress("live1", CHUNKS, now=1010)
        self.assertEqual(result["state"], "working")
        self.assertEqual(result["stage"], "download")
        self.assertEqual(result["percent"], 48)
        self.assertEqual(result["etaSeconds"], 11)

    def test_convert_stage_follows_the_output_file(self):
        self.job(1000)
        write(os.path.join(self.directory, "chunk-000.bin"), 1000)
        write(os.path.join(self.directory, "chunk-001.bin"), 1000)
        write(os.path.join(self.directory, self.signature + ".src"), 2000)
        write(os.path.join(self.directory, self.signature + ".mp4.tmp"), 980)
        result = playback.prepare_progress("live1", CHUNKS, now=1030)
        self.assertEqual(result["stage"], "convert")
        self.assertEqual(result["percent"], 85)

    def test_ready_once_the_file_exists(self):
        write(os.path.join(self.directory, self.signature + ".mp4"), 10)
        self.assertEqual(playback.prepare_progress("live1", CHUNKS)["state"], "ready")

    def test_no_eta_at_the_very_start(self):
        self.job(1000)
        self.assertIsNone(playback.prepare_progress("live1", CHUNKS, now=1000.5)["etaSeconds"])


class PrepareRouteTest(unittest.TestCase):
    def test_route_reports_progress_for_a_live(self):
        class Store(object):
            def post(self, post_id):
                return {"post_id": post_id, "type": "live", "chunks": CHUNKS, "is_deleted": False}

        with patch("app.database", return_value=Store()), patch.dict(os.environ, {"SMA_INTERNAL_KEY": "k"}):
            with patch("app.prepare_progress", return_value={"state": "working", "stage": "download", "percent": 12, "etaSeconds": 30}):
                response = app.test_client().get(
                    "/api/prepare/live1", headers={"X-Sma-Plan": "premium", "X-Sma-Internal": "k"}
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["percent"], 12)

    def test_route_is_ready_for_a_normal_video(self):
        class Store(object):
            def post(self, post_id):
                return {"post_id": post_id, "type": "video", "chunks": CHUNKS, "is_deleted": False}

        with patch("app.database", return_value=Store()):
            response = app.test_client().get("/api/prepare/vid1")
        self.assertEqual(response.get_json()["state"], "ready")


if __name__ == "__main__":
    unittest.main()


class FakeResponse(object):
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]

    def close(self):
        pass


class DownloadTest(unittest.TestCase):
    def test_a_dropped_download_resumes_from_the_saved_bytes(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        blob = bytes(range(256)) * 400
        calls = []

        def fake_get(url, stream, timeout, headers):
            calls.append(dict(headers))
            if len(calls) == 1:
                raise playback.requests.ConnectionError("dropped")
            offset = int(headers["Range"].split("=")[1].rstrip("-")) if headers else 0
            return FakeResponse(blob[offset:], 206 if offset else 200)

        with open(os.path.join(directory, "chunk-000.bin.partial"), "wb") as handle:
            handle.write(blob[:1000])
        with patch("playback.requests.get", fake_get), patch("playback.time.sleep"):
            parts = playback._download_parts(["u"], directory, [len(blob)])
        with open(parts[0], "rb") as handle:
            self.assertEqual(handle.read(), blob)
        self.assertEqual(calls[-1], {"Range": "bytes=1000-"})


class SpanTest(unittest.TestCase):
    def test_a_stream_that_dies_halfway_continues_where_it_stopped(self):
        import media

        blob = bytes(range(256)) * 1000
        calls = []

        class Dying(FakeResponse):
            def iter_content(self, size):
                yield self.body[:70000]
                raise media.requests.ConnectionError("reset")

        def fake_get(url, headers, stream, timeout):
            calls.append(headers.get("Range"))
            if len(calls) == 1:
                return Dying(blob, 200)
            first = int(headers["Range"].split("=")[1].split("-")[0])
            return FakeResponse(blob[first:], 206)

        with patch("media.requests.get", fake_get), patch("media.time.sleep"):
            out = b"".join(media.fetch_span("u", 0, len(blob), len(blob)))
        self.assertEqual(out, blob)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(int(calls[1].split("=")[1].split("-")[0]), 65536)

    def test_gives_up_after_repeated_failures(self):
        import media

        def broken(url, headers, stream, timeout):
            raise media.requests.ConnectionError("down")

        with patch("media.requests.get", broken), patch("media.time.sleep"):
            with self.assertRaises(media.requests.ConnectionError):
                list(media.fetch_span("u", 0, 10, 10))
