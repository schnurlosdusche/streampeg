"""
Cast module — discover and stream to network audio devices.

Supported backends:
  - LMS (Logitech Media Server / Squeezebox / Max2Play) — ACTIVE
  - Sonos (via SoCo) — streaming controlled via settings (default: disabled)
"""

import json
import socket
import threading
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Cache: discovered devices, refreshed in background
# ---------------------------------------------------------------------------
_devices = []           # list of dicts: {id, name, type, host, ...}
_devices_lock = threading.Lock()
_last_discovery = 0
DISCOVERY_CACHE_SECS = 30  # re-discover at most every 30s

# Currently casting: stream_id -> {device_id, type}
_active_casts = {}
_casts_lock = threading.Lock()


# ===== LMS discovery & control =============================================

def _discover_lms_server(timeout=3):
    """Find LMS server via UDP broadcast on port 3483."""
    msg = b"eJSON\0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(msg, ("<broadcast>", 3483))
        data, addr = sock.recvfrom(1024)
        # Response starts with 'E' then JSON port info — but addr gives us the IP
        return addr[0]
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()


def _lms_request(host, port, player_id, command):
    """Send a JSON-RPC request to LMS. Returns parsed response or None."""
    url = f"http://{host}:{port}/jsonrpc.js"
    payload = json.dumps({
        "id": 1,
        "method": "slim.request",
        "params": [player_id, command],
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return json.loads(resp.read())
    except Exception:
        return None


def _discover_lms_players(host, port=9000):
    """Query LMS server for connected players."""
    result = _lms_request(host, port, "", ["players", "0", "100"])
    if not result:
        return []
    players = []
    for p in result.get("result", {}).get("players_loop", []):
        players.append({
            "id": f"lms:{p['playerid']}",
            "name": p.get("name", p["playerid"]),
            "type": "lms",
            "host": host,
            "port": port,
            "player_id": p["playerid"],
            "model": p.get("modelname", ""),
            "connected": bool(p.get("connected", 0)),
            "power": bool(p.get("power", 0)),
        })
    return players


def lms_play(device, stream_url):
    """Tell an LMS player to play a stream URL."""
    _lms_request(
        device["host"], device["port"], device["player_id"],
        ["playlist", "play", stream_url],
    )
    # Power on if needed
    _lms_request(
        device["host"], device["port"], device["player_id"],
        ["power", "1"],
    )


def lms_stop(device):
    """Stop playback on an LMS player."""
    _lms_request(
        device["host"], device["port"], device["player_id"],
        ["stop"],
    )


# ===== Sonos discovery (SoCo) =============================================
# NOTE: Sonos streaming is DISABLED. Only discovery is active so devices
# show up in the UI (greyed out / marked as unavailable).

import soco


def is_sonos_enabled():
    """Check if Sonos streaming is enabled in settings."""
    from db import get_setting
    val = get_setting("cast_sonos_enabled")
    return val == "1"


def set_sonos_enabled(enabled):
    """Enable or disable Sonos streaming in settings."""
    from db import set_setting
    set_setting("cast_sonos_enabled", "1" if enabled else "0")


def _discover_sonos():
    """Discover Sonos speakers on the network. Returns list of device dicts."""
    sonos_enabled = is_sonos_enabled()
    devices = []
    try:
        speakers = soco.discover(timeout=3) or []
        for sp in speakers:
            devices.append({
                "id": f"sonos:{sp.uid}",
                "name": sp.player_name,
                "type": "sonos",
                "host": sp.ip_address,
                "uid": sp.uid,
                "model": sp.get_speaker_info().get("model_name", ""),
                "enabled": sonos_enabled,
            })
    except Exception:
        pass
    return devices


# ===== Unified discovery ===================================================

def discover_devices(force=False):
    """Discover all supported devices. Caches results."""
    global _devices, _last_discovery
    now = time.time()
    if not force and now - _last_discovery < DISCOVERY_CACHE_SECS:
        with _devices_lock:
            return list(_devices)

    devices = []

    # LMS
    lms_host = _discover_lms_server()
    if lms_host:
        players = _discover_lms_players(lms_host)
        for p in players:
            p["enabled"] = True
        devices.extend(players)

    # Sonos (discovery only)
    devices.extend(_discover_sonos())

    with _devices_lock:
        _devices = devices
        _last_discovery = now

    return list(devices)


def get_device(device_id):
    """Find a device by ID from cache."""
    with _devices_lock:
        for d in _devices:
            if d["id"] == device_id:
                return dict(d)
    return None


# ===== Cast control ========================================================

def sonos_play(device, stream_url):
    """Tell a Sonos speaker to play a stream URL."""
    try:
        sp = soco.SoCo(device["host"])
        # Sonos rejects streams without proper MIME metadata (UPnP Error 714).
        # Provide DIDL-Lite with audioBroadcast class and audio/mpeg MIME type.
        from xml.sax.saxutils import escape
        didl = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"'
            ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"'
            ' xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="R:0/0/0" parentID="R:0/0" restricted="true">'
            '<dc:title>{name}</dc:title>'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            '<res protocolInfo="http-get:*:audio/mpeg:*">{url}</res>'
            '</item></DIDL-Lite>'
        ).format(name=escape(device.get("name", "Stream")), url=escape(stream_url))
        sp.play_uri(stream_url, meta=didl)
    except Exception:
        pass


def sonos_stop(device):
    """Stop playback on a Sonos speaker."""
    try:
        sp = soco.SoCo(device["host"])
        sp.stop()
    except Exception:
        pass


def cast_stream(stream_url, device_id):
    """Cast a stream URL to a device. Returns (success, message)."""
    device = get_device(device_id)
    if not device:
        return False, "Gerät nicht gefunden"

    if not device.get("enabled", False):
        if device["type"] == "sonos":
            return False, "Sonos-Streaming ist in den Settings deaktiviert"
        return False, "Gerät ist nicht verfügbar"

    if device["type"] == "lms":
        lms_play(device, stream_url)
        return True, f"Stream gestartet auf {device['name']}"

    if device["type"] == "sonos":
        sonos_play(device, stream_url)
        return True, f"Stream gestartet auf {device['name']}"

    return False, f"Unbekannter Gerätetyp: {device['type']}"


def stop_cast(device_id):
    """Stop casting on a device. Returns (success, message)."""
    device = get_device(device_id)
    if not device:
        return False, "Gerät nicht gefunden"

    if device["type"] == "lms":
        lms_stop(device)
        return True, f"Wiedergabe gestoppt auf {device['name']}"

    if device["type"] == "sonos":
        sonos_stop(device)
        return True, f"Wiedergabe gestoppt auf {device['name']}"

    return False, f"Unbekannter Gerätetyp: {device['type']}"


def set_active_cast(stream_id, device_id):
    """Track which stream is casting to which device."""
    with _casts_lock:
        _active_casts[stream_id] = device_id


def remove_active_cast(stream_id):
    """Remove active cast tracking."""
    with _casts_lock:
        _active_casts.pop(stream_id, None)


def get_active_casts():
    """Return dict of stream_id -> device_id."""
    with _casts_lock:
        return dict(_active_casts)


def get_active_cast_for_stream(stream_id):
    """Return device_id if stream is being cast, else None."""
    with _casts_lock:
        return _active_casts.get(stream_id)


# ===== Volume control =====================================================

def lms_get_volume(device):
    """Get current volume of an LMS player (0-100)."""
    result = _lms_request(
        device["host"], device["port"], device["player_id"],
        ["mixer", "volume", "?"],
    )
    if result:
        try:
            return int(result["result"]["_volume"])
        except (KeyError, TypeError, ValueError):
            pass
    return None


def lms_set_volume(device, level):
    """Set volume of an LMS player (0-100)."""
    _lms_request(
        device["host"], device["port"], device["player_id"],
        ["mixer", "volume", str(level)],
    )


def sonos_get_volume(device):
    """Get current volume of a Sonos speaker (0-100)."""
    try:
        return soco.SoCo(device["host"]).volume
    except Exception:
        return None


def sonos_set_volume(device, level):
    """Set volume of a Sonos speaker (0-100)."""
    try:
        soco.SoCo(device["host"]).volume = level
    except Exception:
        pass


def get_volume(device_id):
    """Get volume for a device by ID. Returns int 0-100 or None."""
    device = get_device(device_id)
    if not device:
        return None
    if device["type"] == "lms":
        return lms_get_volume(device)
    if device["type"] == "sonos":
        return sonos_get_volume(device)
    return None


def set_volume(device_id, level):
    """Set volume for a device by ID. Level should be 0-100."""
    device = get_device(device_id)
    if not device:
        return
    level = max(0, min(100, int(level)))
    if device["type"] == "lms":
        lms_set_volume(device, level)
    elif device["type"] == "sonos":
        sonos_set_volume(device, level)


# ===== Multiroom ============================================================

def lms_sync(master_device, slave_device):
    """Sync a slave LMS player to a master (multiroom)."""
    _lms_request(
        master_device["host"], master_device["port"], slave_device["player_id"],
        ["sync", master_device["player_id"]],
    )
    _lms_request(
        slave_device["host"], slave_device["port"], slave_device["player_id"],
        ["power", "1"],
    )


def lms_unsync(device):
    """Remove an LMS player from sync group."""
    _lms_request(
        device["host"], device["port"], device["player_id"],
        ["sync", "-"],
    )


def lms_get_sync_group(device):
    """Get sync group for an LMS player. Returns list of player_ids or empty."""
    result = _lms_request(
        device["host"], device["port"], device["player_id"],
        ["sync", "?"],
    )
    if result:
        try:
            val = result["result"]["_sync"]
            if val and val != "-":
                return val.split(",")
        except (KeyError, TypeError):
            pass
    return []


def sonos_join(master_device, slave_device):
    """Join a Sonos speaker to a master (multiroom group)."""
    try:
        master = soco.SoCo(master_device["host"])
        slave = soco.SoCo(slave_device["host"])
        slave.join(master)
    except Exception:
        pass


def sonos_unjoin(device):
    """Remove a Sonos speaker from its group."""
    try:
        sp = soco.SoCo(device["host"])
        sp.unjoin()
    except Exception:
        pass


def sonos_get_group(device):
    """Get Sonos group members. Returns list of UIDs."""
    try:
        sp = soco.SoCo(device["host"])
        return [m.uid for m in sp.group.members]
    except Exception:
        return []


def multiroom_add(master_device_id, slave_device_id):
    """Add a speaker to multiroom group. Returns (success, message)."""
    master = get_device(master_device_id)
    slave = get_device(slave_device_id)
    if not master or not slave:
        return False, "Gerät nicht gefunden"
    if master["type"] != slave["type"]:
        return False, "Geräte müssen vom gleichen Typ sein"

    if master["type"] == "lms":
        lms_sync(master, slave)
        return True, f"{slave['name']} zu {master['name']} hinzugefügt"
    if master["type"] == "sonos":
        sonos_join(master, slave)
        return True, f"{slave['name']} zu {master['name']} hinzugefügt"
    return False, "Unbekannter Typ"


def multiroom_remove(device_id):
    """Remove a speaker from its multiroom group."""
    device = get_device(device_id)
    if not device:
        return False, "Gerät nicht gefunden"
    if device["type"] == "lms":
        lms_unsync(device)
        return True, f"{device['name']} aus Gruppe entfernt"
    if device["type"] == "sonos":
        sonos_unjoin(device)
        return True, f"{device['name']} aus Gruppe entfernt"
    return False, "Unbekannter Typ"


def get_multiroom_state():
    """Get multiroom grouping state for all active devices.
    Returns dict: master_device_id -> [slave_device_ids]."""
    groups = {}
    active = get_active_casts()
    if not active:
        return groups

    active_device_ids = set(active.values())
    with _devices_lock:
        devices = list(_devices)

    for d in devices:
        if d["id"] not in active_device_ids:
            continue
        if d["type"] == "lms":
            synced = lms_get_sync_group(d)
            if synced:
                # Find device IDs for these player_ids
                slave_ids = []
                for pid in synced:
                    if pid != d.get("player_id"):
                        for d2 in devices:
                            if d2.get("player_id") == pid:
                                slave_ids.append(d2["id"])
                if slave_ids:
                    groups[d["id"]] = slave_ids
        elif d["type"] == "sonos":
            members = sonos_get_group(d)
            if len(members) > 1:
                slave_ids = []
                for uid in members:
                    if uid != d.get("uid"):
                        for d2 in devices:
                            if d2.get("uid") == uid:
                                slave_ids.append(d2["id"])
                if slave_ids:
                    groups[d["id"]] = slave_ids

    return groups
