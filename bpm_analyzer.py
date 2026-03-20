"""
Background BPM/Key analyzer for library tracks.
Runs as a daemon thread, continuously analyzes tracks missing BPM or Key.
Supports aubio (fast, lightweight) and essentia (more accurate) backends.
"""

import os
import time
import threading
import logging
import subprocess
import tempfile

import db

log = logging.getLogger(__name__)

# Backend availability
_aubio_available = False
_essentia_available = False

try:
    import aubio
    _aubio_available = True
except ImportError:
    pass

try:
    import essentia
    import essentia.standard as es
    _essentia_available = True
except ImportError:
    pass


# Worker state
_worker_thread = None
_worker_running = False
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
    if _aubio_available:
        backends.append("aubio")
    if _essentia_available:
        backends.append("essentia")
    return backends


def get_status():
    """Return current analyzer status."""
    with _worker_lock:
        return dict(_worker_status)


# --- Aubio backend ---

def _analyze_bpm_aubio(filepath):
    """Detect BPM using aubio."""
    try:
        src = aubio.source(filepath, samplerate=0, hop_size=512)
        tempo = aubio.tempo("default", 1024, 512, src.samplerate)
        beats = []
        total_frames = 0
        while True:
            samples, read = src()
            is_beat = tempo(samples)
            if is_beat[0]:
                beats.append(total_frames / float(src.samplerate))
            total_frames += read
            if read < 512:
                break
        bpm = tempo.get_bpm()
        return int(round(bpm)) if bpm > 0 else 0
    except Exception as e:
        log.debug("aubio BPM error for %s: %s", filepath, e)
        return 0


def _analyze_key_aubio(filepath):
    """Aubio doesn't have built-in key detection. Return empty."""
    return ""


# --- Essentia backend ---

def _analyze_bpm_essentia(filepath):
    """Detect BPM using essentia."""
    try:
        audio = es.MonoLoader(filename=filepath, sampleRate=44100)()
        rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
        bpm, beats, confidence, estimates, bpm_intervals = rhythm_extractor(audio)
        return int(round(bpm)) if bpm > 0 else 0
    except Exception as e:
        log.debug("essentia BPM error for %s: %s", filepath, e)
        return 0


def _analyze_key_essentia(filepath):
    """Detect musical key using essentia."""
    try:
        audio = es.MonoLoader(filename=filepath, sampleRate=44100)()
        key_extractor = es.KeyExtractor()
        key, scale, strength = key_extractor(audio)
        if key and scale:
            # Format: "C" + "minor" -> "Cm", "C" + "major" -> "C"
            if scale == "minor":
                return key + "m"
            return key
        return ""
    except Exception as e:
        log.debug("essentia Key error for %s: %s", filepath, e)
        return ""


# --- Analysis dispatch ---

def _analyze_track(filepath, backend):
    """Analyze a single track and return (bpm, key)."""
    bpm = 0
    key = ""

    if backend == "essentia" and _essentia_available:
        bpm = _analyze_bpm_essentia(filepath)
        key = _analyze_key_essentia(filepath)
    elif backend == "aubio" and _aubio_available:
        bpm = _analyze_bpm_aubio(filepath)
        key = _analyze_key_aubio(filepath)

    return bpm, key


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


# --- Background worker ---

def _worker_loop():
    """Main worker loop: find tracks without BPM/Key and analyze them."""
    global _worker_running, _worker_status

    backend = db.get_setting("bpm_backend") or "aubio"

    while _worker_running:
        # Get tracks missing BPM or Key
        conn = db.get_db()
        rows = conn.execute(
            "SELECT id, filepath FROM library_tracks WHERE (bpm = 0 OR bpm IS NULL OR key = '' OR key IS NULL) LIMIT 50"
        ).fetchall()
        conn.close()

        if not rows:
            with _worker_lock:
                _worker_status["remaining"] = 0
                _worker_status["current_file"] = ""
            # Nothing to do, sleep and check again
            for _ in range(30):  # Sleep 30s in 1s increments
                if not _worker_running:
                    return
                time.sleep(1)
            continue

        # Get total remaining count
        conn = db.get_db()
        remaining = conn.execute(
            "SELECT COUNT(*) as cnt FROM library_tracks WHERE (bpm = 0 OR bpm IS NULL OR key = '' OR key IS NULL)"
        ).fetchone()["cnt"]
        conn.close()

        with _worker_lock:
            _worker_status["remaining"] = remaining
            _worker_status["backend"] = backend

        for row in rows:
            if not _worker_running:
                return

            track_id = row["id"]
            filepath = row["filepath"]

            if not os.path.isfile(filepath):
                # Mark as analyzed with 0/empty to avoid retrying
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

            # Re-read backend setting (may change in settings)
            new_backend = db.get_setting("bpm_backend") or "aubio"
            if new_backend != backend:
                backend = new_backend
                with _worker_lock:
                    _worker_status["backend"] = backend

            bpm, key = _analyze_track(filepath, backend)

            # Update DB
            conn = db.get_db()
            conn.execute(
                "UPDATE library_tracks SET bpm = ?, key = CASE WHEN ? != '' THEN ? ELSE key END WHERE id = ?",
                (bpm if bpm > 0 else -1, key, key, track_id),
            )
            conn.commit()
            conn.close()

            # Also write to MP3 file tags
            if bpm > 0 or key:
                _write_tags(filepath, bpm, key)

            with _worker_lock:
                _worker_status["analyzed"] += 1
                _worker_status["remaining"] = max(0, _worker_status["remaining"] - 1)

    with _worker_lock:
        _worker_status["running"] = False
        _worker_status["current_file"] = ""


def start():
    """Start the background BPM/Key analyzer."""
    global _worker_thread, _worker_running, _worker_status

    enabled = db.get_setting("bpm_analyzer_enabled")
    if enabled != "1":
        return False

    with _worker_lock:
        if _worker_status["running"]:
            return True  # Already running
        _worker_running = True
        _worker_status = {
            "running": True,
            "current_file": "",
            "analyzed": 0,
            "remaining": 0,
            "backend": db.get_setting("bpm_backend") or "aubio",
        }

    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()
    return True


def stop():
    """Stop the background analyzer."""
    global _worker_running
    _worker_running = False
    with _worker_lock:
        _worker_status["running"] = False


def is_running():
    """Check if the analyzer is currently running."""
    with _worker_lock:
        return _worker_status["running"]
