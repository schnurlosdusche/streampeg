import os
import subprocess
import threading
import time
import glob
import signal
import urllib.request
from config import RECORDING_BASE, STREAMRIPPER_BIN, USER_AGENTS, DEFAULT_USER_AGENT, MIN_BITRATE
from db import log_event, get_track_stats
from ffmpeg_recorder import FfmpegRecorder, _trim_audio_file, _title_matches_skip_words
from module_manager import get_recorder_class
from sync import sync_file, get_sync_target
import cover_art

# In-memory process registry: stream_id -> {proc, start_time}
_processes = {}

# Cache for file counts (expensive NAS glob): stream_id -> {count, size, timestamp}
_file_count_cache = {}
_FILE_COUNT_TTL = 300  # seconds (NAS glob is expensive over SMB)


class BitrateError(Exception):
    """Raised when stream bitrate is below MIN_BITRATE."""
    pass


def _check_stream_bitrate(url, ua):
    """Probe stream ICY headers and return bitrate. Raises BitrateError if below MIN_BITRATE."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Icy-MetaData": "1"})
        resp = urllib.request.urlopen(req, timeout=10)
        icy_br = resp.headers.get("icy-br")
        resp.close()
        if icy_br:
            bitrate = int(icy_br.split(",")[0])
            if bitrate < MIN_BITRATE:
                raise BitrateError(
                    f"Bitrate {bitrate} kbps ist unter dem Minimum von {MIN_BITRATE} kbps")
            return bitrate
    except BitrateError:
        raise
    except Exception:
        pass  # Can't determine bitrate — allow (will be checked again at runtime)
    return None


def start_stream(stream):
    stream_id = stream["id"]
    if stream_id in _processes and _processes[stream_id]["proc"].poll() is None:
        return _processes[stream_id]["proc"].pid

    dest = os.path.join(RECORDING_BASE, stream["dest_subdir"])
    os.makedirs(dest, exist_ok=True)

    record_mode = stream["record_mode"] if "record_mode" in stream.keys() else "streamripper"

    # Check if a module provides this record mode
    recorder_cls = get_recorder_class(record_mode)
    if recorder_cls:
        recorder = recorder_cls(stream, dest)
        recorder.start()
        _processes[stream_id] = {"proc": recorder, "start_time": time.time(), "mode": record_mode}
        return recorder.pid

    # Check stream bitrate before starting streamripper/ffmpeg recorders
    ua_key = stream["user_agent"] if "user_agent" in stream.keys() else DEFAULT_USER_AGENT
    ua = USER_AGENTS.get(ua_key, USER_AGENTS[DEFAULT_USER_AGENT])
    bitrate = _check_stream_bitrate(stream["url"], ua)
    if bitrate is not None and bitrate < MIN_BITRATE:
        msg = f"Abgelehnt: Bitrate {bitrate} kbps < {MIN_BITRATE} kbps Minimum"
        log_event(stream_id, "error", msg)
        raise BitrateError(msg)

    if record_mode in ("ffmpeg_api", "ffmpeg_icy"):
        recorder = FfmpegRecorder(stream, dest)
        recorder.start()
        _processes[stream_id] = {"proc": recorder, "start_time": time.time(), "mode": record_mode}
        return recorder.pid

    # Default: streamripper
    proc = subprocess.Popen(
        [STREAMRIPPER_BIN, stream["url"], "-d", dest, "--quiet", "-u", ua],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    watcher = _FileWatcher(dest, stream)
    watcher.start()
    _processes[stream_id] = {"proc": proc, "start_time": time.time(), "mode": "streamripper", "watcher": watcher}
    log_event(stream_id, "start", f"Gestartet (PID {proc.pid})")
    return proc.pid


def stop_stream(stream_id):
    info = _processes.get(stream_id)
    if not info:
        return False

    proc = info["proc"]
    mode = info.get("mode", "streamripper")

    if hasattr(proc, 'stop'):
        proc.stop()
    else:
        pid = proc.pid
        if proc.poll() is None:
            # Kill the entire process group so child processes also die
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait()

    watcher = info.get("watcher")
    if watcher:
        watcher.stop()
    del _processes[stream_id]
    log_event(stream_id, "stop", "Gestoppt")
    return True


def stop_all_streams():
    """Stop all running streams gracefully. Called on shutdown."""
    stream_ids = list(_processes.keys())
    for sid in stream_ids:
        try:
            stop_stream(sid)
        except Exception:
            pass


def cleanup_incomplete(streams):
    """Delete incomplete/ directories left by streamripper after shutdown."""
    for s in streams:
        inc_dir = os.path.join(RECORDING_BASE, s["dest_subdir"], "incomplete")
        if os.path.isdir(inc_dir):
            for f in glob.glob(os.path.join(inc_dir, "*")):
                try:
                    os.remove(f)
                except OSError:
                    pass


def _count_audio_files(directory):
    """Count audio files and total size in a directory (excluding incomplete/)."""
    count = 0
    size = 0
    if not os.path.isdir(directory):
        return count, size
    for ext in ("*.mp3", "*.ogg", "*.aac"):
        for f in glob.glob(os.path.join(directory, "**", ext), recursive=True):
            if "/incomplete/" not in f:
                count += 1
                try:
                    size += os.path.getsize(f)
                except OSError:
                    pass
    return count, size


def _get_cached_file_counts(stream_id, dest, nas_dest):
    """Get file counts with caching to avoid slow NAS glob on every poll."""
    now = time.time()
    cached = _file_count_cache.get(stream_id)
    if cached and (now - cached["ts"]) < _FILE_COUNT_TTL:
        return cached["count"], cached["size"]

    worker_count, worker_size = _count_audio_files(dest)
    nas_count, nas_size = _count_audio_files(nas_dest)
    total_count = worker_count + nas_count
    total_size = worker_size + nas_size
    _file_count_cache[stream_id] = {"count": total_count, "size": total_size, "ts": now}
    return total_count, total_size


def get_status_fast(stream):
    """Lightweight status for initial page render — no NAS glob, no DB queries."""
    stream_id = stream["id"]
    info = _processes.get(stream_id)
    running = False
    pid = None
    uptime = 0

    if info and info["proc"].poll() is None:
        running = True
        pid = info["proc"].pid
        uptime = int(time.time() - info["start_time"])

    current_track = None
    rec_state = None
    yt_stats = None

    if info and hasattr(info["proc"], "get_current_track"):
        current_track = info["proc"].get_current_track()
        rec_state = info["proc"].get_state() if hasattr(info["proc"], "get_state") else None
        if hasattr(info["proc"], "get_stats"):
            yt_stats = info["proc"].get_stats()
    elif info:
        watcher = info.get("watcher")
        if watcher:
            rec_state = watcher.get_state()
            current_track = watcher.get_current_track()

    # File count/size with TTL-based cache
    import config, sync
    try:
        subdir = stream["dest_subdir"]
    except (KeyError, IndexError):
        subdir = stream["name"].replace(" ", "_")
    dest = os.path.join(config.RECORDING_BASE, subdir)
    nas_dest = os.path.join(sync.get_sync_target(), subdir)
    try:
        fc, sz = _get_cached_file_counts(stream_id, dest, nas_dest)
        file_count = fc
        disk_usage_mb = round(sz / (1024 * 1024), 1)
    except Exception:
        file_count = "-"
        disk_usage_mb = 0

    # Cover art
    cover_url_val = None
    if running and info and hasattr(info["proc"], "get_cover_url"):
        cover_url_val = info["proc"].get_cover_url()
    if not cover_url_val and running and current_track:
        cover_url_val = cover_art.get_cover_url(stream_id, current_track)

    result = {
        "running": running,
        "pid": pid,
        "uptime": uptime,
        "uptime_str": _format_uptime(uptime) if running else "-",
        "current_track": current_track,
        "bitrate": None,
        "cover_url": cover_url_val,
        "file_count": file_count,
        "disk_usage_mb": disk_usage_mb,
        "rec_state": rec_state,
        "rec_pct": 0,
        "track_stats": None,
    }
    if yt_stats:
        result["yt_stats"] = yt_stats
        result["rec_pct"] = yt_stats.get("rec_pct", 0)
    return result


def get_status(stream):
    stream_id = stream["id"]
    dest = os.path.join(RECORDING_BASE, stream["dest_subdir"])
    nas_dest = os.path.join(get_sync_target(), stream["dest_subdir"])

    info = _processes.get(stream_id)
    running = False
    pid = None
    uptime = 0

    if info and info["proc"].poll() is None:
        running = True
        pid = info["proc"].pid
        uptime = int(time.time() - info["start_time"])

    # For recorder objects (ffmpeg, module-provided), get status from the recorder
    if info and hasattr(info["proc"], "get_current_track"):
        current_track = info["proc"].get_current_track()
        total_count, total_size = _get_cached_file_counts(stream_id, dest, nas_dest)
        bitrate = info["proc"].get_bitrate() if hasattr(info["proc"], "get_bitrate") else None
        rec_state = info["proc"].get_state() if hasattr(info["proc"], "get_state") else None
        track_stats = get_track_stats(stream_id)
        # Cover art: use recorder override (yt-dlp thumb) or iTunes lookup
        cover_url_val = None
        if hasattr(info["proc"], "get_cover_url"):
            cover_url_val = info["proc"].get_cover_url()
        if not cover_url_val and current_track:
            cover_url_val = cover_art.get_cover_url(stream_id, current_track)
        result = {
            "running": running,
            "pid": pid,
            "uptime": uptime,
            "uptime_str": _format_uptime(uptime) if running else "-",
            "current_track": current_track,
            "bitrate": bitrate,
            "cover_url": cover_url_val,
            "file_count": total_count,
            "disk_usage_mb": round(total_size / (1024 * 1024), 1),
            "rec_state": rec_state,
            "rec_pct": track_stats["rec_pct"],
            "track_stats": track_stats,
        }
        if hasattr(info["proc"], "get_stats"):
            result["yt_stats"] = info["proc"].get_stats()
            result["rec_pct"] = result["yt_stats"]["rec_pct"]
        return result

    # Current track from incomplete directory (only when running)
    current_track = None
    rec_state = None
    if running:
        incomplete_dirs = glob.glob(os.path.join(dest, "*/incomplete"))
        if not incomplete_dirs:
            incomplete_dirs = [os.path.join(dest, "incomplete")]
        for inc_dir in incomplete_dirs:
            if os.path.isdir(inc_dir):
                files = sorted(
                    glob.glob(os.path.join(inc_dir, "*.mp3"))
                    + glob.glob(os.path.join(inc_dir, "*.ogg"))
                    + glob.glob(os.path.join(inc_dir, "*.aac")),
                    key=os.path.getmtime,
                    reverse=True,
                )
                if files:
                    name = os.path.splitext(os.path.basename(files[0]))[0]
                    if name not in ("recording",):
                        current_track = name
                    break

        # Streamripper: get rec_state and current_track from watcher if available
        watcher = info.get("watcher") if info else None
        if watcher:
            rec_state = watcher.get_state()
            watcher_track = watcher.get_current_track()
            if watcher_track:
                current_track = watcher_track

    # File count and disk usage (cached)
    total_count, total_size = _get_cached_file_counts(stream_id, dest, nas_dest)

    track_stats = get_track_stats(stream_id)
    cover_url_val = cover_art.get_cover_url(stream_id, current_track) if running and current_track else None
    return {
        "running": running,
        "pid": pid,
        "uptime": uptime,
        "uptime_str": _format_uptime(uptime) if running else "-",
        "current_track": current_track,
        "bitrate": None,
        "cover_url": cover_url_val,
        "file_count": total_count,
        "disk_usage_mb": round(total_size / (1024 * 1024), 1),
        "rec_state": rec_state,
        "rec_pct": track_stats["rec_pct"],
        "track_stats": track_stats,
    }


def check_and_restart(stream):
    stream_id = stream["id"]
    info = _processes.get(stream_id)
    if info and info["proc"].poll() is not None:
        exit_code = info["proc"].returncode
        log_event(stream_id, "error", f"Unerwartet beendet (RC {exit_code}), Neustart...")
        del _processes[stream_id]
        start_stream(stream)
        return True
    return False


def adopt_existing_processes(streams):
    """On app startup, find running streamripper/ffmpeg processes and re-adopt them."""
    for cmd_name in ("streamripper", "ffmpeg"):
        try:
            result = subprocess.run(
                ["pgrep", "-a", cmd_name],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                continue
        except Exception:
            continue

        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            pid = int(parts[0])
            cmdline = parts[1]

            for stream in streams:
                if stream["id"] in _processes:
                    continue
                if stream["url"] in cmdline:
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        continue

                    record_mode = stream["record_mode"] if "record_mode" in stream.keys() else "streamripper"

                    if cmd_name == "ffmpeg" and record_mode in ("ffmpeg_api", "ffmpeg_icy"):
                        # Kill old ffmpeg and start fresh with full FfmpegRecorder
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except OSError:
                            pass
                        start_stream(stream)
                    else:
                        proc = _PidWrapper(pid)
                        mode = "streamripper"
                        _processes[stream["id"]] = {"proc": proc, "start_time": time.time(), "mode": mode}

                    log_event(stream["id"], "adopt", f"Bestehenden {cmd_name}-Prozess adoptiert (PID {pid})")
                    break


class _PidWrapper:
    """Wraps an existing PID to behave like subprocess.Popen."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        try:
            os.kill(self.pid, 0)
            return None
        except OSError:
            self.returncode = -1
            return self.returncode

    def terminate(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass

    def wait(self, timeout=None):
        deadline = time.time() + (timeout or 30)
        while time.time() < deadline:
            if self.poll() is not None:
                return self.returncode
            time.sleep(0.2)
        raise subprocess.TimeoutExpired(cmd="streamripper", timeout=timeout)


class _FileWatcher:
    """Watches a directory for new audio files, trims and syncs them to NAS."""

    def __init__(self, directory, stream, interval=10):
        self.directory = directory
        self.stream = stream
        self.stream_id = stream["id"]
        self.trim_start = stream["trim_start"] if "trim_start" in stream.keys() else 0
        self.trim_end = stream["trim_end"] if "trim_end" in stream.keys() else 0
        self.skip_words = stream["skip_words"] if "skip_words" in stream.keys() else ""
        self.min_size_bytes = (stream["min_size_mb"] if "min_size_mb" in stream.keys() else 2) * 1024 * 1024
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = None
        self._known_files = set()
        self._skip_first = True  # Skip first (partial) track after start
        self._state = "waiting"  # waiting / recording / skipping
        self._current_track = None
        # Seed with existing files so we don't sync old ones
        for ext in ("*.mp3", "*.ogg", "*.aac"):
            for f in glob.glob(os.path.join(directory, ext)):
                self._known_files.add(f)

    def start(self):
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_state(self):
        return self._state

    def get_current_track(self):
        return self._current_track

    def _track_file_exists(self, filepath):
        """Check if this track already exists on NAS."""
        basename = os.path.basename(filepath)
        nas_dest = os.path.join(get_sync_target(), self.stream["dest_subdir"])
        return os.path.exists(os.path.join(nas_dest, basename))

    def _watch_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(self.interval)
            if self._stop_event.is_set():
                break
            try:
                current_files = set()
                for ext in ("*.mp3", "*.ogg", "*.aac"):
                    current_files.update(glob.glob(os.path.join(self.directory, ext)))

                new_files = current_files - self._known_files
                for filepath in new_files:
                    # Skip files in incomplete/
                    if "/incomplete/" in filepath:
                        continue

                    # Rename file if it contains apostrophes (avoid duplicates)
                    basename = os.path.basename(filepath)
                    if "'" in basename or "\u2019" in basename:
                        clean_name = basename.replace("'", "").replace("\u2019", "")
                        clean_path = os.path.join(os.path.dirname(filepath), clean_name)
                        try:
                            os.rename(filepath, clean_path)
                            filepath = clean_path
                        except OSError:
                            pass

                    track_name = os.path.splitext(os.path.basename(filepath))[0]
                    self._current_track = track_name

                    # Skip first (partial) track after start
                    if self._skip_first:
                        self._skip_first = False
                        self._state = "recording"
                        log_event(self.stream_id, "track",
                                  f"Erster Track übersprungen (partiell): {track_name}")
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        continue

                    # Skip if title matches skip words
                    if _title_matches_skip_words(track_name, self.skip_words):
                        self._state = "skipping"
                        log_event(self.stream_id, "track",
                                  f"Übersprungen (Skip-Wort): {track_name}")
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        continue

                    # Skip if track already exists on NAS
                    if self._track_file_exists(filepath):
                        self._state = "skipping"
                        log_event(self.stream_id, "track",
                                  f"Übersprungen (existiert): {track_name}")
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        continue

                    self._state = "recording"
                    # Trim start/end if configured
                    if self.trim_start > 0 or self.trim_end > 0:
                        filepath = _trim_audio_file(
                            filepath, self.trim_start, self.trim_end,
                            self.stream_id)
                    # Check min file size before syncing
                    try:
                        fsize = os.path.getsize(filepath)
                    except OSError:
                        fsize = 0
                    if fsize < self.min_size_bytes:
                        log_event(self.stream_id, "cleanup",
                                  f"Zu klein ({fsize // 1024} KB), gelöscht: {os.path.basename(filepath)}")
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        continue
                    log_event(self.stream_id, "track",
                              f"Neuer Track: {os.path.basename(filepath)}")
                    sync_file(filepath, self.stream)
                self._known_files = current_files
            except Exception:
                pass


def _format_uptime(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"
