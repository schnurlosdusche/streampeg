"""
Library scanner module — walk recording directories, read ID3 tags, and
populate the library_tracks table.  Also handles M3U playlist generation.
"""

import os
import re
import subprocess
import threading
import logging

def _normalize_for_match(text):
    """Normalize a string for fuzzy duplicate matching."""
    if not text:
        return ""
    s = text.lower()
    s = re.sub(r"['\u2019\u2018`_\-]", "", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, ID3NoHeaderError
    _mutagen_available = True
except ImportError:
    _mutagen_available = False


def _ffprobe_duration(filepath):
    """Get duration in seconds via ffprobe (fallback for CBR MP3s without VBR header)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10)
        return int(float(r.stdout.strip())) if r.stdout.strip() else 0
    except Exception:
        return 0

import json as _json
import tempfile
import shutil
import time

import db
import sync
import config

log = logging.getLogger(__name__)

# Loudness normalization constants
_TARGET_LUFS = -14.0   # YouTube / Spotify standard
_TARGET_TP = -1.0      # true peak ceiling (dBTP)
_TARGET_LRA = 11.0     # loudness range


def _normalize_loudness(filepath):
    """Normalize MP3 to -14 LUFS using ffmpeg two-pass loudnorm.
    Returns measured input LUFS on success, None on failure."""

    # Pass 1: Measure integrated loudness
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", filepath, "-af",
             f"loudnorm=I={_TARGET_LUFS}:TP={_TARGET_TP}:LRA={_TARGET_LRA}:print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
        output = r.stderr
        json_start = output.rfind('{')
        json_end = output.rfind('}') + 1
        if json_start < 0 or json_end <= json_start:
            log.warning("loudnorm: no JSON in ffmpeg output for %s", filepath)
            return None
        stats = _json.loads(output[json_start:json_end])
        measured_i = float(stats["input_i"])
        measured_tp = float(stats["input_tp"])
        measured_lra = float(stats["input_lra"])
        measured_thresh = float(stats["input_thresh"])
    except Exception as e:
        log.warning("Loudness measurement failed for %s: %s", filepath, e)
        return None

    # If already within 0.5 dB of target, skip re-encoding
    if abs(measured_i - _TARGET_LUFS) < 0.5:
        log.debug("Track already at target loudness (%.1f LUFS): %s", measured_i, filepath)
        return measured_i

    # Detect original bitrate to preserve quality
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=bit_rate",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10)
        bitrate = int(r.stdout.strip()) // 1000 if r.stdout.strip() else 192
    except Exception:
        bitrate = 192
    bitrate = max(128, min(320, bitrate))

    # Save ID3 tags before re-encoding (ffmpeg may lose cover art)
    saved_tags = None
    if _mutagen_available:
        try:
            tags = ID3(filepath)
            saved_tags = tags
        except Exception:
            pass

    # Pass 2: Apply normalization to temp file
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".mp3", dir=os.path.dirname(filepath))
        os.close(fd)
        r = subprocess.run(
            ["ffmpeg", "-i", filepath, "-af",
             f"loudnorm=I={_TARGET_LUFS}:TP={_TARGET_TP}:LRA={_TARGET_LRA}:"
             f"measured_I={measured_i}:measured_TP={measured_tp}:"
             f"measured_LRA={measured_lra}:measured_thresh={measured_thresh}:"
             f"linear=true",
             "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
             "-map_metadata", "0", "-id3v2_version", "3",
             "-y", tmp_path],
            capture_output=True, text=True, timeout=300)

        if r.returncode != 0:
            log.warning("Loudness normalization failed for %s: %s", filepath, r.stderr[-300:])
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return None

        # Replace original with normalized version
        shutil.move(tmp_path, filepath)
        tmp_path = None

        # Restore ID3 tags (cover art, etc.) that ffmpeg may have dropped
        if saved_tags and _mutagen_available:
            try:
                saved_tags.save(filepath)
            except Exception as e:
                log.debug("Could not restore ID3 tags after normalization for %s: %s", filepath, e)

        log.info("Normalized %s from %.1f to %.1f LUFS @ %dk", filepath, measured_i, _TARGET_LUFS, bitrate)
        return measured_i

    except Exception as e:
        log.warning("Loudness normalization error for %s: %s", filepath, e)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return None

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
        "bitrate": 0,
    }
    if not _mutagen_available:
        return result
    try:
        audio = MP3(filepath)
        dur = int(audio.info.length) if audio.info else 0
        if dur <= 0:
            dur = _ffprobe_duration(filepath)
        result["duration_sec"] = dur
        if audio.info and audio.info.bitrate:
            result["bitrate"] = int(audio.info.bitrate / 1000)  # kbit/s
    except Exception:
        result["duration_sec"] = _ffprobe_duration(filepath)
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


def fix_missing_tags(stream_subdir=None):
    """Find tracks with no ID3 title/artist tags, parse from filename, write to file + DB."""
    if not _mutagen_available:
        log.warning("mutagen not available, cannot fix tags")
        return 0

    from mutagen.id3 import TIT2, TPE1

    conn = db.get_db()
    if stream_subdir:
        rows = conn.execute(
            """SELECT id, filepath, filename, title, artist FROM library_tracks
               WHERE trashed=0 AND stream_subdir=?""",
            (stream_subdir,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, filepath, filename, title, artist FROM library_tracks WHERE trashed=0"
        ).fetchall()
    conn.close()

    fixed = 0
    for row in rows:
        filepath = row["filepath"]
        filename = row["filename"]
        if not os.path.isfile(filepath):
            continue

        # Check if file already has ID3 tags
        try:
            tags = ID3(filepath)
            has_title = bool(tags.get("TIT2"))
            has_artist = bool(tags.get("TPE1"))
        except Exception:
            has_title = False
            has_artist = False
            tags = None

        if has_title and has_artist:
            continue

        fn_artist = ""
        fn_title = ""

        # Try parsing "Artist - Title" from existing ID3 title (e.g. streamripper metadata)
        if has_title and not has_artist:
            id3_title = str(tags.get("TIT2"))
            if " - " in id3_title:
                parts = id3_title.split(" - ", 1)
                fn_artist = parts[0].strip()
                fn_title = parts[1].strip()

        # Fallback: parse from filename
        if not fn_artist or not fn_title:
            name_base = os.path.splitext(filename)[0]
            if " - " in name_base:
                parts = name_base.split(" - ", 1)
                fn_artist = parts[0].strip().replace("_", " ")
                fn_title = parts[1].strip().replace("_", " ")

        if not fn_artist or not fn_title:
            continue

        # Write tags to file
        try:
            from mutagen.id3 import ID3 as _ID3
            try:
                tags = _ID3(filepath)
            except Exception:
                from mutagen.id3 import ID3NoHeaderError as _Err
                tags = _ID3()

            if not has_title:
                tags.add(TIT2(encoding=3, text=fn_title))
            if not has_artist:
                tags.add(TPE1(encoding=3, text=fn_artist))
            tags.save(filepath)

            # Update DB
            conn = db.get_db()
            conn.execute(
                "UPDATE library_tracks SET title=?, artist=? WHERE id=?",
                (fn_title, fn_artist, row["id"]),
            )
            conn.commit()
            conn.close()
            fixed += 1
        except Exception as e:
            log.warning("Failed to write tags for %s: %s", filepath, e)

    log.info("Fixed tags for %d files%s", fixed, f" in {stream_subdir}" if stream_subdir else "")
    return fixed


def _musicbrainz_enrich_batch(max_per_run=50):
    """Find tracks with artist+title but missing album/genre, look up on MusicBrainz."""
    import autotag
    import urllib.request
    import urllib.parse
    import json
    import time

    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, filepath, title, artist, album, genre FROM library_tracks
           WHERE trashed=0 AND artist != '' AND title != ''
           AND (album = '' OR album IS NULL)
           AND (genre = '' OR genre IS NULL)
           LIMIT ?""",
        (max_per_run,),
    ).fetchall()
    conn.close()

    if not rows:
        return 0

    enriched = 0
    UA = "streampeg/0.1 (https://github.com/martin/streampeg)"

    for row in rows:
        if not _daemon_running:
            break

        artist = row["artist"]
        title = row["title"]
        track_id = row["id"]
        filepath = row["filepath"]

        # Search MusicBrainz by artist + title
        autotag._mb_rate_limit()
        try:
            query = urllib.parse.quote(f'artist:"{artist}" AND recording:"{title}"')
            url = f"https://musicbrainz.org/ws/2/recording/?query={query}&limit=1&fmt=json"
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())

            recordings = data.get("recordings", [])
            if not recordings:
                # Mark as checked so we don't retry (set album to "-")
                conn = db.get_db()
                conn.execute("UPDATE library_tracks SET album='-' WHERE id=?", (track_id,))
                conn.commit()
                conn.close()
                continue

            rec = recordings[0]
            mb_title = rec.get("title", "")
            mb_artist = ""
            if rec.get("artist-credit"):
                parts = []
                for ac in rec["artist-credit"]:
                    parts.append(ac.get("name", ""))
                    if ac.get("joinphrase"):
                        parts.append(ac["joinphrase"])
                mb_artist = "".join(parts)

            mb_album = ""
            release_id = None
            if rec.get("releases"):
                rel = rec["releases"][0]
                mb_album = rel.get("title", "")
                release_id = rel.get("id")

            genres = []
            for g in rec.get("tags", []):
                genres.append(g.get("name", ""))

            mb_genre = "; ".join(genres[:3]) if genres else ""

            # Update DB
            conn = db.get_db()
            conn.execute(
                """UPDATE library_tracks SET
                    album = CASE WHEN ? != '' THEN ? ELSE album END,
                    genre = CASE WHEN ? != '' THEN ? ELSE genre END
                WHERE id = ?""",
                (mb_album or "-", mb_album or "-", mb_genre, mb_genre, track_id),
            )
            conn.commit()
            conn.close()

            # Write tags to file if possible
            if os.path.isfile(filepath):
                try:
                    from mutagen.id3 import ID3 as _ID3, TALB, TCON
                    try:
                        tags = _ID3(filepath)
                    except Exception:
                        tags = _ID3()
                    if mb_album and mb_album != "-":
                        tags.add(TALB(encoding=3, text=mb_album))
                    if mb_genre:
                        tags.add(TCON(encoding=3, text=mb_genre))
                    tags.save(filepath)
                except Exception as e:
                    log.debug("Could not write MB tags to %s: %s", filepath, e)

            # Fetch cover art if we have a release_id
            if release_id and os.path.isfile(filepath):
                cover_data = autotag.fetch_cover_art(release_id)
                if cover_data:
                    try:
                        from mutagen.id3 import ID3 as _ID3, APIC
                        tags = _ID3(filepath)
                        tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                                      desc="Front", data=cover_data))
                        tags.save(filepath)
                    except Exception as e:
                        log.debug("Could not write cover to %s: %s", filepath, e)

            enriched += 1

        except Exception as e:
            log.debug("MB lookup failed for %s - %s: %s", artist, title, e)
            # Mark as checked
            conn = db.get_db()
            conn.execute("UPDATE library_tracks SET album='-' WHERE id=?", (track_id,))
            conn.commit()
            conn.close()

    return enriched


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
        _scan_status["phase"] = "new"

    # Build a map of filepath -> mtime from DB for skip check and cleanup
    # Only consider tracks from the same subdirs being scanned
    scan_subdirs = set(subdir for _, subdir in files) if files else set()
    conn = db.get_db()
    if scan_subdirs:
        placeholders = ",".join("?" for _ in scan_subdirs)
        rows = conn.execute(
            f"SELECT filepath, mtime FROM library_tracks WHERE stream_subdir IN ({placeholders})",
            list(scan_subdirs),
        ).fetchall()
    else:
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

    # Phase 1.5: Deduplicate by Artist+Title
    try:
        deduped = _deduplicate_library()
        if deduped > 0:
            log.info("Deduplicated %d tracks", deduped)
    except Exception as e:
        log.error("Dedup error: %s", e)

    # Phase 2: Read ID3 tags only for tracks that actually need data
    # - new files (no tags yet)
    # - tracks missing bitrate, duration, bpm, key, or tag_status
    conn = db.get_db()
    incomplete_rows = conn.execute(
        """SELECT filepath FROM library_tracks WHERE
            bitrate IS NULL OR bitrate = 0
            OR duration_sec IS NULL OR duration_sec = 0
            OR tag_status IS NULL"""
    ).fetchall()
    conn.close()
    incomplete_paths = set(r["filepath"] for r in incomplete_rows)
    phase2_files = list(set(new_files) | incomplete_paths)

    if phase2_files:
        with _scan_lock:
            _scan_status["files_scanned"] = 0
            _scan_status["files_total"] = len(phase2_files)
            _scan_status["files_updated"] = 0
            _scan_status["progress"] = 0
            _scan_status["phase"] = "scan"

        for filepath in phase2_files:
            if not _scan_status["running"]:
                break
            if not os.path.isfile(filepath):
                with _scan_lock:
                    _scan_status["files_scanned"] += 1
                    total = _scan_status["files_total"]
                    _scan_status["progress"] = int(_scan_status["files_scanned"] / total * 100) if total else 0
                continue
            tags = _read_id3(filepath)
            has_artist = bool(tags.get("artist"))
            has_title = bool(tags.get("title"))
            tag_status = "ok" if (has_artist and has_title) else "needs_tag"

            if tags.get("title") or tags.get("artist") or tags.get("bpm") or tags.get("duration_sec") or tags.get("bitrate"):
                conn = db.get_db()
                conn.execute(
                    """UPDATE library_tracks SET
                        title = CASE WHEN ? != '' THEN ? ELSE title END,
                        artist = CASE WHEN ? != '' THEN ? ELSE artist END,
                        album = CASE WHEN ? != '' THEN ? ELSE album END,
                        genre = CASE WHEN ? != '' THEN ? ELSE genre END,
                        bpm = CASE WHEN ? > 0 THEN ? ELSE bpm END,
                        key = CASE WHEN ? != '' THEN ? ELSE key END,
                        duration_sec = CASE WHEN ? > 0 THEN ? ELSE duration_sec END,
                        bitrate = CASE WHEN ? > 0 THEN ? ELSE bitrate END,
                        tag_status = ?
                    WHERE filepath = ?""",
                    (
                        tags["title"], tags["title"],
                        tags["artist"], tags["artist"],
                        tags["album"], tags["album"],
                        tags["genre"], tags["genre"],
                        tags["bpm"], tags["bpm"],
                        tags["key"], tags["key"],
                        tags["duration_sec"], tags["duration_sec"],
                        tags["bitrate"], tags["bitrate"],
                        tag_status,
                        filepath,
                    ),
                )
                conn.commit()
                conn.close()

            with _scan_lock:
                _scan_status["files_scanned"] += 1
                total = _scan_status["files_total"]
                _scan_status["progress"] = int(_scan_status["files_scanned"] / total * 100) if total else 0

    # Phase 3: Autotag tracks that need identification
    try:
        import autotag
        if autotag.is_enabled() and autotag.get_acoustid_key():
            conn = db.get_db()
            needs_tag = conn.execute(
                "SELECT id, filepath FROM library_tracks WHERE tag_status = 'needs_tag' AND trashed = 0"
            ).fetchall()
            conn.close()

            if needs_tag:
                log.info("Phase 3: Autotagging %d tracks", len(needs_tag))
                with _scan_lock:
                    _scan_status["files_scanned"] = 0
                    _scan_status["files_total"] = len(needs_tag)
                    _scan_status["progress"] = 0
                    _scan_status["phase"] = "tag"

                for row in needs_tag:
                    if not _scan_status["running"]:
                        break
                    filepath = row["filepath"]
                    track_id = row["id"]
                    if not os.path.isfile(filepath):
                        with _scan_lock:
                            _scan_status["files_scanned"] += 1
                            total = _scan_status["files_total"]
                            _scan_status["progress"] = int(_scan_status["files_scanned"] / total * 100) if total else 0
                        continue

                    ok, method = autotag.process_file(filepath)
                    if ok and method == "acoustid":
                        # Re-read tags and update DB
                        tags = _read_id3(filepath)
                        conn = db.get_db()
                        conn.execute(
                            """UPDATE library_tracks SET
                                title = ?, artist = ?, album = ?, genre = ?,
                                bpm = CASE WHEN ? > 0 THEN ? ELSE bpm END,
                                key = CASE WHEN ? != '' THEN ? ELSE key END,
                                duration_sec = ?, bitrate = ?, tag_status = 'tagged'
                            WHERE id = ?""",
                            (
                                tags["title"], tags["artist"], tags["album"], tags["genre"],
                                tags["bpm"], tags["bpm"],
                                tags["key"], tags["key"],
                                tags["duration_sec"], tags["bitrate"],
                                track_id,
                            ),
                        )
                        conn.commit()
                        conn.close()
                    else:
                        # Fallback: parse Artist - Title from filename
                        name_base = os.path.splitext(os.path.basename(filepath))[0]
                        name_clean = re.sub(r"_\d{8,}$", "", name_base)
                        name_clean = name_clean.replace("_", " ")
                        # Remove common YT suffixes
                        for suffix in ["Official Music Video", "Official Video", "Official Audio",
                                       "Official Lyric Video", "Official Visualiser", "Official Visualizer",
                                       "Lyric Video", "Lyrics", "HD", "HQ", "4K",
                                       "Music Video", "Visualizer", "Visualiser"]:
                            name_clean = re.sub(r"\s*[\(\[]?" + re.escape(suffix) + r"[\)\]]?\s*", " ", name_clean, flags=re.IGNORECASE)
                        name_clean = re.sub(r"\s+", " ", name_clean).strip()
                        fb_artist, fb_title = "", name_clean
                        for sep in (" - ", " – ", " — "):
                            if sep in name_clean:
                                parts = name_clean.split(sep, 1)
                                fb_artist = parts[0].strip()
                                fb_title = parts[1].strip()
                                break
                        conn = db.get_db()
                        if fb_artist:
                            conn.execute(
                                "UPDATE library_tracks SET artist = ?, title = ?, tag_status = 'filename' WHERE id = ?",
                                (fb_artist, fb_title, track_id),
                            )
                        else:
                            conn.execute(
                                "UPDATE library_tracks SET title = ?, tag_status = 'failed' WHERE id = ?",
                                (fb_title, track_id),
                            )
                        conn.commit()
                        conn.close()

                    with _scan_lock:
                        _scan_status["files_scanned"] += 1
                        total = _scan_status["files_total"]
                        _scan_status["progress"] = int(_scan_status["files_scanned"] / total * 100) if total else 0
    except Exception as e:
        log.error("Phase 3 autotag error: %s", e)


def generate_waveform(filepath, num_bars=512):
    """Generate waveform peaks for a single file. Returns list of floats or None."""
    import struct
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", filepath, "-ac", "1", "-ar", "8000", "-f", "s16le",
             "-acodec", "pcm_s16le", "-v", "quiet", "-"],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            return None
        raw = result.stdout
        num_samples = len(raw) // 2
        if num_samples == 0:
            return None
        bars = num_bars
        block_size = max(1, num_samples // bars)
        peaks = []
        for i in range(bars):
            start = i * block_size * 2
            end = min(start + block_size * 2, len(raw))
            chunk = raw[start:end]
            if not chunk:
                peaks.append(0)
                continue
            total = 0
            count = len(chunk) // 2
            for j in range(count):
                sample = struct.unpack_from('<h', chunk, j * 2)[0]
                total += abs(sample)
            peaks.append(total / count / 32768.0 if count > 0 else 0)
        max_val = max(peaks) if peaks else 1
        if max_val > 0:
            peaks = [p / max_val for p in peaks]
        return [round(p, 3) for p in peaks]
    except Exception:
        return None


def _generate_missing_waveforms():
    """Generate waveforms for tracks that don't have one yet. Batch of 50 per cycle."""
    import json
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, filepath FROM library_tracks WHERE waveform IS NULL AND trashed = 0 LIMIT 50"
    ).fetchall()
    conn.close()

    if not rows:
        return

    with _daemon_lock:
        _daemon_status["phase"] = "waveform"
        _daemon_status["current_subdir"] = ""

    for row in rows:
        if not _daemon_running:
            break
        if _is_client_active():
            break
        if not os.path.isfile(row["filepath"]):
            continue
        peaks = generate_waveform(row["filepath"])
        conn = db.get_db()
        conn.execute("UPDATE library_tracks SET waveform = ? WHERE id = ?",
                     (json.dumps(peaks) if peaks else "[]", row["id"]))
        conn.commit()
        conn.close()


def _deduplicate_library():
    """Find duplicate tracks by normalized Artist+Title within same stream_subdir.
    Keep the one with higher bitrate (then longer duration). Trash the rest."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, filepath, artist, title, stream_subdir, bitrate, duration_sec, size_bytes "
        "FROM library_tracks WHERE trashed = 0 AND artist != '' AND title != ''"
    ).fetchall()
    conn.close()

    groups = {}
    for r in rows:
        key = (r["stream_subdir"] or "") + "|" + _normalize_for_match(r["artist"]) + "|" + _normalize_for_match(r["title"])
        groups.setdefault(key, []).append(dict(r))

    trashed_count = 0
    for key, tracks in groups.items():
        if len(tracks) < 2:
            continue
        tracks.sort(key=lambda t: (t["bitrate"] or 0, t["duration_sec"] or 0, t["size_bytes"] or 0), reverse=True)
        keeper = tracks[0]
        for dup in tracks[1:]:
            db.trash_library_track(dup["id"])
            log.info("Dedup: trashed '%s - %s' (id=%d, %dkbps), keeping id=%d (%dkbps)",
                     dup["artist"], dup["title"], dup["id"], dup["bitrate"] or 0,
                     keeper["id"], keeper["bitrate"] or 0)
            trashed_count += 1
    return trashed_count


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
        import backup
        backup.create_backup()

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


# --- Rescan tags (BPM, Key, etc.) for existing tracks ---
_rescan_status = {
    "running": False,
    "scanned": 0,
    "total": 0,
}
_rescan_lock = threading.Lock()


def _run_rescan_tags(subdir):
    """Re-read ID3 tags AND analyze BPM/Key for all tracks in a stream_subdir."""
    global _rescan_status

    # Import bpm_analyzer for audio analysis
    try:
        import bpm_analyzer
        _has_analyzer = True
    except ImportError:
        _has_analyzer = False

    try:
        conn = db.get_db()
        rows = conn.execute(
            "SELECT id, filepath FROM library_tracks WHERE stream_subdir = ?",
            (subdir,),
        ).fetchall()
        conn.close()

        tracks = [(r["id"], r["filepath"]) for r in rows]

        with _rescan_lock:
            _rescan_status["total"] = len(tracks)
            _rescan_status["scanned"] = 0

        backend = db.get_setting("bpm_backend") or "aubio"

        for track_id, filepath in tracks:
            if not _rescan_status["running"]:
                break
            if not os.path.isfile(filepath):
                with _rescan_lock:
                    _rescan_status["scanned"] += 1
                continue

            # Phase 1: Read existing ID3 tags
            tags = _read_id3(filepath)
            bpm = tags["bpm"]
            key = tags["key"]

            # Phase 2: If BPM or Key missing, run audio analysis
            needs_analysis = (not bpm or bpm <= 0) or not key
            if _has_analyzer and needs_analysis:
                a_bpm, a_key = bpm_analyzer._analyze_track(filepath, backend)
                if not bpm or bpm <= 0:
                    bpm = a_bpm
                if not key:
                    key = a_key

            # Phase 3: Write BPM/Key to MP3 tags if we computed them
            if _has_analyzer and (bpm > 0 or key):
                bpm_analyzer._write_tags(filepath, bpm if bpm > 0 else 0, key or "")

            conn = db.get_db()
            conn.execute(
                """UPDATE library_tracks SET
                    title = CASE WHEN ? != '' THEN ? ELSE title END,
                    artist = CASE WHEN ? != '' THEN ? ELSE artist END,
                    album = ?, genre = ?, bpm = ?, key = ?, duration_sec = ?, bitrate = ?
                WHERE id = ?""",
                (
                    tags["title"], tags["title"],
                    tags["artist"], tags["artist"],
                    tags["album"], tags["genre"],
                    bpm if bpm and bpm > 0 else tags["bpm"],
                    key if key else tags["key"],
                    tags["duration_sec"], tags["bitrate"],
                    track_id,
                ),
            )
            conn.commit()
            conn.close()

            with _rescan_lock:
                _rescan_status["scanned"] += 1
    except Exception as e:
        log.error("Rescan tags error: %s", e)
    finally:
        with _rescan_lock:
            _rescan_status["running"] = False


def start_rescan_tags(subdir):
    """Start background re-scan of ID3 tags for a stream_subdir."""
    global _rescan_status
    with _rescan_lock:
        if _rescan_status["running"]:
            return False
        _rescan_status = {"running": True, "scanned": 0, "total": 0}
    t = threading.Thread(target=_run_rescan_tags, args=(subdir,), daemon=True)
    t.start()
    return True


def get_rescan_status():
    """Returns current rescan-tags status."""
    with _rescan_lock:
        return dict(_rescan_status)


def _safe_playlist_name(name):
    """Sanitize playlist name for filesystem use."""
    return "".join(c for c in name if c.isalnum() or c in " _-").strip()


def _get_playlist_dir(playlist_name):
    """Get the playlist folder path on the sync target."""
    nas_target = sync.get_sync_target()
    if not nas_target or not os.path.isdir(nas_target):
        return None
    safe_name = _safe_playlist_name(playlist_name)
    pl_dir = os.path.join(nas_target, "_playlists", safe_name)
    return pl_dir


def _sync_playlist_files(playlist_id):
    """Copy all playlist tracks into the playlist folder and remove stale files."""
    playlist = db.get_playlist(playlist_id)
    if not playlist:
        return
    tracks = db.get_playlist_tracks(playlist_id)
    pl_dir = _get_playlist_dir(playlist["name"])
    if not pl_dir:
        return

    os.makedirs(pl_dir, exist_ok=True)

    # Build set of expected filenames
    expected_files = set()
    for t in tracks:
        filepath = t.get("filepath", "")
        filename = os.path.basename(filepath)
        expected_files.add(filename)
        dest = os.path.join(pl_dir, filename)
        # Copy if not already there or source is newer
        if os.path.isfile(filepath):
            if not os.path.isfile(dest):
                try:
                    import shutil
                    shutil.copy2(filepath, dest)
                except OSError as e:
                    log.error("Failed to copy %s to playlist dir: %s", filename, e)
            else:
                # Update if source is newer
                try:
                    if os.path.getmtime(filepath) > os.path.getmtime(dest):
                        import shutil
                        shutil.copy2(filepath, dest)
                except OSError:
                    pass

    # Remove files that are no longer in the playlist
    try:
        for f in os.listdir(pl_dir):
            if f.endswith(".m3u"):
                continue
            if f not in expected_files:
                try:
                    os.remove(os.path.join(pl_dir, f))
                except OSError:
                    pass
    except OSError:
        pass


def copy_track_to_playlist(playlist_id, track_id):
    """Copy a single track into the playlist folder."""
    playlist = db.get_playlist(playlist_id)
    if not playlist:
        return
    track = db.get_library_track(track_id)
    if not track:
        return
    pl_dir = _get_playlist_dir(playlist["name"])
    if not pl_dir:
        return
    os.makedirs(pl_dir, exist_ok=True)
    filepath = track["filepath"]
    if os.path.isfile(filepath):
        dest = os.path.join(pl_dir, os.path.basename(filepath))
        if not os.path.isfile(dest):
            try:
                import shutil
                shutil.copy2(filepath, dest)
            except OSError as e:
                log.error("Failed to copy track to playlist: %s", e)


def remove_track_from_playlist_dir(playlist_id, track_id):
    """Remove a single track file from the playlist folder."""
    playlist = db.get_playlist(playlist_id)
    if not playlist:
        return
    track = db.get_library_track(track_id)
    if not track:
        return
    pl_dir = _get_playlist_dir(playlist["name"])
    if not pl_dir:
        return
    dest = os.path.join(pl_dir, os.path.basename(track["filepath"]))
    try:
        if os.path.isfile(dest):
            os.remove(dest)
    except OSError as e:
        log.error("Failed to remove track from playlist dir: %s", e)


def delete_playlist_dir(playlist_name):
    """Remove the entire playlist folder."""
    pl_dir = _get_playlist_dir(playlist_name)
    if pl_dir and os.path.isdir(pl_dir):
        try:
            import shutil
            shutil.rmtree(pl_dir)
        except OSError as e:
            log.error("Failed to delete playlist dir: %s", e)


def rename_playlist_dir(old_name, new_name):
    """Rename playlist folder when playlist is renamed."""
    old_dir = _get_playlist_dir(old_name)
    new_dir = _get_playlist_dir(new_name)
    if old_dir and new_dir and os.path.isdir(old_dir):
        try:
            os.rename(old_dir, new_dir)
        except OSError as e:
            log.error("Failed to rename playlist dir: %s", e)


def generate_m3u(playlist_id):
    """Generate an M3U playlist file in the playlist folder and sync files.
    Returns the written file path or None on error."""
    playlist = db.get_playlist(playlist_id)
    if not playlist:
        return None

    tracks = db.get_playlist_tracks(playlist_id)

    pl_dir = _get_playlist_dir(playlist["name"])
    if not pl_dir:
        return None

    os.makedirs(pl_dir, exist_ok=True)

    # Sync track files to playlist folder
    _sync_playlist_files(playlist_id)

    if not tracks:
        # Empty playlist — write empty M3U
        m3u_path = os.path.join(pl_dir, _safe_playlist_name(playlist["name"]) + ".m3u")
        try:
            with open(m3u_path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
            return m3u_path
        except OSError:
            return None

    lines = ["#EXTM3U"]
    for t in tracks:
        duration = t.get("duration_sec", 0)
        artist = t.get("artist", "")
        title = t.get("title", "") or t.get("filename", "")
        display = f"{artist} - {title}" if artist else title
        # M3U paths relative to playlist folder (just the filename)
        filename = os.path.basename(t.get("filepath", ""))
        lines.append(f"#EXTINF:{duration},{display}")
        lines.append(filename)

    safe_name = _safe_playlist_name(playlist["name"])
    m3u_path = os.path.join(pl_dir, f"{safe_name}.m3u")

    try:
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return m3u_path
    except OSError as e:
        log.error("Failed to write M3U %s: %s", m3u_path, e)
        return None


# --- Continuous background worker ---
_daemon_running = False
_daemon_status = {
    "running": False,
    "phase": "",       # "scan", "tags", "idle"
    "current_subdir": "",
    "scanned": 0,
    "total": 0,
}
_daemon_lock = threading.Lock()


def _is_client_active():
    """Check if a browser client is actively viewing the page."""
    try:
        import app as _app
        return _app.is_client_active()
    except Exception:
        return False


def _wait_for_idle():
    """Block until no active client or daemon stopped."""
    while _is_client_active() and _daemon_running:
        time.sleep(5)


def _daemon_loop():
    """Continuous background loop: scan for new files, then rescan tags per folder.
    Pauses when a browser client is actively viewing the page."""
    global _daemon_running, _daemon_status
    import time

    import bpm_analyzer

    while _daemon_running:
        # Wait for client to go idle before doing heavy work
        _wait_for_idle()
        if not _daemon_running:
            break

        # Daily automatic DB backup (max 1 per day)
        try:
            import backup
            backup.create_backup_if_needed()
        except Exception as e:
            log.debug("Daily backup check: %s", e)

        # Phase 1: Library scan (find new files)
        with _daemon_lock:
            _daemon_status["phase"] = "scan"
            _daemon_status["current_subdir"] = ""

        try:
            # Set scan status so the UI can show progress
            with _scan_lock:
                _scan_status["running"] = True
                _scan_status["files_scanned"] = 0
                _scan_status["files_total"] = 0
                _scan_status["files_updated"] = 0
                _scan_status["progress"] = 0

            scan_library()

            with _scan_lock:
                _scan_status["running"] = False
                _scan_status["progress"] = 100
        except Exception as e:
            log.error("Daemon scan error: %s", e)
            with _scan_lock:
                _scan_status["running"] = False

        if not _daemon_running:
            break

        # Phase 1.5: Fix missing ID3 tags (parse from filename, write to file)
        try:
            fixed = fix_missing_tags()
            if fixed > 0:
                log.info("Auto-fixed %d files with missing ID3 tags", fixed)
        except Exception as e:
            log.error("Fix tags error: %s", e)

        if not _daemon_running:
            break

        # Phase 1.6: MusicBrainz enrichment (fill missing album/genre/cover)
        mb_enabled = db.get_setting("musicbrainz_enrichment") != "0"  # enabled by default
        if mb_enabled:
            try:
                enriched = _musicbrainz_enrich_batch()
                if enriched > 0:
                    log.info("MusicBrainz enriched %d tracks", enriched)
            except Exception as e:
                log.error("MusicBrainz enrichment error: %s", e)

        if not _daemon_running:
            break

        # Phase 2: Rescan tags/BPM/Key for folders with incomplete tracks
        bpm_enabled = db.get_setting("bpm_analyzer_enabled") == "1"
        backend = db.get_setting("bpm_backend") or "aubio"

        conn = db.get_db()
        subdirs_with_missing = conn.execute(
            """SELECT DISTINCT stream_subdir FROM library_tracks
               WHERE (bpm = 0 OR bpm IS NULL) OR (key = '' OR key IS NULL)"""
        ).fetchall()
        conn.close()
        subdirs = [r["stream_subdir"] for r in subdirs_with_missing]

        for subdir in subdirs:
            if not _daemon_running:
                break

            with _daemon_lock:
                _daemon_status["phase"] = "tags"
                _daemon_status["current_subdir"] = subdir

            conn = db.get_db()
            rows = conn.execute(
                """SELECT id, filepath FROM library_tracks
                   WHERE stream_subdir = ? AND (bpm = 0 OR bpm IS NULL OR bpm = -1 OR key = '' OR key IS NULL)""",
                (subdir,),
            ).fetchall()
            conn.close()

            tracks = [(r["id"], r["filepath"]) for r in rows]

            with _rescan_lock:
                _rescan_status["running"] = True
                _rescan_status["total"] = len(tracks)
                _rescan_status["scanned"] = 0

            for track_id, filepath in tracks:
                if not _daemon_running:
                    break
                if not os.path.isfile(filepath):
                    with _rescan_lock:
                        _rescan_status["scanned"] += 1
                    continue

                tags = _read_id3(filepath)
                bpm = tags["bpm"]
                key = tags["key"]

                # BPM/Key analysis via subprocess (only if enabled)
                if bpm_enabled and ((not bpm or bpm <= 0) or not key):
                    a_bpm, a_key = bpm_analyzer._analyze_track(filepath, backend)
                    if not bpm or bpm <= 0:
                        bpm = a_bpm
                    if not key:
                        key = a_key

                    # Write back to MP3 tags
                    if bpm > 0 or key:
                        bpm_analyzer._write_tags(filepath, bpm if bpm > 0 else 0, key or "")

                conn = db.get_db()
                conn.execute(
                    """UPDATE library_tracks SET
                        title = CASE WHEN ? != '' THEN ? ELSE title END,
                        artist = CASE WHEN ? != '' THEN ? ELSE artist END,
                        album = ?, genre = ?, bpm = ?, key = ?, duration_sec = ?, bitrate = ?
                    WHERE id = ?""",
                    (
                        tags["title"], tags["title"],
                        tags["artist"], tags["artist"],
                        tags["album"], tags["genre"],
                        bpm if bpm and bpm > 0 else (tags["bpm"] if tags["bpm"] else -1),
                        key if key else tags["key"],
                        tags["duration_sec"], tags["bitrate"],
                        track_id,
                    ),
                )
                conn.commit()
                conn.close()

                with _rescan_lock:
                    _rescan_status["scanned"] += 1

                # Small pause to not overload the system
                time.sleep(0.5)

            with _rescan_lock:
                _rescan_status["running"] = False

        # Phase 3: Sync playlist folders with DB
        if not _daemon_running:
            break
        with _daemon_lock:
            _daemon_status["phase"] = "playlist-sync"
            _daemon_status["current_subdir"] = ""
        try:
            playlists = db.get_all_playlists()
            for pl in playlists:
                if not _daemon_running:
                    break
                with _daemon_lock:
                    _daemon_status["current_subdir"] = pl["name"]
                _sync_playlist_files(pl["id"])
                generate_m3u(pl["id"])
        except Exception as e:
            log.error("Playlist sync error: %s", e)

        # Phase 4: Generate missing waveforms
        if not _daemon_running:
            break
        _wait_for_idle()
        try:
            _generate_missing_waveforms()
        except Exception as e:
            log.error("Waveform generation error: %s", e)

        # Phase 5: Idle — wait before next cycle
        with _daemon_lock:
            _daemon_status["phase"] = "idle"
            _daemon_status["current_subdir"] = ""

        # Sleep 5 minutes, check every second if we should stop
        for _ in range(300):
            if not _daemon_running:
                return
            time.sleep(1)

    with _daemon_lock:
        _daemon_status["running"] = False
        _daemon_status["phase"] = ""


def start_daemon():
    """Start the continuous background library worker."""
    global _daemon_running, _daemon_status
    with _daemon_lock:
        if _daemon_status["running"]:
            return
        _daemon_running = True
        _daemon_status["running"] = True
        _daemon_status["phase"] = "starting"
    t = threading.Thread(target=_daemon_loop, daemon=True)
    t.start()


def stop_daemon():
    """Stop the background library worker (and loudness thread)."""
    global _daemon_running, _loudness_running
    _daemon_running = False
    _loudness_running = False
    with _daemon_lock:
        _daemon_status["running"] = False


def get_daemon_status():
    """Return daemon status (includes loudness info)."""
    with _daemon_lock:
        status = dict(_daemon_status)
    with _loudness_lock:
        status["loudness"] = dict(_loudness_status)
    return status


# --- Independent loudness normalization thread ---
_loudness_running = False
_loudness_status = {
    "running": False,
    "processed": 0,
    "total": 0,
    "current_file": "",
}
_loudness_lock = threading.Lock()


def _loudness_loop():
    """Continuous background loop: normalize tracks to -14 LUFS.
    Runs independently from the main daemon, always active."""
    global _loudness_running

    # Wait 30s after startup so the main scan can register files first
    for _ in range(30):
        if not _loudness_running:
            return
        time.sleep(1)

    while _loudness_running:
        try:
            conn = db.get_db()
            rows = conn.execute(
                """SELECT id, filepath, filename FROM library_tracks
                   WHERE trashed = 0 AND loudness_lufs IS NULL
                   LIMIT 100"""
            ).fetchall()
            conn.close()
        except Exception as e:
            log.error("Loudness query error: %s", e)
            time.sleep(60)
            continue

        if not rows:
            # Nothing to do — sleep 5 minutes, then check again
            for _ in range(300):
                if not _loudness_running:
                    return
                time.sleep(1)
            continue

        with _loudness_lock:
            _loudness_status["running"] = True
            _loudness_status["total"] = len(rows)
            _loudness_status["processed"] = 0

        for row in rows:
            if not _loudness_running:
                break

            # Pause while stream test or active client
            while _is_client_active() and _loudness_running:
                time.sleep(20)

            track_id = row["id"]
            filepath = row["filepath"]

            with _loudness_lock:
                _loudness_status["current_file"] = row["filename"]

            if not os.path.isfile(filepath):
                conn = db.get_db()
                conn.execute(
                    "UPDATE library_tracks SET loudness_lufs = 0 WHERE id = ?",
                    (track_id,),
                )
                conn.commit()
                conn.close()
                with _loudness_lock:
                    _loudness_status["processed"] += 1
                continue

            measured = _normalize_loudness(filepath)
            if measured is not None:
                new_mtime = os.stat(filepath).st_mtime
                conn = db.get_db()
                conn.execute(
                    "UPDATE library_tracks SET loudness_lufs = ?, mtime = ? WHERE id = ?",
                    (measured, new_mtime, track_id),
                )
                conn.commit()
                conn.close()
            else:
                conn = db.get_db()
                conn.execute(
                    "UPDATE library_tracks SET loudness_lufs = 0 WHERE id = ?",
                    (track_id,),
                )
                conn.commit()
                conn.close()

            with _loudness_lock:
                _loudness_status["processed"] += 1

            # Pause between tracks
            time.sleep(1)

        with _loudness_lock:
            _loudness_status["running"] = False
            _loudness_status["current_file"] = ""

        log.info("Loudness normalization batch done: %d tracks", len(rows))

    with _loudness_lock:
        _loudness_status["running"] = False


def start_loudness_daemon():
    """Start the independent loudness normalization thread."""
    global _loudness_running
    with _loudness_lock:
        if _loudness_status["running"]:
            return
        _loudness_running = True
        _loudness_status["running"] = True
    t = threading.Thread(target=_loudness_loop, daemon=True)
    t.start()
    log.info("Loudness normalization thread started (-14 LUFS target)")


def get_loudness_status():
    """Return loudness normalization status."""
    with _loudness_lock:
        return dict(_loudness_status)


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
