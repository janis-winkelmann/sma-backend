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
- `GET /api/lookup?user=name&platform=tiktok` — returns the first page of stored posts. A username that is not saved yet is checked on TikTok first (`404` with `notFound` when it does not exist, `503` when TikTok could not be asked), then saved with its name and bio and scraped at once. `firstScrape` carries the live state of an account's first scrape, or `null`
- `POST /api/add?user=name` — the same check-and-add without loading a timeline, used before sign-in so the first scrape is already running
- `GET /api/live/<username>` — Server-Sent Events for that first scrape (current action, counts, each saved post). `GET /api/live/<username>/state` is the same snapshot as JSON
- `GET /api/posts?user=name&offset=0&limit=12&order=latest|first&type=&status=&q=` — the next page for infinite scroll
- `GET /api/post/<username>/<post_id>` — one archived post
- `GET /api/users` — looked-up accounts
- `GET /api/media/<post_id>` — streams the file. A stored Discord link is used while its expiry is still valid, and refreshed after that. Sends the file size and honors a `Range` header so a video can start without downloading the whole file again.
- `GET /api/slide/<post_id>/<index>` — one image from a photo post
- `GET /api/thumb/<post_id>` — refreshes the stored cover image
- `GET /api/pfp/<username>` — refreshes the stored profile image

Set `SMA_SCRAPER_URL` (default `http://127.0.0.1:8100`) to reach sma-scraper's local API, which checks TikTok and runs the first scrape. Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `DISCORD_BOT_TOKEN`. Create the tables with `schema.sql` first. A lookup inserts the username; sma-scraper fills in the profile and posts.

## Plan enforcement

`/api/*` is reachable from the internet through nginx, so the `X-Sma-Plan` header is only trusted when the request also carries `X-Sma-Internal` equal to the `SMA_INTERNAL_KEY` environment variable. Set the same value for the backend and the frontend server. Without the key every caller is treated as free, so locked posts stay locked.
