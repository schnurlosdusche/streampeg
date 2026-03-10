"""
FFmpeg-based stream recorder with metadata-driven track splitting.

Supports two metadata sources:
  1. Shoutcast v2 API (/currentsong endpoint)
  2. Inline ICY metadata (parsed from the stream directly, single-connection)

Used when streamripper cannot handle a stream (e.g. HTTPS redirects)
but track metadata is still available.
"""

import collections
import os
import re
import subprocess
import signal
import threading
import time
import urllib.request
import urllib.parse
from config import METADATA_POLL_INTERVAL, USER_AGENTS, DEFAULT_USER_AGENT
from db import log_event
from sync import sync_file


def _sanitize_filename(name):
    """Create a safe filename from a track title."""
    s = re.sub(r'[<>:"/\\|?*]', '', name).strip()
    s = re.sub(r'\s+', ' ', s)
    return s[:200] or "unknown"


def _detect_metadata_url(stream_url):
    """Derive the Shoutcast v2 /currentsong URL from a stream URL."""
    parsed = urllib.parse.urlparse(stream_url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base += f":{parsed.port}"
    return f"{base}/currentsong?sid=1"


def _fetch_current_song(metadata_url, ua):
    """Fetch the current song title from the Shoutcast API."""
    try:
        req = urllib.request.Request(
            metadata_url,
            headers={"User-Agent": ua},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            title = resp.read().decode("utf-8", errors="replace").strip()
            return title if title else None
    except Exception:
        return None


class IcyStreamSplitter:
    """Single-connection ICY stream reader that pipes audio to ffmpeg.

    Reads the raw ICY stream, strips metadata, pipes pure audio to ffmpeg stdin.
    On track change: clean split — stop old ffmpeg, start new one for new track.
    """

    def __init__(self, stream_url, ua, dest, stream_id, split_offset=0, stream=None):
        self.stream_url = stream_url
        self.ua = ua
        self.dest = dest
        self.stream_id = stream_id
        self.stream = stream
        self.split_offset = split_offset  # positive = metadata early (delay split), negative = metadata late (pre-buffer)
        self._current_track = None
        self._current_file = None
        self._ffmpeg_proc = None
        self._pending_split = None  # {"title": ..., "deadline": ...}
        self._prebuffer = collections.deque()  # (timestamp, audio_chunk) for negative split_offset

        self._resp = None
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.pid = None
        self.start_time = time.time()

    def start(self):
        os.makedirs(self.dest, exist_ok=True)
        os.makedirs(os.path.join(self.dest, "incomplete"), exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._resp:
            try:
                self._resp.close()
            except Exception:
                pass
        with self._lock:
            self._stop_current_ffmpeg()
            self._finalize_track()
        if self._thread:
            self._thread.join(timeout=10)

    def poll(self):
        if self._ffmpeg_proc:
            return self._ffmpeg_proc.poll()
        if self._stop_event.is_set():
            return -1
        return None

    @property
    def returncode(self):
        if self._ffmpeg_proc:
            return self._ffmpeg_proc.returncode
        return None

    def get_current_track(self):
        with self._lock:
            return self._current_track

    def _run(self):
        """Main loop: connect, read stream, split on metadata."""
        while not self._stop_event.is_set():
            try:
                self._connect_and_stream()
            except Exception as e:
                if not self._stop_event.is_set():
                    log_event(self.stream_id, "error", f"ICY-Verbindung unterbrochen: {str(e)[:100]}")
            if not self._stop_event.is_set():
                self._stop_event.wait(3)

    def _connect_and_stream(self):
        req = urllib.request.Request(
            self.stream_url,
            headers={"User-Agent": self.ua, "Icy-MetaData": "1"},
        )
        self._resp = urllib.request.urlopen(req, timeout=15)

        content_type = self._resp.headers.get("Content-Type", "")
        metaint_str = self._resp.headers.get("icy-metaint")
        if not metaint_str:
            self._resp.close()
            log_event(self.stream_id, "error", "Stream liefert keine ICY-Metadaten")
            self._stop_event.wait(30)
            return

        metaint = int(metaint_str)

        # Detect input format for ffmpeg
        if "aac" in content_type or "aacp" in content_type:
            input_fmt = "aac"
        elif "ogg" in content_type:
            input_fmt = "ogg"
        else:
            input_fmt = "mp3"

        # Start initial ffmpeg (no track title yet, will rename on first metadata)
        self._start_ffmpeg_pipe(input_fmt)
        log_event(self.stream_id, "start",
                  f"ICY-Stream verbunden (metaint={metaint}, fmt={input_fmt}, PID {self.pid})")

        while not self._stop_event.is_set():
            # Execute pending delayed split if deadline reached (positive split_offset)
            if self._pending_split and time.time() >= self._pending_split["deadline"]:
                self._do_split(self._pending_split["title"], input_fmt)
                self._pending_split = None

            # Read audio chunk
            audio = self._resp.read(metaint)
            if not audio:
                break

            now = time.time()

            if self.split_offset < 0:
                # Negative offset: metadata arrives late, buffer audio and write delayed
                self._prebuffer.append((now, audio))
                cutoff = now + self.split_offset  # split_offset is negative
                # Write only chunks older than the buffer window to current file
                with self._lock:
                    while self._prebuffer and self._prebuffer[0][0] < cutoff:
                        old_ts, old_chunk = self._prebuffer.popleft()
                        if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
                            try:
                                self._ffmpeg_proc.stdin.write(old_chunk)
                            except (OSError, BrokenPipeError):
                                break
            else:
                # No negative offset: write audio immediately
                with self._lock:
                    if self._ffmpeg_proc and self._ffmpeg_proc.poll() is None:
                        try:
                            self._ffmpeg_proc.stdin.write(audio)
                        except (OSError, BrokenPipeError):
                            break

            # Read metadata length byte
            meta_len_byte = self._resp.read(1)
            if not meta_len_byte:
                break

            length = meta_len_byte[0] * 16
            if length > 0:
                meta_raw = self._resp.read(length)
                if not meta_raw:
                    break
                meta = meta_raw.decode("utf-8", errors="replace").rstrip("\x00")
                m = re.search(r"StreamTitle='([^']*)'", meta)
                if m:
                    title = m.group(1).strip()
                    pending_title = self._pending_split["title"] if self._pending_split else None
                    if title and title != self._current_track and title != pending_title and " - " in title:
                        if self.split_offset > 0:
                            # Metadata early: delay split
                            self._pending_split = {
                                "title": title,
                                "deadline": time.time() + self.split_offset,
                            }
                        else:
                            # Metadata on time or late: split immediately
                            self._do_split(title, input_fmt)

        self._resp.close()

    def _do_split(self, new_title, input_fmt):
        """Handle track change with offset-aware split."""
        with self._lock:
            old_track = self._current_track
            self._current_track = new_title

            # Stop old ffmpeg and finalize its file
            self._stop_current_ffmpeg()
            self._finalize_track()

            # Start new ffmpeg for the new track
            self._ffmpeg_proc = None
            self._current_file = None
            self._start_ffmpeg_pipe(input_fmt)

            # Pre-buffer replay: feed buffered audio into new file (negative split_offset = metadata late)
            if self.split_offset < 0 and self._prebuffer and self._ffmpeg_proc:
                for _ts, chunk in self._prebuffer:
                    try:
                        self._ffmpeg_proc.stdin.write(chunk)
                    except (OSError, BrokenPipeError):
                        break
                self._prebuffer.clear()

            if old_track:
                log_event(self.stream_id, "track",
                          f"Neuer Track: {new_title} (vorher: {old_track})")

    def _start_ffmpeg_pipe(self, input_fmt):
        """Start ffmpeg reading from stdin pipe."""
        track_name = _sanitize_filename(self._current_track or "recording")
        filename = f"{track_name}.mp3"
        filepath = os.path.join(self.dest, "incomplete", filename)

        if os.path.exists(filepath):
            ts = int(time.time())
            filename = f"{track_name}_{ts}.mp3"
            filepath = os.path.join(self.dest, "incomplete", filename)

        self._current_file = filepath

        self._ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-f", input_fmt,
                "-i", "pipe:0",
                "-map", "0:a",
                "-c:a", "libmp3lame", "-q:a", "2",
                filepath,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.pid = self._ffmpeg_proc.pid

    def _stop_current_ffmpeg(self):
        proc = self._ffmpeg_proc
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._ffmpeg_proc = None

    def _finalize_track(self):
        """Move completed track from incomplete/ to dest/."""
        if not self._current_file or not os.path.exists(self._current_file):
            return
        size = os.path.getsize(self._current_file)
        if size < 10000:
            try:
                os.remove(self._current_file)
            except OSError:
                pass
            return

        basename = os.path.basename(self._current_file)
        target = os.path.join(self.dest, basename)
        if os.path.exists(target):
            name, ext = os.path.splitext(basename)
            target = os.path.join(self.dest, f"{name}_{int(time.time())}{ext}")
        try:
            os.rename(self._current_file, target)
            if self.stream:
                sync_file(target, self.stream)
        except OSError:
            pass


class IcyMetadataReader:
    """Reads ICY metadata from a stream in a background thread.
    Used only as fallback / for testing. FfmpegRecorder with use_icy
    now uses IcyStreamSplitter instead for perfect sync.
    """

    def __init__(self, stream_url, ua):
        self.stream_url = stream_url
        self.ua = ua
        self._current_title = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._resp = None

    def start(self):
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._resp:
            try:
                self._resp.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def get_title(self):
        with self._lock:
            return self._current_title

    def _read_loop(self):
        while not self._stop_event.is_set():
            try:
                self._connect_and_read()
            except Exception:
                pass
            if not self._stop_event.is_set():
                self._stop_event.wait(3)

    def _connect_and_read(self):
        req = urllib.request.Request(
            self.stream_url,
            headers={"User-Agent": self.ua, "Icy-MetaData": "1"},
        )
        self._resp = urllib.request.urlopen(req, timeout=10)
        metaint_str = self._resp.headers.get("icy-metaint")
        if not metaint_str:
            self._resp.close()
            self._stop_event.wait(30)
            return

        metaint = int(metaint_str)
        while not self._stop_event.is_set():
            audio = self._resp.read(metaint)
            if not audio:
                break
            meta_len_byte = self._resp.read(1)
            if not meta_len_byte:
                break
            length = meta_len_byte[0] * 16
            if length > 0:
                meta_raw = self._resp.read(length)
                if not meta_raw:
                    break
                meta = meta_raw.decode("utf-8", errors="replace").rstrip("\x00")
                m = re.search(r"StreamTitle='([^']*)'", meta)
                if m:
                    title = m.group(1).strip()
                    if title:
                        with self._lock:
                            self._current_title = title
        self._resp.close()


class FfmpegRecorder:
    """Records a stream with ffmpeg, splitting files based on metadata changes.

    For ffmpeg_icy mode: delegates to IcyStreamSplitter (single-connection, perfect sync).
    For ffmpeg_api mode: uses separate ffmpeg process + API polling.
    """

    def __init__(self, stream, dest):
        self.stream = stream
        self.stream_id = stream["id"]
        self.dest = dest
        self.stream_url = stream["url"]
        metadata_url = stream["metadata_url"] if "metadata_url" in stream.keys() else ""
        self.metadata_url = metadata_url or ""
        ua_key = stream["user_agent"] if "user_agent" in stream.keys() else DEFAULT_USER_AGENT
        self.ua = USER_AGENTS.get(ua_key, USER_AGENTS[DEFAULT_USER_AGENT])

        record_mode = stream["record_mode"] if "record_mode" in stream.keys() else "ffmpeg_api"
        self.use_icy = (record_mode == "ffmpeg_icy")
        if not self.use_icy and not self.metadata_url:
            self.metadata_url = _detect_metadata_url(stream["url"])

        self._splitter = None  # IcyStreamSplitter for ffmpeg_icy mode
        self._ffmpeg_proc = None
        self._current_track = None
        self._current_file = None
        self._record_start = None
        self._poll_thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.pid = None

    def start(self):
        """Start recording and polling."""
        os.makedirs(self.dest, exist_ok=True)
        os.makedirs(os.path.join(self.dest, "incomplete"), exist_ok=True)

        if self.use_icy:
            # Use single-connection splitter for perfect metadata/audio sync
            split_offset = self.stream["split_offset"] if "split_offset" in self.stream.keys() else 0
            self._splitter = IcyStreamSplitter(
                self.stream_url, self.ua, self.dest, self.stream_id,
                split_offset=split_offset, stream=self.stream)
            self._splitter.start()
            self.pid = self._splitter.pid
            self.start_time = self._splitter.start_time
            return

        # ffmpeg_api mode: separate ffmpeg + API polling
        self._current_track = _fetch_current_song(self.metadata_url, self.ua)
        log_msg = f"FFmpeg+API gestartet, Metadata: {self.metadata_url}"

        self._start_ffmpeg()

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        log_event(self.stream_id, "start", f"{log_msg} (PID {self.pid})")

    def _start_ffmpeg(self):
        """Start an ffmpeg process recording to incomplete/ (API mode only)."""
        track_name = _sanitize_filename(self._current_track or "recording")
        filename = f"{track_name}.mp3"
        filepath = os.path.join(self.dest, "incomplete", filename)

        if os.path.exists(filepath):
            ts = int(time.time())
            filename = f"{track_name}_{ts}.mp3"
            filepath = os.path.join(self.dest, "incomplete", filename)

        self._current_file = filepath
        self._record_start = time.time()

        self._ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-user_agent", self.ua,
                "-i", self.stream_url,
                "-map", "0:a",
                "-c:a", "libmp3lame", "-q:a", "2",
                filepath,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.pid = self._ffmpeg_proc.pid

    def _stop_ffmpeg(self):
        """Gracefully stop the current ffmpeg process (API mode only)."""
        proc = self._ffmpeg_proc
        if proc is None:
            return
        stderr_output = ""
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass
        try:
            _, stderr_bytes = proc.communicate(timeout=5)
            if stderr_bytes:
                stderr_output = stderr_bytes.decode("utf-8", errors="replace")[-500:]
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if stderr_output and proc.returncode and proc.returncode != 0:
            log_event(self.stream_id, "ffmpeg_err", f"RC {proc.returncode}: {stderr_output[-200:]}")

    def _finalize_track(self):
        """Move completed track from incomplete/ to the main directory."""
        if not self._current_file or not os.path.exists(self._current_file):
            return
        size = os.path.getsize(self._current_file)
        if size < 10000:
            try:
                os.remove(self._current_file)
            except OSError:
                pass
            return

        basename = os.path.basename(self._current_file)
        target = os.path.join(self.dest, basename)
        if os.path.exists(target):
            name, ext = os.path.splitext(basename)
            target = os.path.join(self.dest, f"{name}_{int(time.time())}{ext}")
        try:
            os.rename(self._current_file, target)
            sync_file(target, self.stream)
        except OSError:
            pass

    def _get_new_track(self):
        """Get the current track title from the API."""
        return _fetch_current_song(self.metadata_url, self.ua)

    def _poll_loop(self):
        """Poll metadata and split on track change (API mode only)."""
        split_offset = self.stream["split_offset"] if "split_offset" in self.stream.keys() else 0
        pending_split = None  # {"title": ..., "deadline": ...}

        while not self._stop_event.is_set():
            self._stop_event.wait(METADATA_POLL_INTERVAL)
            if self._stop_event.is_set():
                break

            # Execute pending delayed split if deadline reached
            if pending_split and time.time() >= pending_split["deadline"]:
                with self._lock:
                    old_track = self._current_track
                    self._current_track = pending_split["title"]
                    self._stop_ffmpeg()
                    self._finalize_track()
                    self._start_ffmpeg()
                    log_event(self.stream_id, "track",
                              f"Neuer Track: {pending_split['title']}" + (f" (vorher: {old_track})" if old_track else ""))
                pending_split = None

            new_track = self._get_new_track()
            if not new_track or new_track == self._current_track:
                continue
            # Skip if already pending for this title
            if pending_split and new_track == pending_split["title"]:
                continue

            if split_offset > 0:
                # Metadata arrives early: delay the split
                pending_split = {"title": new_track, "deadline": time.time() + split_offset}
            else:
                with self._lock:
                    old_track = self._current_track
                    self._current_track = new_track
                    self._stop_ffmpeg()
                    self._finalize_track()
                    self._start_ffmpeg()
                    log_event(self.stream_id, "track",
                              f"Neuer Track: {new_track}" + (f" (vorher: {old_track})" if old_track else ""))

    def stop(self):
        """Stop recording completely."""
        if self._splitter:
            self._splitter.stop()
            return

        self._stop_event.set()
        with self._lock:
            self._stop_ffmpeg()
            self._finalize_track()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)

    def poll(self):
        """Check if recording is still running (subprocess.Popen compatible)."""
        if self._splitter:
            return self._splitter.poll()
        if self._ffmpeg_proc:
            return self._ffmpeg_proc.poll()
        return -1

    @property
    def returncode(self):
        if self._splitter:
            return self._splitter.returncode
        if self._ffmpeg_proc:
            return self._ffmpeg_proc.returncode
        return None

    def get_current_track(self):
        if self._splitter:
            return self._splitter.get_current_track()
        return self._current_track
