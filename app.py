import os
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, request

from db import Database
from media import DiscordFiles, content_type, media_plan, replace_urls

app = Flask(__name__)

PLATFORMS = [{"id": "tiktok", "label": "TikTok"}]


def database():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        return None
    return Database(url, key)


def remember_link(files, stored, save):
    mapping, updates = files.prepare([stored])
    fresh = updates.get(stored)
    if fresh:
        save(fresh)
    return mapping.get(stored) or stored


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


PAGE_LIMIT = 12


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


def clamp_limit(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return PAGE_LIMIT
    return max(1, min(number, 24))


def clamp_offset(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(number, 100000))


def clean_search(value):
    kept = []
    for character in (value or "").strip().lower():
        if character.isalnum() or character in " ._'#-":
            kept.append(character)
    return "".join(kept)[:80].strip()


FREE_WINDOW = timedelta(days=30)


def viewer_is_premium():
    return request.headers.get("X-Sma-Plan", "premium").strip().lower() != "free"


def free_cutoff(now=None):
    moment = now or datetime.now(timezone.utc)
    return (moment - FREE_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")


def free_visible_filter(cutoff):
    return "posted_at.gte.%s" % cutoff


def post_is_locked(row, premium, now=None):
    if premium or not row:
        return False
    posted = row.get("posted_at")
    if not posted:
        return True
    try:
        parsed = datetime.fromisoformat(posted.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    cutoff = (now or datetime.now(timezone.utc)) - FREE_WINDOW
    return parsed < cutoff


def premium_required():
    return jsonify(
        {"error": "Premium is required to view posts older than 30 days.", "locked": True}
    ), 402


def lookup_payload(user, posts, added, username, total=None, counts=None, locked=0, premium=True):
    shown = [] if added else [present_post(row, post_is_locked(row, premium)) for row in posts]
    if added:
        total = 0
        counts = empty_counts()
        locked = 0
    else:
        if total is None:
            total = len(shown)
        if counts is None:
            counts = empty_counts()
            counts["all"] = len(shown)
    return {
        "platform": "tiktok",
        "username": user.get("username") or username,
        "name": user.get("name") or "",
        "bio": user.get("bio") or "",
        "visibility": user.get("visibility") or "",
        "pfpUrl": "/api/pfp/%s" % username if user.get("pfp") else None,
        "added": added,
        "posts": shown,
        "total": total,
        "counts": counts,
        "lockedVideos": locked,
    }


def present_post(row, locked=False):
    deleted = bool(row.get("is_deleted"))
    when = format_when(row.get("posted_at"))
    detail = "Removed from the profile" if deleted else "On the profile"
    if when:
        detail = "%s · %s" % (detail, when)
    chunks = row.get("chunks") or []
    slides = sorted(row.get("slides") or [], key=lambda item: item.get("index") or 0)
    post_id = row.get("post_id")
    kind = row.get("type") or "video"
    if slides:
        image_urls = [
            "/api/slide/%s/%s" % (post_id, item.get("index") if item.get("index") is not None else index)
            for index, item in enumerate(slides)
        ]
    elif kind == "images" and chunks:
        image_urls = ["/api/media/%s" % post_id]
    else:
        image_urls = []
    if locked:
        image_urls = []
    return {
        "id": post_id,
        "title": row.get("caption") or kind or "Post",
        "detail": detail,
        "status": "Deleted" if deleted else "Archived",
        "type": kind,
        "locked": bool(locked),
        "mediaUrl": None if locked else ("/api/media/%s" % post_id if chunks and kind != "images" else None),
        "thumbnailUrl": "/api/thumb/%s" % post_id if row.get("thumbnail") else None,
        "imageUrls": image_urls,
        "imageCount": len(image_urls),
        "postedAt": row.get("posted_at") or None,
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
        premium = viewer_is_premium()
        cutoff = None if premium else free_cutoff()
        if added:
            page = {"posts": [], "total": 0}
            counts = empty_counts()
            locked = 0
        else:
            page = store.posts_page(user.get("sec_uid"), 0, PAGE_LIMIT, "latest", "all", "all", "")
            counts = store.post_counts(user.get("sec_uid"))
            locked = 0 if premium else store.locked_video_count(user.get("sec_uid"), cutoff)
    except requests.HTTPError:
        return jsonify({"error": "TikTok tables are not ready in Supabase yet."}), 503
    return jsonify(lookup_payload(user, page["posts"], added, username, page["total"], counts, locked, premium))


@app.get("/api/posts")
def posts():
    username = clean_username(request.args.get("user", ""))
    if not username:
        return jsonify({"error": "Enter a username."}), 400
    store = database()
    if store is None:
        return jsonify({"error": "Supabase is not configured."}), 503
    kind = request.args.get("type") or "all"
    status = request.args.get("status") or "all"
    order = request.args.get("order") if request.args.get("order") in ("latest", "first") else "latest"
    offset = clamp_offset(request.args.get("offset"))
    limit = clamp_limit(request.args.get("limit"))
    premium = viewer_is_premium()
    cutoff = None if premium else free_cutoff()
    try:
        user = store.get_user(username)
        if not user:
            return jsonify({"posts": [], "total": 0, "offset": offset, "limit": limit, "lockedVideos": 0})
        page = store.posts_page(
            user.get("sec_uid"),
            offset,
            limit,
            order,
            kind,
            status,
            clean_search(request.args.get("q")),
        )
        locked = 0 if premium else store.locked_video_count(user.get("sec_uid"), cutoff)
    except requests.HTTPError:
        return jsonify({"error": "TikTok tables are not ready in Supabase yet."}), 503
    return jsonify(
        {
            "posts": [present_post(row, post_is_locked(row, premium)) for row in page["posts"]],
            "total": page["total"],
            "offset": offset,
            "limit": limit,
            "lockedVideos": locked,
        }
    )


@app.get("/api/users")
def users():
    store = database()
    if store is None:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        rows = store.list_users()
    except requests.HTTPError:
        return jsonify({"error": "TikTok tables are not ready in Supabase yet."}), 503
    return jsonify(
        {
            "users": [
                {
                    "username": row.get("username") or "",
                    "name": row.get("name") or "",
                    "bio": row.get("bio") or "",
                    "visibility": row.get("visibility") or "",
                    "pfpUrl": "/api/pfp/%s" % row.get("username") if row.get("pfp") else None,
                }
                for row in rows
            ]
        }
    )


@app.get("/api/post/<username>/<post_id>")
def one_post(username, post_id):
    store = database()
    if store is None:
        return jsonify({"error": "Supabase is not configured."}), 503
    try:
        user = store.get_user(clean_username(username))
        row = store.post(post_id)
    except requests.HTTPError:
        return jsonify({"error": "TikTok tables are not ready in Supabase yet."}), 503
    if not user or not row or row.get("sec_uid") != user.get("sec_uid"):
        return jsonify({"error": "Post not found."}), 404
    payload = present_post(row, post_is_locked(row, viewer_is_premium()))
    payload["username"] = user.get("username") or clean_username(username)
    payload["name"] = user.get("name") or ""
    payload["pfpUrl"] = "/api/pfp/%s" % payload["username"] if user.get("pfp") else None
    return jsonify(payload)


@app.get("/api/media/<post_id>")
def media(post_id):
    store = database()
    files = discord_files()
    if store is None or files is None:
        return jsonify({"error": "Storage is not configured."}), 503
    row = store.post(post_id)
    if post_is_locked(row, viewer_is_premium()):
        return premium_required()
    chunks = (row or {}).get("chunks") or []
    if not chunks:
        return jsonify({"error": "No file stored for this post."}), 404
    plan = media_plan(chunks, request.headers.get("Range"))
    headers = dict(plan["headers"])
    headers["Cache-Control"] = "private, max-age=3600"
    if plan["status"] == 416:
        return Response(status=416, headers=headers)
    if plan["slices"] is None:
        urls = [chunk.get("url") for chunk in chunks]
    else:
        urls = [chunk.get("url") for chunk, _start, _end in plan["slices"]]
    mapping, updates = files.prepare(urls)
    rewritten = replace_urls(chunks, updates)
    if rewritten is not None:
        store.patch_post(post_id, {"chunks": rewritten})
    mime = content_type(row.get("type"), chunks)
    if plan["slices"] is None:
        body = files.stream(chunks, mapping)
        status = 200
    else:
        body = files.stream_slices(plan["slices"], mapping)
        status = plan["status"]
    return Response(body, status=status, mimetype=mime, headers=headers)


@app.get("/api/slide/<post_id>/<int:index>")
def slide(post_id, index):
    store = database()
    files = discord_files()
    if store is None or files is None:
        return jsonify({"error": "Storage is not configured."}), 503
    row = store.post(post_id)
    if post_is_locked(row, viewer_is_premium()):
        return premium_required()
    slides = (row or {}).get("slides") or []
    match = None
    for item in slides:
        if item.get("index") == index:
            match = item
            break
    if match is None:
        ordered = sorted(slides, key=lambda item: item.get("index") or 0)
        if 0 <= index < len(ordered):
            match = ordered[index]
    stored = (match or {}).get("url")
    if not stored:
        return jsonify({"error": "No slide stored for this post."}), 404
    url = remember_link(files, stored, lambda fresh: store.patch_post(post_id, {"slides": replace_urls(slides, {stored: fresh})}))
    upstream = requests.get(url, stream=True, timeout=30)
    upstream.raise_for_status()
    return Response(
        upstream.iter_content(256 * 1024),
        mimetype=upstream.headers.get("Content-Type", "image/jpeg"),
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
    url = remember_link(files, stored, lambda fresh: store.patch_post(post_id, {"thumbnail": fresh}))
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
    url = remember_link(
        files,
        user["pfp"],
        lambda fresh: store.patch_user(clean_username(username), {"pfp": fresh}),
    )
    upstream = requests.get(url, stream=True, timeout=30)
    upstream.raise_for_status()
    return Response(upstream.iter_content(256 * 1024), mimetype=upstream.headers.get("Content-Type", "image/jpeg"))
