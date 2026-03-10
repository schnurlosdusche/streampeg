import os
import re
import shutil
import urllib.request
import urllib.parse
import json
from flask import Flask, render_template, request, redirect, url_for, jsonify
from config import HOST, PORT, SECRET_KEY, RECORDING_BASE, SMB_TARGET, USER_AGENTS, DEFAULT_USER_AGENT
import db
import process_manager
import cleanup
import sync
from auth import require_auth
from scheduler import SyncScheduler

app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler = None


def _sanitize_subdir(name):
    """Create a safe directory name from stream name."""
    s = re.sub(r"[^\w\s-]", "", name).strip()
    s = re.sub(r"[\s]+", "_", s)
    return s.lower() or "stream"


# --- Dashboard ---

@app.route("/")
@require_auth
def dashboard():
    streams = db.get_all_streams()
    statuses = {}
    for s in streams:
        statuses[s["id"]] = process_manager.get_status(s)
    return render_template("dashboard.html", streams=streams, statuses=statuses)


# --- Stream CRUD ---

@app.route("/stream/new", methods=["GET", "POST"])
@require_auth
def stream_new():
    if request.method == "POST":
        name = request.form["name"].strip()
        url = request.form["url"].strip()
        dest = request.form.get("dest_subdir", "").strip() or _sanitize_subdir(name)
        min_size = int(request.form.get("min_size_mb", 2))
        user_agent = request.form.get("user_agent", DEFAULT_USER_AGENT)
        if user_agent not in USER_AGENTS:
            user_agent = DEFAULT_USER_AGENT
        record_mode = request.form.get("record_mode", "streamripper")
        if record_mode not in ("streamripper", "ffmpeg_api", "ffmpeg_icy", "youtube"):
            record_mode = "streamripper"
        metadata_url = request.form.get("metadata_url", "").strip()
        split_offset = int(request.form.get("split_offset", 0))
        db.create_stream(name, url, dest, min_size, user_agent, record_mode, metadata_url, split_offset)
        return redirect(url_for("dashboard"))
    return render_template("stream_form.html", stream=None, user_agents=USER_AGENTS, default_ua=DEFAULT_USER_AGENT)


@app.route("/stream/<int:stream_id>/edit", methods=["GET", "POST"])
@require_auth
def stream_edit(stream_id):
    stream = db.get_stream(stream_id)
    if not stream:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form["name"].strip()
        url = request.form["url"].strip()
        dest = request.form.get("dest_subdir", "").strip() or stream["dest_subdir"]
        min_size = int(request.form.get("min_size_mb", 2))
        user_agent = request.form.get("user_agent", DEFAULT_USER_AGENT)
        if user_agent not in USER_AGENTS:
            user_agent = DEFAULT_USER_AGENT
        record_mode = request.form.get("record_mode", "streamripper")
        if record_mode not in ("streamripper", "ffmpeg_api", "ffmpeg_icy", "youtube"):
            record_mode = "streamripper"
        metadata_url = request.form.get("metadata_url", "").strip()
        split_offset = int(request.form.get("split_offset", 0))
        # Stop if running before changing config
        process_manager.stop_stream(stream_id)
        db.update_stream(stream_id, name, url, dest, min_size, user_agent, record_mode, metadata_url, split_offset)
        return redirect(url_for("dashboard"))
    return render_template("stream_form.html", stream=stream, user_agents=USER_AGENTS, default_ua=DEFAULT_USER_AGENT)


@app.route("/stream/<int:stream_id>/delete", methods=["POST"])
@require_auth
def stream_delete(stream_id):
    process_manager.stop_stream(stream_id)
    db.delete_stream(stream_id)
    return redirect(url_for("dashboard"))


# --- Stream Control ---

@app.route("/stream/<int:stream_id>/start", methods=["POST"])
@require_auth
def stream_start(stream_id):
    stream = db.get_stream(stream_id)
    if stream:
        process_manager.start_stream(stream)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for("dashboard"))


@app.route("/stream/<int:stream_id>/stop", methods=["POST"])
@require_auth
def stream_stop(stream_id):
    process_manager.stop_stream(stream_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for("dashboard"))


# --- Stream Detail ---

@app.route("/stream/<int:stream_id>")
@require_auth
def stream_detail(stream_id):
    stream = db.get_stream(stream_id)
    if not stream:
        return redirect(url_for("dashboard"))
    status = process_manager.get_status(stream)
    tracks = sync.get_track_history(stream)
    events = db.get_events(stream_id, limit=30)
    sync_logs = db.get_sync_logs(stream_id, limit=20)
    return render_template(
        "stream_detail.html",
        stream=stream, status=status, tracks=tracks,
        events=events, sync_logs=sync_logs,
    )


# --- Logs ---

@app.route("/logs")
@require_auth
def logs():
    sync_logs = db.get_sync_logs(limit=100)
    events = db.get_events(limit=100)
    return render_template("logs.html", sync_logs=sync_logs, events=events)


# --- Stream Test ---

def _detect_stream_metadata(stream_url, ua):
    """Detect metadata capabilities of a stream.

    Returns dict with:
      - has_icy_metadata: bool (inline ICY metadata via icy-metaint)
      - has_shoutcast_api: bool (Shoutcast v2 /currentsong endpoint)
      - streamripper_ok: bool (streamripper can connect)
      - metadata_url: str (Shoutcast API URL if available)
      - current_song: str (current song if available)
      - recommended_mode: "streamripper" | "ffmpeg_api" | "ffmpeg_icy"
    """
    result = {
        "has_icy_metadata": False,
        "has_shoutcast_api": False,
        "streamripper_ok": False,
        "metadata_url": "",
        "current_song": "",
        "recommended_mode": "streamripper",
    }

    # 1. Check for inline ICY metadata
    try:
        req = urllib.request.Request(stream_url, method="GET",
                                      headers={"User-Agent": ua, "Icy-MetaData": "1"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            icy_metaint = resp.headers.get("icy-metaint")
            if icy_metaint:
                result["has_icy_metadata"] = True
            resp.read(1024)
    except Exception:
        pass

    # 2. Check Shoutcast v2 API
    parsed = urllib.parse.urlparse(stream_url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base += f":{parsed.port}"
    currentsong_url = f"{base}/currentsong?sid=1"

    try:
        req = urllib.request.Request(currentsong_url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=5) as resp:
            song = resp.read().decode("utf-8", errors="replace").strip()
            if song and len(song) < 500:
                result["has_shoutcast_api"] = True
                result["metadata_url"] = currentsong_url
                result["current_song"] = song
    except Exception:
        pass

    # 3. Quick streamripper connectivity check
    import subprocess
    try:
        sr_result = subprocess.run(
            ["streamripper", stream_url, "-d", "/tmp", "-l", "3",
             "--quiet", "-o", "never", "-u", ua],
            capture_output=True, text=True, timeout=10,
        )
        result["streamripper_ok"] = (sr_result.returncode == 0 or sr_result.returncode == 2)
    except Exception:
        pass
    # Cleanup
    subprocess.run(["rm", "-rf", "/tmp/sr_test.mp3"], capture_output=True, timeout=3)

    # 4. Determine recommended mode
    if result["has_icy_metadata"] and result["streamripper_ok"]:
        result["recommended_mode"] = "streamripper"
    elif result["has_icy_metadata"] and not result["streamripper_ok"]:
        result["recommended_mode"] = "ffmpeg_icy"
    elif result["has_shoutcast_api"]:
        result["recommended_mode"] = "ffmpeg_api"
    else:
        result["recommended_mode"] = "streamripper"

    return result


@app.route("/api/test-stream")
@require_auth
def api_test_stream():
    """Test if a stream URL is reachable and recordable, detect metadata capabilities."""
    url = request.args.get("url", "")
    if not url:
        return jsonify({"ok": False, "error": "Keine URL angegeben"})

    import subprocess
    ua_key = request.args.get("user_agent", DEFAULT_USER_AGENT)
    ua = USER_AGENTS.get(ua_key, USER_AGENTS[DEFAULT_USER_AGENT])

    try:
        # First: check if stream is reachable at all
        req = urllib.request.Request(url, method="GET",
                                      headers={"User-Agent": ua, "Icy-MetaData": "1"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            content_type = resp.headers.get("Content-Type", "")
            icy_name = resp.headers.get("icy-name", "")
            chunk = resp.read(4096)
            if len(chunk) == 0 or not ("audio" in content_type or "mpegurl" in content_type
                                        or "ogg" in content_type or icy_name):
                return jsonify({"ok": False, "error": f"Kein Audio-Stream ({content_type})"})

        # Stream is reachable — now detect metadata capabilities
        meta = _detect_stream_metadata(url, ua)

        message = f"Stream erreichbar: {icy_name or content_type}"
        if meta["recommended_mode"] == "streamripper":
            message += " | ICY-Metadaten vorhanden, Streamripper OK"
        elif meta["recommended_mode"] == "ffmpeg_icy":
            message += " | ICY-Metadaten vorhanden, aber Streamripper scheitert (HTTPS-Redirect?) - FFmpeg+ICY empfohlen"
        elif meta["recommended_mode"] == "ffmpeg_api":
            message += f" | Keine ICY-Metadaten, Shoutcast-API verfuegbar: {meta['current_song']}"
        else:
            message += " | Keine Metadaten erkannt"

        return jsonify({
            "ok": True,
            "message": message,
            "content_type": content_type,
            "icy_name": icy_name,
            "metadata": meta,
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# --- Radio Browser ---

RADIO_BROWSER_API = "https://de1.api.radio-browser.info"

@app.route("/browse")
@require_auth
def browse():
    return render_template("browse.html")


@app.route("/api/browse/tags")
@require_auth
def api_browse_tags():
    """Get popular tags/genres from radio-browser.info."""
    try:
        url = f"{RADIO_BROWSER_API}/json/tags?order=stationcount&reverse=true&limit=80&hidebroken=true"
        req = urllib.request.Request(url, headers={"User-Agent": "Streampeg/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            tags = json.loads(resp.read())
        # Filter to tags with at least 20 stations
        tags = [t for t in tags if t.get("stationcount", 0) >= 20]
        return jsonify(tags)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/browse/search")
@require_auth
def api_browse_search():
    """Search stations by tag or name."""
    tag = request.args.get("tag", "")
    name = request.args.get("name", "")
    limit = min(int(request.args.get("limit", 50)), 200)

    try:
        if tag:
            url = f"{RADIO_BROWSER_API}/json/stations/bytagexact/{urllib.parse.quote(tag)}?order=clickcount&reverse=true&limit={limit}&hidebroken=true"
        elif name:
            url = f"{RADIO_BROWSER_API}/json/stations/byname/{urllib.parse.quote(name)}?order=clickcount&reverse=true&limit={limit}&hidebroken=true"
        else:
            return jsonify([])

        req = urllib.request.Request(url, headers={"User-Agent": "Streampeg/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            stations = json.loads(resp.read())

        # Filter: only stations with a resolved URL and audio codec
        result = []
        for s in stations:
            if not s.get("url_resolved") and not s.get("url"):
                continue
            result.append({
                "name": s.get("name", "").strip(),
                "url": s.get("url_resolved") or s.get("url"),
                "tags": s.get("tags", ""),
                "country": s.get("country", ""),
                "codec": s.get("codec", ""),
                "bitrate": s.get("bitrate", 0),
                "votes": s.get("votes", 0),
                "favicon": s.get("favicon", ""),
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# --- API ---

@app.route("/api/status")
@require_auth
def api_status():
    streams = db.get_all_streams()
    result = []
    for s in streams:
        st = process_manager.get_status(s)
        st["id"] = s["id"]
        st["name"] = s["name"]
        st["url"] = s["url"]
        result.append(st)
    return jsonify({"streams": result, "disk": _get_disk_info()})


@app.route("/api/disk")
@require_auth
def api_disk():
    return jsonify(_get_disk_info())


@app.route("/api/sync/<int:stream_id>", methods=["POST"])
@require_auth
def api_sync(stream_id):
    stream = db.get_stream(stream_id)
    if not stream:
        return jsonify({"error": "Stream nicht gefunden"}), 404

    dest = os.path.join(RECORDING_BASE, stream["dest_subdir"])
    if os.path.isdir(dest):
        cleanup.run_all(dest, stream["min_size_mb"])

    result = sync.sync_stream(stream)
    return jsonify(result)


@app.route("/api/restart-service", methods=["POST"])
@require_auth
def api_restart_service():
    """Restart the application. Docker: exit and let restart policy handle it.
       Bare metal: use systemctl."""
    if os.environ.get("RUNNING_IN_DOCKER"):
        import threading
        def _exit():
            import time
            time.sleep(1)
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
        return jsonify({"ok": True, "message": "Container wird neu gestartet..."})

    import subprocess
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "streamripper-ui"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return jsonify({"ok": True, "message": "Service wird neu gestartet..."})
        else:
            return jsonify({"ok": False, "error": result.stderr.strip()}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _get_disk_info():
    info = {}
    try:
        usage = shutil.disk_usage(RECORDING_BASE)
        info["worker_free_gb"] = round(usage.free / (1024**3), 1)
        info["worker_total_gb"] = round(usage.total / (1024**3), 1)
    except OSError:
        info["worker_free_gb"] = 0
        info["worker_total_gb"] = 0

    try:
        usage = shutil.disk_usage(SMB_TARGET)
        info["nas_free_gb"] = round(usage.free / (1024**3), 1)
        info["nas_total_gb"] = round(usage.total / (1024**3), 1)
    except OSError:
        info["nas_free_gb"] = 0
        info["nas_total_gb"] = 0

    return info


if __name__ == "__main__":
    db.init_db()

    # Adopt existing streamripper processes
    streams = db.get_all_streams()
    if streams:
        process_manager.adopt_existing_processes(streams)

    # Start background scheduler
    scheduler = SyncScheduler(app)
    scheduler.start()

    app.run(host=HOST, port=PORT, threaded=True)
