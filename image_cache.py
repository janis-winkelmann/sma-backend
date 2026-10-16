import hashlib
import os
import shutil
import threading
import time
from urllib.parse import urlparse

from media import looks_like_image

CACHE_TTL = 6 * 3600
CACHE_MAX_BYTES = 120 * 1024 * 1024
_PRUNE_EVERY = 40
_writes = 0
_write_lock = threading.Lock()


def cache_root():
    return os.environ.get("SMA_IMAGE_CACHE", "/dev/shm/sma-image-cache")


def image_cache_key(url):
    parsed = urlparse(url or "")
    stable = parsed.path or (url or "")
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _path_for(url):
    key = image_cache_key(url)
    return os.path.join(cache_root(), key[:2], key)


def cached_image(url):
    """Bytes saved for this attachment, ignoring the expiring signature on the URL."""
    path = _path_for(url)
    try:
        modified = os.path.getmtime(path)
    except OSError:
        return None
    if time.time() - modified > CACHE_TTL:
        return None
    try:
        with open(path, "rb") as handle:
            header, data = handle.read().split(b"\n", 1)
    except (OSError, ValueError):
        return None
    content_type = header.decode("utf-8", "replace").strip() or "image/jpeg"
    if not looks_like_image(data, content_type):
        return None
    if time.time() - modified > 600:
        try:
            os.utime(path, None)
        except OSError:
            pass
    return data, content_type


def store_cached_image(url, data, content_type):
    if not url or not looks_like_image(data, content_type):
        return
    path = _path_for(url)
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        temporary = path + ".tmp-%s" % os.getpid()
        kind = (content_type or "image/jpeg").split(";", 1)[0].strip() or "image/jpeg"
        with open(temporary, "wb") as handle:
            handle.write(kind.encode("utf-8") + b"\n")
            handle.write(data)
        os.replace(temporary, path)
    except OSError:
        return
    maybe_prune()


def maybe_prune():
    global _writes
    with _write_lock:
        _writes += 1
        if _writes % _PRUNE_EVERY != 0:
            return
    root = cache_root()
    files = []
    total = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.startswith("."):
                continue
            path = os.path.join(dirpath, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            files.append((info.st_mtime, info.st_size, path))
            total += info.st_size
    if total <= CACHE_MAX_BYTES:
        return
    files.sort()
    for _modified, size, path in files:
        if total <= int(CACHE_MAX_BYTES * 0.8):
            break
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size


def clear_image_cache():
    shutil.rmtree(cache_root(), ignore_errors=True)
