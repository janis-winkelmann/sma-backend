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
- `GET /api/lookup?user=name&platform=tiktok` — queues the username and returns stored posts
- `GET /api/media/<post_id>` — refreshes the Discord chunk links and streams the file
- `GET /api/pfp/<username>` — refreshes the stored profile image

Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `DISCORD_BOT_TOKEN`. Create the tables with `schema.sql` first. A lookup inserts the username; sma-scraper fills in the profile and posts.
