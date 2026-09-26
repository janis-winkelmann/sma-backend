import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/janis-winkelmann/sma-backend, 1.0)"
EXPIRY_LEEWAY = 120
IMAGE_MAX_BYTES = 12 * 1024 * 1024


def link_expiry(url):
    if not url:
        return None
    raw = (parse_qs(urlparse(url).query).get("ex") or [None])[0]
    if not raw:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def looks_like_image(data, content_type=""):
    if not data or len(data) > IMAGE_MAX_BYTES:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return content_type.split(";", 1)[0].strip().lower().startswith("image/")


def read_image_bytes(url, get=None, attempts=3, pause=None):
    """Download a whole image. A stalled host is retried instead of streamed to the browser."""
    fetch = get or _download_image
    wait = pause or time.sleep
    last = "error"
    for attempt in range(attempts):
        try:
            data, content_type = fetch(url)
            if looks_like_image(data, content_type):
                return data, (content_type or "image/jpeg").split(";", 1)[0].strip() or "image/jpeg"
            last = "invalid"
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            last = str(status or type(exc).__name__)
        if attempt + 1 < attempts:
            wait(0.25 * (attempt + 1))
    raise requests.RequestException(last)


def _download_image(url):
    response = requests.get(url, timeout=(4, 8))
    try:
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "image/jpeg").split(";", 1)[0].strip()
        return response.content, content_type
    finally:
        response.close()


def link_is_live(url, now=None):
    expiry = link_expiry(url)
    if expiry is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    return expiry > now + EXPIRY_LEEWAY


def replace_urls(items, updates):
    if not updates:
        return None
    changed = False
    result = []
    for item in items:
        url = item.get("url") if isinstance(item, dict) else None
        new = updates.get(url)
        if new and new != url:
            copied = dict(item)
            copied["url"] = new
            result.append(copied)
            changed = True
        else:
            result.append(item)
    return result if changed else None


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


# TikTok live HLS audio is HE-AAC: a 24 kHz AAC-LC core with SBR to 48 kHz stereo.
# empty_moov writes the sample entry before aac_adtstoasc has seen a packet, so the
# stored file has an SLConfig descriptor and no AudioSpecificConfig. Chrome then
# rejects the first audio packet and the picture never starts.
HE_AAC_ESDS = bytes.fromhex(
    "000000336573647300000000"
    "0380808022000100048080801440150000000000fa3a0000f720"
    "05808080021310"
    "068080800102"
)
_CONTAINER_BOXES = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd")


def moov_end(data):
    """Byte length through the moov box, or None when this is not an fMP4 header."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return None
    ftyp = int.from_bytes(data[:4], "big")
    if ftyp < 8 or ftyp > 1024 * 1024:
        return None
    if len(data) < ftyp + 8:
        return ftyp + 8
    if data[ftyp + 4 : ftyp + 8] != b"moov":
        return None
    moov = int.from_bytes(data[ftyp : ftyp + 4], "big")
    if moov < 8 or moov > 8 * 1024 * 1024:
        return None
    return ftyp + moov


def _descriptor_size(data, offset, end):
    size = 0
    read = 0
    while read < 4 and offset + read < end:
        byte = data[offset + read]
        read += 1
        size = (size << 7) | (byte & 0x7F)
        if byte < 0x80:
            return size, read
    return None


def _descriptors_have_tag(data, offset, end, tag):
    while offset + 2 <= end:
        kind = data[offset]
        parsed = _descriptor_size(data, offset + 1, end)
        if parsed is None:
            return False
        size, header = parsed
        body = offset + 1 + header
        if size < 0 or body + size > end:
            return False
        if kind == tag:
            return True
        if kind in (0x03, 0x04):
            inner = body + (3 if kind == 0x03 else 13)
            if inner < body + size and _descriptors_have_tag(data, inner, body + size, tag):
                return True
        offset = body + size
    return False


def _bump_containers(buf, box_start, delta):
    def walk(start, end):
        offset = start
        while offset + 8 <= end:
            size = int.from_bytes(buf[offset : offset + 4], "big")
            kind = bytes(buf[offset + 4 : offset + 8])
            if size < 8 or offset + size > len(buf):
                return
            if offset >= box_start:
                return
            if box_start < offset + size:
                buf[offset : offset + 4] = (size + delta).to_bytes(4, "big")
                if kind in _CONTAINER_BOXES:
                    walk(offset + 8, offset + size)
            offset += size

    walk(0, len(buf))


def audio_header_patch(data):
    """Return (prefix, replaced) when the audio sample entry has no decoder config.

    prefix replaces the original bytes data[:replaced]. The remainder of the file
    stays byte-for-byte identical, shifted forward by len(prefix) - replaced.
    """
    if moov_end(data) is None or len(data) < 32:
        return None
    mp4a = data.find(b"mp4a")
    if mp4a < 4:
        return None
    entry_start = mp4a - 4
    entry_size = int.from_bytes(data[entry_start : entry_start + 4], "big")
    if entry_size < 36 or entry_start + entry_size > len(data):
        return None
    esds_at = data.find(b"esds", mp4a, entry_start + entry_size)
    if esds_at < 4:
        return None
    box_start = esds_at - 4
    old_size = int.from_bytes(data[box_start : box_start + 4], "big")
    if old_size < 12 or box_start + old_size > entry_start + entry_size:
        return None
    payload = data[box_start + 12 : box_start + old_size]
    if _descriptors_have_tag(payload, 0, len(payload), 0x05):
        return None
    delta = len(HE_AAC_ESDS) - old_size
    buf = bytearray(data)
    buf[box_start : box_start + old_size] = HE_AAC_ESDS
    buf[entry_start : entry_start + 4] = (entry_size + delta).to_bytes(4, "big")
    _bump_containers(buf, box_start, delta)
    replaced = box_start + old_size
    prefix = bytes(buf[: box_start + len(HE_AAC_ESDS)])
    return prefix, replaced


def _with_audio_patch(ordered, audio_patch):
    if not audio_patch or not ordered:
        return ordered
    prefix, replaced = audio_patch
    first = ordered[0]
    first_size = chunk_size(first)
    if first_size is None or replaced < 0 or replaced > first_size or len(prefix) < replaced:
        return ordered
    virtual = [{"index": -1, "inline": prefix, "size": len(prefix)}]
    tail_size = first_size - replaced
    if tail_size:
        tail = dict(first)
        tail["size"] = tail_size
        tail["byte_offset"] = replaced
        virtual.append(tail)
    virtual.extend(ordered[1:])
    return virtual


def file_plan(total, range_header):
    """Byte range for one local file. Same status and headers as media_plan."""
    if total <= 0:
        return {
            "status": 200,
            "headers": {"Content-Length": "0", "Accept-Ranges": "bytes"},
            "start": 0,
            "end": -1,
        }
    parsed = parse_range(range_header, total)
    if parsed is None:
        return {
            "status": 416,
            "headers": {"Content-Range": "bytes */%s" % total, "Accept-Ranges": "bytes"},
            "start": None,
            "end": None,
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
    return {"status": status, "headers": headers, "start": start, "end": end}


def iter_file(path, start, end):
    remaining = end - start + 1
    if remaining <= 0:
        return
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            piece = handle.read(min(256 * 1024, remaining))
            if not piece:
                return
            remaining -= len(piece)
            yield piece


def media_plan(chunks, range_header, audio_patch=None):
    ordered = sorted(chunks, key=lambda item: item.get("index") or 0)
    sizes = [chunk_size(chunk) for chunk in ordered]
    if any(size is None for size in sizes):
        return {"status": 200, "headers": {}, "slices": None}
    ordered = _with_audio_patch(ordered, audio_patch)
    sizes = [chunk_size(chunk) for chunk in ordered]
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


def chunk_uploaded_at(chunk):
    raw = chunk.get("uploaded_at") if isinstance(chunk, dict) else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def live_is_recording(chunks, now=None):
    moment = now or datetime.now(timezone.utc)
    usable = [chunk for chunk in (chunks or []) if isinstance(chunk, dict)]
    if not usable:
        return False
    last = max(usable, key=lambda chunk: int(chunk.get("index") or 0))
    if last.get("closed"):
        return False
    latest = chunk_uploaded_at(last)
    if latest is None:
        return False
    return moment - latest < timedelta(minutes=6)


def chunks_are_playable(chunks):
    """A finished live that has already been stored on Discord as a normal MP4."""
    usable = [chunk for chunk in (chunks or []) if isinstance(chunk, dict)]
    return bool(usable) and all(chunk.get("playable") for chunk in usable)


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

    def upload(self, channel_id, filename, data, content_type="video/mp4"):
        response = None
        last = "error"
        for attempt in range(6):
            try:
                response = self.session.post(
                    "%s/channels/%s/messages" % (API, channel_id),
                    data={"payload_json": json.dumps({"content": filename})},
                    files={"files[0]": (filename, data, content_type)},
                    timeout=180,
                )
            except requests.RequestException as exc:
                response = None
                last = type(exc).__name__
                if attempt == 5:
                    break
                time.sleep(1 + attempt)
                continue
            if response.status_code != 429 or attempt == 5:
                break
            try:
                wait = min(max(float(response.headers.get("Retry-After")), 0.5), 30)
            except (TypeError, ValueError):
                wait = 2
            time.sleep(wait)
        if response is None or response.status_code >= 400:
            status = getattr(response, "status_code", None)
            raise requests.RequestException("upload failed (%s)" % (status or last))
        message = response.json()
        attachment = (message.get("attachments") or [None])[0] or {}
        url = attachment.get("url")
        if not url or not message.get("id"):
            raise requests.RequestException("upload failed (empty)")
        return {
            "filename": attachment.get("filename") or filename,
            "url": url,
            "channel_id": str(channel_id),
            "message_id": str(message["id"]),
            "size": int(attachment.get("size") or len(data)),
        }

    def delete_message(self, channel_id, message_id):
        response = self.session.delete(
            "%s/channels/%s/messages/%s" % (API, channel_id, message_id),
            timeout=30,
        )
        if response.status_code in (200, 204, 404):
            return
        raise requests.RequestException("delete failed (%s)" % response.status_code)

    def prepare(self, urls):
        mapping = {}
        stale = []
        seen = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            if link_is_live(url):
                mapping[url] = url
            else:
                stale.append(url)
        updates = {}
        if stale:
            refreshed = self.refresh(stale)
            for url in stale:
                new = refreshed.get(url) or url
                mapping[url] = new
                if new != url:
                    updates[url] = new
        return mapping, updates

    def stream(self, chunks, mapping):
        ordered = sorted(chunks, key=lambda chunk: chunk.get("index") or 0)

        def generate():
            for chunk in ordered:
                url = mapping.get(chunk.get("url")) or chunk.get("url")
                response = requests.get(url, stream=True, timeout=60)
                response.raise_for_status()
                for piece in response.iter_content(256 * 1024):
                    if piece:
                        yield piece

        return generate()

    def stream_slices(self, slices, mapping):
        def generate():
            for chunk, local_start, local_end in slices:
                count = local_end - local_start + 1
                if count <= 0:
                    continue
                inline = chunk.get("inline")
                if inline is not None:
                    yield bytes(inline[local_start : local_end + 1])
                    continue
                url = mapping.get(chunk.get("url")) or chunk.get("url")
                if not url:
                    continue
                offset = int(chunk.get("byte_offset") or 0)
                size = (chunk_size(chunk) or 0) + offset
                real_start = local_start + offset
                real_end = local_end + offset
                headers = {}
                partial = real_start > 0 or real_end < size - 1
                if partial:
                    headers["Range"] = "bytes=%s-%s" % (real_start, real_end)
                response = requests.get(url, headers=headers, stream=True, timeout=60)
                try:
                    response.raise_for_status()
                    skip = real_start if partial and response.status_code == 200 else 0
                    for piece in take_bytes(response.iter_content(256 * 1024), count, skip):
                        yield piece
                finally:
                    response.close()

        return generate()
