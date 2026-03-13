"""
YouTube-based song downloader for streams.

Instead of recording and splitting the stream audio, this mode:
  1. Listens to the stream for ICY metadata only
  2. On each new song: searches YouTube and downloads via yt-dlp
  3. SQLite DB prevents duplicate downloads

Songs arrive as complete, high-quality MP3s with cover art from YouTube.
"""

import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from config import USER_AGENTS, DEFAULT_USER_AGENT, RECORDING_BASE, MIN_BITRATE
from db import log_event
from sync import sync_file

# Local data directory for YouTube song DBs (not on NAS/SMB)
# Use project root's data/ directory (parent of modules/)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YT_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
os.makedirs(YT_DATA_DIR, exist_ok=True)

# yt-dlp binary — use full path since systemd may not have ~/.local/bin in PATH
_yt_dlp_candidates = [
    os.path.expanduser("~/.local/bin/yt-dlp"),
    "/usr/local/bin/yt-dlp",
    "/usr/bin/yt-dlp",
    "yt-dlp",
]
YT_DLP = next((p for p in _yt_dlp_candidates if os.path.isfile(p)), "yt-dlp")

# Browser user-agent for yt-dlp requests
YT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def _extract_stream_title(meta):
    """Extract StreamTitle from ICY metadata, handling apostrophes in titles."""
    idx = meta.find("StreamTitle='")
    if idx < 0:
        return None
    start = idx + len("StreamTitle='")
    end = meta.find("';", start)
    if end < 0:
        end = meta.rfind("'", start)
    if end <= start:
        return None
    return meta[start:end].strip() or None


def _sanitize_filename(name):
    s = re.sub(r'[<>:"/\\|?*\'\u2019]', '_', name).strip()
    s = re.sub(r'[_\s]+', ' ', s)
    return s.strip('. ')[:200] or "unknown"


def _title_case(s):
    """Convert ALL CAPS to Title Case."""
    if not s.isupper():
        return s
    keep_upper = {"DJ", "MC", "VS", "VS.", "II", "III", "IV", "EP", "LP", "OK"}
    words = []
    for word in s.split():
        if word.upper() in keep_upper:
            words.append(word.upper())
        elif word.startswith("(") or word.startswith("["):
            words.append(word[0] + word[1:].capitalize())
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _parse_icy_title(title):
    """Parse 'ARTIST - TITLE' into (artist, title)."""
    parts = title.split(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", title.strip()


def _get_file_bitrate(filepath):
    """Get audio bitrate of a file in kbps using ffprobe. Returns None on error."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
             "-show_entries", "stream=bit_rate", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            bps = int(result.stdout.strip())
            return bps // 1000  # Convert to kbps
    except Exception:
        pass
    return None


class YouTubeSongDB:
    """Tracks downloaded songs to avoid duplicates."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._init()

    def _init(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS yt_songs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist TEXT NOT NULL,
                    title TEXT NOT NULL,
                    icy_raw TEXT,
                    filename TEXT,
                    filepath TEXT,
                    status TEXT DEFAULT 'downloaded',
                    downloaded_at TEXT,
                    play_count INTEGER DEFAULT 1,
                    last_seen TEXT,
                    UNIQUE(artist, title)
                )
            """)

    def is_known(self, artist, title):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM yt_songs WHERE artist = ? AND title = ?",
                (artist.lower().strip(), title.lower().strip()),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE yt_songs SET play_count = play_count + 1, last_seen = datetime('now') WHERE id = ?",
                    (row[0],),
                )
                return True
            return False

    def add_song(self, artist, title, icy_raw, filename="", filepath="", status="downloaded"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO yt_songs
                   (artist, title, icy_raw, filename, filepath, status, downloaded_at, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                (artist.lower().strip(), title.lower().strip(), icy_raw,
                 filename, filepath, status),
            )

    def stats(self):
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM yt_songs").fetchone()[0]
            downloaded = conn.execute(
                "SELECT COUNT(*) FROM yt_songs WHERE status = 'downloaded' AND filename != ''"
            ).fetchone()[0]
            not_found = conn.execute(
                "SELECT COUNT(*) FROM yt_songs WHERE status = 'not_found'"
            ).fetchone()[0]
            total_plays = conn.execute(
                "SELECT COALESCE(SUM(play_count), 0) FROM yt_songs"
            ).fetchone()[0]
            rec_pct = round(downloaded / total_plays * 100) if total_plays > 0 else 0
            return {"total": total, "downloaded": downloaded, "not_found": not_found,
                    "total_plays": total_plays, "rec_pct": rec_pct}

    def cleanup_missing(self, dest_dir, nas_dir=None):
        """Remove DB entries where file no longer exists on disk or NAS.
        Returns number of entries removed."""
        removed = 0
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, filename, filepath FROM yt_songs WHERE status = 'downloaded' AND filename != ''"
            ).fetchall()
            for row_id, filename, filepath in rows:
                exists = False
                # Check original filepath
                if filepath and os.path.exists(filepath):
                    exists = True
                # Check dest dir
                if not exists and filename and os.path.exists(os.path.join(dest_dir, filename)):
                    exists = True
                # Check NAS dir
                if not exists and nas_dir and filename and os.path.exists(os.path.join(nas_dir, filename)):
                    exists = True
                if not exists:
                    conn.execute("DELETE FROM yt_songs WHERE id = ?", (row_id,))
                    removed += 1
        return removed


class YouTubeRecorder:
    """Listens to stream ICY metadata and downloads songs from YouTube.

    Compatible interface with FfmpegRecorder (poll, stop, get_current_track, pid).
    """

    def __init__(self, stream, dest):
        self.stream = stream
        self.stream_id = stream["id"]
        self.dest = dest
        self.stream_url = stream["url"]
        ua_key = stream["user_agent"] if "user_agent" in stream.keys() else DEFAULT_USER_AGENT
        self.ua = USER_AGENTS.get(ua_key, USER_AGENTS[DEFAULT_USER_AGENT])

        # DB lokal speichern (nicht auf NAS/SMB wo SQLite Probleme macht)
        db_name = f"yt_songs_{stream['dest_subdir']}.db"
        self._song_db = YouTubeSongDB(os.path.join(YT_DATA_DIR, db_name))
        self._current_track = None
        self._state = "waiting"  # "waiting", "recording", "skipping"
        self._stop_event = threading.Event()
        self._thread = None
        self._download_thread = None
        self.pid = os.getpid()  # No subprocess, use own PID
        self.start_time = time.time()
        self.returncode = None
        self._stats_cache = {"total": 0, "downloaded": 0, "not_found": 0}

    def start(self):
        os.makedirs(self.dest, exist_ok=True)
        self._stats_cache = self._song_db.stats()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        log_event(self.stream_id, "start",
                  f"YouTube-Modus gestartet (DB: {self._stats_cache['downloaded']} Songs)")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.returncode = 0

    def poll(self):
        if self._stop_event.is_set():
            return 0
        if self._thread and not self._thread.is_alive():
            return -1
        return None

    def get_current_track(self):
        return self._current_track

    def get_state(self):
        return self._state

    def get_stats(self):
        return self._stats_cache

    def _listen_loop(self):
        while not self._stop_event.is_set():
            try:
                self._connect_and_listen()
            except Exception as e:
                if not self._stop_event.is_set():
                    log_event(self.stream_id, "error", f"Stream-Fehler: {str(e)[:100]}")
            if not self._stop_event.is_set():
                self._stop_event.wait(10)

    def _connect_and_listen(self):
        req = urllib.request.Request(
            self.stream_url,
            headers={"User-Agent": self.ua, "Icy-MetaData": "1"},
        )
        resp = urllib.request.urlopen(req, timeout=15)

        metaint_str = resp.headers.get("icy-metaint")
        if not metaint_str:
            resp.close()
            log_event(self.stream_id, "error", "Stream liefert keine ICY-Metadaten")
            self._stop_event.wait(30)
            return

        metaint = int(metaint_str)

        log_event(self.stream_id, "start", f"ICY-Stream verbunden (metaint={metaint})")

        current_icy = None

        while not self._stop_event.is_set():
            # Read and discard audio data
            audio = resp.read(metaint)
            if not audio:
                break

            meta_len_byte = resp.read(1)
            if not meta_len_byte:
                break

            length = meta_len_byte[0] * 16
            if length > 0:
                meta_raw = resp.read(length)
                if not meta_raw:
                    break
                meta = meta_raw.decode("utf-8", errors="replace").rstrip("\x00")
                title = _extract_stream_title(meta)
                if title and title != current_icy:
                        current_icy = title
                        self._handle_new_title(title)

        resp.close()

    def _handle_new_title(self, icy_title):
        # Skip station idents + user-configured skip words
        lower = icy_title.lower()
        skip_words = ["jingle", "werbung", "commercial"]
        user_skip = self.stream.get("skip_words", "") if hasattr(self.stream, "get") else (self.stream["skip_words"] if "skip_words" in self.stream.keys() else "")
        if user_skip:
            skip_words += [w.strip().lower() for w in user_skip.split(";") if w.strip()]
        if any(w in lower for w in skip_words):
            self._current_track = icy_title
            self._state = "skipping"
            log_event(self.stream_id, "track",
                      f"Übersprungen (Skip-Wort): {icy_title}")
            return

        artist_raw, title_raw = _parse_icy_title(icy_title)
        if not artist_raw or len(artist_raw) < 2 or len(title_raw) < 2:
            return

        artist = _title_case(artist_raw)
        title = _title_case(title_raw)
        self._current_track = f"{artist} - {title}"

        # Already known?
        if self._song_db.is_known(artist_raw, title_raw):
            self._state = "skipping"
            log_event(self.stream_id, "track",
                      f"Bekannt: {artist} - {title}")
            return

        # Download in background thread (don't block metadata reading)
        self._state = "recording"
        record_mode = self.stream.get("record_mode", "youtube") if hasattr(self.stream, "get") else "youtube"
        source_label = {"youtube": "YouTube", "soundcloud": "SoundCloud"}.get(record_mode, "YouTube")
        log_event(self.stream_id, "track",
                  f"Neu: {artist} - {title} -> {source_label}-Download")

        # Only one download at a time
        if self._download_thread and self._download_thread.is_alive():
            # Queue would be better, but for simplicity just skip
            log_event(self.stream_id, "track",
                      f"Download laeuft noch, ueberspringe: {artist} - {title}")
            # No DB entry — will retry next time the song plays
            return

        self._download_thread = threading.Thread(
            target=self._download_song,
            args=(artist, title, artist_raw, title_raw, icy_title),
            daemon=True,
        )
        self._download_thread.start()

    def _download_song(self, artist, title, artist_raw, title_raw, icy_title):
        safe_artist = re.sub(r'[<>:"/\\|?*]', '_', artist).strip('._')[:80]
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title).strip('._')[:80]
        safe_artist = re.sub(r'[_\s]+', '_', safe_artist)
        safe_title = re.sub(r'[_\s]+', '_', safe_title)
        output_template = os.path.join(self.dest, f"{safe_artist} - {safe_title}.%(ext)s")

        # Determine sources based on record_mode + dl_fallback
        record_mode = self.stream.get("record_mode", "youtube") if hasattr(self.stream, "get") else "youtube"
        dl_fallback = self.stream.get("dl_fallback", 0) if hasattr(self.stream, "get") else 0
        if record_mode == "soundcloud":
            sources = [("scsearch1", "SoundCloud")]
            if dl_fallback:
                sources.append(("ytsearch1", "YouTube"))
        else:
            sources = [("ytsearch1", "YouTube")]
            if dl_fallback:
                sources.append(("scsearch1", "SoundCloud"))

        # deno path for yt-dlp
        env = os.environ.copy()
        deno_path = os.path.expanduser("~/.deno/bin")
        if os.path.isdir(deno_path):
            env["PATH"] = f"{deno_path}:{env.get('PATH', '')}"

        for search_prefix, source_name in sources:
            if self._stop_event.is_set():
                return

            queries = [
                f"{artist} - {title}",
                f"{artist} - {title} audio",
                f"{artist} {title} official audio",
            ]

            found = self._try_source(
                queries, search_prefix, source_name, output_template, env,
                artist_raw, title_raw, icy_title, artist, title,
            )
            if found:
                return

        # All sources failed — no DB entry, will retry next time the song plays
        log_event(self.stream_id, "download_fail",
                  f"Nicht gefunden (kein DB-Eintrag): {artist} - {title}")

    def _try_source(self, queries, search_prefix, source_name, output_template,
                    env, artist_raw, title_raw, icy_title, artist, title):
        """Try downloading from a single source. Returns True if successful."""
        is_soundcloud = search_prefix == "scsearch1"

        for i, query in enumerate(queries):
            if self._stop_event.is_set():
                return False

            try:
                cmd = [
                    YT_DLP,
                    f"{search_prefix}:{query}",
                    "--extract-audio",
                    "--audio-format", "mp3",
                    "--audio-quality", "0",
                    "--format", "bestaudio",
                    "--postprocessor-args", "ffmpeg:-b:a 320k",
                    "--no-playlist",
                    "--max-downloads", "1",
                    "--output", output_template,
                    "--print", "after_move:filepath",
                    "--no-overwrites",
                    "--restrict-filenames",
                    "--match-filter", "duration < 600",
                ]
                if not is_soundcloud:
                    cmd += ["--embed-thumbnail"]
                    cmd += ["--user-agent", YT_UA]
                    cmd += ["--remote-components", "ejs:github"]

                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120, env=env,
                )
            except subprocess.TimeoutExpired:
                log_event(self.stream_id, "yt_error", f"Timeout ({source_name}): {query}")
                continue
            except Exception as e:
                log_event(self.stream_id, "yt_error", f"Exception ({source_name}): {str(e)[:150]}")
                continue

            if result.returncode not in (0, 101):
                stderr = result.stderr[:300]
                log_event(self.stream_id, "yt_error",
                          f"RC {result.returncode} ({source_name} {i+1}/3): {stderr[:150]}")
                if "Sign in to confirm" in stderr or "age" in stderr.lower():
                    continue
                if i < len(queries) - 1:
                    continue
                break

            # Parse filepath from output — only accept MP3
            filepath = None
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and line.endswith(".mp3"):
                    filepath = line
                    break

            # Clean up any non-mp3 files that yt-dlp may have created
            if not filepath:
                try:
                    for ext in ("*.m4a", "*.webm", "*.mp4", "*.ogg", "*.opus", "*.wav"):
                        for f in Path(self.dest).glob(ext):
                            if time.time() - f.stat().st_mtime < 60:
                                f.unlink()
                except Exception:
                    pass

            if filepath and os.path.exists(filepath):
                # Check bitrate — reject files below minimum
                file_br = _get_file_bitrate(filepath)
                if file_br is not None and file_br < MIN_BITRATE:
                    log_event(self.stream_id, "download_fail",
                              f"Bitrate zu niedrig ({file_br} kbps < {MIN_BITRATE} kbps), "
                              f"gelöscht: {os.path.basename(filepath)}")
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                    return False

                self._song_db.add_song(
                    artist_raw, title_raw, icy_title,
                    filename=os.path.basename(filepath),
                    filepath=filepath,
                    status="downloaded",
                )
                self._stats_cache = self._song_db.stats()
                log_event(self.stream_id, "download",
                          f"Download OK ({source_name}): {os.path.basename(filepath)} "
                          f"(DB: {self._stats_cache['downloaded']} Songs)")
                sync_file(filepath, self.stream)
                return True

            # Fallback: check for recently created mp3
            try:
                recent = sorted(
                    Path(self.dest).glob("*.mp3"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:3]
                now = time.time()
                for f in recent:
                    if now - f.stat().st_mtime < 60:
                        file_br = _get_file_bitrate(str(f))
                        if file_br is not None and file_br < MIN_BITRATE:
                            log_event(self.stream_id, "download_fail",
                                      f"Bitrate zu niedrig ({file_br} kbps < {MIN_BITRATE} kbps), "
                                      f"gelöscht: {f.name}")
                            try:
                                f.unlink()
                            except OSError:
                                pass
                            return False

                        self._song_db.add_song(
                            artist_raw, title_raw, icy_title,
                            filename=f.name, filepath=str(f),
                            status="downloaded",
                        )
                        self._stats_cache = self._song_db.stats()
                        log_event(self.stream_id, "download",
                                  f"Download OK ({source_name}): {f.name}")
                        sync_file(str(f), self.stream)
                        return True
            except Exception:
                pass

        return False


def cleanup_youtube_db(stream, dest, nas_dest):
    """Cleanup missing files from YouTube song DB. Called by scheduler."""
    db_name = f"yt_songs_{stream['dest_subdir']}.db"
    db_path = os.path.join(YT_DATA_DIR, db_name)
    if os.path.exists(db_path):
        song_db = YouTubeSongDB(db_path)
        removed = song_db.cleanup_missing(dest, nas_dest)
        if removed:
            from db import log_event as _log
            _log(stream["id"], "cleanup",
                 f"DB bereinigt: {removed} fehlende Dateien entfernt")


# --- Module registration ---

_YT_ICON = (
    '<svg title="YouTube-Download" width="18" height="13" viewBox="0 0 24 17" style="vertical-align:middle;">'
    '<path d="M23.5 2.5A3 3 0 0 0 21.4.4C19.5 0 12 0 12 0S4.5 0 2.6.4A3 3 0 0 0 .5 2.5 31.5 31.5 0 0 0 0 8.5a31.5 31.5 0 0 0 .5 6A3 3 0 0 0 2.6 16.6C4.5 17 12 17 12 17s7.5 0 9.4-.4a3 3 0 0 0 2.1-2.1 31.5 31.5 0 0 0 .5-6 31.5 31.5 0 0 0-.5-6z" fill="#FF0000"/>'
    '<path d="M9.6 12.2l6.3-3.7-6.3-3.7z" fill="#fff"/></svg>'
)
_SC_ICON = (
    '<svg title="SoundCloud-Download" width="20" height="20" viewBox="0 0 24 24" style="vertical-align:middle;">'
    '<path d="M1 18.5v-5a.5.5 0 0 1 1 0v5a.5.5 0 0 1-1 0zm3 1v-7a.5.5 0 0 1 1 0v7a.5.5 0 0 1-1 0z'
    'm3-.5V12a.5.5 0 0 1 1 0v7a.5.5 0 0 1-1 0zm3 .5V10a.5.5 0 0 1 1 0v9.5a.5.5 0 0 1-1 0z" fill="#FF5500"/>'
    '<path d="M13 9c0-3.3 2.7-6 6-6 2.5 0 4.6 1.5 5.5 3.7.3.1.5.3.5.6V19c0 .6-.4 1-1 1h-11V9z" fill="#FF5500"/></svg>'
)

MODULE_INFO = {
    "name": "youtube",
    "label": "YouTube / SoundCloud Download",
    "description": "ICY-Metadaten als Trigger, Songs von YouTube/SoundCloud laden (yt-dlp erforderlich)",
    "record_modes": ["youtube", "soundcloud"],
    "recorder_class": YouTubeRecorder,
    "icons": {
        "youtube": _YT_ICON,
        "soundcloud": _SC_ICON,
    },
    "form_options": [
        {"value": "youtube", "label": "YouTube-Download (ICY-Metadaten als Trigger)"},
        {"value": "soundcloud", "label": "SoundCloud-Download (ICY-Metadaten als Trigger)"},
    ],
    "form_hints": {
        "youtube": "Songs werden von YouTube geladen (yt-dlp).",
        "soundcloud": "Songs werden von SoundCloud geladen (yt-dlp).",
    },
    "hide_fields": ["offset-group", "trim-group"],
    "has_fallback": True,
    "cleanup_fn": cleanup_youtube_db,
}
