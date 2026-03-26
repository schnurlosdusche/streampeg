"""
Cast module — discover and stream to network audio devices.

Supported backends:
  - SlimProto (embedded server for Squeezelite players — no LMS needed) — ACTIVE
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

# Currently casting: device_id -> stream_id  (one device plays one stream,
# but the same stream may be cast to multiple devices simultaneously)
_active_casts = {}
_casts_lock = threading.Lock()

# ICY metadata cache for streams that are cast but not recording
_icy_cache = {}       # stream_id -> {"track": str, "cover_url": str|None, "ts": float}
_icy_cache_lock = threading.Lock()
_ICY_POLL_INTERVAL = 10  # seconds between ICY polls per stream


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


def lms_pause(device):
    """Toggle pause on an LMS player."""
    _lms_request(
        device["host"], device["port"], device["player_id"],
        ["pause"],
    )


def lms_get_current_track(device):
    """Get current track title and artist from LMS player."""
    result = _lms_request(
        device["host"], device["port"], device["player_id"],
        ["status", "-", "1", "tags:aAlc"],
    )
    if not result:
        return None, None, None
    r = result.get("result", {})
    # remoteMeta has parsed artist/title for remote streams
    meta = r.get("remoteMeta", {})
    artist = meta.get("artist", "")
    title = meta.get("title", "")
    cover_id = meta.get("coverid", "")
    # Build cover URL from LMS if available
    cover_url = None
    if cover_id:
        cover_url = f"http://{device['host']}:{device['port']}/music/{cover_id}/cover.jpg"
    # Combine artist - title
    if artist and title:
        track = f"{artist} - {title}"
    elif r.get("current_title"):
        track = r["current_title"]
    else:
        track = title or None
    return track, cover_url, r.get("mode")


def seek_device(device_id, position_seconds):
    """Seek a device to a specific position in seconds."""
    device = _find_device(device_id)
    if not device:
        return False, "Device not found"
    if device["type"] == "slim":
        import slimproto
        ok = slimproto.seek_player(device["player_id"], position_seconds)
        return ok, "OK" if ok else "SlimProto seek not supported"
    elif device["type"] == "lms":
        result = _lms_request(
            device["host"], device["port"], device["player_id"],
            ["time", str(position_seconds)],
        )
        return bool(result), "OK" if result else "LMS seek failed"
    elif device["type"] == "sonos":
        try:
            sp = soco.SoCo(device["host"])
            h = int(position_seconds // 3600)
            m = int((position_seconds % 3600) // 60)
            s = int(position_seconds % 60)
            sp.seek(f"{h}:{m:02d}:{s:02d}")
            return True, "OK"
        except Exception as e:
            return False, str(e)
    return False, "Unknown device type"


def get_device_playback_mode(device_id):
    """Return playback mode for a device: 'play', 'stop', 'pause', or None."""
    device = _find_device(device_id)
    if not device:
        return None
    if device["type"] == "slim":
        import slimproto
        return slimproto.get_state(device["player_id"])
    elif device["type"] == "lms":
        _track, _cover, mode = lms_get_current_track(device)
        return mode  # 'play', 'stop', 'pause'
    elif device["type"] == "sonos":
        try:
            sp = soco.SoCo(device["host"])
            info = sp.get_current_transport_info()
            state = info.get("current_transport_state", "")
            if state == "PLAYING":
                return "play"
            elif state == "PAUSED_PLAYBACK":
                return "pause"
            else:
                return "stop"
        except Exception:
            return None
    return None


def _find_device(device_id):
    """Find a device by its ID from discovered devices."""
    devices = discover_devices()
    for d in devices:
        if d["id"] == device_id:
            return d
    return None


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

    # SlimProto (embedded server — direct Squeezelite connections, no LMS)
    try:
        import slimproto
        if slimproto.is_running():
            slim_players = slimproto.get_players()
            devices.extend(slim_players)
    except Exception:
        pass

    # Collect SlimProto player IDs to avoid duplicates from LMS
    slim_player_ids = {d.get("player_id") for d in devices if d["type"] == "slim"}

    # LMS
    lms_host = _discover_lms_server()
    if lms_host:
        players = _discover_lms_players(lms_host)
        for p in players:
            # Skip players already connected via SlimProto
            if p.get("player_id") in slim_player_ids:
                continue
            p["enabled"] = True
            devices.append(p)

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

def _sonos_play_uri(sp, name, stream_url):
    """Play a stream URL on a Sonos speaker with DIDL-Lite metadata."""
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
    ).format(name=escape(name), url=escape(stream_url))
    sp.play_uri(stream_url, meta=didl)


def sonos_play(device, stream_url):
    """Tell a Sonos speaker to play a stream URL. Returns (success, error_msg)."""
    try:
        sp = soco.SoCo(device["host"])
        # Unjoin from any group so this speaker becomes its own coordinator
        try:
            if sp.group and sp.group.coordinator.ip_address != sp.ip_address:
                sp.unjoin()
                import time as _t
                _t.sleep(0.5)
        except Exception:
            pass
        _sonos_play_uri(sp, device.get("name", "Stream"), stream_url)
        return True, None
    except Exception as e:
        return False, str(e)


def _sonos_get_coordinator(sp):
    """Return the coordinator for this speaker, or sp itself if already coordinator."""
    try:
        coord = sp.group.coordinator
        if coord.ip_address != sp.ip_address:
            return coord
    except Exception:
        pass
    return sp


def sonos_stop(device):
    """Stop playback on a Sonos speaker."""
    try:
        sp = soco.SoCo(device["host"])
        sp = _sonos_get_coordinator(sp)
        sp.stop()
    except Exception:
        pass


def sonos_pause(device, stream_url=None):
    """Toggle pause on a Sonos speaker. For streams, resume = replay URL."""
    try:
        sp = soco.SoCo(device["host"])
        sp = _sonos_get_coordinator(sp)
        info = sp.get_current_transport_info()
        state = info.get("current_transport_state", "")
        if state in ("PAUSED_PLAYBACK", "STOPPED"):
            if stream_url:
                # Streams can't truly resume — replay with DIDL-Lite metadata
                _sonos_play_uri(sp, device.get("name", "Stream"), stream_url)
            else:
                sp.play()
        else:
            sp.pause()
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

    if device["type"] == "slim":
        import slimproto
        ok = slimproto.play_url(device["player_id"], stream_url)
        if ok:
            return True, f"Stream gestartet auf {device['name']}"
        return False, f"SlimProto-Fehler auf {device['name']}"

    if device["type"] == "lms":
        lms_play(device, stream_url)
        return True, f"Stream gestartet auf {device['name']}"

    if device["type"] == "sonos":
        ok, err = sonos_play(device, stream_url)
        if ok:
            return True, f"Stream gestartet auf {device['name']}"
        return False, f"Sonos-Fehler auf {device['name']}: {err}"

    return False, f"Unbekannter Gerätetyp: {device['type']}"


def stop_cast(device_id):
    """Stop casting on a device. Returns (success, message)."""
    device = get_device(device_id)
    if not device:
        return False, "Gerät nicht gefunden"

    if device["type"] == "slim":
        import slimproto
        slimproto.stop_player(device["player_id"])
        return True, f"Wiedergabe gestoppt auf {device['name']}"

    if device["type"] == "lms":
        lms_stop(device)
        return True, f"Wiedergabe gestoppt auf {device['name']}"

    if device["type"] == "sonos":
        sonos_stop(device)
        return True, f"Wiedergabe gestoppt auf {device['name']}"

    return False, f"Unbekannter Gerätetyp: {device['type']}"


def pause_cast(device_id, stream_url=None):
    """Toggle pause on a device. Returns (success, message)."""
    device = get_device(device_id)
    if not device:
        return False, "Device not found"

    if device["type"] == "slim":
        import slimproto
        slimproto.pause_player(device["player_id"])
        return True, f"Pause toggled on {device['name']}"

    if device["type"] == "lms":
        lms_pause(device)
        return True, f"Pause toggled on {device['name']}"

    if device["type"] == "sonos":
        sonos_pause(device, stream_url=stream_url)
        return True, f"Pause toggled on {device['name']}"

    return False, f"Unknown device type: {device['type']}"


def _persist_casts():
    """Save active casts to DB so they survive restarts."""
    from db import set_setting
    with _casts_lock:
        data = dict(_active_casts)
    set_setting("active_casts", json.dumps(data))


def _load_casts():
    """Load active casts from DB on startup.
    Stored format is device_id -> stream_id (string keys in JSON).
    Migrates old format (stream_id -> device_id) automatically."""
    from db import get_setting
    raw = get_setting("active_casts")
    if raw:
        try:
            data = json.loads(raw)
            with _casts_lock:
                for k, v in data.items():
                    # Detect old format: key is numeric (stream_id), value is device string
                    if str(k).isdigit() and isinstance(v, str) and not str(v).isdigit():
                        # Old format: stream_id -> device_id → invert
                        _active_casts[v] = int(k)
                    else:
                        # New format: device_id -> stream_id
                        _active_casts[k] = int(v) if isinstance(v, (int, float)) or str(v).isdigit() else v
        except (ValueError, TypeError):
            pass
    _persist_casts()  # re-save in new format


def set_active_cast(stream_id, device_id):
    """Track which stream is casting to which device."""
    with _casts_lock:
        _active_casts[device_id] = stream_id
    _persist_casts()


def remove_active_cast_by_device(device_id):
    """Remove active cast tracking for a device."""
    with _casts_lock:
        _active_casts.pop(device_id, None)
    _persist_casts()


def remove_active_casts_for_stream(stream_id):
    """Remove all active casts for a given stream (all devices)."""
    with _casts_lock:
        to_remove = [did for did, sid in _active_casts.items() if sid == stream_id]
        for did in to_remove:
            del _active_casts[did]
    _persist_casts()


def get_active_casts():
    """Return dict of device_id -> stream_id."""
    with _casts_lock:
        return dict(_active_casts)


def get_active_casts_by_stream():
    """Return dict of stream_id -> [device_ids] for API compatibility."""
    with _casts_lock:
        result = {}
        for device_id, stream_id in _active_casts.items():
            result.setdefault(stream_id, []).append(device_id)
        return result


def get_active_cast_for_stream(stream_id):
    """Return first device_id if stream is being cast, else None."""
    with _casts_lock:
        for device_id, sid in _active_casts.items():
            if sid == stream_id:
                return device_id
    return None


def get_devices_for_stream(stream_id):
    """Return all device_ids casting a given stream."""
    with _casts_lock:
        return [did for did, sid in _active_casts.items() if sid == stream_id]


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
    if device["type"] == "slim":
        import slimproto
        return slimproto.get_volume(device["player_id"])
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
    if device["type"] == "slim":
        import slimproto
        slimproto.set_volume(device["player_id"], level)
    elif device["type"] == "lms":
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


# ===== ICY metadata polling for non-recording casts ========================

import re as _re

def _fetch_icy_title(url, ua="VLC/3.0.21", timeout=5):
    """Quick ICY title fetch — reads just enough to get StreamTitle."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Icy-MetaData": "1",
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        metaint_str = resp.headers.get("icy-metaint")
        if not metaint_str:
            resp.close()
            return None
        metaint = int(metaint_str)
        # Read one block + metadata
        resp.read(metaint)
        meta_len = resp.read(1)
        if not meta_len:
            resp.close()
            return None
        length = meta_len[0] * 16
        if length > 0:
            meta = resp.read(length).decode("utf-8", errors="replace").rstrip("\x00")
            m = _re.search(r"StreamTitle='([^']*)'", meta)
            if m and m.group(1).strip():
                resp.close()
                return m.group(1).strip()
        resp.close()
    except Exception:
        pass
    return None


def poll_icy_for_cast(stream_id, stream_url):
    """Fetch ICY title for a cast that is not recording. Updates cache."""
    with _icy_cache_lock:
        cached = _icy_cache.get(stream_id)
        if cached and time.time() - cached["ts"] < _ICY_POLL_INTERVAL:
            return  # still fresh

    title = _fetch_icy_title(stream_url)
    if title:
        cover_url = None
        try:
            import cover_art
            cover_url = cover_art.get_cover_url(stream_id, title)
        except Exception:
            pass
        with _icy_cache_lock:
            _icy_cache[stream_id] = {"track": title, "cover_url": cover_url, "ts": time.time()}


def get_icy_cache(stream_id):
    """Return cached ICY info for a stream, or None."""
    with _icy_cache_lock:
        cached = _icy_cache.get(stream_id)
        if cached and time.time() - cached["ts"] < 30:
            return cached
    return None


def clear_icy_cache(stream_id):
    """Clear ICY cache when a stream stops being cast."""
    with _icy_cache_lock:
        _icy_cache.pop(stream_id, None)


def get_cast_track_info(device_id, stream_id, stream_url):
    """Get current track and cover for a cast, using the best source available.
    LMS: query the server directly (most reliable).
    Sonos: fall back to ICY metadata polling.
    Cover art: always via iTunes (cover_art module).
    Returns (track, cover_url)."""
    import cover_art

    device = get_device(device_id)
    if not device:
        return None, None

    track = None

    if device["type"] == "lms":
        t, _lms_cover, mode = lms_get_current_track(device)
        if t:
            track = t

    # Sonos or LMS without track info: use ICY cache
    if not track:
        icy = get_icy_cache(stream_id)
        if not icy:
            poll_icy_for_cast(stream_id, stream_url)
            icy = get_icy_cache(stream_id)
        if icy:
            track = icy["track"]

    # Cover art via iTunes lookup (non-blocking, cached)
    cover_url = cover_art.get_cover_url(stream_id, track) if track else None

    return track, cover_url


# ===== Background ICY poller for active casts ================================

_icy_poller_running = False

def _icy_poller_loop():
    """Background thread that continuously polls ICY metadata for active casts
    that are not being recorded."""
    import db as _db
    import process_manager as _pm
    while True:
        try:
            active = get_active_casts()  # device_id -> stream_id
            if active:
                # Get unique stream_ids
                stream_ids = set(active.values())
                all_streams = {s["id"]: s for s in _db.get_all_streams()}
                for stream_id in stream_ids:
                    stream = all_streams.get(stream_id)
                    if not stream:
                        continue
                    # Check if stream is being recorded
                    st = _pm.get_status(stream)
                    if st.get("running") and st.get("current_track"):
                        continue  # recording provides track info
                    # Not recording or no track — poll ICY
                    poll_icy_for_cast(stream_id, stream["url"])
        except Exception:
            pass
        time.sleep(_ICY_POLL_INTERVAL)


def start_icy_poller():
    """Start the background ICY poller thread (called once at app startup)."""
    global _icy_poller_running
    if _icy_poller_running:
        return
    _icy_poller_running = True
    t = threading.Thread(target=_icy_poller_loop, daemon=True)
    t.start()


# Load persisted casts on module import
_load_casts()
