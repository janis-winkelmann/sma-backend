from flask import Flask, jsonify, request

app = Flask(__name__)

PLATFORMS = [{"id": "tiktok", "label": "TikTok"}]

POSTS = [
    ("Video", "Mar 2"),
    ("Video caption", "Jan 18"),
    ("Comment", "Dec 9"),
]


def posts_for(username):
    return [
        {
            "id": "%s-%s" % (username.lower(), index + 1),
            "title": title,
            "detail": "Removed from the profile · %s" % when,
            "status": "Deleted",
        }
        for index, (title, when) in enumerate(POSTS)
    ]


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "sma-backend"})


@app.get("/api/platforms")
def platforms():
    return jsonify({"platforms": PLATFORMS})


@app.get("/api/lookup")
def lookup():
    platform = request.args.get("platform", "tiktok")
    username = request.args.get("user", "").strip().lstrip("@")
    if platform != "tiktok":
        return jsonify({"error": "Only TikTok is available.", "platform": platform}), 404
    if not username:
        return jsonify({"error": "Enter a username."}), 400
    return jsonify(
        {
            "platform": "tiktok",
            "username": username,
            "posts": posts_for(username),
        }
    )
