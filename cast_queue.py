"""
In-memory queue manager for casting streams to devices.

Each device can have a queue of streams. Streams can be advanced manually
("next") or automatically via a timer (auto-switch after N minutes).
Queue state is lost on restart.
"""

import threading
import time

_queues = {}        # device_id -> [{"stream_id": int, "url": str, "name": str}, ...]
_queue_lock = threading.Lock()
_timers = {}        # device_id -> {"timer": threading.Timer, "minutes": int, "started": float}


def add_to_queue(device_id, stream_id, url, name):
    """Append a stream to a device's queue."""
    with _queue_lock:
        if device_id not in _queues:
            _queues[device_id] = []
        _queues[device_id].append({
            "stream_id": stream_id,
            "url": url,
            "name": name,
        })


def remove_from_queue(device_id, index):
    """Remove a queue item by position. Returns True if removed."""
    with _queue_lock:
        q = _queues.get(device_id, [])
        if 0 <= index < len(q):
            q.pop(index)
            return True
        return False


def get_queue(device_id):
    """Return a copy of the device's queue."""
    with _queue_lock:
        return list(_queues.get(device_id, []))


def clear_queue(device_id):
    """Clear all items from a device's queue."""
    with _queue_lock:
        _queues.pop(device_id, None)
    cancel_timer(device_id)


def advance_queue(device_id):
    """Pop the front item, cast it to the device. Returns the item played or None."""
    import cast

    with _queue_lock:
        q = _queues.get(device_id, [])
        if not q:
            return None
        item = q.pop(0)

    cast.cast_stream(item["url"], device_id)

    # If timer is active and queue still has items, restart the timer
    timer_info = _timers.get(device_id)
    if timer_info and timer_info.get("minutes"):
        minutes = timer_info["minutes"]
        # Only restart if there are more items
        with _queue_lock:
            has_more = len(_queues.get(device_id, [])) > 0
        if has_more:
            set_timer(device_id, minutes)
        else:
            cancel_timer(device_id)

    return item


def _timer_callback(device_id):
    """Called when the auto-switch timer fires."""
    advance_queue(device_id)


def set_timer(device_id, minutes):
    """Set an auto-advance timer. Cancels any existing timer first."""
    cancel_timer(device_id)
    if minutes <= 0:
        return
    t = threading.Timer(minutes * 60, _timer_callback, args=[device_id])
    t.daemon = True
    t.start()
    _timers[device_id] = {
        "timer": t,
        "minutes": minutes,
        "started": time.time(),
    }


def cancel_timer(device_id):
    """Cancel the auto-advance timer for a device."""
    info = _timers.pop(device_id, None)
    if info and info.get("timer"):
        info["timer"].cancel()


def get_timer_info(device_id):
    """Return timer info dict with 'minutes' and 'remaining', or None."""
    info = _timers.get(device_id)
    if not info:
        return None
    elapsed = time.time() - info["started"]
    total = info["minutes"] * 60
    remaining = max(0, total - elapsed)
    return {
        "minutes": info["minutes"],
        "remaining": round(remaining / 60, 1),
    }
