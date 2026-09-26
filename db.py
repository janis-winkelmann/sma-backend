import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

# Every gunicorn thread used to open its own TLS connection, and the count
# queries opened yet another. Those handshakes are what timed out. One pool
# per worker keeps connections warm and lets a lot of queries share them.
_POOL_LOCK = threading.Lock()
_POOL = None
_HTTP = threading.local()
QUERY_TIMEOUT = (20, 90)
log = logging.getLogger("sma.db")


def _shared_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = HTTPAdapter(pool_connections=8, pool_maxsize=40, max_retries=0, pool_block=True)
        return _POOL


class Database(object):
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self._headers = {
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }
        self.session = self._session()

    def _session(self):
        session = getattr(_HTTP, "session", None)
        if session is not None:
            return session
        session = requests.Session()
        session.headers.update(self._headers)
        session.mount("https://", _shared_pool())
        _HTTP.session = session
        return session

    def _send(self, method, url, **kwargs):
        kwargs.setdefault("timeout", QUERY_TIMEOUT)
        try:
            return getattr(self._session(), method)(url, **kwargs)
        except (requests.Timeout, requests.ConnectionError):
            # Leave the shared pool alone. This thread just borrows a different connection.
            log.warning("supabase connection failed; retrying on the shared pool")
            _HTTP.session = None
            return getattr(self._session(), method)(url, **kwargs)

    def get_user(self, username):
        response = self._send(
            "get",
            self.base + "/tiktok_users",
            params={"select": "*", "username": "eq.%s" % username, "limit": "1"},
            timeout=QUERY_TIMEOUT,
        )
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None

    def ensure_user(self, username):
        found = self.get_user(username)
        if found:
            return found, False
        response = self._send(
            "post",
            self.base + "/tiktok_users",
            headers={"Prefer": "return=representation"},
            json={"username": username},
            timeout=QUERY_TIMEOUT,
        )
        if response.status_code == 409:
            return self.get_user(username), False
        response.raise_for_status()
        rows = response.json()
        return rows[0], True

    def list_users(self):
        rows = []
        offset = 0
        page_size = 1000
        while True:
            response = self._send(
                "get",
                self.base + "/tiktok_users",
                params={
                    "select": "username,name,bio,pfp,visibility",
                    "order": "created_at.desc",
                    "limit": str(page_size),
                    "offset": str(offset),
                },
                timeout=QUERY_TIMEOUT,
            )
            response.raise_for_status()
            batch = response.json()
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            offset += page_size

    def posts_page(self, sec_uid, offset, limit, order, kind, status, query, visible_or=None):
        if not sec_uid:
            return {"posts": [], "total": 0}
        params = {
            "select": "post_id,type,caption,chunks,thumbnail,slides,is_deleted,posted_at",
            "sec_uid": "eq.%s" % sec_uid,
            "order": "posted_at.asc.nullslast,post_id.asc"
            if order == "first"
            else "posted_at.desc.nullslast,post_id.desc",
            "limit": str(limit),
            "offset": str(offset),
        }
        if kind in ("video", "images", "live", "story"):
            params["type"] = "eq.%s" % kind
        if status == "archived":
            params["is_deleted"] = "eq.false"
        elif status == "deleted":
            params["is_deleted"] = "eq.true"
        if query:
            params["caption"] = "ilike.*%s*" % query
        if visible_or:
            params["or"] = visible_or
        response = self._send(
            "get",
            self.base + "/tiktok_posts",
            headers={"Prefer": "count=exact"},
            params=params,
            timeout=QUERY_TIMEOUT,
        )
        response.raise_for_status()
        rows = response.json()
        return {"posts": rows, "total": content_range_total(response.headers.get("Content-Range"), len(rows))}

    def post_counts(self, sec_uid, visible_or=None):
        counts = empty_counts()
        if not sec_uid:
            return counts
        window = {"or": visible_or} if visible_or else {}
        jobs = [
            ("all", window or None),
            ("video", dict(window, type="eq.video")),
            ("images", dict(window, type="eq.images")),
            ("live", dict(window, type="eq.live")),
            ("story", dict(window, type="eq.story")),
            ("archived", dict(window, is_deleted="eq.false")),
            ("deleted", dict(window, is_deleted="eq.true")),
        ]
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(self._count, sec_uid, extra) for _, extra in jobs]
            for (key, _), future in zip(jobs, futures):
                counts[key] = future.result()
        return counts

    def locked_video_count(self, sec_uid, cutoff):
        if not sec_uid or not cutoff:
            return 0
        return self._count(
            sec_uid,
            {
                "or": "(posted_at.lt.%s,posted_at.is.null,type.eq.live)" % cutoff,
            },
        )

    def _count(self, sec_uid, extra):
        params = {"select": "post_id", "sec_uid": "eq.%s" % sec_uid, "limit": "1"}
        if extra:
            params.update(extra)
        # Each count runs on its own thread and borrows a connection from the shared pool.
        response = self._send(
            "get",
            self.base + "/tiktok_posts",
            headers={"Prefer": "count=exact"},
            params=params,
            stream=True,
        )
        try:
            response.raise_for_status()
            return content_range_total(response.headers.get("Content-Range"), 0)
        finally:
            response.close()

    def thumbnails(self, post_ids):
        ids = [post_id for post_id in post_ids if isinstance(post_id, str) and post_id.isdigit()]
        if not ids:
            return {}
        response = self._send(
            "get",
            self.base + "/tiktok_posts",
            params={
                "select": "post_id,thumbnail",
                "post_id": "in.(%s)" % ",".join(ids),
            },
            timeout=QUERY_TIMEOUT,
        )
        response.raise_for_status()
        found = {}
        for row in response.json():
            post_id = row.get("post_id")
            thumbnail = row.get("thumbnail")
            if post_id and thumbnail:
                found[post_id] = thumbnail
        return found

    def post(self, post_id):
        response = self._send(
            "get",
            self.base + "/tiktok_posts",
            params={
                "select": "post_id,sec_uid,type,caption,chunks,thumbnail,slides,is_deleted,posted_at",
                "post_id": "eq.%s" % post_id,
                "limit": "1",
            },
            timeout=QUERY_TIMEOUT,
        )
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None

    def patch_post(self, post_id, fields):
        response = self._send(
            "patch",
            self.base + "/tiktok_posts",
            params={"post_id": "eq.%s" % post_id},
            headers={"Prefer": "return=minimal"},
            json=fields,
            timeout=QUERY_TIMEOUT,
        )
        response.raise_for_status()

    def patch_user(self, username, fields):
        response = self._send(
            "patch",
            self.base + "/tiktok_users",
            params={"username": "eq.%s" % username},
            headers={"Prefer": "return=minimal"},
            json=fields,
            timeout=QUERY_TIMEOUT,
        )
        response.raise_for_status()


def empty_counts():
    return {
        "all": 0,
        "video": 0,
        "images": 0,
        "live": 0,
        "story": 0,
        "archived": 0,
        "deleted": 0,
    }


def content_range_total(header, fallback):
    total = (header or "").split("/")[-1]
    if total.isdigit():
        return int(total)
    return fallback
