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

from config import USER_AGENTS, DEFAULT_USER_AGENT, RECORDING_BASE
from db import log_event

# Local data directory for YouTube song DBs (not on NAS/SMB)
YT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
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


def _sanitize_filename(name):
    s = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
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
            return {"total": total, "downloaded": downloaded, "not_found": not_found}


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
                m = re.search(r"StreamTitle='([^']*)'", meta)
                if m:
                    title = m.group(1).strip()
                    if title and title != current_icy:
                        current_icy = title
                        self._handle_new_title(title)

        resp.close()

    def _handle_new_title(self, icy_title):
        # Skip station idents
        lower = icy_title.lower()
        skip_words = ["bigfm", "big fm", "jingle", "werbung", "commercial"]
        if any(w in lower for w in skip_words):
            return

        artist_raw, title_raw = _parse_icy_title(icy_title)
        if not artist_raw or len(artist_raw) < 2 or len(title_raw) < 2:
            return

        artist = _title_case(artist_raw)
        title = _title_case(title_raw)
        self._current_track = f"{artist} - {title}"

        # Already known?
        if self._song_db.is_known(artist_raw, title_raw):
            log_event(self.stream_id, "track",
                      f"Bekannt: {artist} - {title}")
            return

        # Download in background thread (don't block metadata reading)
        log_event(self.stream_id, "track",
                  f"Neu: {artist} - {title} -> YouTube-Download")

        # Only one download at a time
        if self._download_thread and self._download_thread.is_alive():
            # Queue would be better, but for simplicity just skip
            log_event(self.stream_id, "track",
                      f"Download laeuft noch, ueberspringe: {artist} - {title}")
            self._song_db.add_song(artist_raw, title_raw, icy_title, status="skipped")
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

        queries = [
            f"{artist} - {title}",
            f"{artist} - {title} audio",
            f"{artist} {title} official audio",
        ]

        # deno path for yt-dlp
        env = os.environ.copy()
        deno_path = os.path.expanduser("~/.deno/bin")
        if os.path.isdir(deno_path):
            env["PATH"] = f"{deno_path}:{env.get('PATH', '')}"

        for i, query in enumerate(queries):
            if self._stop_event.is_set():
                return

            try:
                cmd = [
                    YT_DLP,
                    f"ytsearch1:{query}",
                    "--extract-audio",
                    "--audio-format", "mp3",
                    "--audio-quality", "0",
                    "--embed-thumbnail",
                    "--no-playlist",
                    "--max-downloads", "1",
                    "--output", output_template,
                    "--print", "after_move:filepath",
                    "--no-overwrites",
                    "--restrict-filenames",
                    "--user-agent", YT_UA,
                    "--remote-components", "ejs:github",
                    "--match-filter", "duration < 600",
                ]

                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120, env=env,
                )
            except subprocess.TimeoutExpired:
                log_event(self.stream_id, "yt_error", f"Timeout: {query}")
                continue
            except Exception as e:
                log_event(self.stream_id, "yt_error", f"Exception: {str(e)[:150]}")
                continue

            if result.returncode not in (0, 101):
                stderr = result.stderr[:300]
                log_event(self.stream_id, "yt_error",
                          f"RC {result.returncode} ({i+1}/3): {stderr[:150]}")
                if "Sign in to confirm" in stderr or "age" in stderr.lower():
                    continue
                if i < len(queries) - 1:
                    continue
                break

            # Parse filepath from output
            filepath = None
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and (line.endswith(".mp3") or line.endswith(".m4a")):
                    filepath = line
                    break

            if filepath and os.path.exists(filepath):
                self._song_db.add_song(
                    artist_raw, title_raw, icy_title,
                    filename=os.path.basename(filepath),
                    filepath=filepath,
                    status="downloaded",
                )
                self._stats_cache = self._song_db.stats()
                log_event(self.stream_id, "download",
                          f"Download OK: {os.path.basename(filepath)} "
                          f"(DB: {self._stats_cache['downloaded']} Songs)")
                return

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
                        self._song_db.add_song(
                            artist_raw, title_raw, icy_title,
                            filename=f.name, filepath=str(f),
                            status="downloaded",
                        )
                        self._stats_cache = self._song_db.stats()
                        log_event(self.stream_id, "download",
                                  f"Download OK: {f.name}")
                        return
            except Exception:
                pass

        # All queries failed
        self._song_db.add_song(artist_raw, title_raw, icy_title, status="not_found")
        self._stats_cache = self._song_db.stats()
        log_event(self.stream_id, "download_fail",
                  f"Nicht gefunden: {artist} - {title}")
