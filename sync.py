import os
import glob
import subprocess
from config import RECORDING_BASE, SMB_TARGET
from db import log_sync


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
    src = os.path.join(RECORDING_BASE, stream["dest_subdir"]) + "/"
    dst = os.path.join(SMB_TARGET, stream["dest_subdir"]) + "/"

    if not os.path.isdir(src):
        return {"success": False, "message": "Quellverzeichnis nicht vorhanden"}

    # Check NAS mount
    if not os.path.ismount(SMB_TARGET):
        msg = f"NAS nicht gemountet: {SMB_TARGET}"
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
    dst = os.path.join(SMB_TARGET, stream["dest_subdir"])
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
