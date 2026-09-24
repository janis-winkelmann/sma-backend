import requests

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/janis-winkelmann/sma-backend, 1.0)"


def content_type(post_type, chunks):
    name = ""
    if chunks:
        name = (chunks[0].get("filename") or "").lower()
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".gif"):
        return "image/gif"
    if name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if post_type == "images":
        return "image/jpeg"
    return "video/mp4"


class DiscordFiles(object):
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers["Authorization"] = "Bot " + token
        self.session.headers["User-Agent"] = USER_AGENT

    def refresh(self, urls):
        response = self.session.post(
            API + "/attachments/refresh-urls",
            json={"attachment_urls": urls},
            timeout=30,
        )
        response.raise_for_status()
        fresh = {}
        for item in response.json().get("refreshed_urls", []):
            fresh[item["original"]] = item["refreshed"]
        return fresh

    def stream(self, chunks):
        ordered = sorted(chunks, key=lambda chunk: chunk.get("index") or 0)
        fresh = self.refresh([chunk["url"] for chunk in ordered if chunk.get("url")])

        def generate():
            for chunk in ordered:
                url = fresh.get(chunk["url"]) or chunk["url"]
                response = requests.get(url, stream=True, timeout=60)
                response.raise_for_status()
                for piece in response.iter_content(256 * 1024):
                    if piece:
                        yield piece

        return generate()
