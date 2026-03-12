import os
import glob
import subprocess
from config import RECORDING_BASE, SMB_TARGET
from db import log_sync, log_event


def is_sync_enabled():
    """Check if sync is enabled in settings. Default: enabled."""
    from db import get_setting
    val = get_setting("sync_enabled")
    return val != "0"  # enabled by default


def get_sync_target():
    """Return configured sync target path. Falls back to config.py SMB_TARGET."""
    from db import get_setting
    val = get_setting("sync_target")
    return val if val else SMB_TARGET


def sync_file(filepath, stream):
    """Sync a single file to NAS and remove locally. Called after download/recording."""
    if not filepath or not os.path.exists(filepath):
        return False

    if not is_sync_enabled():
        return False

    target = get_sync_target()
    if not os.path.ismount(target):
        return False

    dst_dir = os.path.join(target, stream["dest_subdir"])
    os.makedirs(dst_dir, exist_ok=True)

    dst = os.path.join(dst_dir, os.path.basename(filepath))
    if os.path.exists(dst):
        name, ext = os.path.splitext(os.path.basename(filepath))
        dst = os.path.join(dst_dir, f"{name}_{int(os.path.getmtime(filepath))}{ext}")

    try:
        result = subprocess.run(
            ["rsync", "-aq", "--remove-source-files", filepath, dst],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            log_event(stream["id"], "sync", f"Sync: {os.path.basename(filepath)}")
            return True
        else:
            log_event(stream["id"], "sync_error",
                      f"Sync-Fehler: {result.stderr.strip()[:100]}")
    except Exception as e:
        log_event(stream["id"], "sync_error", f"Sync-Fehler: {str(e)[:100]}")
    return False


def _convert_to_mp3(src_dir):
    """Convert any non-MP3 audio files in src_dir to MP3, then remove originals."""
    converted = 0
    for ext in ("*.aac", "*.ogg", "*.opus"):
        for filepath in glob.glob(os.path.join(src_dir, ext)):
            mp3_path = os.path.splitext(filepath)[0] + ".mp3"
            if os.path.exists(mp3_path):
                mp3_path = os.path.splitext(filepath)[0] + f"_{int(os.path.getmtime(filepath))}.mp3"
            try:
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", filepath,
                     "-c:a", "libmp3lame", "-q:a", "2", mp3_path],
                    capture_output=True, timeout=120,
                )
                if result.returncode == 0 and os.path.getsize(mp3_path) > 0:
                    os.remove(filepath)
                    converted += 1
                else:
                    # Conversion failed, remove broken output
                    if os.path.exists(mp3_path):
                        os.remove(mp3_path)
            except Exception:
                if os.path.exists(mp3_path):
                    os.remove(mp3_path)
    return converted


def sync_stream(stream):
    """Rsync one stream's recording dir to NAS. Return result dict."""
    if not is_sync_enabled():
        return {"success": True, "message": "Sync deaktiviert"}

    target = get_sync_target()
    src = os.path.join(RECORDING_BASE, stream["dest_subdir"]) + "/"
    dst = os.path.join(target, stream["dest_subdir"]) + "/"

    if not os.path.isdir(src):
        return {"success": False, "message": "Quellverzeichnis nicht vorhanden"}

    # Check NAS mount
    if not os.path.ismount(target):
        msg = f"NAS nicht gemountet: {target}"
        log_sync(stream["id"], False, msg)
        return {"success": False, "message": msg}

    os.makedirs(dst, exist_ok=True)

    # Convert non-MP3 files to MP3 before syncing
    converted = _convert_to_mp3(src)

    try:
        result = subprocess.run(
            ["rsync", "-aq", "--exclude", "incomplete",
             "--remove-source-files", src, dst],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        msg = "Rsync Timeout (300s)"
        log_sync(stream["id"], False, msg)
        return {"success": False, "message": msg}

    success = result.returncode == 0

    if success:
        # Clean up empty directories
        subprocess.run(
            ["find", src, "-mindepth", "1", "-type", "d",
             "-empty", "!", "-name", "incomplete", "-delete"],
            capture_output=True, timeout=10,
        )
        msg = "Sync erfolgreich" + (f" ({converted} Dateien konvertiert)" if converted else "")
    else:
        msg = f"Rsync Fehler (RC {result.returncode}): {result.stderr.strip()}"

    # Count synced files (rough: count audio files in target)
    files_synced = 0
    if success:
        for ext in ("*.mp3", "*.ogg", "*.aac"):
            files_synced += len(glob.glob(os.path.join(dst, "**", ext), recursive=True))

    log_sync(stream["id"], success, msg, files_synced)
    return {"success": success, "message": msg}


def get_track_history(stream, limit=100):
    """List recorded tracks on NAS for this stream."""
    dst = os.path.join(get_sync_target(), stream["dest_subdir"])
    if not os.path.isdir(dst):
        return []

    tracks = []
    for root, dirs, files in os.walk(dst):
        if "incomplete" in root:
            continue
        for name in sorted(files):
            if name.lower().endswith((".mp3", ".ogg", ".aac")):
                full = os.path.join(root, name)
                try:
                    stat = os.stat(full)
                    tracks.append({
                        "name": os.path.splitext(name)[0],
                        "filename": name,
                        "size_mb": round(stat.st_size / (1024 * 1024), 1),
                        "mtime": stat.st_mtime,
                    })
                except OSError:
                    pass

    tracks.sort(key=lambda t: t["mtime"], reverse=True)
    return tracks[:limit]
