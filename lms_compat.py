"""
LMS Compatibility Layer — minimal LMS CLI/JSON-RPC/CometD emulation.

Allows Jivelite (Max2Play touchscreen) to browse the Streampeg library
and control playback on SlimProto players.

Implements:
  - HTTP server on port 9000 (JSON-RPC + CometD + Cover Art)
  - TCP CLI server on port 9090 (text commands)
"""

import html
import json
import logging
import os
import queue
import socket
import socketserver
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

import db

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
HTTP_PORT = 9000
CLI_PORT = 9090
SERVER_NAME = "Streampeg"
SERVER_VERSION = "0.0.80a"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_player_id():
    """Get the first connected SlimProto player ID, or a dummy."""
    try:
        import slimproto
        players = slimproto.get_players()
        if players:
            return players[0]["player_id"]
    except Exception:
        pass
    return "00:00:00:00:00:00"


def _get_player_name():
    try:
        import slimproto
        players = slimproto.get_players()
        if players:
            return players[0]["name"]
    except Exception:
        pass
    return "Streampeg Player"


def _get_player_count():
    try:
        import slimproto
        return len(slimproto.get_players())
    except Exception:
        return 0


def _base_url():
    return f"http://{_get_local_ip()}:{HTTP_PORT}"


def _stream_url(track_id):
    """Build HTTP stream URL for a track (served by main Flask app on port 5000)."""
    return f"http://{_get_local_ip()}:5000/api/library/track/{track_id}/stream"


def _cover_url(track_id):
    return f"http://{_get_local_ip()}:5000/api/library/track/{track_id}/cover"


# ── Library Queries ───────────────────────────────────────────────────────────

def _query_artists(start=0, count=200, search=None):
    conn = db.get_db()
    if search:
        rows = conn.execute(
            "SELECT DISTINCT artist FROM library_tracks WHERE trashed=0 AND artist IS NOT NULL AND artist != '' AND artist LIKE ? ORDER BY artist COLLATE NOCASE",
            (f"%{search}%",)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT artist FROM library_tracks WHERE trashed=0 AND artist IS NOT NULL AND artist != '' ORDER BY artist COLLATE NOCASE"
        ).fetchall()
    conn.close()
    total = len(rows)
    return [r["artist"] for r in rows[start:start + count]], total


def _query_albums(start=0, count=200, artist=None, search=None):
    conn = db.get_db()
    if artist:
        rows = conn.execute(
            "SELECT DISTINCT stream_subdir FROM library_tracks WHERE trashed=0 AND artist=? ORDER BY stream_subdir COLLATE NOCASE",
            (artist,)
        ).fetchall()
    elif search:
        rows = conn.execute(
            "SELECT DISTINCT stream_subdir FROM library_tracks WHERE trashed=0 AND stream_subdir LIKE ? ORDER BY stream_subdir COLLATE NOCASE",
            (f"%{search}%",)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT stream_subdir FROM library_tracks WHERE trashed=0 ORDER BY stream_subdir COLLATE NOCASE"
        ).fetchall()
    conn.close()
    total = len(rows)
    return [r["stream_subdir"] for r in rows[start:start + count]], total


def _query_tracks(start=0, count=200, album=None, artist=None, search=None, playlist_id=None):
    conn = db.get_db()
    if playlist_id:
        rows = conn.execute(
            """SELECT t.id, t.title, t.artist, t.album, t.duration_sec, t.stream_subdir, t.filename
               FROM playlist_tracks pt JOIN library_tracks t ON pt.track_id = t.id
               WHERE t.trashed=0 ORDER BY pt.position""",
            ).fetchall() if playlist_id == "all" else conn.execute(
            """SELECT t.id, t.title, t.artist, t.album, t.duration_sec, t.stream_subdir, t.filename
               FROM playlist_tracks pt JOIN library_tracks t ON pt.track_id = t.id
               WHERE pt.playlist_id=? AND t.trashed=0 ORDER BY pt.position""",
            (playlist_id,)).fetchall()
    elif album:
        rows = conn.execute(
            "SELECT id, title, artist, album, duration_sec, stream_subdir, filename FROM library_tracks WHERE trashed=0 AND stream_subdir=? ORDER BY filename COLLATE NOCASE",
            (album,)).fetchall()
    elif artist:
        rows = conn.execute(
            "SELECT id, title, artist, album, duration_sec, stream_subdir, filename FROM library_tracks WHERE trashed=0 AND artist=? ORDER BY filename COLLATE NOCASE",
            (artist,)).fetchall()
    elif search:
        rows = conn.execute(
            "SELECT id, title, artist, album, duration_sec, stream_subdir, filename FROM library_tracks WHERE trashed=0 AND (title LIKE ? OR artist LIKE ? OR filename LIKE ?) ORDER BY filename COLLATE NOCASE",
            (f"%{search}%", f"%{search}%", f"%{search}%")).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, artist, album, duration_sec, stream_subdir, filename FROM library_tracks WHERE trashed=0 ORDER BY filename COLLATE NOCASE"
        ).fetchall()
    conn.close()
    total = len(rows)
    return [dict(r) for r in rows[start:start + count]], total


def _query_playlists(start=0, count=200):
    conn = db.get_db()
    rows = conn.execute("SELECT id, name FROM playlists ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    total = len(rows)
    return [dict(r) for r in rows[start:start + count]], total


def _query_folders(start=0, count=200, folder_id=None):
    """Browse by folder (stream_subdir). folder_id=None means root."""
    if folder_id:
        return _query_tracks(start, count, album=folder_id)
    return _query_albums(start, count)


def _get_track(track_id):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM library_tracks WHERE id=?", (track_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── SlimBrowse Response Builders ──────────────────────────────────────────────

def _build_home_menu(player_id):
    """Build the home menu for Jivelite."""
    ip = _get_local_ip()
    items = [
        {
            "text": "Music Folder",
            "icon-id": "/html/images/musicfolder.png",
            "actions": {
                "go": {"cmd": ["browselibrary", "items"], "params": {"mode": "folders", "menu": "1"}},
            },
            "weight": 10,
        },
        {
            "text": "Artists",
            "icon-id": "/html/images/artists.png",
            "actions": {
                "go": {"cmd": ["browselibrary", "items"], "params": {"mode": "artists", "menu": "1"}},
            },
            "weight": 20,
        },
        {
            "text": "Playlists",
            "icon-id": "/html/images/playlists.png",
            "actions": {
                "go": {"cmd": ["browselibrary", "items"], "params": {"mode": "playlists", "menu": "1"}},
            },
            "weight": 30,
        },
        {
            "text": "Search",
            "icon-id": "/html/images/search.png",
            "actions": {
                "go": {"cmd": ["browselibrary", "items"], "params": {"mode": "search", "menu": "1"}},
            },
            "input": {
                "len": 200,
                "processingPopup": {"text": "Searching..."},
                "help": {"text": "Search"},
            },
            "weight": 40,
        },
    ]
    return {"count": len(items), "item_loop": items, "offset": 0}


def _build_browse_response(cmd_params, player_id):
    """Handle browselibrary items command and return SlimBrowse JSON."""
    mode = cmd_params.get("mode", "folders")
    start = int(cmd_params.get("_index", 0) or cmd_params.get("start", 0) or 0)
    count = int(cmd_params.get("_quantity", 200) or cmd_params.get("count", 200) or 200)
    search = cmd_params.get("search")
    ip = _get_local_ip()

    if mode == "folders":
        folder_id = cmd_params.get("folder_id")
        if folder_id:
            # Show tracks in folder
            tracks, total = _query_tracks(start, count, album=folder_id)
            items = []
            for t in tracks:
                display = f"{t['artist']} - {t['title']}" if t.get("artist") and t.get("title") else t.get("title") or t["filename"]
                items.append({
                    "text": display,
                    "icon": _cover_url(t["id"]),
                    "actions": {
                        "play": {"cmd": ["playlistcontrol"], "params": {"cmd": "load", "track_id": str(t["id"])}},
                        "add": {"cmd": ["playlistcontrol"], "params": {"cmd": "add", "track_id": str(t["id"])}},
                    },
                    "type": "audio",
                    "trackType": "mp3",
                })
            return {"count": total, "item_loop": items, "offset": start}
        else:
            # Show folders (stream_subdirs)
            albums, total = _query_albums(start, count, search=search)
            items = []
            for a in albums:
                # Get first track for cover
                conn = db.get_db()
                first = conn.execute("SELECT id FROM library_tracks WHERE stream_subdir=? AND trashed=0 LIMIT 1", (a,)).fetchone()
                conn.close()
                icon = _cover_url(first["id"]) if first else ""
                track_count = 0
                conn = db.get_db()
                tc = conn.execute("SELECT COUNT(*) FROM library_tracks WHERE stream_subdir=? AND trashed=0", (a,)).fetchone()
                conn.close()
                track_count = tc[0] if tc else 0
                items.append({
                    "text": a,
                    "icon": icon,
                    "actions": {
                        "go": {"cmd": ["browselibrary", "items"], "params": {"mode": "folders", "folder_id": a, "menu": "1"}},
                        "play": {"cmd": ["playlistcontrol"], "params": {"cmd": "load", "folder": a}},
                    },
                    "type": "playlist",
                    "textkey": a[0].upper() if a else "",
                })
            return {"count": total, "item_loop": items, "offset": start}

    elif mode == "artists":
        artist_name = cmd_params.get("artist_id")
        if artist_name:
            # Show tracks by artist
            tracks, total = _query_tracks(start, count, artist=artist_name)
            items = []
            for t in tracks:
                display = t.get("title") or t["filename"]
                items.append({
                    "text": display,
                    "icon": _cover_url(t["id"]),
                    "actions": {
                        "play": {"cmd": ["playlistcontrol"], "params": {"cmd": "load", "track_id": str(t["id"])}},
                        "add": {"cmd": ["playlistcontrol"], "params": {"cmd": "add", "track_id": str(t["id"])}},
                    },
                    "type": "audio",
                })
            return {"count": total, "item_loop": items, "offset": start}
        else:
            artists, total = _query_artists(start, count, search=search)
            items = []
            for a in artists:
                items.append({
                    "text": a,
                    "actions": {
                        "go": {"cmd": ["browselibrary", "items"], "params": {"mode": "artists", "artist_id": a, "menu": "1"}},
                        "play": {"cmd": ["playlistcontrol"], "params": {"cmd": "load", "artist": a}},
                    },
                    "textkey": a[0].upper() if a else "",
                })
            return {"count": total, "item_loop": items, "offset": start}

    elif mode == "playlists":
        playlist_id = cmd_params.get("playlist_id")
        if playlist_id:
            tracks, total = _query_tracks(start, count, playlist_id=playlist_id)
            items = []
            for t in tracks:
                display = f"{t['artist']} - {t['title']}" if t.get("artist") and t.get("title") else t.get("title") or t["filename"]
                items.append({
                    "text": display,
                    "icon": _cover_url(t["id"]),
                    "actions": {
                        "play": {"cmd": ["playlistcontrol"], "params": {"cmd": "load", "track_id": str(t["id"])}},
                        "add": {"cmd": ["playlistcontrol"], "params": {"cmd": "add", "track_id": str(t["id"])}},
                    },
                    "type": "audio",
                })
            return {"count": total, "item_loop": items, "offset": start}
        else:
            playlists, total = _query_playlists(start, count)
            items = []
            for p in playlists:
                items.append({
                    "text": p["name"],
                    "icon-id": "/html/images/playlists.png",
                    "actions": {
                        "go": {"cmd": ["browselibrary", "items"], "params": {"mode": "playlists", "playlist_id": str(p["id"]), "menu": "1"}},
                        "play": {"cmd": ["playlistcontrol"], "params": {"cmd": "load", "playlist_id": str(p["id"])}},
                    },
                })
            return {"count": total, "item_loop": items, "offset": start}

    elif mode == "search":
        if not search:
            return {"count": 0, "item_loop": [], "offset": 0}
        tracks, total = _query_tracks(start, count, search=search)
        items = []
        for t in tracks:
            display = f"{t['artist']} - {t['title']}" if t.get("artist") and t.get("title") else t.get("title") or t["filename"]
            items.append({
                "text": display,
                "icon": _cover_url(t["id"]),
                "actions": {
                    "play": {"cmd": ["playlistcontrol"], "params": {"cmd": "load", "track_id": str(t["id"])}},
                    "add": {"cmd": ["playlistcontrol"], "params": {"cmd": "add", "track_id": str(t["id"])}},
                },
                "type": "audio",
            })
        return {"count": total, "item_loop": items, "offset": start}

    return {"count": 0, "item_loop": [], "offset": 0}


# ── Playback Control ──────────────────────────────────────────────────────────

def _handle_playlistcontrol(params, player_id):
    """Handle playlistcontrol command — play a track/folder/playlist."""
    import slimproto
    cmd = params.get("cmd", "load")
    track_id = params.get("track_id")
    folder = params.get("folder")
    artist = params.get("artist")
    playlist_id = params.get("playlist_id")

    if track_id:
        url = _stream_url(track_id)
        slimproto.play_url(player_id, url, mime_type="audio/mpeg")
        return {"count": 0}

    # For folders/artists/playlists: play first track
    if folder:
        tracks, _ = _query_tracks(0, 1, album=folder)
    elif artist:
        tracks, _ = _query_tracks(0, 1, artist=artist)
    elif playlist_id:
        tracks, _ = _query_tracks(0, 1, playlist_id=playlist_id)
    else:
        return {"count": 0}

    if tracks:
        url = _stream_url(tracks[0]["id"])
        slimproto.play_url(player_id, url, mime_type="audio/mpeg")
    return {"count": 0}


def _handle_player_status(player_id, params):
    """Handle status command — return current player state."""
    import slimproto
    state = slimproto.get_state(player_id) or "stop"
    vol = slimproto.get_volume(player_id) or 50
    elapsed = slimproto.get_elapsed(player_id) or 0
    name = _get_player_name()

    mode_map = {"play": "play", "pause": "pause", "stop": "stop"}
    result = {
        "player_name": name,
        "player_connected": 1,
        "power": 1,
        "mode": mode_map.get(state, "stop"),
        "mixer volume": vol,
        "time": elapsed,
        "playlist_tracks": 0,
        "playlist_cur_index": 0,
        "playlist repeat": 0,
        "playlist shuffle": 0,
        "seq_no": 0,
        "can_seek": 0,
        "rate": 1,
    }
    return result


# ── CLI Protocol Handler ──────────────────────────────────────────────────────

def _handle_cli_command(line):
    """Parse and handle a CLI text command. Returns response string."""
    line = line.strip()
    if not line:
        return ""

    # URL-decode the entire line
    parts = line.split(" ")
    parts = [urllib.parse.unquote(p) for p in parts]

    # Check if first part is a player ID (MAC address format)
    player_id = None
    if len(parts) > 1 and ":" in parts[0] and len(parts[0]) == 17:
        player_id = parts[0]
        parts = parts[1:]

    cmd = parts[0] if parts else ""

    if cmd == "version":
        return f"version {SERVER_VERSION}"

    elif cmd == "serverstatus":
        count = _get_player_count()
        players_info = ""
        try:
            import slimproto
            for i, p in enumerate(slimproto.get_players()):
                pid = urllib.parse.quote(p["player_id"])
                pname = urllib.parse.quote(p["name"])
                players_info += f" playerid:{pid} name:{pname} connected:1 power:1 model:squeezelite isplayer:1 playerindex:{i}"
        except Exception:
            pass
        return f"serverstatus 0 100 info total albums:0 info total artists:0 info total songs:18000 player count:{count}{players_info}"

    elif cmd == "players":
        count = _get_player_count()
        players_info = ""
        try:
            import slimproto
            for i, p in enumerate(slimproto.get_players()):
                pid = urllib.parse.quote(p["player_id"])
                pname = urllib.parse.quote(p["name"])
                players_info += f" playerid:{pid} name:{pname} connected:1 power:1 model:squeezelite isplayer:1 playerindex:{i}"
        except Exception:
            pass
        return f"players 0 100 count:{count}{players_info}"

    elif cmd == "menu":
        # Home menu — return basic items
        pid = player_id or _get_player_id()
        pid_enc = urllib.parse.quote(pid)
        return f"{pid_enc} menu 0 100 count:4 offset:0"

    elif cmd == "status":
        pid = player_id or _get_player_id()
        state = "stop"
        vol = 50
        try:
            import slimproto
            state = slimproto.get_state(pid) or "stop"
            vol = slimproto.get_volume(pid) or 50
        except Exception:
            pass
        pid_enc = urllib.parse.quote(pid)
        return f"{pid_enc} status - 1 tags: player_name:{urllib.parse.quote(_get_player_name())} player_connected:1 power:1 mode:{state} mixer%20volume:{vol} playlist%20tracks:0"

    elif cmd == "browselibrary":
        # Forward to browse handler
        params = {}
        for p in parts[2:]:  # skip 'browselibrary items'
            if ":" in p:
                k, v = p.split(":", 1)
                params[k] = v
        result = _build_browse_response(params, player_id or _get_player_id())
        pid_enc = urllib.parse.quote(player_id or _get_player_id())
        return f"{pid_enc} browselibrary items 0 200 count:{result['count']}"

    elif cmd == "playlistcontrol":
        params = {}
        for p in parts[1:]:
            if ":" in p:
                k, v = p.split(":", 1)
                params[k] = v
        _handle_playlistcontrol(params, player_id or _get_player_id())
        return ""

    elif cmd in ("play", "pause", "stop"):
        pid = player_id or _get_player_id()
        try:
            import slimproto
            if cmd == "play":
                from aioslimproto.models import PlayerState
                if slimproto.get_state(pid) == "pause":
                    slimproto.pause_player(pid)
                # else already playing or need URL
            elif cmd == "pause":
                slimproto.pause_player(pid)
            elif cmd == "stop":
                slimproto.stop_player(pid)
        except Exception:
            pass
        return ""

    elif cmd == "mixer":
        if len(parts) > 2 and parts[1] == "volume":
            pid = player_id or _get_player_id()
            try:
                import slimproto
                if parts[2] == "?":
                    vol = slimproto.get_volume(pid) or 50
                    return f"{urllib.parse.quote(pid)} mixer volume {vol}"
                else:
                    slimproto.set_volume(pid, int(parts[2]))
            except Exception:
                pass
        return ""

    elif cmd == "power":
        pid = player_id or _get_player_id()
        try:
            import slimproto
            if len(parts) > 1:
                slimproto._run_async(slimproto._get_player(pid).power(powered=parts[1] == "1"))
        except Exception:
            pass
        return ""

    # Unknown command — return empty
    log.debug("Unknown CLI command: %s", line)
    return ""


# ── CLI TCP Server ────────────────────────────────────────────────────────────

class CLIHandler(socketserver.StreamRequestHandler):
    """Handle CLI text connections (telnet port 9090)."""

    def handle(self):
        log.info("CLI client connected: %s", self.client_address)
        try:
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                log.debug("CLI recv: %s", line)
                response = _handle_cli_command(line)
                if response:
                    self.wfile.write((response + "\n").encode("utf-8"))
                    self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        log.info("CLI client disconnected: %s", self.client_address)


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    allow_reuse_port = True
    daemon_threads = True


# ── CometD State ──────────────────────────────────────────────────────────────

_cometd_clients = {}  # clientId -> {"queues": {channel: Queue}, "last_seen": time}
_cometd_counter = 0
_cometd_lock = threading.Lock()


def _cometd_new_client():
    global _cometd_counter
    with _cometd_lock:
        _cometd_counter += 1
        client_id = f"streampeg_{_cometd_counter}"
        _cometd_clients[client_id] = {"subscriptions": set(), "queue": queue.Queue(maxsize=100), "last_seen": time.time()}
        return client_id


def _cometd_push(client_id, channel, data):
    with _cometd_lock:
        client = _cometd_clients.get(client_id)
        if client:
            try:
                client["queue"].put_nowait({"channel": channel, "data": data})
            except queue.Full:
                pass


# ── HTTP Server (port 9000) ──────────────────────────────────────────────────

class LMSHTTPHandler(BaseHTTPRequestHandler):
    """Handle JSON-RPC, CometD, and cover art requests."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/music/") and "/cover" in self.path:
            self._handle_cover_art()
        elif self.path == "/jsonrpc.js":
            self.send_error(405)
        else:
            # Serve a minimal status page
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Streampeg LMS Compat</h1></body></html>")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")

        if self.path == "/jsonrpc.js":
            self._handle_jsonrpc(body)
        elif self.path == "/cometd":
            self._handle_cometd(body)
        else:
            self.send_error(404)

    def _handle_jsonrpc(self, body):
        """Handle LMS JSON-RPC requests (slim.request format)."""
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400)
            return

        req_id = req.get("id", 1)
        method = req.get("method", "")
        params = req.get("params", [])

        if method == "slim.request":
            player_id = params[0] if len(params) > 0 else ""
            cmd = params[1] if len(params) > 1 else []
            result = self._dispatch_slim_command(player_id, cmd)
        else:
            result = {}

        response = json.dumps({"id": req_id, "method": method, "params": params, "result": result})
        self._send_json(response)

    def _dispatch_slim_command(self, player_id, cmd):
        """Dispatch a slim.request command and return result dict."""
        if not cmd:
            return {}

        cmd_name = cmd[0]

        if cmd_name == "serverstatus":
            count = _get_player_count()
            players_loop = []
            try:
                import slimproto
                for p in slimproto.get_players():
                    players_loop.append({
                        "playerid": p["player_id"],
                        "name": p["name"],
                        "connected": 1,
                        "power": 1,
                        "model": "squeezelite",
                        "isplayer": 1,
                        "canpoweroff": 1,
                    })
            except Exception:
                pass
            return {
                "info total albums": 0,
                "info total artists": 0,
                "info total songs": 18000,
                "player count": count,
                "players_loop": players_loop,
                "version": SERVER_VERSION,
            }

        elif cmd_name == "players":
            start = int(cmd[1]) if len(cmd) > 1 else 0
            count_req = int(cmd[2]) if len(cmd) > 2 else 100
            players_loop = []
            try:
                import slimproto
                for p in slimproto.get_players():
                    players_loop.append({
                        "playerid": p["player_id"],
                        "name": p["name"],
                        "connected": 1,
                        "power": 1,
                        "model": "squeezelite",
                        "isplayer": 1,
                    })
            except Exception:
                pass
            return {"count": len(players_loop), "players_loop": players_loop}

        elif cmd_name == "status":
            if not player_id:
                player_id = _get_player_id()
            return _handle_player_status(player_id, _parse_cmd_params(cmd[1:]))

        elif cmd_name == "menu":
            if not player_id:
                player_id = _get_player_id()
            return _build_home_menu(player_id)

        elif cmd_name == "browselibrary" and len(cmd) > 1 and cmd[1] == "items":
            params = _parse_cmd_params(cmd[2:])
            if not player_id:
                player_id = _get_player_id()
            return _build_browse_response(params, player_id)

        elif cmd_name == "playlistcontrol":
            params = _parse_cmd_params(cmd[1:])
            if not player_id:
                player_id = _get_player_id()
            return _handle_playlistcontrol(params, player_id)

        elif cmd_name == "playlist":
            if len(cmd) > 2 and cmd[1] == "play":
                url = cmd[2]
                if not player_id:
                    player_id = _get_player_id()
                try:
                    import slimproto
                    slimproto.play_url(player_id, url, mime_type="audio/mpeg")
                except Exception:
                    pass
            return {}

        elif cmd_name == "mixer":
            if len(cmd) > 2 and cmd[1] == "volume":
                if not player_id:
                    player_id = _get_player_id()
                try:
                    import slimproto
                    if cmd[2] == "?":
                        vol = slimproto.get_volume(player_id) or 50
                        return {"_volume": vol}
                    else:
                        slimproto.set_volume(player_id, int(cmd[2]))
                except Exception:
                    pass
            return {}

        elif cmd_name == "power":
            if not player_id:
                player_id = _get_player_id()
            try:
                import slimproto
                powered = cmd[1] == "1" if len(cmd) > 1 else True
                slimproto._run_async(slimproto._get_player(player_id).power(powered=powered))
            except Exception:
                pass
            return {}

        log.debug("Unknown slim command: %s %s", player_id, cmd)
        return {}

    def _handle_cometd(self, body):
        """Handle CometD/Bayeux protocol messages."""
        try:
            messages = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400)
            return

        if not isinstance(messages, list):
            messages = [messages]

        responses = []
        for msg in messages:
            channel = msg.get("channel", "")

            if channel == "/meta/handshake":
                client_id = _cometd_new_client()
                responses.append({
                    "channel": "/meta/handshake",
                    "version": "1.0",
                    "supportedConnectionTypes": ["long-polling"],
                    "clientId": client_id,
                    "successful": True,
                    "advice": {"reconnect": "retry", "interval": 0, "timeout": 30000},
                })

            elif channel == "/meta/connect":
                client_id = msg.get("clientId", "")
                with _cometd_lock:
                    client = _cometd_clients.get(client_id)
                    if client:
                        client["last_seen"] = time.time()

                # Long-poll: wait up to 5s for events
                events = []
                if client_id in _cometd_clients:
                    try:
                        q = _cometd_clients[client_id]["queue"]
                        deadline = time.time() + 5
                        while time.time() < deadline:
                            try:
                                evt = q.get(timeout=0.5)
                                events.append(evt)
                            except queue.Empty:
                                if events:
                                    break
                    except Exception:
                        pass

                responses.append({
                    "channel": "/meta/connect",
                    "clientId": client_id,
                    "successful": True,
                    "advice": {"reconnect": "retry", "interval": 0, "timeout": 30000},
                })
                responses.extend(events)

            elif channel == "/meta/subscribe":
                client_id = msg.get("clientId", "")
                subscription = msg.get("subscription", "")
                with _cometd_lock:
                    client = _cometd_clients.get(client_id)
                    if client:
                        client["subscriptions"].add(subscription)

                responses.append({
                    "channel": "/meta/subscribe",
                    "clientId": client_id,
                    "subscription": subscription,
                    "successful": True,
                })

                # If subscribing to serverstatus, push initial data
                if "serverstatus" in subscription:
                    _cometd_push(client_id, subscription, self._dispatch_slim_command("", ["serverstatus", "0", "100"]))
                elif "menustatus" in subscription or "menu" in subscription:
                    pid = _get_player_id()
                    _cometd_push(client_id, subscription, _build_home_menu(pid))
                elif "playerstatus" in subscription:
                    pid = _get_player_id()
                    _cometd_push(client_id, subscription, _handle_player_status(pid, {}))

            elif channel == "/slim/request":
                # Inline request via CometD
                client_id = msg.get("clientId", "")
                data = msg.get("data", {})
                req_cmd = data.get("request", [])
                response_channel = data.get("response", f"/{client_id}/slim/request")
                player_id = ""
                if req_cmd and ":" in str(req_cmd[0]) and len(str(req_cmd[0])) == 17:
                    player_id = req_cmd[0]
                    req_cmd = req_cmd[1:]
                result = self._dispatch_slim_command(player_id, req_cmd)
                responses.append({
                    "channel": response_channel,
                    "data": result,
                })

            elif channel == "/meta/disconnect":
                client_id = msg.get("clientId", "")
                with _cometd_lock:
                    _cometd_clients.pop(client_id, None)
                responses.append({
                    "channel": "/meta/disconnect",
                    "clientId": client_id,
                    "successful": True,
                })

        self._send_json(json.dumps(responses))

    def _handle_cover_art(self):
        """Serve cover art: /music/<track_id>/cover.jpg"""
        import re
        m = re.match(r"/music/(\d+)/cover", self.path)
        if not m:
            self.send_error(404)
            return
        track_id = int(m.group(1))
        # Redirect to main Flask app's cover endpoint
        self.send_response(302)
        self.send_header("Location", f"http://{_get_local_ip()}:5000/api/library/track/{track_id}/cover")
        self.end_headers()

    def _send_json(self, data):
        encoded = data.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)


def _parse_cmd_params(parts):
    """Parse 'key:value' pairs from a command's remaining parts."""
    params = {}
    skip_numeric = True
    for p in parts:
        if skip_numeric and p.isdigit():
            if "_index" not in params:
                params["_index"] = p
            elif "_quantity" not in params:
                params["_quantity"] = p
            continue
        skip_numeric = False
        if ":" in p:
            k, v = p.split(":", 1)
            params[k] = v
        elif "subscribe" not in p:
            skip_numeric = False
    return params


# ── Server Lifecycle ──────────────────────────────────────────────────────────

_http_server = None
_http_thread = None
_cli_server = None
_cli_thread = None
_running = False


def start():
    """Start the LMS compatibility servers (HTTP on 9000, CLI on 9090)."""
    global _http_server, _http_thread, _cli_server, _cli_thread, _running
    if _running:
        return

    # HTTP server (JSON-RPC + CometD + Cover Art)
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True
    try:
        _http_server = ReusableHTTPServer(("0.0.0.0", HTTP_PORT), LMSHTTPHandler)
        _http_thread = threading.Thread(target=_http_server.serve_forever, daemon=True)
        _http_thread.start()
        log.info("LMS compat HTTP server started on port %d", HTTP_PORT)
    except OSError as e:
        log.error("LMS compat HTTP server failed: %s", e)
        return

    # CLI server
    try:
        _cli_server = ThreadedTCPServer(("0.0.0.0", CLI_PORT), CLIHandler)
        _cli_thread = threading.Thread(target=_cli_server.serve_forever, daemon=True)
        _cli_thread.start()
        log.info("LMS compat CLI server started on port %d", CLI_PORT)
    except OSError as e:
        log.error("LMS compat CLI server failed: %s", e)

    _running = True


def stop():
    """Stop the LMS compatibility servers."""
    global _http_server, _cli_server, _running
    _running = False
    if _http_server:
        _http_server.shutdown()
        _http_server = None
    if _cli_server:
        _cli_server.shutdown()
        _cli_server = None


def is_running():
    return _running
