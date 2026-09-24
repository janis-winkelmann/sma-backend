import os

import requests


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
            timeout=30,
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
            timeout=30,
        )
        if response.status_code == 409:
            return self.get_user(username), False
        response.raise_for_status()
        rows = response.json()
        return rows[0], True

    def list_users(self):
        response = self.session.get(
            self.base + "/tiktok_users",
            params={
                "select": "username,name,bio,pfp,visibility",
                "order": "created_at.desc",
                "limit": "200",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def posts_page(self, sec_uid, offset, limit, order, kind, status, query):
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
        response = self.session.get(
            self.base + "/tiktok_posts",
            headers={"Prefer": "count=exact"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        return {"posts": rows, "total": content_range_total(response.headers.get("Content-Range"), len(rows))}

    def post_counts(self, sec_uid):
        counts = empty_counts()
        if not sec_uid:
            return counts
        counts["all"] = self._count(sec_uid, None)
        for kind in ("video", "images", "live", "story"):
            counts[kind] = self._count(sec_uid, {"type": "eq.%s" % kind})
        counts["archived"] = self._count(sec_uid, {"is_deleted": "eq.false"})
        counts["deleted"] = self._count(sec_uid, {"is_deleted": "eq.true"})
        return counts

    def _count(self, sec_uid, extra):
        params = {"select": "post_id", "sec_uid": "eq.%s" % sec_uid, "limit": "1"}
        if extra:
            params.update(extra)
        response = self.session.get(
            self.base + "/tiktok_posts",
            headers={"Prefer": "count=exact"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return content_range_total(response.headers.get("Content-Range"), len(response.json()))

    def post(self, post_id):
        response = self.session.get(
            self.base + "/tiktok_posts",
            params={
                "select": "post_id,sec_uid,type,caption,chunks,thumbnail,slides,is_deleted,posted_at",
                "post_id": "eq.%s" % post_id,
                "limit": "1",
            },
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None


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
