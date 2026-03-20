"""
Library scanner module — walk recording directories, read ID3 tags, and
populate the library_tracks table.  Also handles M3U playlist generation.
"""

import os
import threading
import logging

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, ID3NoHeaderError
    _mutagen_available = True
except ImportError:
    _mutagen_available = False

import db
import sync
import config

log = logging.getLogger(__name__)

# Background scan state
_scan_status = {
    "running": False,
    "progress": 0,
    "files_scanned": 0,
    "files_total": 0,
    "files_updated": 0,
}
_scan_lock = threading.Lock()


def _read_id3(filepath):
    """Read ID3 tags from an MP3 file. Returns a dict of tag values."""
    result = {
        "title": "",
        "artist": "",
        "album": "",
        "genre": "",
        "bpm": 0,
        "key": "",
        "duration_sec": 0,
    }
    if not _mutagen_available:
        return result
    try:
        audio = MP3(filepath)
        result["duration_sec"] = int(audio.info.length) if audio.info else 0
    except Exception:
        pass
    try:
        tags = ID3(filepath)
    except (ID3NoHeaderError, Exception):
        return result

    tag_map = {
        "TIT2": "title",
        "TPE1": "artist",
        "TALB": "album",
        "TCON": "genre",
        "TBPM": "bpm",
        "TKEY": "key",
    }
    for tag_id, field in tag_map.items():
        frame = tags.get(tag_id)
        if frame:
            text = str(frame)
            if field == "bpm":
                try:
                    result["bpm"] = int(float(text))
                except (ValueError, TypeError):
                    result["bpm"] = 0
            else:
                result[field] = text
    return result


def _collect_mp3s(base_dir, subdir=None):
    """Walk directory tree and return list of (filepath, stream_subdir) tuples."""
    files = []
    if not os.path.isdir(base_dir):
        return files

    if subdir:
        scan_dir = os.path.join(base_dir, subdir)
        if not os.path.isdir(scan_dir):
            return files
        for root, dirs, filenames in os.walk(scan_dir):
            if "incomplete" in root:
                continue
            for f in filenames:
                if f.lower().endswith(".mp3"):
                    files.append((os.path.join(root, f), subdir))
    else:
        # Walk all top-level subdirectories
        try:
            entries = os.listdir(base_dir)
        except OSError:
            return files
        for entry in sorted(entries):
            entry_path = os.path.join(base_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            for root, dirs, filenames in os.walk(entry_path):
                if "incomplete" in root:
                    continue
                for f in filenames:
                    if f.lower().endswith(".mp3"):
                        files.append((os.path.join(root, f), entry))
    return files


def _scan_files(files):
    """Scan a list of (filepath, stream_subdir) tuples into the library."""
    global _scan_status

    with _scan_lock:
        _scan_status["files_total"] = len(files)
        _scan_status["files_scanned"] = 0
        _scan_status["files_updated"] = 0
        _scan_status["progress"] = 0

    # Build a map of filepath -> mtime from DB for skip check and cleanup
    conn = db.get_db()
    rows = conn.execute("SELECT filepath, mtime FROM library_tracks").fetchall()
    conn.close()
    existing_mtimes = {r["filepath"]: r["mtime"] for r in rows}
    existing_paths = set(existing_mtimes.keys())

    scanned_paths = set()

    # Phase 1: Quick scan — register files by name/path (no ID3 reading)
    # This is fast even over NAS because we only stat() files.
    new_files = []
    for filepath, stream_subdir in files:
        if not _scan_status["running"]:
            break

        scanned_paths.add(filepath)

        if filepath in existing_mtimes:
            # Already known, skip
            with _scan_lock:
                _scan_status["files_scanned"] += 1
                total = _scan_status["files_total"]
                _scan_status["progress"] = int(_scan_status["files_scanned"] / total * 100) if total else 0
            continue

        # New file — quick insert with filename-derived title/artist
        try:
            stat = os.stat(filepath)
        except OSError:
            with _scan_lock:
                _scan_status["files_scanned"] += 1
                total = _scan_status["files_total"]
                _scan_status["progress"] = int(_scan_status["files_scanned"] / total * 100) if total else 0
            continue

        filename = os.path.basename(filepath)
        name_base = os.path.splitext(filename)[0]
        # Try to parse "Artist - Title" from filename
        title, artist = name_base, ""
        if " - " in name_base:
            parts = name_base.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()

        data = {
            "filepath": filepath,
            "filename": filename,
            "stream_subdir": stream_subdir,
            "title": title,
            "artist": artist,
            "album": "",
            "genre": "",
            "bpm": 0,
            "key": "",
            "duration_sec": 0,
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
        }
        db.upsert_library_track(data)
        new_files.append(filepath)

        with _scan_lock:
            _scan_status["files_updated"] += 1
            _scan_status["files_scanned"] += 1
            total = _scan_status["files_total"]
            _scan_status["progress"] = int(_scan_status["files_scanned"] / total * 100) if total else 0

    # Cleanup: remove DB entries for files that no longer exist on disk
    removed = existing_paths - scanned_paths
    for path in removed:
        db.delete_library_track_by_path(path)

    # Phase 2: Read ID3 tags for new files (slower, but library is already browsable)
    if new_files:
        with _scan_lock:
            _scan_status["files_scanned"] = 0
            _scan_status["files_total"] = len(new_files)
            _scan_status["files_updated"] = 0
            _scan_status["progress"] = 0

        for filepath in new_files:
            if not _scan_status["running"]:
                break
            tags = _read_id3(filepath)
            # Only update if we got meaningful tag data
            if tags.get("title") or tags.get("artist") or tags.get("bpm") or tags.get("duration_sec"):
                conn = db.get_db()
                conn.execute(
                    """UPDATE library_tracks SET
                        title = CASE WHEN ? != '' THEN ? ELSE title END,
                        artist = CASE WHEN ? != '' THEN ? ELSE artist END,
                        album = ?, genre = ?, bpm = ?, key = ?, duration_sec = ?
                    WHERE filepath = ?""",
                    (
                        tags["title"], tags["title"],
                        tags["artist"], tags["artist"],
                        tags["album"], tags["genre"],
                        tags["bpm"], tags["key"], tags["duration_sec"],
                        filepath,
                    ),
                )
                conn.commit()
                conn.close()

            with _scan_lock:
                _scan_status["files_scanned"] += 1
                total = _scan_status["files_total"]
                _scan_status["progress"] = int(_scan_status["files_scanned"] / total * 100) if total else 0


def scan_library():
    """Walk all subdirectories under sync target (NAS) and RECORDING_BASE (local).
    NAS is primary; local is fallback for files not yet synced."""
    files = []
    seen_paths = set()

    # Primary: NAS (sync target)
    nas_target = sync.get_sync_target()
    if nas_target and os.path.isdir(nas_target):
        for item in _collect_mp3s(nas_target):
            files.append(item)
            seen_paths.add(os.path.basename(item[0]) + "|" + item[1])

    # Secondary: local recording base (only files not already found on NAS)
    local_base = config.RECORDING_BASE
    if local_base and os.path.isdir(local_base):
        for filepath, subdir in _collect_mp3s(local_base):
            key = os.path.basename(filepath) + "|" + subdir
            if key not in seen_paths:
                files.append((filepath, subdir))

    _scan_files(files)


def scan_stream(stream_subdir):
    """Scan only one stream's directory."""
    files = []
    seen_paths = set()

    nas_target = sync.get_sync_target()
    if nas_target and os.path.isdir(nas_target):
        for item in _collect_mp3s(nas_target, subdir=stream_subdir):
            files.append(item)
            seen_paths.add(os.path.basename(item[0]))

    local_base = config.RECORDING_BASE
    if local_base and os.path.isdir(local_base):
        for filepath, subdir in _collect_mp3s(local_base, subdir=stream_subdir):
            if os.path.basename(filepath) not in seen_paths:
                files.append((filepath, subdir))

    _scan_files(files)


def _run_scan(subdir=None):
    """Background scan worker."""
    global _scan_status
    try:
        if subdir:
            scan_stream(subdir)
        else:
            scan_library()
    except Exception as e:
        log.error("Library scan error: %s", e)
    finally:
        with _scan_lock:
            _scan_status["running"] = False
            _scan_status["progress"] = 100


def start_scan(subdir=None):
    """Start background library scan thread. Returns False if already running."""
    global _scan_status
    with _scan_lock:
        if _scan_status["running"]:
            return False
        _scan_status = {
            "running": True,
            "progress": 0,
            "files_scanned": 0,
            "files_total": 0,
            "files_updated": 0,
        }
    t = threading.Thread(target=_run_scan, args=(subdir,), daemon=True)
    t.start()
    return True


def get_scan_status():
    """Returns current scan status dict."""
    with _scan_lock:
        return dict(_scan_status)


def generate_m3u(playlist_id):
    """Generate an M3U playlist file on the sync target.
    Returns the written file path or None on error."""
    playlist = db.get_playlist(playlist_id)
    if not playlist:
        return None

    tracks = db.get_playlist_tracks(playlist_id)
    if not tracks:
        return None

    nas_target = sync.get_sync_target()
    if not nas_target or not os.path.isdir(nas_target):
        return None

    lines = ["#EXTM3U"]
    for t in tracks:
        duration = t.get("duration_sec", 0)
        artist = t.get("artist", "")
        title = t.get("title", "") or t.get("filename", "")
        display = f"{artist} - {title}" if artist else title

        # Make path relative to sync target root
        filepath = t.get("filepath", "")
        if filepath.startswith(nas_target):
            rel_path = os.path.relpath(filepath, nas_target)
        else:
            # File is local; use stream_subdir/filename as relative path
            rel_path = os.path.join(t.get("stream_subdir", ""), t.get("filename", ""))

        lines.append(f"#EXTINF:{duration},{display}")
        lines.append(rel_path)

    playlist_name = playlist["name"]
    # Sanitize filename
    safe_name = "".join(c for c in playlist_name if c.isalnum() or c in " _-").strip()
    m3u_path = os.path.join(nas_target, f"{safe_name}.m3u")

    try:
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return m3u_path
    except OSError as e:
        log.error("Failed to write M3U %s: %s", m3u_path, e)
        return None


def delete_m3u(playlist_name):
    """Remove an M3U file from the sync target."""
    nas_target = sync.get_sync_target()
    if not nas_target:
        return False

    safe_name = "".join(c for c in playlist_name if c.isalnum() or c in " _-").strip()
    m3u_path = os.path.join(nas_target, f"{safe_name}.m3u")

    try:
        if os.path.exists(m3u_path):
            os.remove(m3u_path)
            return True
    except OSError as e:
        log.error("Failed to delete M3U %s: %s", m3u_path, e)
    return False
