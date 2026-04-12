import sqlite3
import hashlib
from config import DB_PATH, AUTH_PASSWORD, DEFAULT_USER_AGENT


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS streams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            dest_subdir TEXT NOT NULL UNIQUE,
            enabled INTEGER DEFAULT 1,
            min_size_mb INTEGER DEFAULT 2,
            user_agent TEXT DEFAULT 'lyrion',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE,
            timestamp TEXT DEFAULT (datetime('now')),
            success INTEGER,
            message TEXT,
            files_synced INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE,
            timestamp TEXT DEFAULT (datetime('now')),
            event_type TEXT,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS metadata_methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_host TEXT NOT NULL,
            stream_path TEXT DEFAULT '',
            method TEXT NOT NULL,
            method_url TEXT DEFAULT '',
            has_titles INTEGER DEFAULT 0,
            sample_title TEXT DEFAULT '',
            tested_at TEXT DEFAULT (datetime('now')),
            notes TEXT DEFAULT '',
            UNIQUE(stream_host, stream_path, method)
        );

        CREATE TABLE IF NOT EXISTS library_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            stream_subdir TEXT NOT NULL,
            title TEXT DEFAULT '',
            artist TEXT DEFAULT '',
            album TEXT DEFAULT '',
            genre TEXT DEFAULT '',
            bpm INTEGER DEFAULT 0,
            key TEXT DEFAULT '',
            duration_sec INTEGER DEFAULT 0,
            size_bytes INTEGER DEFAULT 0,
            mtime REAL DEFAULT 0,
            scanned_at TEXT DEFAULT (datetime('now')),
            trashed INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_library_bpm ON library_tracks(bpm);
        CREATE INDEX IF NOT EXISTS idx_library_key ON library_tracks(key);
        CREATE INDEX IF NOT EXISTS idx_library_subdir ON library_tracks(stream_subdir);

        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS playlist_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
            track_id INTEGER REFERENCES library_tracks(id) ON DELETE CASCADE,
            position INTEGER DEFAULT 0,
            added_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_track_unique
            ON playlist_tracks (playlist_id, track_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_library_filepath
            ON library_tracks (filepath);

        CREATE TABLE IF NOT EXISTS cue_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER REFERENCES library_tracks(id) ON DELETE CASCADE,
            cue_number INTEGER NOT NULL,
            position_sec REAL NOT NULL,
            UNIQUE(track_id, cue_number)
        );

        CREATE TABLE IF NOT EXISTS stream_favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_name TEXT NOT NULL,
            stream_name TEXT NOT NULL,
            stream_id INTEGER,
            cover_url TEXT DEFAULT '',
            favorited_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stream_bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            tags TEXT DEFAULT '',
            favicon TEXT DEFAULT '',
            codec TEXT DEFAULT '',
            bitrate INTEGER DEFAULT 0,
            country TEXT DEFAULT '',
            added_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # Migrate: add columns if missing
    cursor = conn.execute("PRAGMA table_info(streams)")
    columns = [row[1] for row in cursor.fetchall()]
    if "user_agent" not in columns:
        conn.execute(f"ALTER TABLE streams ADD COLUMN user_agent TEXT DEFAULT '{DEFAULT_USER_AGENT}'")
    if "record_mode" not in columns:
        conn.execute("ALTER TABLE streams ADD COLUMN record_mode TEXT DEFAULT 'streamripper'")
    if "metadata_url" not in columns:
        conn.execute("ALTER TABLE streams ADD COLUMN metadata_url TEXT DEFAULT ''")
    if "split_delay" not in columns:
        conn.execute("ALTER TABLE streams ADD COLUMN split_delay INTEGER DEFAULT 0")
    migrate_offsets = "offset_start" not in columns
    if migrate_offsets:
        conn.execute("ALTER TABLE streams ADD COLUMN offset_start INTEGER DEFAULT 0")
    if "offset_end" not in columns:
        conn.execute("ALTER TABLE streams ADD COLUMN offset_end INTEGER DEFAULT 0")
    migrate_split_offset = "split_offset" not in columns
    if migrate_split_offset:
        conn.execute("ALTER TABLE streams ADD COLUMN split_offset INTEGER DEFAULT 0")
    if "trim_start" not in columns:
        conn.execute("ALTER TABLE streams ADD COLUMN trim_start INTEGER DEFAULT 0")
    if "trim_end" not in columns:
        conn.execute("ALTER TABLE streams ADD COLUMN trim_end INTEGER DEFAULT 0")
    if "skip_words" not in columns:
        conn.execute("ALTER TABLE streams ADD COLUMN skip_words TEXT DEFAULT ''")
    if "dl_fallback" not in columns:
        conn.execute("ALTER TABLE streams ADD COLUMN dl_fallback INTEGER DEFAULT 0")
    # Migrate library_tracks: add trashed column
    cursor = conn.execute("PRAGMA table_info(library_tracks)")
    lib_columns = [row[1] for row in cursor.fetchall()]
    if "trashed" not in lib_columns:
        conn.execute("ALTER TABLE library_tracks ADD COLUMN trashed INTEGER DEFAULT 0")
    conn.commit()

    # Migrate library_tracks: add rating column
    cursor = conn.execute("PRAGMA table_info(library_tracks)")
    lib_columns = [row[1] for row in cursor.fetchall()]
    if "rating" not in lib_columns:
        conn.execute("ALTER TABLE library_tracks ADD COLUMN rating INTEGER DEFAULT 0")
    if "favorited" not in lib_columns:
        conn.execute("ALTER TABLE library_tracks ADD COLUMN favorited INTEGER DEFAULT 0")
        conn.commit()
    # Migrate library_tracks: add loudness_lufs column
    if "loudness_lufs" not in lib_columns:
        conn.execute("ALTER TABLE library_tracks ADD COLUMN loudness_lufs REAL DEFAULT NULL")
        conn.commit()
    # Migrate library_tracks: add bitrate column
    if "bitrate" not in lib_columns:
        conn.execute("ALTER TABLE library_tracks ADD COLUMN bitrate INTEGER DEFAULT NULL")
        conn.commit()
    # Migrate library_tracks: add tag_status column
    cursor = conn.execute("PRAGMA table_info(library_tracks)")
    lib_columns2 = [row[1] for row in cursor.fetchall()]
    if "tag_status" not in lib_columns2:
        conn.execute("ALTER TABLE library_tracks ADD COLUMN tag_status TEXT DEFAULT NULL")
        conn.commit()
    if "waveform" not in lib_columns2:
        conn.execute("ALTER TABLE library_tracks ADD COLUMN waveform TEXT DEFAULT NULL")
        conn.commit()
    if "unusable" not in lib_columns2:
        conn.execute("ALTER TABLE library_tracks ADD COLUMN unusable INTEGER DEFAULT 0")
        conn.commit()

    # Migrate playlists: add color column
    cursor = conn.execute("PRAGMA table_info(playlists)")
    pl_columns = [row[1] for row in cursor.fetchall()]
    if "color" not in pl_columns:
        conn.execute("ALTER TABLE playlists ADD COLUMN color TEXT DEFAULT NULL")
        conn.commit()

    # Migrate split_delay -> offset_end for existing streams
    if migrate_offsets:
        conn.execute("UPDATE streams SET offset_end = split_delay WHERE split_delay > 0")
        conn.commit()
    # Migrate offset_start/offset_end -> split_offset
    if migrate_split_offset:
        # offset_end > 0 means metadata arrives early -> positive split_offset
        conn.execute("UPDATE streams SET split_offset = offset_end WHERE offset_end > 0")
        # offset_start < 0 means metadata arrives late -> negative split_offset
        conn.execute("UPDATE streams SET split_offset = offset_start WHERE offset_start < 0 AND split_offset = 0")
        conn.commit()

    # Set default password
    pw_hash = hashlib.sha256(AUTH_PASSWORD.encode()).hexdigest()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("auth_password_hash", pw_hash),
    )
    conn.commit()
    conn.close()


def get_all_streams():
    conn = get_db()
    rows = conn.execute("SELECT * FROM streams ORDER BY name").fetchall()
    conn.close()
    return rows


def get_stream(stream_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    conn.close()
    return row


def create_stream(name, url, dest_subdir, min_size_mb=2, user_agent=DEFAULT_USER_AGENT,
                   record_mode="streamripper", metadata_url="", split_offset=0,
                   trim_start=0, trim_end=0, skip_words="", dl_fallback=0):
    conn = get_db()
    conn.execute(
        "INSERT INTO streams (name, url, dest_subdir, min_size_mb, user_agent, record_mode, metadata_url, split_offset, trim_start, trim_end, skip_words, dl_fallback) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, url, dest_subdir, min_size_mb, user_agent, record_mode, metadata_url, split_offset, trim_start, trim_end, skip_words, dl_fallback),
    )
    conn.commit()
    stream_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return stream_id


def update_stream(stream_id, name, url, dest_subdir, min_size_mb=2, user_agent=DEFAULT_USER_AGENT,
                  record_mode="streamripper", metadata_url="", split_offset=0,
                  trim_start=0, trim_end=0, skip_words="", dl_fallback=0):
    conn = get_db()
    conn.execute(
        "UPDATE streams SET name=?, url=?, dest_subdir=?, min_size_mb=?, user_agent=?, record_mode=?, metadata_url=?, split_offset=?, trim_start=?, trim_end=?, skip_words=?, dl_fallback=? WHERE id=?",
        (name, url, dest_subdir, min_size_mb, user_agent, record_mode, metadata_url, split_offset, trim_start, trim_end, skip_words, dl_fallback, stream_id),
    )
    conn.commit()
    conn.close()


def delete_stream(stream_id):
    conn = get_db()
    conn.execute("DELETE FROM streams WHERE id = ?", (stream_id,))
    conn.commit()
    conn.close()


def log_sync(stream_id, success, message, files_synced=0):
    conn = get_db()
    conn.execute(
        "INSERT INTO sync_log (stream_id, success, message, files_synced) VALUES (?, ?, ?, ?)",
        (stream_id, 1 if success else 0, message, files_synced),
    )
    conn.commit()
    conn.close()


def log_event(stream_id, event_type, message):
    conn = get_db()
    conn.execute(
        "INSERT INTO events (stream_id, event_type, message) VALUES (?, ?, ?)",
        (stream_id, event_type, message),
    )
    conn.commit()
    conn.close()


def get_sync_logs(stream_id=None, limit=100):
    conn = get_db()
    if stream_id:
        rows = conn.execute(
            "SELECT sl.*, s.name as stream_name FROM sync_log sl "
            "JOIN streams s ON sl.stream_id = s.id "
            "WHERE sl.stream_id = ? ORDER BY sl.timestamp DESC LIMIT ?",
            (stream_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT sl.*, s.name as stream_name FROM sync_log sl "
            "JOIN streams s ON sl.stream_id = s.id "
            "ORDER BY sl.timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return rows


def get_events(stream_id=None, limit=50):
    conn = get_db()
    if stream_id:
        rows = conn.execute(
            "SELECT * FROM events WHERE stream_id = ? ORDER BY timestamp DESC LIMIT ?",
            (stream_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT e.*, s.name as stream_name FROM events e "
            "JOIN streams s ON e.stream_id = s.id "
            "ORDER BY e.timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return rows


def get_track_stats(stream_id):
    """Return recorded/skipped counts from events table.
    Skip-word entries are excluded (jingles, ads, not real songs)."""
    conn = get_db()
    row = conn.execute(
        """SELECT
            COUNT(CASE WHEN message LIKE 'Neuer Track:%' OR message LIKE 'Neu:%' THEN 1 END) as recorded,
            COUNT(CASE WHEN message LIKE 'Übersprungen (existiert)%' OR message LIKE 'Bekannt%' THEN 1 END) as skipped
        FROM events
        WHERE stream_id = ? AND event_type = 'track'""",
        (stream_id,),
    ).fetchone()
    conn.close()
    recorded = row["recorded"]
    skipped = row["skipped"]
    total = recorded + skipped
    return {
        "recorded": recorded,
        "skipped": skipped,
        "rec_pct": round(recorded / total * 100) if total > 0 else 0,
    }


def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


# === Library / Playlist helpers ===

def upsert_library_track(data_dict):
    """Insert a new track or revive/update an existing one (by filepath).
    NEVER deletes-and-reinserts — the row's id is preserved so that
    playlist memberships, cue points, ratings, and favorites survive."""
    conn = get_db()
    filepath = data_dict.get("filepath", "")

    existing = conn.execute(
        "SELECT id, favorited, trashed FROM library_tracks WHERE filepath = ?",
        (filepath,),
    ).fetchone()

    if existing:
        # Track already in DB — revive if trashed, update mtime/size
        updates = {
            "stream_subdir": data_dict.get("stream_subdir", ""),
            "size_bytes": data_dict.get("size_bytes", 0),
            "mtime": data_dict.get("mtime", 0),
            "trashed": 0,
            "scanned_at": "datetime('now')",
        }
        conn.execute(
            """UPDATE library_tracks SET
                stream_subdir = ?, size_bytes = ?, mtime = ?,
                trashed = 0, scanned_at = datetime('now')
            WHERE id = ?""",
            (updates["stream_subdir"], updates["size_bytes"],
             updates["mtime"], existing["id"]),
        )
    else:
        # Genuinely new file — insert with all fields
        cols = ["filepath", "filename", "stream_subdir", "title", "artist",
                "album", "genre", "bpm", "key", "duration_sec",
                "size_bytes", "mtime"]
        vals = [data_dict.get(c, "") for c in cols]

        # Check if favorited as a stream favorite
        favorited = 0
        title = data_dict.get("title", "")
        artist = data_dict.get("artist", "")
        track_name = (artist + " - " + title) if artist else title
        if track_name:
            fav = conn.execute(
                "SELECT id FROM stream_favorites WHERE track_name = ?",
                (track_name,),
            ).fetchone()
            if fav:
                favorited = 1

        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        conn.execute(
            f"INSERT INTO library_tracks ({col_names}, favorited, scanned_at) "
            f"VALUES ({placeholders}, ?, datetime('now'))",
            vals + [favorited],
        )

    conn.commit()
    conn.close()


# Camelot wheel mapping for sort-by-key
_CAMELOT_MAP = {
    # Minor keys -> xA
    "Abm": "1A",  "G#m": "1A",
    "Ebm": "2A",  "D#m": "2A",
    "Bbm": "3A",  "A#m": "3A",
    "Fm":  "4A",
    "Cm":  "5A",
    "Gm":  "6A",
    "Dm":  "7A",
    "Am":  "8A",
    "Em":  "9A",
    "Bm":  "10A",
    "F#m": "11A", "Gbm": "11A",
    "C#m": "12A", "Dbm": "12A",
    # Major keys -> xB
    "B":   "1B",  "Cb":  "1B",
    "F#":  "2B",  "Gb":  "2B",
    "C#":  "3B",  "Db":  "3B",
    "Ab":  "4B",  "G#":  "4B",
    "Eb":  "5B",  "D#":  "5B",
    "Bb":  "6B",  "A#":  "6B",
    "F":   "7B",
    "C":   "8B",
    "G":   "9B",
    "D":   "10B",
    "A":   "11B",
    "E":   "12B",
}


def _build_camelot_case():
    """Build a SQL CASE expression mapping key values to Camelot sort numbers."""
    # We map each Camelot position to a sortable integer: 1A=1, 1B=2, 2A=3, 2B=4, ...
    camelot_sort = {}
    for key_name, camelot in _CAMELOT_MAP.items():
        num = int(camelot[:-1])
        letter = camelot[-1]
        sort_val = (num - 1) * 2 + (0 if letter == "A" else 1) + 1
        camelot_sort[key_name] = sort_val

    whens = " ".join(
        f"WHEN key = '{k}' THEN {v}" for k, v in camelot_sort.items()
    )
    return f"CASE {whens} ELSE 999 END"


def get_library_tracks(page=1, per_page=200, sort="title", order="asc",
                       stream=None, search=None, bpm_min=None, bpm_max=None,
                       key_filter=None):
    """Paginated query with sorting/filtering. Returns (tracks_list, total_count)."""
    conn = get_db()
    where_clauses = ["trashed = 0"]
    params = []

    if stream:
        where_clauses.append("stream_subdir = ?")
        params.append(stream)
    if search:
        where_clauses.append("(title LIKE ? OR artist LIKE ? OR filename LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if bpm_min is not None:
        where_clauses.append("bpm >= ?")
        params.append(int(bpm_min))
    if bpm_max is not None:
        where_clauses.append("bpm <= ?")
        params.append(int(bpm_max))
    if key_filter:
        where_clauses.append("key = ?")
        params.append(key_filter)

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Count
    count_row = conn.execute(
        f"SELECT COUNT(*) as cnt FROM library_tracks{where_sql}", params
    ).fetchone()
    total = count_row["cnt"]

    # Sort
    if order.lower() not in ("asc", "desc"):
        order = "asc"

    if sort == "camelot":
        camelot_case = _build_camelot_case()
        order_sql = f"ORDER BY {camelot_case} {order}, bpm {order}"
    elif sort in ("title", "artist", "album", "genre", "filename", "stream_subdir"):
        order_sql = f"ORDER BY LOWER({sort}) {order}"
    elif sort in ("bpm", "key", "duration_sec", "size_bytes", "mtime", "rating", "favorited", "bitrate", "unusable"):
        order_sql = f"ORDER BY {sort} {order}"
    else:
        order_sql = f"ORDER BY LOWER(title) {order}"

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT lt.*, (SELECT GROUP_CONCAT(cue_number) FROM cue_points WHERE track_id = lt.id) as cue_nums FROM library_tracks lt{where_sql} {order_sql} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def get_library_track(track_id):
    """Single track by ID."""
    conn = get_db()
    row = conn.execute("SELECT * FROM library_tracks WHERE id = ?", (track_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_library_stats():
    """Returns dict with total_tracks, tracks_with_bpm, tracks_with_key, per_stream counts."""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as cnt FROM library_tracks").fetchone()["cnt"]
    with_bpm = conn.execute(
        "SELECT COUNT(*) as cnt FROM library_tracks WHERE bpm > 0"
    ).fetchone()["cnt"]
    with_key = conn.execute(
        "SELECT COUNT(*) as cnt FROM library_tracks WHERE key != '' AND key IS NOT NULL"
    ).fetchone()["cnt"]
    per_stream = conn.execute(
        "SELECT stream_subdir, COUNT(*) as cnt FROM library_tracks GROUP BY stream_subdir ORDER BY cnt DESC"
    ).fetchall()
    conn.close()
    return {
        "total_tracks": total,
        "tracks_with_bpm": with_bpm,
        "tracks_with_key": with_key,
        "per_stream": {row["stream_subdir"]: row["cnt"] for row in per_stream},
    }


def get_stream_subdirs():
    """Distinct stream_subdir values."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT stream_subdir FROM library_tracks ORDER BY stream_subdir"
    ).fetchall()
    conn.close()
    return [row["stream_subdir"] for row in rows]


def create_playlist(name):
    """Creates playlist, returns id."""
    conn = get_db()
    conn.execute("INSERT INTO playlists (name) VALUES (?)", (name,))
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return pid


def get_all_playlists():
    """List of playlists with track counts."""
    conn = get_db()
    rows = conn.execute(
        "SELECT p.*, COUNT(pt.id) as track_count "
        "FROM playlists p LEFT JOIN playlist_tracks pt ON p.id = pt.playlist_id "
        "GROUP BY p.id ORDER BY p.name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_playlist(playlist_id):
    """Single playlist by ID."""
    conn = get_db()
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_playlist_tracks(playlist_id):
    """Tracks in playlist ordered by position, JOINed with library_tracks."""
    conn = get_db()
    rows = conn.execute(
        "SELECT lt.*, pt.position, pt.id as playlist_track_id, "
        "(SELECT GROUP_CONCAT(cue_number) FROM cue_points WHERE track_id = lt.id) as cue_nums "
        "FROM playlist_tracks pt "
        "JOIN library_tracks lt ON pt.track_id = lt.id "
        "WHERE pt.playlist_id = ? AND lt.trashed = 0 ORDER BY pt.position",
        (playlist_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_to_playlist(playlist_id, track_ids):
    """Adds tracks to playlist, sets position = max+1, +2, ..."""
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) as max_pos FROM playlist_tracks WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    pos = row["max_pos"]
    for tid in track_ids:
        pos += 1
        conn.execute(
            "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
            (playlist_id, tid, pos),
        )
    conn.execute(
        "UPDATE playlists SET updated_at = datetime('now') WHERE id = ?",
        (playlist_id,),
    )
    conn.commit()
    conn.close()


def remove_from_playlist(playlist_id, track_id):
    """Removes track and reorders positions."""
    conn = get_db()
    conn.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?",
        (playlist_id, track_id),
    )
    # Reorder remaining positions
    rows = conn.execute(
        "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
        (playlist_id,),
    ).fetchall()
    for i, row in enumerate(rows, 1):
        conn.execute("UPDATE playlist_tracks SET position = ? WHERE id = ?", (i, row["id"]))
    conn.execute(
        "UPDATE playlists SET updated_at = datetime('now') WHERE id = ?",
        (playlist_id,),
    )
    conn.commit()
    conn.close()


def reorder_playlist(playlist_id, track_ids_ordered):
    """Updates positions based on the ordered list of track IDs."""
    conn = get_db()
    for pos, tid in enumerate(track_ids_ordered, 1):
        conn.execute(
            "UPDATE playlist_tracks SET position = ? WHERE playlist_id = ? AND track_id = ?",
            (pos, playlist_id, tid),
        )
    conn.execute(
        "UPDATE playlists SET updated_at = datetime('now') WHERE id = ?",
        (playlist_id,),
    )
    conn.commit()
    conn.close()


def delete_playlist(playlist_id):
    """Deletes playlist and its tracks (cascade)."""
    conn = get_db()
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    conn.commit()
    conn.close()


def rename_playlist(playlist_id, new_name):
    """Renames a playlist."""
    conn = get_db()
    conn.execute(
        "UPDATE playlists SET name = ?, updated_at = datetime('now') WHERE id = ?",
        (new_name, playlist_id),
    )
    conn.commit()
    conn.close()


def delete_library_track_by_path(filepath):
    """Soft-delete a library track by filepath (mark trashed, preserve ID).
    Playlist/cue links survive so the track can be revived later."""
    conn = get_db()
    conn.execute("UPDATE library_tracks SET trashed = 1 WHERE filepath = ?", (filepath,))
    conn.commit()
    conn.close()


def trash_library_track(track_id):
    """Mark track as trashed (file deleted, kept in DB to prevent re-download)."""
    conn = get_db()
    conn.execute("UPDATE library_tracks SET trashed = 1 WHERE id = ?", (track_id,))
    conn.commit()
    conn.close()


def delete_library_track(track_id):
    """Fully delete track from DB (allows re-download)."""
    conn = get_db()
    conn.execute("DELETE FROM playlist_tracks WHERE track_id = ?", (track_id,))
    conn.execute("DELETE FROM cue_points WHERE track_id = ?", (track_id,))
    conn.execute("DELETE FROM library_tracks WHERE id = ?", (track_id,))
    conn.commit()
    conn.close()


def set_track_rating(track_id, rating):
    """Set rating (0-5) for a library track."""
    conn = get_db()
    conn.execute("UPDATE library_tracks SET rating = ? WHERE id = ?", (max(0, min(5, rating)), track_id))
    conn.commit()
    conn.close()


def toggle_unusable(track_id):
    """Toggle unusable status. Returns new state (0 or 1)."""
    conn = get_db()
    row = conn.execute("SELECT unusable FROM library_tracks WHERE id = ?", (track_id,)).fetchone()
    if not row:
        conn.close()
        return 0
    new_val = 0 if row["unusable"] else 1
    conn.execute("UPDATE library_tracks SET unusable = ? WHERE id = ?", (new_val, track_id))
    conn.commit()
    conn.close()
    return new_val


def toggle_favorite(track_id):
    """Toggle favorite status. Returns new state (0 or 1)."""
    conn = get_db()
    row = conn.execute("SELECT favorited FROM library_tracks WHERE id = ?", (track_id,)).fetchone()
    if not row:
        conn.close()
        return 0
    new_val = 0 if row["favorited"] else 1
    conn.execute("UPDATE library_tracks SET favorited = ? WHERE id = ?", (new_val, track_id))
    conn.commit()
    conn.close()
    return new_val


def get_stream_bookmarks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM stream_bookmarks ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_stream_bookmark(name, url, tags="", favicon="", codec="", bitrate=0, country=""):
    conn = get_db()
    conn.execute(
        "INSERT INTO stream_bookmarks (name, url, tags, favicon, codec, bitrate, country) VALUES (?,?,?,?,?,?,?)",
        (name, url, tags, favicon, codec, bitrate, country))
    conn.commit()
    conn.close()


def delete_stream_bookmark(bookmark_id):
    conn = get_db()
    conn.execute("DELETE FROM stream_bookmarks WHERE id = ?", (bookmark_id,))
    conn.commit()
    conn.close()


def get_cue_points(track_id):
    """Get cue points for a track as {cue_number: position_sec}."""
    conn = get_db()
    rows = conn.execute(
        "SELECT cue_number, position_sec FROM cue_points WHERE track_id = ?",
        (track_id,),
    ).fetchall()
    conn.close()
    return {str(r["cue_number"]): r["position_sec"] for r in rows}


def set_cue_points(track_id, cues):
    """Set cue points for a track. cues = {cue_number: position_sec}."""
    conn = get_db()
    conn.execute("DELETE FROM cue_points WHERE track_id = ?", (track_id,))
    for num, pos in cues.items():
        conn.execute(
            "INSERT INTO cue_points (track_id, cue_number, position_sec) VALUES (?, ?, ?)",
            (track_id, int(num), float(pos)),
        )
    conn.commit()
    conn.close()


# --- Stream favorites (live listening) ---

def add_stream_favorite(track_name, stream_name, stream_id=None, cover_url=""):
    """Add a favorite from live listening. Returns the new row id."""
    conn = get_db()
    conn.execute(
        "INSERT INTO stream_favorites (track_name, stream_name, stream_id, cover_url) VALUES (?, ?, ?, ?)",
        (track_name, stream_name, stream_id, cover_url),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def remove_stream_favorite(fav_id):
    conn = get_db()
    conn.execute("DELETE FROM stream_favorites WHERE id = ?", (fav_id,))
    conn.commit()
    conn.close()


def is_stream_favorite(track_name, stream_name):
    """Check if a track is already favorited. Returns row id or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM stream_favorites WHERE track_name = ? AND stream_name = ?",
        (track_name, stream_name),
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def get_stream_favorites(sort="newest", limit=200):
    """Get all stream favorites. sort: newest, oldest, stream, track."""
    order = {
        "newest": "favorited_at DESC",
        "oldest": "favorited_at ASC",
        "stream": "stream_name ASC, favorited_at DESC",
        "track": "track_name ASC",
    }.get(sort, "favorited_at DESC")
    conn = get_db()
    rows = conn.execute(
        f"SELECT * FROM stream_favorites ORDER BY {order} LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
