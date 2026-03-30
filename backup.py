"""Database backup and restore for Streampeg."""

import os
import re
import shutil
import sqlite3
import glob
import logging
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamripper-ui.db")
LOCAL_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
NAS_BACKUP_DIR = "/mnt/unraid-streams/backups"
MAX_BACKUPS = 10
_FILENAME_PATTERN = re.compile(r"^streamripper-ui_\d{8}_\d{6}\.db$")


def create_backup():
    """Create a backup of the DB in both local and NAS directories. Returns filename or None."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"streamripper-ui_{ts}.db"

    for backup_dir in (LOCAL_BACKUP_DIR, NAS_BACKUP_DIR):
        try:
            os.makedirs(backup_dir, exist_ok=True)
            dest = os.path.join(backup_dir, filename)
            shutil.copy2(DB_PATH, dest)
            _rotate_backups(backup_dir)
            log.info("DB backup created: %s", dest)
        except Exception as e:
            log.warning("DB backup failed for %s: %s", backup_dir, e)

    return filename


def create_backup_if_needed():
    """Create a daily backup, but only between 2:00-5:00 at night and max once per day."""
    now = datetime.now()
    if not (2 <= now.hour < 5):
        return None
    # Check if a backup from today already exists
    today = now.strftime("%Y%m%d")
    existing = glob.glob(os.path.join(LOCAL_BACKUP_DIR, f"streamripper-ui_{today}_*.db"))
    if existing:
        return None
    log.info("Creating daily automatic backup")
    return create_backup()


def _rotate_backups(backup_dir):
    """Keep only MAX_BACKUPS newest backups in a directory."""
    files = sorted(glob.glob(os.path.join(backup_dir, "streamripper-ui_*.db")))
    while len(files) > MAX_BACKUPS:
        try:
            os.remove(files.pop(0))
        except OSError:
            pass


def _get_backup_info(filepath):
    """Extract info from a backup file (fast, no DB access)."""
    filename = os.path.basename(filepath)
    m = re.match(r"streamripper-ui_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.db", filename)
    if not m:
        return None
    date_str = f"{m.group(3)}.{m.group(2)}.{m.group(1)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
    size = os.path.getsize(filepath)
    return {
        "filename": filename,
        "date": date_str,
        "size_bytes": size,
        "size_mb": round(size / 1024 / 1024, 1),
        "path": filepath,
    }


def list_backups():
    """List all available backups from both locations, newest first."""
    backups = []
    seen = set()

    for backup_dir, location in ((LOCAL_BACKUP_DIR, "local"), (NAS_BACKUP_DIR, "nas")):
        if not os.path.isdir(backup_dir):
            continue
        for filepath in glob.glob(os.path.join(backup_dir, "streamripper-ui_*.db")):
            filename = os.path.basename(filepath)
            if not _FILENAME_PATTERN.match(filename):
                continue
            info = _get_backup_info(filepath)
            if info:
                info["location"] = location
                # If same filename already seen (from other location), add as separate entry
                backups.append(info)

    # Sort newest first
    backups.sort(key=lambda b: b["filename"], reverse=True)
    return backups


def restore_backup(filename, location):
    """Restore a backup. Returns True on success."""
    if not _FILENAME_PATTERN.match(filename):
        return False

    backup_dir = LOCAL_BACKUP_DIR if location == "local" else NAS_BACKUP_DIR
    src = os.path.join(backup_dir, filename)

    if not os.path.isfile(src):
        return False

    try:
        # Backup current DB before restoring
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        pre_restore = os.path.join(LOCAL_BACKUP_DIR, f"streamripper-ui_{ts}_pre_restore.db")
        os.makedirs(LOCAL_BACKUP_DIR, exist_ok=True)
        shutil.copy2(DB_PATH, pre_restore)

        shutil.copy2(src, DB_PATH)
        log.info("DB restored from: %s", src)
        return True
    except Exception as e:
        log.error("DB restore failed: %s", e)
        return False
