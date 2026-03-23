"""
Background BPM/Key analyzer for library tracks.
Runs analysis in a separate subprocess to avoid blocking Flask.
Supports aubio (fast, lightweight) and essentia (more accurate) backends.
"""

import os
import sys
import json
import time
import threading
import logging
import subprocess
import signal

import db

log = logging.getLogger(__name__)

# Worker state
_worker_thread = None
_worker_running = False
_worker_process = None  # subprocess.Popen
_worker_lock = threading.Lock()
_worker_status = {
    "running": False,
    "current_file": "",
    "analyzed": 0,
    "remaining": 0,
    "backend": "",
}


def get_available_backends():
    """Return list of available analysis backends."""
    backends = []
    try:
        import aubio
        backends.append("aubio")
    except ImportError:
        pass
    try:
        import essentia
        backends.append("essentia")
    except ImportError:
        pass
    return backends


def get_status():
    """Return current analyzer status."""
    with _worker_lock:
        return dict(_worker_status)


def _write_tags(filepath, bpm, key):
    """Write BPM and Key back to the MP3's ID3 tags."""
    try:
        from mutagen.id3 import ID3, TBPM, TKEY, ID3NoHeaderError
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            return
        if bpm > 0:
            tags.delall("TBPM")
            tags.add(TBPM(encoding=3, text=[str(bpm)]))
        if key:
            tags.delall("TKEY")
            tags.add(TKEY(encoding=3, text=[key]))
        tags.save()
    except Exception as e:
        log.debug("Failed to write tags to %s: %s", filepath, e)


def _analyze_track(filepath, backend):
    """Analyze a single track in a subprocess. Returns (bpm, key).
    Uses nice/ionice for low priority and enforces timeout + memory limit."""
    # Skip files > 100MB (long mixes that eat too much RAM)
    try:
        fsize = os.path.getsize(filepath)
        if fsize > 100 * 1024 * 1024:
            log.debug("Skipping BPM analysis for large file (%dMB): %s",
                      fsize // (1024 * 1024), filepath)
            return -1, "-"
    except OSError:
        return 0, ""

    script = os.path.join(os.path.dirname(__file__), "_bpm_worker.py")
    try:
        # Run with nice 19 (lowest priority) and ionice idle class
        cmd = ["nice", "-n", "19", "ionice", "-c", "3",
               sys.executable, script, filepath, backend]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        )
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            log.debug("BPM analysis timed out for %s", filepath)
            return 0, ""

        if proc.returncode == 0 and stdout.strip():
            data = json.loads(stdout.strip())
            return data.get("bpm", 0), data.get("key", "")
    except Exception as e:
        log.debug("BPM analysis error for %s: %s", filepath, e)
    return 0, ""


# --- Background worker ---

def _is_client_active():
    """Check if a browser client is actively viewing the page."""
    try:
        import app as _app
        return _app.is_client_active()
    except Exception:
        return False


def _worker_loop():
    """Main worker loop: find tracks without BPM/Key and analyze them.
    Pauses when a browser client is actively viewing the page."""
    global _worker_running

    backend = db.get_setting("bpm_backend") or "aubio"

    while _worker_running:
        # Wait while a client is actively viewing
        if _is_client_active():
            with _worker_lock:
                _worker_status["paused"] = True
            for _ in range(10):
                if not _worker_running:
                    return
                time.sleep(1)
            continue
        with _worker_lock:
            _worker_status["paused"] = False

        conn = db.get_db()
        rows = conn.execute(
            "SELECT id, filepath FROM library_tracks "
            "WHERE (bpm = 0 OR bpm IS NULL OR key = '' OR key IS NULL) LIMIT 50"
        ).fetchall()
        conn.close()

        if not rows:
            with _worker_lock:
                _worker_status["remaining"] = 0
                _worker_status["current_file"] = ""
            for _ in range(30):
                if not _worker_running:
                    return
                time.sleep(1)
            continue

        conn = db.get_db()
        remaining = conn.execute(
            "SELECT COUNT(*) as cnt FROM library_tracks "
            "WHERE (bpm = 0 OR bpm IS NULL OR key = '' OR key IS NULL)"
        ).fetchone()["cnt"]
        conn.close()

        with _worker_lock:
            _worker_status["remaining"] = remaining
            _worker_status["backend"] = backend

        for row in rows:
            if not _worker_running:
                return

            # Pause if client becomes active mid-batch
            while _is_client_active() and _worker_running:
                with _worker_lock:
                    _worker_status["paused"] = True
                time.sleep(5)
            with _worker_lock:
                _worker_status["paused"] = False

            track_id = row["id"]
            filepath = row["filepath"]

            if not os.path.isfile(filepath):
                conn = db.get_db()
                conn.execute(
                    "UPDATE library_tracks SET bpm = -1 WHERE id = ? AND (bpm = 0 OR bpm IS NULL)",
                    (track_id,),
                )
                conn.commit()
                conn.close()
                continue

            with _worker_lock:
                _worker_status["current_file"] = os.path.basename(filepath)

            new_backend = db.get_setting("bpm_backend") or "aubio"
            if new_backend != backend:
                backend = new_backend
                with _worker_lock:
                    _worker_status["backend"] = backend

            # Run analysis in subprocess (non-blocking for Flask)
            bpm, key = _analyze_track(filepath, backend)

            conn = db.get_db()
            conn.execute(
                "UPDATE library_tracks SET bpm = ?, key = CASE WHEN ? != '' THEN ? ELSE key END WHERE id = ?",
                (bpm if bpm > 0 else -1, key, key, track_id),
            )
            conn.commit()
            conn.close()

            if bpm > 0 or key:
                _write_tags(filepath, bpm, key)

            with _worker_lock:
                _worker_status["analyzed"] += 1
                _worker_status["remaining"] = max(0, _worker_status["remaining"] - 1)

            # Pause between tracks to keep server responsive
            time.sleep(2)

    with _worker_lock:
        _worker_status["running"] = False
        _worker_status["current_file"] = ""


def start():
    """Start the background BPM/Key analyzer."""
    global _worker_thread, _worker_running

    enabled = db.get_setting("bpm_analyzer_enabled")
    if enabled != "1":
        return False

    with _worker_lock:
        if _worker_status["running"]:
            return True
        _worker_running = True
        _worker_status.update({
            "running": True,
            "current_file": "",
            "analyzed": 0,
            "remaining": 0,
            "backend": db.get_setting("bpm_backend") or "aubio",
        })

    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()
    return True


def stop():
    """Stop the background analyzer immediately."""
    global _worker_running
    _worker_running = False
    with _worker_lock:
        _worker_status["running"] = False


def is_running():
    """Check if the analyzer is currently running."""
    with _worker_lock:
        return _worker_status["running"]
