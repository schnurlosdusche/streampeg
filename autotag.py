"""
Auto-tagging module — fingerprint audio files and write ID3 tags.

Pipeline: fpcalc (chromaprint) -> AcoustID -> MusicBrainz -> Cover Art Archive -> mutagen (ID3)
Fallback: parse Artist - Title from filename.
"""

import os
import re
import json
import subprocess
import threading
import time
import urllib.request
import urllib.parse

try:
    import mutagen
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, APIC, ID3NoHeaderError
    _mutagen_available = True
except ImportError:
    _mutagen_available = False

# Background job tracking
_jobs = {}  # stream_id -> {"total": int, "done": int, "running": bool, "errors": []}
_jobs_lock = threading.Lock()

# Rate limiting for MusicBrainz (1 req/sec)
_mb_last_request = 0
_mb_lock = threading.Lock()

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
UA = "Streampeg/1.0 (stream-recording-ui)"


def is_enabled():
    """Check if auto-tagging is enabled."""
    from db import get_setting
    return get_setting("autotag_enabled") == "1"


def set_enabled(enabled):
    from db import set_setting
    set_setting("autotag_enabled", "1" if enabled else "0")


def get_acoustid_key():
    from db import get_setting
    return get_setting("autotag_acoustid_key") or ""


def set_acoustid_key(key):
    from db import set_setting
    set_setting("autotag_acoustid_key", key.strip())


def _fpcalc_available():
    """Check if fpcalc binary is available."""
    try:
        subprocess.run(["fpcalc", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# === Fingerprinting ===

def fingerprint_file(filepath):
    """Run fpcalc on a file. Returns (duration, fingerprint) or (None, None)."""
    try:
        result = subprocess.run(
            ["fpcalc", "-json", filepath],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("duration"), data.get("fingerprint")
    except Exception:
        pass
    return None, None


# === AcoustID lookup ===

def lookup_acoustid(duration, fingerprint, api_key):
    """Query AcoustID. Returns list of (recording_id, score) or empty list."""
    if not api_key or not fingerprint:
        return []
    try:
        params = urllib.parse.urlencode({
            "client": api_key,
            "duration": str(int(duration)),
            "fingerprint": fingerprint,
            "meta": "recordings",
        })
        url = f"{ACOUSTID_URL}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        results = []
        for r in data.get("results", []):
            for rec in r.get("recordings", []):
                results.append((rec["id"], r.get("score", 0)))
        results.sort(key=lambda x: x[1], reverse=True)
        return results
    except Exception:
        return []


# === MusicBrainz metadata ===

def _mb_rate_limit():
    """Ensure at least 1 second between MusicBrainz requests."""
    global _mb_last_request
    with _mb_lock:
        now = time.time()
        wait = 1.0 - (now - _mb_last_request)
        if wait > 0:
            time.sleep(wait)
        _mb_last_request = time.time()


def fetch_musicbrainz(recording_id):
    """Fetch metadata from MusicBrainz. Returns dict or None."""
    _mb_rate_limit()
    try:
        url = f"{MB_BASE}/recording/{recording_id}?inc=artists+releases+genres&fmt=json"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())

        artist = ""
        if data.get("artist-credit"):
            parts = []
            for ac in data["artist-credit"]:
                parts.append(ac.get("name", ""))
                if ac.get("joinphrase"):
                    parts.append(ac["joinphrase"])
            artist = "".join(parts)

        title = data.get("title", "")

        album = ""
        release_id = None
        date = ""
        if data.get("releases"):
            rel = data["releases"][0]
            album = rel.get("title", "")
            release_id = rel.get("id")
            date = rel.get("date", "")

        genres = []
        for g in data.get("genres", []):
            genres.append(g.get("name", ""))

        return {
            "artist": artist,
            "title": title,
            "album": album,
            "date": date[:4] if date else "",
            "genre": "; ".join(genres[:3]) if genres else "",
            "release_id": release_id,
        }
    except Exception:
        return None


# === Cover Art Archive ===

def fetch_cover_art(release_id):
    """Download front cover from Cover Art Archive. Returns bytes or None."""
    if not release_id:
        return None
    _mb_rate_limit()
    try:
        url = f"{CAA_BASE}/release/{release_id}/front-500"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.read()
    except Exception:
        return None


# === ID3 tag writing ===

def _has_tags(filepath):
    """Check if file already has meaningful ID3 tags (artist + title)."""
    if not _mutagen_available:
        return False
    try:
        tags = ID3(filepath)
        has_title = tags.get("TIT2") is not None
        has_artist = tags.get("TPE1") is not None
        return has_title and has_artist
    except Exception:
        return False


def write_tags(filepath, metadata, cover_bytes=None):
    """Write ID3v2.4 tags to an MP3 file using mutagen."""
    if not _mutagen_available:
        return False
    try:
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()

        if metadata.get("title"):
            tags["TIT2"] = TIT2(encoding=3, text=[metadata["title"]])
        if metadata.get("artist"):
            tags["TPE1"] = TPE1(encoding=3, text=[metadata["artist"]])
        if metadata.get("album"):
            tags["TALB"] = TALB(encoding=3, text=[metadata["album"]])
        if metadata.get("date"):
            tags["TDRC"] = TDRC(encoding=3, text=[metadata["date"]])
        if metadata.get("genre"):
            tags["TCON"] = TCON(encoding=3, text=[metadata["genre"]])
        if cover_bytes:
            tags["APIC:"] = APIC(
                encoding=3, mime="image/jpeg", type=3,
                desc="Cover", data=cover_bytes,
            )

        tags.save(filepath, v2_version=4)
        return True
    except Exception:
        return False


# === Filename fallback ===

def _parse_filename(filepath):
    """Parse Artist - Title from filename."""
    name = os.path.splitext(os.path.basename(filepath))[0]
    # Remove common suffixes like _1234567890
    name = re.sub(r"_\d{8,}$", "", name)
    for sep in (" - ", " – ", " — ", "_-_"):
        if sep in name:
            parts = name.split(sep, 1)
            return {"artist": parts[0].strip().replace("_", " "),
                    "title": parts[1].strip().replace("_", " ")}
    return {"artist": "", "title": name.replace("_", " ")}


# === Full pipeline ===

def process_file(filepath, api_key=None):
    """Tag a single file. Returns (success, method) where method is 'acoustid', 'filename', or 'failed'."""
    if not filepath or not os.path.exists(filepath):
        return False, "not_found"

    if not filepath.lower().endswith(".mp3"):
        return False, "not_mp3"

    if not _mutagen_available:
        return False, "no_mutagen"

    # Skip already tagged files
    if _has_tags(filepath):
        return True, "already_tagged"

    if not api_key:
        api_key = get_acoustid_key()

    metadata = None
    cover_bytes = None
    method = "failed"

    # Try AcoustID + MusicBrainz
    if api_key and _fpcalc_available():
        duration, fp = fingerprint_file(filepath)
        if fp:
            matches = lookup_acoustid(duration, fp, api_key)
            if matches:
                recording_id, score = matches[0]
                if score > 0.5:
                    mb_data = fetch_musicbrainz(recording_id)
                    if mb_data and mb_data.get("title"):
                        metadata = mb_data
                        cover_bytes = fetch_cover_art(mb_data.get("release_id"))
                        method = "acoustid"

    # Fallback: filename parsing
    if not metadata:
        metadata = _parse_filename(filepath)
        method = "filename"

    if metadata and (metadata.get("artist") or metadata.get("title")):
        ok = write_tags(filepath, metadata, cover_bytes)
        return ok, method

    return False, "failed"


# === Batch processing ===

def process_directory(stream_id, dirpath, api_key=None):
    """Tag all untagged MP3 files in a directory. Runs in foreground (call from thread)."""
    if not os.path.isdir(dirpath):
        return

    if not api_key:
        api_key = get_acoustid_key()

    files = []
    for root, dirs, filenames in os.walk(dirpath):
        if "incomplete" in root:
            continue
        for f in filenames:
            if f.lower().endswith(".mp3"):
                files.append(os.path.join(root, f))

    with _jobs_lock:
        _jobs[stream_id] = {"total": len(files), "done": 0, "running": True, "errors": []}

    for filepath in files:
        ok, method = process_file(filepath, api_key)
        with _jobs_lock:
            job = _jobs[stream_id]
            job["done"] += 1
            if not ok and method not in ("already_tagged", "not_mp3"):
                job["errors"].append(os.path.basename(filepath))

    with _jobs_lock:
        _jobs[stream_id]["running"] = False


def start_batch(stream_id, dirpath):
    """Start batch tagging in background thread."""
    with _jobs_lock:
        if stream_id in _jobs and _jobs[stream_id].get("running"):
            return False  # Already running
    t = threading.Thread(target=process_directory, args=(stream_id, dirpath), daemon=True)
    t.start()
    return True


def get_job_status(stream_id):
    """Get batch job status. Returns dict or None."""
    with _jobs_lock:
        return dict(_jobs[stream_id]) if stream_id in _jobs else None
