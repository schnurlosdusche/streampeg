import os
import subprocess
import time
import glob
import signal
from config import RECORDING_BASE, SMB_TARGET, STREAMRIPPER_BIN, USER_AGENTS, DEFAULT_USER_AGENT
from db import log_event
from ffmpeg_recorder import FfmpegRecorder

# In-memory process registry: stream_id -> {proc, start_time}
_processes = {}


def start_stream(stream):
    stream_id = stream["id"]
    if stream_id in _processes and _processes[stream_id]["proc"].poll() is None:
        return _processes[stream_id]["proc"].pid

    dest = os.path.join(RECORDING_BASE, stream["dest_subdir"])
    os.makedirs(dest, exist_ok=True)

    record_mode = stream["record_mode"] if "record_mode" in stream.keys() else "streamripper"

    if record_mode in ("ffmpeg_api", "ffmpeg_icy"):
        recorder = FfmpegRecorder(stream, dest)
        recorder.start()
        _processes[stream_id] = {"proc": recorder, "start_time": time.time(), "mode": record_mode}
        return recorder.pid

    # Default: streamripper
    ua_key = stream["user_agent"] if "user_agent" in stream.keys() else DEFAULT_USER_AGENT
    ua = USER_AGENTS.get(ua_key, USER_AGENTS[DEFAULT_USER_AGENT])
    proc = subprocess.Popen(
        [STREAMRIPPER_BIN, stream["url"], "-d", dest, "--quiet", "-u", ua],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    _processes[stream_id] = {"proc": proc, "start_time": time.time(), "mode": "streamripper"}
    log_event(stream_id, "start", f"Gestartet (PID {proc.pid})")
    return proc.pid


def stop_stream(stream_id):
    info = _processes.get(stream_id)
    if not info:
        return False

    proc = info["proc"]
    mode = info.get("mode", "streamripper")

    if mode in ("ffmpeg_api", "ffmpeg_icy"):
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

    del _processes[stream_id]
    log_event(stream_id, "stop", "Gestoppt")
    return True


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


def get_status(stream):
    stream_id = stream["id"]
    dest = os.path.join(RECORDING_BASE, stream["dest_subdir"])
    nas_dest = os.path.join(SMB_TARGET, stream["dest_subdir"])

    info = _processes.get(stream_id)
    running = False
    pid = None
    uptime = 0

    if info and info["proc"].poll() is None:
        running = True
        pid = info["proc"].pid
        uptime = int(time.time() - info["start_time"])

    # For ffmpeg modes, get current track from the recorder
    if info and info.get("mode") in ("ffmpeg_api", "ffmpeg_icy") and hasattr(info["proc"], "get_current_track"):
        current_track = info["proc"].get_current_track()
        if current_track:
            worker_count, worker_size = _count_audio_files(dest)
            nas_count, nas_size = _count_audio_files(nas_dest)
            return {
                "running": running,
                "pid": pid,
                "uptime": uptime,
                "uptime_str": _format_uptime(uptime) if running else "-",
                "current_track": current_track,
                "file_count": worker_count + nas_count,
                "disk_usage_mb": round((worker_size + nas_size) / (1024 * 1024), 1),
            }

    # Current track from incomplete directory
    current_track = None
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
                current_track = os.path.splitext(os.path.basename(files[0]))[0]
                break

    # File count and disk usage (worker + NAS)
    worker_count, worker_size = _count_audio_files(dest)
    nas_count, nas_size = _count_audio_files(nas_dest)

    return {
        "running": running,
        "pid": pid,
        "uptime": uptime,
        "uptime_str": _format_uptime(uptime) if running else "-",
        "current_track": current_track,
        "file_count": worker_count + nas_count,
        "disk_usage_mb": round((worker_size + nas_size) / (1024 * 1024), 1),
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


def _format_uptime(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"
