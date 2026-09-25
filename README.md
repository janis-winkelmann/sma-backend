# sma-backend

Flask API used by [sma-frontend](https://github.com/janis-winkelmann/sma-frontend). This proof of concept returns TikTok-only mock data.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
gunicorn --bind 127.0.0.1:8000 app:app
```

## Endpoints

- `GET /health`
- `GET /api/platforms` — one option, TikTok
- `GET /api/lookup?user=name&platform=tiktok` — queues the username and returns the first page of stored posts
- `GET /api/posts?user=name&offset=0&limit=12&order=latest|first&type=&status=&q=` — the next page for infinite scroll
- `GET /api/post/<username>/<post_id>` — one archived post
- `GET /api/users` — looked-up accounts
- `GET /api/media/<post_id>` — refreshes the Discord chunk links and streams the file. Sends the file size and honors a `Range` header so a video can start without downloading the whole file again.
- `GET /api/slide/<post_id>/<index>` — one image from a photo post
- `GET /api/thumb/<post_id>` — refreshes the stored cover image
- `GET /api/pfp/<username>` — refreshes the stored profile image

Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `DISCORD_BOT_TOKEN`. Create the tables with `schema.sql` first. A lookup inserts the username; sma-scraper fills in the profile and posts.
