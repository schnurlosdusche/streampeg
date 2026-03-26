"""
SlimProto server module — embedded SlimProto server for direct Squeezelite control.

Uses aioslimproto to accept connections from Squeezelite players without LMS.
Players appear as cast devices in the Streampeg UI.
"""

import asyncio
import logging
import threading

from aioslimproto import SlimServer
from aioslimproto.models import EventType

log = logging.getLogger(__name__)

# ── Module state ──────────────────────────────────────────────────────────────
_server: SlimServer | None = None
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_running = False


def _run_loop(loop):
    """Run the asyncio event loop in a background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def start():
    """Start the SlimProto server on port 3483 in a background thread."""
    global _server, _loop, _thread, _running
    if _running:
        return

    _loop = asyncio.new_event_loop()
    _thread = threading.Thread(target=_run_loop, args=(_loop,), daemon=True)
    _thread.start()

    future = asyncio.run_coroutine_threadsafe(_start_server(), _loop)
    future.result(timeout=10)
    _running = True
    log.info("SlimProto server started on port 3483")


async def _start_server():
    """Create and start the SlimServer (runs inside the asyncio loop)."""
    global _server
    _server = SlimServer(
        cli_port=None,
        cli_port_json=None,
    )
    _server.subscribe(_on_event)
    await _server.start()


async def _on_event(evt):
    """Handle player events from aioslimproto."""
    if evt.type == EventType.PLAYER_HEARTBEAT:
        return
    if evt.type == EventType.PLAYER_CONNECTED:
        log.info("SlimProto player connected: %s", evt.player_id)
    elif evt.type == EventType.PLAYER_DISCONNECTED:
        log.info("SlimProto player disconnected: %s", evt.player_id)
    elif evt.type == EventType.PLAYER_NAME_RECEIVED:
        player = _server.get_player(evt.player_id) if _server else None
        if player:
            log.info("SlimProto player name: %s (%s)", player.name, evt.player_id)


def stop():
    """Stop the SlimProto server."""
    global _server, _loop, _thread, _running
    _running = False
    if _loop and _server:
        try:
            future = asyncio.run_coroutine_threadsafe(_stop_server(), _loop)
            future.result(timeout=5)
        except Exception:
            pass
    if _loop:
        _loop.call_soon_threadsafe(_loop.stop)
    _server = None
    _loop = None
    _thread = None


async def _stop_server():
    """Stop the SlimServer (runs inside the asyncio loop)."""
    if _server:
        await _server.stop()


# ── Player access (thread-safe) ──────────────────────────────────────────────

def get_players():
    """Return list of connected SlimProto players as device dicts for cast.py."""
    if not _server or not _running:
        return []
    players = []
    for p in _server.players:
        if not p.connected:
            continue
        players.append({
            "id": f"slim:{p.player_id}",
            "name": p.name or p.player_id,
            "type": "slim",
            "player_id": p.player_id,
            "model": p.device_model or p.device_type or "Squeezelite",
            "connected": True,
            "enabled": True,
            "powered": p.powered,
        })
    return players


def _get_player(player_id):
    """Get a SlimClient by its player_id (MAC address)."""
    if not _server:
        return None
    return _server.get_player(player_id)


def _run_async(coro):
    """Run a coroutine on the SlimProto event loop from a sync context."""
    if not _loop or not _running:
        return None
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=10)
    except Exception as e:
        log.error("SlimProto async call failed: %s", e)
        return None


def play_url(player_id, url, mime_type="audio/mpeg"):
    """Play a URL on a SlimProto player."""
    player = _get_player(player_id)
    if not player:
        return False
    _run_async(player.power(powered=True))
    _run_async(player.play_url(url, mime_type=mime_type))
    return True


def stop_player(player_id):
    """Stop playback on a SlimProto player."""
    player = _get_player(player_id)
    if not player:
        return False
    _run_async(player.stop())
    return True


def pause_player(player_id):
    """Toggle pause on a SlimProto player."""
    player = _get_player(player_id)
    if not player:
        return False
    _run_async(player.toggle_pause())
    return True


def get_volume(player_id):
    """Get volume level (0-100) of a SlimProto player."""
    player = _get_player(player_id)
    if not player:
        return None
    return player.volume_level


def set_volume(player_id, level):
    """Set volume level (0-100) on a SlimProto player."""
    player = _get_player(player_id)
    if not player:
        return False
    _run_async(player.volume_set(max(0, min(100, int(level)))))
    return True


def get_state(player_id):
    """Get playback state: 'play', 'pause', 'stop', or None."""
    player = _get_player(player_id)
    if not player:
        return None
    state_str = str(player.state.value) if player.state else "stopped"
    if state_str == "playing":
        return "play"
    elif state_str == "paused":
        return "pause"
    else:
        return "stop"


def get_elapsed(player_id):
    """Get elapsed playback time in seconds."""
    player = _get_player(player_id)
    if not player:
        return 0
    return player.elapsed_seconds


def seek_player(player_id, position_seconds):
    """Seek to a position (not natively supported by SlimProto — restart with offset)."""
    # SlimProto/Squeezelite doesn't support arbitrary seek easily
    # For now return False
    return False


def is_running():
    """Check if the SlimProto server is running."""
    return _running
