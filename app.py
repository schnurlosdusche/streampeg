import os
import re
import shutil
import time
import atexit
import signal
import urllib.request
import urllib.parse
import json
from flask import Flask, render_template, request, redirect, url_for, jsonify
from config import HOST, PORT, SECRET_KEY, RECORDING_BASE, SMB_TARGET, USER_AGENTS, DEFAULT_USER_AGENT, MIN_BITRATE
import db
import process_manager
import cleanup
import sync
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
def dashboard():
    streams = db.get_all_streams()
    statuses = {}
    for s in streams:
        statuses[s["id"]] = process_manager.get_status(s)
    return render_template("dashboard.html", streams=streams, statuses=statuses)


# --- Stream CRUD ---

@app.route("/stream/new", methods=["GET", "POST"])
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
        trim_start = int(request.form.get("trim_start", 0))
        trim_end = int(request.form.get("trim_end", 0))
        skip_words = request.form.get("skip_words", "").strip()
        db.create_stream(name, url, dest, min_size, user_agent, record_mode, metadata_url, split_offset,
                         trim_start, trim_end, skip_words)
        return redirect(url_for("dashboard"))
    prefill = {
        "name": request.args.get("name", ""),
        "url": request.args.get("url", ""),
        "record_mode": request.args.get("record_mode", ""),
    }
    return render_template("stream_form.html", stream=None, user_agents=USER_AGENTS, default_ua=DEFAULT_USER_AGENT, prefill=prefill)


@app.route("/stream/<int:stream_id>/edit", methods=["GET", "POST"])
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
        trim_start = int(request.form.get("trim_start", 0))
        trim_end = int(request.form.get("trim_end", 0))
        skip_words = request.form.get("skip_words", "").strip()
        # Stop if running before changing config
        process_manager.stop_stream(stream_id)
        db.update_stream(stream_id, name, url, dest, min_size, user_agent, record_mode, metadata_url, split_offset,
                         trim_start, trim_end, skip_words)
        return redirect(url_for("dashboard"))
    return render_template("stream_form.html", stream=stream, user_agents=USER_AGENTS, default_ua=DEFAULT_USER_AGENT)


@app.route("/stream/<int:stream_id>/delete", methods=["POST"])
def stream_delete(stream_id):
    process_manager.stop_stream(stream_id)
    db.delete_stream(stream_id)
    return redirect(url_for("dashboard"))


# --- Stream Control ---

@app.route("/stream/<int:stream_id>/start", methods=["POST"])
def stream_start(stream_id):
    stream = db.get_stream(stream_id)
    if stream:
        try:
            process_manager.start_stream(stream)
        except process_manager.BitrateError as e:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"ok": False, "error": str(e)}), 400
            return redirect(url_for("dashboard"))
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for("dashboard"))


@app.route("/stream/<int:stream_id>/stop", methods=["POST"])
def stream_stop(stream_id):
    process_manager.stop_stream(stream_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for("dashboard"))


# --- Stream Listen (Proxy) ---

@app.route("/stream/<int:stream_id>/listen")
def stream_listen_proxy(stream_id):
    """Proxy the audio stream to avoid CORS issues."""
    stream = db.get_stream(stream_id)
    if not stream:
        return "Not found", 404

    ua_key = stream["user_agent"] if stream["user_agent"] else DEFAULT_USER_AGENT
    ua = USER_AGENTS.get(ua_key, USER_AGENTS[DEFAULT_USER_AGENT])

    def generate():
        req = urllib.request.Request(stream["url"], headers={"User-Agent": ua, "Icy-MetaData": "0"})
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                yield chunk
        except Exception:
            pass

    # Try to determine content type
    content_type = "audio/mpeg"
    url_lower = stream["url"].lower()
    if ".ogg" in url_lower or "vorbis" in url_lower:
        content_type = "audio/ogg"
    elif ".aac" in url_lower or "aacp" in url_lower:
        content_type = "audio/aac"

    return Response(generate(), mimetype=content_type,
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


# --- Stream Detail ---

@app.route("/stream/<int:stream_id>")
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
def logs():
    sync_logs = db.get_sync_logs(limit=100)
    events = db.get_events(limit=100)
    return render_template("logs.html", sync_logs=sync_logs, events=events)


# --- Stream Test ---

from flask import Response

@app.route("/api/test-stream")
def api_test_stream():
    """Stream-Tester mit Server-Sent Events für Live-Updates."""
    url = request.args.get("url", "")
    if not url:
        return jsonify({"ok": False, "error": "Keine URL angegeben"})

    ua_key = request.args.get("user_agent", DEFAULT_USER_AGENT)
    ua = USER_AGENTS.get(ua_key, USER_AGENTS[DEFAULT_USER_AGENT])

    def generate():
        import urllib.parse as _up
        from stream_tester import (_test_http, _test_icy_deep, _test_shoutcast_api,
                                   _test_icecast_api, _test_tunein,
                                   _test_streamripper, _test_ffmpeg, _recommend,
                                   _save_method, _get_known_methods)

        def send(data):
            return f"data: {json.dumps(data)}\n\n"

        parsed = _up.urlparse(url)
        stream_host = parsed.hostname or ""
        stream_path = parsed.path or ""

        # Step 1: HTTP
        yield send({"step": 1, "label": "HTTP-Verbindung testen...", "total": 7})
        http = _test_http(url, ua, 15)
        effective_url = http.get("effective_url", url)
        detail = ""
        if http.get("redirected"):
            detail += "Redirect, "
        if http.get("https"):
            detail += "HTTPS, "
        if http.get("content_type"):
            detail += http["content_type"]
        yield send({"step": 1, "result": "OK" if http.get("ok") else "Fehler", "detail": detail.strip(", "), "ok": http.get("ok", False)})

        if not http.get("ok"):
            yield send({"done": True, "recommendation": "FEHLER: Stream nicht erreichbar", "suitable": False})
            return

        # Step 2: ICY (deep probe)
        yield send({"step": 2, "label": "ICY-Metadaten (Deep Probe, bis 30s)...", "total": 7})
        icy = _test_icy_deep(effective_url, ua)
        if icy.get("title"):
            detail = f"Title: {icy['title'][:50]}"
        elif icy.get("has_metaint"):
            detail = f"{icy.get('blocks_read', 0)} Blöcke in {icy.get('seconds', 0)}s - kein Titel"
        else:
            detail = icy.get("error", "nicht verfügbar")
        yield send({"step": 2, "result": "OK" if icy.get("title") else ("Kein Titel" if icy.get("has_metaint") else "Fehlt"), "detail": detail, "ok": bool(icy.get("title"))})
        _save_method(stream_host, stream_path, "icy", effective_url,
                     has_titles=bool(icy.get("title")), sample_title=icy.get("title", ""))

        # Step 3: Shoutcast API
        yield send({"step": 3, "label": "Shoutcast API prüfen...", "total": 7})
        api_result = _test_shoutcast_api(effective_url, ua)
        detail = api_result.get("title", "") if api_result.get("ok") else "nicht verfügbar"
        yield send({"step": 3, "result": "OK" if api_result.get("ok") else "Fehlt", "detail": detail[:60], "ok": api_result.get("ok", False)})

        # Step 4: Icecast API
        yield send({"step": 4, "label": "Icecast Status-API prüfen...", "total": 7})
        eff_parsed = _up.urlparse(effective_url)
        icecast = _test_icecast_api(effective_url, eff_parsed.path, ua)
        if icecast.get("ok"):
            detail = f"Title: {icecast.get('title', '')[:50]}"
        elif icecast.get("mountpoint"):
            detail = f"Mountpoint '{icecast['mountpoint']}', kein Titel"
        else:
            detail = "nicht verfügbar"
        yield send({"step": 4, "result": "OK" if icecast.get("ok") else "Fehlt", "detail": detail, "ok": icecast.get("ok", False)})

        # Step 5: TuneIn
        yield send({"step": 5, "label": "TuneIn Now-Playing prüfen...", "total": 7})
        tunein = _test_tunein(url, http.get("icy_name", ""))
        if tunein.get("ok"):
            detail = f"{tunein.get('station_name', '')}: {tunein.get('title', '')[:40]}"
        elif tunein.get("station_id"):
            detail = f"Station gefunden, {tunein.get('note', 'kein Titel')}"
        else:
            detail = tunein.get("error", "nicht gefunden")
        yield send({"step": 5, "result": "OK" if tunein.get("ok") else "Fehlt", "detail": detail[:60], "ok": tunein.get("ok", False)})

        # Step 6: Streamripper
        yield send({"step": 6, "label": "Streamripper testen (10s)...", "total": 7})
        sr = _test_streamripper(url, ua)
        detail = f"{sr.get('files_created', 0)} Dateien" if sr.get("ok") else sr.get("error", "fehlgeschlagen")[:60]
        yield send({"step": 6, "result": "OK" if sr.get("ok") else "Fehler", "detail": detail, "ok": sr.get("ok", False)})

        # Step 7: FFmpeg
        yield send({"step": 7, "label": "FFmpeg testen (10s)...", "total": 7})
        ffmpeg = _test_ffmpeg(effective_url, ua)
        if ffmpeg.get("ok"):
            detail = f"{round(ffmpeg.get('file_size', 0) / 1024)} KB in 10s"
        else:
            detail = ffmpeg.get("error", "fehlgeschlagen")[:60]
        yield send({"step": 7, "result": "OK" if ffmpeg.get("ok") else "Fehler", "detail": detail, "ok": ffmpeg.get("ok", False)})

        # Recommendation
        known = _get_known_methods(stream_host)
        results = {
            "url": url, "host": stream_host,
            "tests": {"http": http, "icy": icy, "api": api_result, "icecast": icecast,
                       "tunein": tunein, "streamripper": sr, "ffmpeg": ffmpeg},
            "known_methods": known,
        }
        rec = _recommend(results)

        if isinstance(rec, str):
            yield send({"done": True, "recommendation": rec, "suitable": False})
        else:
            modes = [{"mode": r["mode"], "score": r["score"], "reason": r["reason"]} for r in rec]
            yield send({"done": True, "recommendation": modes, "suitable": True, "best_mode": rec[0]["mode"]})

    return Response(generate(), mimetype="text/event-stream")


# --- Radio Browser ---

RADIO_BROWSER_API = "https://de1.api.radio-browser.info"

@app.route("/browse")
def browse():
    return render_template("browse.html")


@app.route("/api/browse/tags")
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

        # Filter: only stations with a resolved URL, audio codec, and sufficient bitrate
        result = []
        for s in stations:
            if not s.get("url_resolved") and not s.get("url"):
                continue
            if s.get("bitrate", 0) > 0 and s.get("bitrate", 0) < MIN_BITRATE:
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
def api_disk():
    return jsonify(_get_disk_info())


@app.route("/api/sync/<int:stream_id>", methods=["POST"])
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


_disk_cache = {"info": None, "ts": 0}

def _get_disk_info():
    now = time.time()
    if _disk_cache["info"] and (now - _disk_cache["ts"]) < 60:
        return _disk_cache["info"]

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

    _disk_cache["info"] = info
    _disk_cache["ts"] = now
    return info


def _shutdown():
    """Graceful shutdown: stop all streams, cleanup incomplete files."""
    print("Shutting down: stopping all streams...")
    process_manager.stop_all_streams()
    streams = db.get_all_streams()
    process_manager.cleanup_incomplete(streams)
    if scheduler:
        scheduler.stop()
    print("Shutdown complete.")


if __name__ == "__main__":
    db.init_db()

    # Register shutdown handler
    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, lambda sig, frame: (atexit._run_exitfuncs(), exit(0)))

    # Cleanup incomplete files from previous run
    streams = db.get_all_streams()
    process_manager.cleanup_incomplete(streams)

    # Adopt existing streamripper processes
    if streams:
        process_manager.adopt_existing_processes(streams)

    # Start background scheduler
    scheduler = SyncScheduler(app)
    scheduler.start()

    app.run(host=HOST, port=PORT, threaded=True)
