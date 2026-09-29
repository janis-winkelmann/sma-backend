import http.client
import os
from urllib.parse import quote, urlparse

import requests


class ScraperUnavailable(Exception):
    pass


def base_url():
    return os.environ.get("SMA_SCRAPER_URL", "http://127.0.0.1:8100").rstrip("/")


def add_account(username, address=""):
    """Ask sma-scraper to check an account on TikTok, save it, and start scraping it.

    Returns (http_status, payload). Raises ScraperUnavailable when it cannot be reached.
    """
    try:
        response = requests.post(
            "%s/first/%s" % (base_url(), quote(username, safe="")),
            headers={"X-Client-IP": address or ""},
            timeout=(2, 30),
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ScraperUnavailable(str(error))
    return response.status_code, payload if isinstance(payload, dict) else {}


def scrape_state(username):
    """The first scrape's snapshot, or None when there is none or the scraper cannot be asked."""
    try:
        response = requests.get("%s/state/%s" % (base_url(), quote(username, safe="")), timeout=(0.5, 1.5))
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    scrape = payload.get("scrape") if isinstance(payload, dict) else None
    return scrape if isinstance(scrape, dict) else None


def stream_events(username):
    """Yield the first scrape's event stream line by line.

    http.client is used because a blocking read on a response with no length
    must return as soon as a line arrives, which requests does not promise.
    """
    parts = urlparse(base_url())
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=30)
    try:
        connection.request("GET", "/events/%s" % quote(username, safe=""))
        response = connection.getresponse()
        if response.status != 200:
            raise ScraperUnavailable("events returned %s" % response.status)
        while True:
            line = response.readline()
            if not line:
                return
            yield line
    except (OSError, http.client.HTTPException) as error:
        raise ScraperUnavailable(str(error))
    finally:
        connection.close()
