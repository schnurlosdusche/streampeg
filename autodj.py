"""Auto-DJ server-side module for Streampeg.
Provides BPM/Key-compatible track selection as fallback when client-side
_libTracks is not available."""

import re
import db

_CAMELOT = {
    "Abm": "1A", "G#m": "1A", "Ebm": "2A", "D#m": "2A", "Bbm": "3A", "A#m": "3A",
    "Fm": "4A", "Cm": "5A", "Gm": "6A", "Dm": "7A", "Am": "8A", "Em": "9A",
    "Bm": "10A", "F#m": "11A", "Gbm": "11A", "C#m": "12A", "Dbm": "12A",
    "B": "1B", "Cb": "1B", "F#": "2B", "Gb": "2B", "C#": "3B", "Db": "3B",
    "Ab": "4B", "G#": "4B", "Eb": "5B", "D#": "5B", "Bb": "6B", "A#": "6B",
    "F": "7B", "C": "8B", "G": "9B", "D": "10B", "A": "11B", "E": "12B",
}


def _parse_camelot(code):
    if not code:
        return None
    m = re.match(r"^(\d+)([AB])$", code)
    return (int(m.group(1)), m.group(2)) if m else None


def _score_key(key1, key2):
    cam1 = _CAMELOT.get(key1)
    cam2 = _CAMELOT.get(key2)
    if not cam1 or not cam2:
        return 25
    p1 = _parse_camelot(cam1)
    p2 = _parse_camelot(cam2)
    if not p1 or not p2:
        return 25
    if p1 == p2:
        return 50
    if p1[1] == p2[1]:
        diff = abs(p1[0] - p2[0])
        if diff == 1 or diff == 11:
            return 40
        if diff == 2 or diff == 10:
            return 20
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return 35
    return 0


def _score_bpm(bpm1, bpm2):
    if not bpm1 or not bpm2 or bpm1 <= 0 or bpm2 <= 0:
        return 25
    ratio = bpm2 / bpm1
    if 0.95 <= ratio <= 1.05:
        return 50 - abs(1 - ratio) * 1000
    if 0.475 <= ratio <= 0.525:
        return 35
    if 1.9 <= ratio <= 2.1:
        return 35
    return 0


def get_next_track(current_track_id, playlist_id=None, stream=None):
    """Find the best next track by BPM/Key compatibility.
    Returns dict with track info or None."""
    conn = db.get_db()

    # Get current track info
    current = conn.execute(
        "SELECT bpm, key FROM library_tracks WHERE id = ?", (current_track_id,)
    ).fetchone()
    if not current:
        conn.close()
        return None

    cur_bpm = current["bpm"] or 0
    cur_key = current["key"] or ""

    # Get candidates
    if playlist_id:
        rows = conn.execute(
            "SELECT lt.id, lt.title, lt.artist, lt.bpm, lt.key, lt.stream_subdir "
            "FROM playlist_tracks pt JOIN library_tracks lt ON pt.track_id = lt.id "
            "WHERE pt.playlist_id = ? AND lt.trashed = 0 AND lt.id != ?",
            (playlist_id, current_track_id),
        ).fetchall()
    elif stream:
        rows = conn.execute(
            "SELECT id, title, artist, bpm, key, stream_subdir FROM library_tracks "
            "WHERE trashed = 0 AND stream_subdir = ? AND id != ?",
            (stream, current_track_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, artist, bpm, key, stream_subdir FROM library_tracks "
            "WHERE trashed = 0 AND id != ? LIMIT 500",
            (current_track_id,),
        ).fetchall()
    conn.close()

    if not rows:
        return None

    # Score candidates
    scored = []
    for r in rows:
        score = _score_bpm(cur_bpm, r["bpm"]) + _score_key(cur_key, r["key"])
        scored.append((score, dict(r)))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Pick randomly from top candidates
    best = scored[0][0]
    top = [s for s in scored if s[0] >= best - 5]

    import random
    chosen = random.choice(top)[1]
    return chosen
