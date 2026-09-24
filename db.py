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

    def posts_for(self, sec_uid):
        if not sec_uid:
            return []
        response = self.session.get(
            self.base + "/tiktok_posts",
            params={
                "select": "post_id,type,caption,chunks,is_deleted,posted_at",
                "sec_uid": "eq.%s" % sec_uid,
                "order": "posted_at.desc.nullslast",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def post(self, post_id):
        response = self.session.get(
            self.base + "/tiktok_posts",
            params={"select": "post_id,type,chunks", "post_id": "eq.%s" % post_id, "limit": "1"},
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None
