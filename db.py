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
                   record_mode="streamripper", metadata_url="", split_offset=0):
    conn = get_db()
    conn.execute(
        "INSERT INTO streams (name, url, dest_subdir, min_size_mb, user_agent, record_mode, metadata_url, split_offset) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, url, dest_subdir, min_size_mb, user_agent, record_mode, metadata_url, split_offset),
    )
    conn.commit()
    stream_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return stream_id


def update_stream(stream_id, name, url, dest_subdir, min_size_mb=2, user_agent=DEFAULT_USER_AGENT,
                  record_mode="streamripper", metadata_url="", split_offset=0):
    conn = get_db()
    conn.execute(
        "UPDATE streams SET name=?, url=?, dest_subdir=?, min_size_mb=?, user_agent=?, record_mode=?, metadata_url=?, split_offset=? WHERE id=?",
        (name, url, dest_subdir, min_size_mb, user_agent, record_mode, metadata_url, split_offset, stream_id),
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


def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None
