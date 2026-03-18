"""
Cover art lookup via iTunes Search API + yt-dlp thumbnails.

Provides cached cover art URLs for currently playing tracks.
Non-blocking: cache misses trigger a background fetch, returning None immediately.
"""

import threading
import time
import urllib.request
import urllib.parse
import json
import re

# Cache: track_key -> {"url": str|None, "ts": float}
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 600  # 10 minutes
MAX_CACHE = 500

# Keys currently being fetched in background
_pending = set()
_pending_lock = threading.Lock()

# Per-stream override (e.g. yt-dlp thumbnail)
_overrides = {}
_overrides_lock = threading.Lock()


def is_enabled():
    """Check if cover art display is enabled in settings."""
    from db import get_setting
    val = get_setting("cover_art_enabled")
    # Default: enabled
    return val != "0"


def set_enabled(enabled):
    """Enable or disable cover art display."""
    from db import set_setting
    set_setting("cover_art_enabled", "1" if enabled else "0")


def _parse_artist_title(track_str):
    """Try to split 'Artist - Title' into (artist, title). Returns (query, None) if no separator."""
    if not track_str:
        return None, None
    for sep in (" - ", " – ", " — "):
        if sep in track_str:
            parts = track_str.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return track_str.strip(), None


def _clean_query(text):
    """Remove parenthetical extras like (Live), [Remaster], etc."""
    text = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", text)
    return text.strip()


def _itunes_search(query, timeout=4):
    """Search iTunes for artwork URL. Returns URL string or None."""
    try:
        params = urllib.parse.urlencode({
            "term": query,
            "media": "music",
            "limit": "1",
        })
        url = f"https://itunes.apple.com/search?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Streampeg/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read())
        results = data.get("results", [])
        if results:
            art = results[0].get("artworkUrl100", "")
            if art:
                return art.replace("100x100bb", "300x300bb")
    except Exception:
        pass
    return None


def _cache_key(track_str):
    """Normalize track string for cache key."""
    return track_str.strip().lower()


def _store_in_cache(key, url):
    """Store a result in cache, evicting old entries if needed."""
    with _cache_lock:
        if len(_cache) >= MAX_CACHE:
            oldest = sorted(_cache.items(), key=lambda x: x[1]["ts"])[:MAX_CACHE // 4]
            for k, _ in oldest:
                del _cache[k]
        _cache[key] = {"url": url, "ts": time.time()}


def _background_fetch(key, track_str):
    """Fetch cover art in background thread and store in cache."""
    try:
        artist, title = _parse_artist_title(track_str)
        if not artist:
            _store_in_cache(key, None)
            return
        if title:
            query = f"{_clean_query(artist)} {_clean_query(title)}"
        else:
            query = _clean_query(artist)
        url = _itunes_search(query)
        _store_in_cache(key, url)
    finally:
        with _pending_lock:
            _pending.discard(key)


def lookup(track_str):
    """Look up cover art URL. Returns cached URL or None. Triggers background fetch on cache miss."""
    if not track_str or track_str.strip() in ("", "-", "recording"):
        return None

    key = _cache_key(track_str)

    # Check cache
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.time() - cached["ts"] < CACHE_TTL:
            return cached["url"]

    # Trigger background fetch if not already pending
    with _pending_lock:
        if key not in _pending:
            _pending.add(key)
            t = threading.Thread(target=_background_fetch, args=(key, track_str), daemon=True)
            t.start()

    return None


def set_override(stream_id, url):
    """Set a cover art override for a stream (e.g. from yt-dlp thumbnail)."""
    with _overrides_lock:
        _overrides[stream_id] = url


def clear_override(stream_id):
    """Clear cover art override for a stream."""
    with _overrides_lock:
        _overrides.pop(stream_id, None)


def get_override(stream_id):
    """Get cover art override for a stream, or None."""
    with _overrides_lock:
        return _overrides.get(stream_id)


def get_cover_url(stream_id, track_str):
    """Get cover art URL: override first, then iTunes lookup. Returns None if not yet available."""
    if not is_enabled():
        return None
    # Check override (yt-dlp thumbnail)
    override = get_override(stream_id)
    if override:
        return override
    # iTunes lookup (non-blocking)
    return lookup(track_str)
