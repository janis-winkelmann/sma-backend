import os
from datetime import datetime

import requests
from flask import Flask, Response, jsonify, request

from db import Database
from media import DiscordFiles, content_type

app = Flask(__name__)

PLATFORMS = [{"id": "tiktok", "label": "TikTok"}]


def database():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        return None
    return Database(url, key)


def discord_files():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return None
    return DiscordFiles(token)


def clean_username(value):
    return (value or "").strip().lstrip("@").lower()


def format_when(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return "%s %s" % (parsed.strftime("%b"), parsed.day)


def lookup_payload(user, posts, added, username):
    shown = [] if added else [present_post(row) for row in posts]
    return {
        "platform": "tiktok",
        "username": user.get("username") or username,
        "name": user.get("name") or "",
        "bio": user.get("bio") or "",
        "visibility": user.get("visibility") or "",
        "pfpUrl": "/api/pfp/%s" % username if user.get("pfp") else None,
        "added": added,
        "posts": shown,
    }


def present_post(row):
    deleted = bool(row.get("is_deleted"))
    when = format_when(row.get("posted_at"))
    detail = "Removed from the profile" if deleted else "On the profile"
    if when:
        detail = "%s · %s" % (detail, when)
    chunks = row.get("chunks") or []
    return {
        "id": row.get("post_id"),
        "title": row.get("caption") or row.get("type") or "Post",
        "detail": detail,
        "status": "Deleted" if deleted else "Archived",
        "type": row.get("type") or "video",
        "mediaUrl": "/api/media/%s" % row.get("post_id") if chunks else None,
        "thumbnailUrl": "/api/thumb/%s" % row.get("post_id") if row.get("thumbnail") else None,
    }


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "sma-backend"})


@app.get("/api/platforms")
def platforms():
    return jsonify({"platforms": PLATFORMS})


@app.get("/api/lookup")
def lookup():
    platform = request.args.get("platform", "tiktok")
    username = clean_username(request.args.get("user", ""))
    if platform != "tiktok":
        return jsonify({"error": "Only TikTok is available.", "platform": platform}), 404
    if not username:
        return jsonify({"error": "Enter a username."}), 400
    store = database()
    if store is None:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        user, added = store.ensure_user(username)
        posts = [] if added else store.posts_for(user.get("sec_uid"))
    except requests.HTTPError:
        return jsonify({"error": "TikTok tables are not ready in Supabase yet."}), 503
    return jsonify(lookup_payload(user, posts, added, username))


@app.get("/api/media/<post_id>")
def media(post_id):
    store = database()
    files = discord_files()
    if store is None or files is None:
        return jsonify({"error": "Storage is not configured."}), 503
    row = store.post(post_id)
    chunks = (row or {}).get("chunks") or []
    if not chunks:
        return jsonify({"error": "No file stored for this post."}), 404
    return Response(
        files.stream(chunks),
        mimetype=content_type(row.get("type"), chunks),
    )


@app.get("/api/thumb/<post_id>")
def thumb(post_id):
    store = database()
    files = discord_files()
    if store is None or files is None:
        return jsonify({"error": "Storage is not configured."}), 503
    row = store.post(post_id)
    stored = (row or {}).get("thumbnail")
    if not stored:
        return jsonify({"error": "No thumbnail stored for this post."}), 404
    fresh = files.refresh([stored])
    url = fresh.get(stored) or stored
    upstream = requests.get(url, stream=True, timeout=30)
    upstream.raise_for_status()
    return Response(
        upstream.iter_content(256 * 1024),
        mimetype=upstream.headers.get("Content-Type", "image/jpeg"),
    )


@app.get("/api/pfp/<username>")
def pfp(username):
    store = database()
    files = discord_files()
    if store is None or files is None:
        return jsonify({"error": "Storage is not configured."}), 503
    user = store.get_user(clean_username(username))
    if not user or not user.get("pfp"):
        return jsonify({"error": "No profile image stored."}), 404
    fresh = files.refresh([user["pfp"]])
    url = fresh.get(user["pfp"]) or user["pfp"]
    upstream = requests.get(url, stream=True, timeout=30)
    upstream.raise_for_status()
    return Response(upstream.iter_content(256 * 1024), mimetype=upstream.headers.get("Content-Type", "image/jpeg"))
