import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("playback")

CACHE_ROOT = os.environ.get("SMA_LIVE_CACHE", "/var/cache/sma-live")
DEFAULT_WAIT = float(os.environ.get("SMA_LIVE_REMUX_WAIT", "20"))
FAIL_COOLDOWN = 20
KEEP_FILES = 3
FRAGMENTED_BRANDS = (b"iso5", b"iso6", b"dash", b"msdh")


def live_should_remux(patch, brand):
    """Fragmented HE-AAC (iso5) does not play on phones. A faststart AAC-LC file does."""
    if patch:
        return True
    if isinstance(brand, str):
        brand = brand.encode("ascii", "ignore")
    return brand in FRAGMENTED_BRANDS


def live_signature(post_id, chunks):
    ordered = sorted(chunks, key=lambda item: int(item.get("index") or 0))
    last = ordered[-1]
    raw = "\n".join(
        [
            str(post_id),
            str(len(ordered)),
            str(sum(int(item.get("size") or 0) for item in ordered)),
            str(last.get("message_id") or ""),
            str(last.get("filename") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def safe_post_id(post_id):
    cleaned = "".join(character for character in str(post_id) if character.isalnum())
    return cleaned or "post"


def assemble_source(chunks, prefix, replaced):
    pieces = []
    for index, blob in enumerate(chunks):
        if index == 0 and prefix is not None:
            pieces.append(prefix)
            pieces.append(blob[replaced:])
        else:
            pieces.append(blob)
    return b"".join(pieces)


def ffmpeg_remux_args(source, target):
    # Video is copied. Audio is rewritten as AAC-LC so the file is a normal faststart MP4.
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-fflags",
        "+discardcorrupt+genpts",
        "-i",
        source,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-threads",
        "1",
        "-movflags",
        "+faststart",
        target,
    ]


def _nonempty(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _playable_output(path):
    """A truncated live can make ffmpeg exit after it has already written a real MP4."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 1000000:
            return False
    except OSError:
        return False
    try:
        with open(path, "rb") as handle:
            header = handle.read(12)
    except OSError:
        return False
    if len(header) < 12 or header[4:8] != b"ftyp":
        return False
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    codec = probe.stdout.decode("ascii", "ignore").strip()
    return probe.returncode == 0 and codec.startswith("h264")


def _inside_cache(path):
    root = os.path.realpath(CACHE_ROOT)
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


def newest_mp4(directory, exclude=None):
    best = None
    best_mtime = -1
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".mp4") or name.endswith(".tmp"):
            continue
        path = os.path.join(directory, name)
        if exclude and os.path.realpath(path) == os.path.realpath(exclude):
            continue
        try:
            if os.path.getsize(path) <= 0:
                continue
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime >= best_mtime:
            best = path
            best_mtime = mtime
    return best


def _read_pin(directory, signature):
    pin_path = os.path.join(directory, signature + ".pin")
    try:
        with open(pin_path) as handle:
            target = handle.read().strip()
    except OSError:
        return None
    if not target or not _nonempty(target) or not _inside_cache(target):
        return None
    return os.path.realpath(target)


def _pin_new(directory, signature, target):
    pin_path = os.path.join(directory, signature + ".pin")
    if os.path.isfile(pin_path):
        return
    temporary = pin_path + ".tmp"
    with open(temporary, "w") as handle:
        handle.write(os.path.realpath(target))
    os.replace(temporary, pin_path)


def _touch_pin(directory, signature):
    try:
        os.utime(os.path.join(directory, signature + ".pin"), None)
    except OSError:
        pass


def cached_choice(directory, signature):
    """File to keep serving for this snapshot. A pin wins so later ranges match the first response."""
    if not directory or not os.path.isdir(directory):
        return None
    pinned = _read_pin(directory, signature)
    if pinned:
        return pinned
    exact = os.path.join(directory, signature + ".mp4")
    if _nonempty(exact):
        return exact
    return newest_mp4(directory)


def _pid_alive(pidfile):
    try:
        pid = int(open(pidfile).read().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _any_running(directory):
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    for name in names:
        if name.endswith(".pid") and _pid_alive(os.path.join(directory, name)):
            return True
    return False


def _recently_failed(path):
    try:
        return time.time() - os.path.getmtime(path) < FAIL_COOLDOWN
    except OSError:
        return False


def _wait_for_file(directory, exact, seconds):
    deadline = time.time() + max(seconds, 0)
    while time.time() < deadline:
        if _nonempty(exact):
            return exact
        found = newest_mp4(directory)
        if found:
            return found
        time.sleep(0.25)
    if _nonempty(exact):
        return exact
    return newest_mp4(directory)


def _schedule(directory, signature, ordered, files, patch, remember):
    exact = os.path.join(directory, signature + ".mp4")
    pidfile = os.path.join(directory, signature + ".pid")
    failed = os.path.join(directory, signature + ".failed")
    if _nonempty(exact) or _any_running(directory) or _recently_failed(failed):
        return
    lock_path = os.path.join(directory, "build.lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _nonempty(exact) or _any_running(directory) or _recently_failed(failed):
            return
        urls = []
        try:
            mapping, updates = files.prepare([chunk.get("url") for chunk in ordered])
        except requests.RequestException:
            log.exception("live chunk refresh failed")
            return
        if updates and remember:
            try:
                remember(updates)
            except Exception:
                log.exception("live chunk save failed")
        for chunk in ordered:
            url = mapping.get(chunk.get("url")) or chunk.get("url")
            if not url:
                log.error("live chunk missing a url")
                return
            urls.append(url)
        prefix = None
        replaced = 0
        if patch:
            prefix, replaced = patch
            first_size = int(ordered[0].get("size") or 0)
            if replaced < 0 or replaced > first_size or len(prefix) < replaced:
                log.error("live audio patch does not fit the first chunk")
                return
        job = {
            "urls": urls,
            "sizes": [int(chunk.get("size") or 0) for chunk in ordered],
            "source": os.path.join(directory, signature + ".src"),
            "target": exact,
            "prefix_hex": prefix.hex() if prefix is not None else "",
            "replaced": int(replaced),
            "pidfile": pidfile,
            "failed": failed,
            "directory": directory,
        }
        job_path = os.path.join(directory, signature + ".job.json")
        descriptor = os.open(job_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(job, handle)
        log_path = os.path.join(directory, signature + ".log")
        log_handle = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", "import sys, playback; playback.run_job(sys.argv[1])", job_path],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        except OSError:
            log.exception("live remux failed to start")
            log_handle.close()
            try:
                os.remove(job_path)
            except OSError:
                pass
            return
        with open(pidfile, "w") as handle:
            handle.write(str(proc.pid))
        log_handle.close()
        threading.Thread(target=proc.wait, daemon=True).start()
        log.info("started live remux %s", signature)


def ensure_playable(post_id, chunks, files, patch, remember=None, wait_seconds=None):
    """Return a faststart MP4 for this live, starting a remux when the stored file is fragmented."""
    ordered = sorted(chunks or [], key=lambda item: int(item.get("index") or 0))
    if not ordered:
        return None
    signature = live_signature(post_id, ordered)
    directory = os.path.join(CACHE_ROOT, safe_post_id(post_id))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        log.exception("live cache directory")
        return None
    exact = os.path.join(directory, signature + ".mp4")
    choice = cached_choice(directory, signature)
    if choice:
        if not _nonempty(exact):
            _schedule(directory, signature, ordered, files, patch, remember)
        _pin_new(directory, signature, choice)
        _touch_pin(directory, signature)
        return choice
    _schedule(directory, signature, ordered, files, patch, remember)
    wait = DEFAULT_WAIT if wait_seconds is None else wait_seconds
    found = _wait_for_file(directory, exact, wait) if wait else None
    if not found:
        return None
    _pin_new(directory, signature, found)
    _touch_pin(directory, signature)
    return found


def _download_parts(urls, directory, sizes=None):
    def one(index):
        expected = int(sizes[index]) if sizes and index < len(sizes) else 0
        dest = os.path.join(directory, "chunk-%03d.bin" % index)
        partial = dest + ".partial"
        try:
            if expected and os.path.isfile(dest) and os.path.getsize(dest) == expected:
                return dest
        except OSError:
            pass
        last_error = "error"
        for attempt in range(4):
            response = None
            try:
                try:
                    os.remove(partial)
                except OSError:
                    pass
                response = requests.get(urls[index], stream=True, timeout=(10, 25))
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    for piece in response.iter_content(256 * 1024):
                        if piece:
                            handle.write(piece)
                if expected and os.path.getsize(partial) != expected:
                    raise OSError("short")
                os.replace(partial, dest)
                return dest
            except (requests.RequestException, OSError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                last_error = status or type(exc).__name__
                print("chunk %s retry %s (%s)" % (index, attempt + 1, last_error), flush=True)
                time.sleep(1 + attempt)
            finally:
                if response is not None:
                    response.close()
        raise RuntimeError("chunk %s download failed (%s)" % (index, last_error))

    # One download at a time. Parallel reads from the file host stall and never finish.
    workers = 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, range(len(urls))))


def _patch_from_chunk(path):
    """Read the moov at the start of a stored chunk when the header request did not finish."""
    from media import audio_header_patch, moov_end

    with open(path, "rb") as handle:
        sample = handle.read(256 * 1024)
        end = moov_end(sample)
        if end and end > len(sample):
            handle.seek(0)
            sample = handle.read(min(end, 8 * 1024 * 1024))
    return audio_header_patch(sample)


def _write_source(parts, source, prefix, replaced):
    with open(source, "wb") as output:
        for index, part in enumerate(parts):
            with open(part, "rb") as handle:
                if index == 0 and prefix is not None:
                    if os.path.getsize(part) < replaced:
                        raise RuntimeError("first chunk is shorter than the audio patch")
                    output.write(prefix)
                    handle.seek(replaced)
                while True:
                    blob = handle.read(1024 * 1024)
                    if not blob:
                        break
                    output.write(blob)


def _cleanup_named(directory, predicate):
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if predicate(name):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


def _cleanup_partials(directory):
    _cleanup_named(
        directory,
        lambda name: name.startswith("chunk-") and (name.endswith(".partial") or name.endswith(".bin.partial")),
    )


def _cleanup_parts(directory):
    _cleanup_named(
        directory,
        lambda name: name.startswith("chunk-") and name.endswith(".bin") and not name.endswith(".partial"),
    )
    _cleanup_partials(directory)


def _fresh_pins(directory, max_age):
    fresh = set()
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return fresh
    for name in names:
        if not name.endswith(".pin"):
            continue
        pin_path = os.path.join(directory, name)
        try:
            stale = now - os.path.getmtime(pin_path) > max_age
        except OSError:
            continue
        if stale:
            try:
                os.remove(pin_path)
            except OSError:
                pass
            continue
        target = _read_pin(directory, name[: -len(".pin")])
        if target:
            fresh.add(os.path.realpath(target))
    return fresh


def _prune(directory, keep):
    # Keep the newest files and any file a player requested in the last 20 minutes.
    fresh = _fresh_pins(directory, 20 * 60)
    files = []
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".mp4") or name.endswith(".tmp"):
            continue
        path = os.path.join(directory, name)
        try:
            files.append((os.path.getmtime(path), path))
        except OSError:
            continue
    files.sort(reverse=True)
    for index, (_mtime, path) in enumerate(files):
        if index < keep or os.path.realpath(path) in fresh:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def run_job(path):
    logging.basicConfig(level=logging.INFO)
    try:
        os.nice(10)
    except OSError:
        pass
    with open(path) as handle:
        job = json.load(handle)
    directory = job["directory"]
    pidfile = job["pidfile"]
    target = job["target"]
    source = job["source"]
    tmp = target + ".tmp"
    prefix = bytes.fromhex(job["prefix_hex"]) if job.get("prefix_hex") else None
    replaced = int(job.get("replaced") or 0)
    with open(pidfile, "w") as handle:
        handle.write(str(os.getpid()))
    print("remux start chunks=%s" % len(job.get("urls") or []), flush=True)
    try:
        _cleanup_partials(directory)
        parts = _download_parts(job["urls"], directory, job.get("sizes"))
        if prefix is None:
            derived = _patch_from_chunk(parts[0])
            if derived:
                prefix, replaced = derived
                print("derived audio patch", flush=True)
        _write_source(parts, source, prefix, replaced)
        _cleanup_parts(directory)
        with open(source, "rb") as handle:
            magic = handle.read(12)
        if len(magic) < 12 or magic[4:8] != b"ftyp":
            raise RuntimeError("stored live is not an mp4")
        result = subprocess.run(ffmpeg_remux_args(source, tmp), stderr=subprocess.PIPE, timeout=900)
        if result.returncode != 0:
            tail = (result.stderr or b"")[-1500:]
            try:
                sys.stderr.buffer.write(tail + b"\n")
            except Exception:
                pass
            if not _playable_output(tmp):
                raise RuntimeError("ffmpeg exited %s" % result.returncode)
            print("remux kept a playable file after ffmpeg exited %s" % result.returncode, flush=True)
        elif not _nonempty(tmp):
            raise RuntimeError("remux produced an empty file")
        os.replace(tmp, target)
        _prune(directory, KEEP_FILES)
        failed = job.get("failed")
        if failed and os.path.isfile(failed):
            os.remove(failed)
        print("remux done bytes=%s" % os.path.getsize(target), flush=True)
    except Exception as exc:
        print("remux failed: %s" % type(exc).__name__, flush=True)
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        try:
            with open(job["failed"], "a"):
                pass
        except OSError:
            pass
        raise
    finally:
        for leftover in (source, path):
            try:
                if leftover and os.path.isfile(leftover):
                    os.remove(leftover)
            except OSError:
                pass
        _cleanup_partials(directory)
        try:
            current = int(open(pidfile).read().strip())
        except (OSError, ValueError):
            current = None
        if current == os.getpid():
            try:
                os.remove(pidfile)
            except OSError:
                pass
