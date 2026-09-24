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
- `GET /api/lookup?user=name&platform=tiktok` — archived posts for that username

Any platform other than `tiktok` returns an error. sma-frontend reads `SMA_API_URL` (default `http://127.0.0.1:8000`).
