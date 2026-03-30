#!/usr/bin/env python3
"""
Import Streampeg cue points into Mixxx database.

Usage:
    python3 import_cues_to_mixxx.py playlist.xml [--mixxx-db PATH] [--dry-run] [--undo]

Arguments:
    playlist.xml   - Exported XML from Streampeg (Mixxx XML export)
    --mixxx-db     - Path to mixxxdb.sqlite (default: auto-detect)
    --dry-run      - Show what would be done without writing
    --undo         - Restore backup (undo last import)

The script:
1. Creates a backup of mixxxdb.sqlite before any changes
2. Reads cue points from the Streampeg XML
3. Matches tracks by filename in the Mixxx library
4. Inserts cue points into Mixxx's cue table
5. Can undo via --undo flag (restores backup)

Mixxx must be CLOSED while running this script.
"""

import os
import sys
import shutil
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime

# Default Mixxx DB locations per platform
def _find_mixxx_db():
    candidates = []
    home = os.path.expanduser("~")
    # macOS
    candidates.append(os.path.join(home, "Library", "Containers", "org.mixxx.mixxx", "Data", "Library", "Application Support", "Mixxx", "mixxxdb.sqlite"))
    candidates.append(os.path.join(home, "Library", "Application Support", "Mixxx", "mixxxdb.sqlite"))
    # Linux
    candidates.append(os.path.join(home, ".mixxx", "mixxxdb.sqlite"))
    # Windows
    candidates.append(os.path.join(home, "AppData", "Local", "Mixxx", "mixxxdb.sqlite"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _backup_db(db_path):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = db_path + f".backup_{ts}"
    shutil.copy2(db_path, backup)
    # Also keep a "last" backup for easy undo
    shutil.copy2(db_path, db_path + ".backup_last")
    print(f"Backup created: {backup}")
    return backup


def _restore_backup(db_path):
    backup = db_path + ".backup_last"
    if not os.path.isfile(backup):
        print("No backup found to restore.")
        return False
    shutil.copy2(backup, db_path)
    print(f"Restored from: {backup}")
    return True


def _parse_xml(xml_path):
    """Parse Streampeg playlist XML and return list of tracks with cues."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    tracks = []
    for track_el in root.findall("Track"):
        location = track_el.findtext("Location", "")
        title = track_el.findtext("Title", "")
        artist = track_el.findtext("Artist", "")
        cues = []
        cue_el = track_el.find("CuePoints")
        if cue_el is not None:
            for cue in cue_el.findall("Cue"):
                num = int(cue.get("number", 0))
                pos = float(cue.get("position", 0))
                if num > 0 and pos >= 0:
                    cues.append((num, pos))
        if cues:
            tracks.append({
                "location": location,
                "filename": os.path.basename(location),
                "title": title,
                "artist": artist,
                "cues": cues,
            })
    return tracks


def _find_mixxx_track(conn, filename):
    """Find a track in Mixxx library by filename."""
    row = conn.execute(
        "SELECT id FROM library WHERE location LIKE ?",
        (f"%/{filename}",)
    ).fetchone()
    if row:
        return row[0]
    # Try with backslash (Windows)
    row = conn.execute(
        "SELECT id FROM library WHERE location LIKE ?",
        (f"%\\{filename}",)
    ).fetchone()
    return row[0] if row else None


# Mixxx cue types
CUE_TYPE_HOTCUE = 1  # Hot cue (numbered cue point)

# Mixxx hot cue colors (from Mixxx source, first 8)
HOTCUE_COLORS = [
    0xC50A08,  # red
    0x32BE44,  # green
    0x0044FF,  # blue
    0xF8D200,  # yellow
    0x8B00CC,  # purple
    0x00CCCC,  # cyan
    0xF2650F,  # orange
    0xFF00FF,  # magenta
]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Import Streampeg cue points into Mixxx")
    parser.add_argument("xml_file", nargs="?", help="Streampeg playlist XML file")
    parser.add_argument("--mixxx-db", help="Path to mixxxdb.sqlite")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    parser.add_argument("--undo", action="store_true", help="Restore last backup")
    args = parser.parse_args()

    # Find Mixxx DB
    db_path = args.mixxx_db or _find_mixxx_db()
    if not db_path or not os.path.isfile(db_path):
        print("Mixxx database not found. Use --mixxx-db to specify path.")
        print("Looked in:", db_path or "default locations")
        sys.exit(1)
    print(f"Mixxx DB: {db_path}")

    # Undo mode
    if args.undo:
        if _restore_backup(db_path):
            print("Done. Start Mixxx to verify.")
        sys.exit(0)

    if not args.xml_file:
        parser.error("XML file required (or use --undo)")

    if not os.path.isfile(args.xml_file):
        print(f"File not found: {args.xml_file}")
        sys.exit(1)

    # Parse XML
    tracks = _parse_xml(args.xml_file)
    if not tracks:
        print("No tracks with cue points found in XML.")
        sys.exit(0)
    print(f"Found {len(tracks)} tracks with cue points in XML")

    # Backup
    if not args.dry_run:
        _backup_db(db_path)

    # Connect to Mixxx DB
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    matched = 0
    cues_added = 0

    for t in tracks:
        track_id = _find_mixxx_track(conn, t["filename"])
        if not track_id:
            print(f"  NOT FOUND in Mixxx: {t['artist']} - {t['title']} ({t['filename']})")
            continue

        matched += 1
        for num, pos in t["cues"]:
            hotcue = num - 1  # Mixxx uses 0-based hotcue numbers
            # Convert seconds to samples (Mixxx stores position in samples at 2x rate)
            # Mixxx 2.3+ uses seconds directly in the 'position' column
            # Check which schema version
            if args.dry_run:
                print(f"  Would add cue {num} at {pos:.2f}s to: {t['artist']} - {t['title']}")
                cues_added += 1
                continue

            # Remove existing hotcue with same number for this track
            conn.execute(
                "DELETE FROM cues WHERE track_id = ? AND type = ? AND hotcue = ?",
                (track_id, CUE_TYPE_HOTCUE, hotcue)
            )
            # Insert new cue
            color = HOTCUE_COLORS[hotcue % len(HOTCUE_COLORS)]
            conn.execute(
                "INSERT INTO cues (track_id, type, position, hotcue, color) VALUES (?, ?, ?, ?, ?)",
                (track_id, CUE_TYPE_HOTCUE, pos, hotcue, color)
            )
            cues_added += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    action = "Would add" if args.dry_run else "Added"
    print(f"\n{action} {cues_added} cue points for {matched} tracks")
    if not args.dry_run:
        print("Start Mixxx to verify. Use --undo to revert if needed.")


if __name__ == "__main__":
    main()
