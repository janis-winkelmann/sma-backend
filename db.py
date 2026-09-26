import os
from concurrent.futures import ThreadPoolExecutor

import requests

# A dead handshake used to occupy a worker for the full 30s, which is longer
# than Cloudflare will wait. Connect failures give up quickly; a healthy query
# still has room to finish.
QUERY_TIMEOUT = (3, 8)


class Database(object):
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": key,
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            }
        )

    def get_user(self, username):
        response = self.session.get(
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
        response = self.session.post(
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
            response = self.session.get(
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
        response = self.session.get(
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
        # A fresh request per call so the seven counts can run at once.
        response = requests.get(
            self.base + "/tiktok_posts",
            headers={
                "apikey": self.session.headers["apikey"],
                "Authorization": self.session.headers["Authorization"],
                "Prefer": "count=exact",
            },
            params=params,
            timeout=QUERY_TIMEOUT,
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
        response = self.session.get(
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
        response = self.session.get(
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
        response = self.session.patch(
            self.base + "/tiktok_posts",
            params={"post_id": "eq.%s" % post_id},
            headers={"Prefer": "return=minimal"},
            json=fields,
            timeout=QUERY_TIMEOUT,
        )
        response.raise_for_status()

    def patch_user(self, username, fields):
        response = self.session.patch(
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
