import requests

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/janis-winkelmann/sma-backend, 1.0)"


def chunk_size(chunk):
    size = chunk.get("size")
    if isinstance(size, bool) or not isinstance(size, (int, float)):
        return None
    size = int(size)
    if size < 0:
        return None
    return size


def parse_range(header, total):
    if not header:
        return 0, total - 1, False
    if not header.startswith("bytes=") or "," in header:
        return None
    spec = header[6:].strip()
    if "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if start_text == "":
            length = int(end_text)
            if length <= 0:
                return None
            start = max(total - length, 0)
            end = total - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else total - 1
    except ValueError:
        return None
    if start < 0 or start >= total or end < start:
        return None
    return start, min(end, total - 1), True


def slices_for_range(chunks, start, end):
    cursor = 0
    slices = []
    for chunk in sorted(chunks, key=lambda item: item.get("index") or 0):
        size = chunk_size(chunk)
        chunk_end = cursor + size - 1
        if chunk_end >= start and cursor <= end:
            local_start = max(start, cursor) - cursor
            local_end = min(end, chunk_end) - cursor
            slices.append((chunk, local_start, local_end))
        cursor += size
    return slices


def media_plan(chunks, range_header):
    ordered = sorted(chunks, key=lambda item: item.get("index") or 0)
    sizes = [chunk_size(chunk) for chunk in ordered]
    if any(size is None for size in sizes):
        return {"status": 200, "headers": {}, "slices": None}
    total = sum(sizes)
    if total <= 0:
        return {"status": 200, "headers": {"Content-Length": "0", "Accept-Ranges": "bytes"}, "slices": []}
    parsed = parse_range(range_header, total)
    if parsed is None:
        return {
            "status": 416,
            "headers": {"Content-Range": "bytes */%s" % total, "Accept-Ranges": "bytes"},
            "slices": [],
        }
    start, end, partial = parsed
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    status = 200
    if partial:
        status = 206
        headers["Content-Range"] = "bytes %s-%s/%s" % (start, end, total)
    return {"status": status, "headers": headers, "slices": slices_for_range(ordered, start, end)}


def take_bytes(pieces, count, skip=0):
    remaining_skip = skip
    remaining = count
    for piece in pieces:
        if not piece:
            continue
        if remaining_skip:
            if len(piece) <= remaining_skip:
                remaining_skip -= len(piece)
                continue
            piece = piece[remaining_skip:]
            remaining_skip = 0
        if len(piece) > remaining:
            if remaining:
                yield piece[:remaining]
            return
        yield piece
        remaining -= len(piece)
        if remaining <= 0:
            return


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

    def stream_slices(self, slices):
        urls = [chunk["url"] for chunk, _start, _end in slices if chunk.get("url")]
        fresh = self.refresh(urls) if urls else {}

        def generate():
            for chunk, local_start, local_end in slices:
                url = fresh.get(chunk.get("url")) or chunk.get("url")
                size = chunk_size(chunk) or 0
                count = local_end - local_start + 1
                if count <= 0 or not url:
                    continue
                headers = {}
                partial = local_start > 0 or local_end < size - 1
                if partial:
                    headers["Range"] = "bytes=%s-%s" % (local_start, local_end)
                response = requests.get(url, headers=headers, stream=True, timeout=60)
                try:
                    response.raise_for_status()
                    skip = local_start if partial and response.status_code == 200 else 0
                    for piece in take_bytes(response.iter_content(256 * 1024), count, skip):
                        yield piece
                finally:
                    response.close()

        return generate()
