import os
import re
import shutil
import time
import atexit
import signal
import urllib.request
import urllib.parse
import json
import struct
import requests as req_lib
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
from config import HOST, PORT, SECRET_KEY, RECORDING_BASE, USER_AGENTS, DEFAULT_USER_AGENT, MIN_BITRATE
import db
import process_manager
import cleanup
import sync
import module_manager
import cast
import cast_queue
import cover_art
import autotag
import dlna_server
import library
import i18n
from scheduler import SyncScheduler

VERSION = "0.0.42a"

app = Flask(__name__)
app.secret_key = SECRET_KEY
scheduler = None

# Make t(), language info and version available in all templates
@app.context_processor
def inject_globals():
    lang = i18n.get_language()
    return {
        "t": lambda key: i18n.t(key, lang),
        "current_lang": lang,
        "all_translations_json": i18n.get_all_translations(lang),
        "version": VERSION,
    }


def _sanitize_subdir(name):
    """Create a safe directory name from stream name."""
    s = re.sub(r"[^\w\s-]", "", name).strip()
    s = re.sub(r"[\s]+", "_", s)
    return s.lower() or "stream"


# --- Dashboard ---

@app.route("/")
def dashboard():
    streams = db.get_all_streams()
    # Only pass lightweight in-memory status (no NAS glob, no DB stats)
    statuses = {}
    for s in streams:
        statuses[s["id"]] = process_manager.get_status_fast(s)
    return render_template("dashboard.html", streams=streams, statuses=statuses,
                           module_icons=module_manager.get_module_icons())


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
        if record_mode not in module_manager.get_all_record_modes():
            record_mode = "streamripper"
        metadata_url = request.form.get("metadata_url", "").strip()
        split_offset = int(request.form.get("split_offset", 0))
        trim_start = int(request.form.get("trim_start", 0))
        trim_end = int(request.form.get("trim_end", 0))
        skip_words = request.form.get("skip_words", "").strip()
        dl_fallback = 1 if request.form.get("dl_fallback") else 0
        db.create_stream(name, url, dest, min_size, user_agent, record_mode, metadata_url, split_offset,
                         trim_start, trim_end, skip_words, dl_fallback)
        return redirect(url_for("dashboard"))
    prefill = {
        "name": request.args.get("name", ""),
        "url": request.args.get("url", ""),
        "record_mode": request.args.get("record_mode", ""),
    }
    return render_template("stream_form.html", stream=None, user_agents=USER_AGENTS, default_ua=DEFAULT_USER_AGENT,
                           prefill=prefill, module_options=module_manager.get_module_form_options(),
                           module_hints=module_manager.get_module_form_hints(),
                           module_hide_fields=module_manager.get_module_hide_fields())


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
        if record_mode not in module_manager.get_all_record_modes():
            record_mode = "streamripper"
        metadata_url = request.form.get("metadata_url", "").strip()
        split_offset = int(request.form.get("split_offset", 0))
        trim_start = int(request.form.get("trim_start", 0))
        trim_end = int(request.form.get("trim_end", 0))
        skip_words = request.form.get("skip_words", "").strip()
        dl_fallback = 1 if request.form.get("dl_fallback") else 0
        # Stop if running before changing config
        process_manager.stop_stream(stream_id)
        db.update_stream(stream_id, name, url, dest, min_size, user_agent, record_mode, metadata_url, split_offset,
                         trim_start, trim_end, skip_words, dl_fallback)
        return redirect(url_for("dashboard"))
    return render_template("stream_form.html", stream=stream, user_agents=USER_AGENTS, default_ua=DEFAULT_USER_AGENT,
                           module_options=module_manager.get_module_form_options(),
                           module_hints=module_manager.get_module_form_hints(),
                           module_hide_fields=module_manager.get_module_hide_fields())


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


@app.route("/api/start-all", methods=["POST"])
def api_start_all():
    streams = db.get_all_streams()
    started = 0
    errors = []
    for s in streams:
        st = process_manager.get_status(s)
        if not st.get("running"):
            try:
                process_manager.start_stream(s)
                started += 1
            except Exception as e:
                errors.append(f"{s['name']}: {str(e)[:80]}")
    return jsonify({"ok": True, "started": started, "errors": errors})


@app.route("/api/stop-all", methods=["POST"])
def api_stop_all():
    streams = db.get_all_streams()
    stopped = 0
    for s in streams:
        st = process_manager.get_status(s)
        if st.get("running"):
            process_manager.stop_stream(s["id"])
            stopped += 1
    return jsonify({"ok": True, "stopped": stopped})


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


@app.route("/api/listen")
def api_listen_proxy():
    """Proxy any stream URL for preview listening in Radio Browser."""
    url = request.args.get("url", "")
    if not url:
        return "No URL", 400

    def generate():
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENTS[DEFAULT_USER_AGENT],
            "Icy-MetaData": "0",
        })
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                yield chunk
        except Exception:
            pass

    content_type = "audio/mpeg"
    url_lower = url.lower()
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
        yield send({"step": 1, "result": "OK" if http.get("ok") else i18n.t("general.error"), "detail": detail.strip(", "), "ok": http.get("ok", False)})

        if not http.get("ok"):
            yield send({"done": True, "recommendation": i18n.t("general.stream_unreachable"), "suitable": False})
            return

        # Step 2: ICY (deep probe)
        yield send({"step": 2, "label": "ICY-Metadaten (Deep Probe, bis 30s)...", "total": 7})
        icy = _test_icy_deep(effective_url, ua)
        if icy.get("title"):
            detail = f"Title: {icy['title'][:50]}"
        elif icy.get("has_metaint"):
            detail = i18n.t("general.blocks_in_seconds").replace("{s}", str(icy.get('seconds', 0))).replace("{n}", str(icy.get('blocks_read', 0)))
        else:
            detail = icy.get("error", i18n.t("general.not_available"))
        yield send({"step": 2, "result": "OK" if icy.get("title") else (i18n.t("general.no_title") if icy.get("has_metaint") else i18n.t("general.missing")), "detail": detail, "ok": bool(icy.get("title"))})
        _save_method(stream_host, stream_path, "icy", effective_url,
                     has_titles=bool(icy.get("title")), sample_title=icy.get("title", ""))

        # Step 3: Shoutcast API
        yield send({"step": 3, "label": "Shoutcast API prüfen...", "total": 7})
        api_result = _test_shoutcast_api(effective_url, ua)
        detail = api_result.get("title", "") if api_result.get("ok") else i18n.t("general.not_available")
        yield send({"step": 3, "result": "OK" if api_result.get("ok") else i18n.t("general.missing"), "detail": detail[:60], "ok": api_result.get("ok", False)})

        # Step 4: Icecast API
        yield send({"step": 4, "label": "Icecast Status-API prüfen...", "total": 7})
        eff_parsed = _up.urlparse(effective_url)
        icecast = _test_icecast_api(effective_url, eff_parsed.path, ua)
        if icecast.get("ok"):
            detail = f"Title: {icecast.get('title', '')[:50]}"
        elif icecast.get("mountpoint"):
            detail = f"Mountpoint '{icecast['mountpoint']}', {i18n.t('general.no_title').lower()}"
        else:
            detail = i18n.t("general.not_available")
        yield send({"step": 4, "result": "OK" if icecast.get("ok") else i18n.t("general.missing"), "detail": detail, "ok": icecast.get("ok", False)})

        # Step 5: TuneIn
        yield send({"step": 5, "label": "TuneIn Now-Playing prüfen...", "total": 7})
        tunein = _test_tunein(url, http.get("icy_name", ""))
        if tunein.get("ok"):
            detail = f"{tunein.get('station_name', '')}: {tunein.get('title', '')[:40]}"
        elif tunein.get("station_id"):
            detail = f"{i18n.t('general.station_found')}, {tunein.get('note', i18n.t('general.no_title').lower())}"
        else:
            detail = tunein.get("error", i18n.t("detail.not_found"))
        yield send({"step": 5, "result": "OK" if tunein.get("ok") else i18n.t("general.missing"), "detail": detail[:60], "ok": tunein.get("ok", False)})

        # Step 6: Streamripper
        yield send({"step": 6, "label": "Streamripper testen (10s)...", "total": 7})
        sr = _test_streamripper(url, ua)
        detail = f"{sr.get('files_created', 0)} {i18n.t('general.files_count')}" if sr.get("ok") else sr.get("error", i18n.t("general.failed"))[:60]
        yield send({"step": 6, "result": "OK" if sr.get("ok") else i18n.t("general.error"), "detail": detail, "ok": sr.get("ok", False)})

        # Step 7: FFmpeg
        yield send({"step": 7, "label": "FFmpeg testen (10s)...", "total": 7})
        ffmpeg = _test_ffmpeg(effective_url, ua)
        if ffmpeg.get("ok"):
            detail = f"{round(ffmpeg.get('file_size', 0) / 1024)} KB in 10s"
        else:
            detail = ffmpeg.get("error", i18n.t("general.failed"))[:60]
        yield send({"step": 7, "result": "OK" if ffmpeg.get("ok") else i18n.t("general.error"), "detail": detail, "ok": ffmpeg.get("ok", False)})

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


# --- Settings ---

@app.route("/settings")
def settings():
    all_modules = module_manager.get_all_modules()
    enabled = {name: module_manager._is_enabled(name) for name in all_modules}
    sync_enabled = sync.is_sync_enabled()
    sync_target = sync.get_sync_target()
    sonos_enabled = cast.is_sonos_enabled()
    cover_art_enabled = cover_art.is_enabled()
    autotag_enabled = autotag.is_enabled()
    acoustid_key = autotag.get_acoustid_key()
    fpcalc_ok = autotag._fpcalc_available()
    dlna_enabled = dlna_server.is_enabled()
    dlna_status = dlna_server.get_status()
    return render_template("settings.html", modules=all_modules, enabled=enabled,
                           builtin_modes=sorted(module_manager.BUILTIN_MODES),
                           sync_enabled=sync_enabled, sync_target=sync_target,
                           sonos_enabled=sonos_enabled,
                           cover_art_enabled=cover_art_enabled,
                           autotag_enabled=autotag_enabled,
                           acoustid_key=acoustid_key,
                           fpcalc_ok=fpcalc_ok,
                           dlna_enabled=dlna_enabled,
                           dlna_status=dlna_status,
                           languages=i18n.LANGUAGES)


@app.route("/settings/module/<name>/toggle", methods=["POST"])
def settings_module_toggle(name):
    all_modules = module_manager.get_all_modules()
    if name not in all_modules:
        return jsonify({"ok": False, "error": "Module not found"}), 404
    currently_enabled = module_manager._is_enabled(name)
    module_manager.set_module_enabled(name, not currently_enabled)
    return jsonify({"ok": True, "enabled": not currently_enabled})


@app.route("/settings/cover-art/toggle", methods=["POST"])
def settings_cover_art_toggle():
    currently = cover_art.is_enabled()
    cover_art.set_enabled(not currently)
    return jsonify({"ok": True, "enabled": not currently})


@app.route("/settings/language", methods=["POST"])
def settings_language():
    data = request.get_json() or {}
    lang = data.get("language", "en")
    i18n.set_language(lang)
    return jsonify({"ok": True, "language": lang})


@app.route("/settings/cast/sonos/toggle", methods=["POST"])
def settings_sonos_toggle():
    currently = cast.is_sonos_enabled()
    cast.set_sonos_enabled(not currently)
    return jsonify({"ok": True, "enabled": not currently})


@app.route("/settings/dlna/toggle", methods=["POST"])
def settings_dlna_toggle():
    currently = dlna_server.is_enabled()
    if currently:
        dlna_server.stop()
        dlna_server.set_enabled(False)
    else:
        dlna_server.set_enabled(True)
        dlna_server.start()
    return jsonify({"ok": True, "enabled": not currently, "status": dlna_server.get_status()})


@app.route("/api/dlna/status")
def api_dlna_status():
    return jsonify(dlna_server.get_status())


@app.route("/settings/dlna/save", methods=["POST"])
def settings_dlna_save():
    """Save DLNA server settings."""
    data = request.get_json() or {}
    if "port" in data:
        try:
            port = int(data["port"])
            if 1024 <= port <= 65535:
                dlna_server.set_port(port)
        except (TypeError, ValueError):
            pass
    if "name" in data:
        name = data["name"].strip()
        if name:
            dlna_server.set_friendly_name(name)
    if "media_path" in data:
        path = data["media_path"].strip()
        if path:
            dlna_server.set_media_path(path)
    # Restart if running
    was_running = dlna_server.get_status()["running"]
    if was_running:
        dlna_server.stop()
        dlna_server.start()
    return jsonify({"ok": True, "status": dlna_server.get_status()})


@app.route("/settings/autotag/toggle", methods=["POST"])
def settings_autotag_toggle():
    currently = autotag.is_enabled()
    autotag.set_enabled(not currently)
    return jsonify({"ok": True, "enabled": not currently})


@app.route("/settings/autotag/key", methods=["POST"])
def settings_autotag_key():
    data = request.get_json() or {}
    key = data.get("key", "").strip()
    autotag.set_acoustid_key(key)
    return jsonify({"ok": True})


@app.route("/api/autotag/<int:stream_id>", methods=["POST"])
def api_autotag_batch(stream_id):
    """Start batch auto-tagging for a stream's NAS files."""
    stream = db.get_stream(stream_id)
    if not stream:
        return jsonify({"success": False, "error": i18n.t("detail.not_found")}), 404
    dirpath = os.path.join(sync.get_sync_target(), stream["dest_subdir"])
    if not os.path.isdir(dirpath):
        return jsonify({"success": False, "error": i18n.t("general.not_available")}), 404
    started = autotag.start_batch(stream_id, dirpath)
    if not started:
        return jsonify({"success": False, "error": "Batch läuft bereits"})
    return jsonify({"success": True})


@app.route("/api/autotag/<int:stream_id>/status")
def api_autotag_status(stream_id):
    """Get batch tagging status."""
    status = autotag.get_job_status(stream_id)
    return jsonify({"status": status})


@app.route("/settings/sync", methods=["POST"])
def settings_sync():
    sync_enabled = request.form.get("sync_enabled") == "1"
    sync_target = request.form.get("sync_target", "").strip()
    db.set_setting("sync_enabled", "1" if sync_enabled else "0")
    if sync_target:
        db.set_setting("sync_target", sync_target)
    return redirect(url_for("settings"))


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
        st["record_mode"] = s["record_mode"]
        st["dl_fallback"] = s["dl_fallback"]
        result.append(st)
    return jsonify({"streams": result, "disk": _get_disk_info()})


@app.route("/api/stream/<int:stream_id>/icy")
def api_stream_icy(stream_id):
    """Get ICY metadata (current track) for a stream, even when not recording."""
    stream = db.get_stream(stream_id)
    if not stream:
        return jsonify({"error": "not found"}), 404
    # First try process metadata (if recording)
    st = process_manager.get_status(stream)
    ct = st.get("current_track", "")
    cover = st.get("cover_url")
    if ct and ct not in ("recording", "-", ""):
        return jsonify({"current_track": ct, "cover_url": cover})
    # Fallback: fetch ICY metadata directly from stream URL
    try:
        r = req_lib.get(stream["url"], headers={"Icy-MetaData": "1"},
                        stream=True, timeout=5)
        icy_interval = int(r.headers.get("icy-metaint", 0))
        if icy_interval > 0:
            r.raw.read(icy_interval)
            meta_len = struct.unpack("B", r.raw.read(1))[0] * 16
            if meta_len > 0:
                meta = r.raw.read(meta_len).decode("utf-8", errors="ignore").rstrip("\0")
                m = re.search(r"StreamTitle='([^']*)'", meta)
                if m and m.group(1).strip():
                    ct = m.group(1).strip()
        r.close()
    except Exception:
        pass
    # Fetch cover art for the track if not already available
    if ct and ct not in ("recording", "-", "") and not cover:
        try:
            cover = cover_art.get_cover_url(stream_id, ct)
        except Exception:
            pass
    return jsonify({"current_track": ct, "cover_url": cover})


@app.route("/api/disk")
def api_disk():
    return jsonify(_get_disk_info())


# --- Cast API ---

@app.route("/api/cast/devices")
def api_cast_devices():
    """Return discovered cast devices (LMS + Sonos)."""
    force = request.args.get("refresh", "0") == "1"
    devices = cast.discover_devices(force=force)
    active = cast.get_active_casts()  # device_id -> stream_id
    # Include volume for devices with active casts
    volumes = {}
    for dev in devices:
        if dev["id"] in active:
            vol = cast.get_volume(dev["id"])
            if vol is not None:
                volumes[dev["id"]] = vol
    # Convert to stream_id -> device_id for frontend compatibility
    active_by_stream = cast.get_active_casts_by_stream()
    return jsonify({"devices": devices, "active_casts": active_by_stream, "volumes": volumes})


@app.route("/api/cast/play", methods=["POST"])
def api_cast_play():
    """Cast a stream to a device."""
    data = request.get_json() or {}
    stream_id = data.get("stream_id")
    device_id = data.get("device_id")
    if not stream_id or not device_id:
        return jsonify({"success": False, "error": "stream_id und device_id erforderlich"}), 400

    stream = db.get_stream(int(stream_id))
    if not stream:
        return jsonify({"success": False, "error": "Stream nicht gefunden"}), 404

    # If this device is already casting something, stop it first
    active = cast.get_active_casts()
    if device_id in active:
        cast.stop_cast(device_id)
        cast.remove_active_cast_by_device(device_id)

    # Limit to 4 simultaneous casts
    active = cast.get_active_casts()
    if len(active) >= 4:
        return jsonify({"success": False, "message": i18n.t("cast.max_reached")}), 400

    ok, msg = cast.cast_stream(stream["url"], device_id)
    if ok:
        cast.set_active_cast(int(stream_id), device_id)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/cast/stop", methods=["POST"])
def api_cast_stop():
    """Stop casting a stream. Accepts stream_id (stops all devices) or device_id (stops one)."""
    data = request.get_json() or {}
    stream_id = data.get("stream_id")
    device_id = data.get("device_id")

    if device_id:
        # Stop a specific device
        ok, msg = cast.stop_cast(device_id)
        if ok:
            cast.remove_active_cast_by_device(device_id)
        return jsonify({"success": ok, "message": msg})

    if not stream_id:
        return jsonify({"success": False, "error": "stream_id erforderlich"}), 400

    # Stop all devices casting this stream
    devices = cast.get_devices_for_stream(int(stream_id))
    if not devices:
        return jsonify({"success": False, "error": "Stream wird nicht gecastet"}), 400

    for did in devices:
        cast.stop_cast(did)
        cast.remove_active_cast_by_device(did)
    return jsonify({"success": True, "message": f"Cast gestoppt"})


@app.route("/api/cast/pause", methods=["POST"])
def api_cast_pause():
    """Toggle pause on a cast stream."""
    data = request.get_json() or {}
    stream_id = data.get("stream_id")
    if not stream_id:
        return jsonify({"success": False, "error": "stream_id required"}), 400

    device_id = cast.get_active_cast_for_stream(int(stream_id))
    if not device_id:
        return jsonify({"success": False, "error": "Stream not casting"}), 400

    # Get stream URL for Sonos resume (streams need to be replayed)
    stream = db.get_stream(int(stream_id))
    stream_url = stream["url"] if stream else None

    ok, msg = cast.pause_cast(device_id, stream_url=stream_url)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/cast/player")
def api_cast_player():
    """Return all data needed for the global player bar."""
    active = cast.get_active_casts()
    if not active:
        return jsonify({"active": False})

    devices = cast.discover_devices()
    dev_map = {d["id"]: d for d in devices}

    # Get stream status for active casts
    players = []
    all_streams = db.get_all_streams()
    stream_map = {s["id"]: s for s in all_streams}

    for device_id, stream_id_raw in active.items():
        stream_id = int(stream_id_raw) if isinstance(stream_id_raw, str) else stream_id_raw
        stream = stream_map.get(stream_id)
        device = dev_map.get(device_id)
        if not stream or not device:
            continue

        st = process_manager.get_status(stream)
        current_track = st.get("current_track", "")
        cover_url = st.get("cover_url")

        # If recording doesn't provide track info, ask the device/ICY directly
        if not current_track:
            cast_track, cast_cover = cast.get_cast_track_info(
                device_id, stream_id, stream["url"])
            if cast_track:
                current_track = cast_track
            if cast_cover:
                cover_url = cast_cover or cover_url

        vol = cast.get_volume(device_id)
        players.append({
            "stream_id": stream_id,
            "stream_name": stream["name"],
            "device_id": device_id,
            "device_name": device.get("name", device_id),
            "device_type": device.get("type", ""),
            "current_track": current_track,
            "cover_url": cover_url,
            "volume": vol,
            "running": st.get("running", False),
        })

    # All available speakers for multiroom toggle
    all_speakers = []
    for d in devices:
        if not d.get("enabled", False):
            continue
        # Check if this speaker is synced/grouped to an active device
        in_group_of = None
        for p in players:
            if d["id"] == p["device_id"]:
                in_group_of = p["device_id"]
                break
        all_speakers.append({
            "id": d["id"],
            "name": d["name"],
            "type": d["type"],
            "active_for": in_group_of,
        })

    return jsonify({
        "active": True,
        "players": players,
        "speakers": all_speakers,
    })


@app.route("/api/cast/multiroom/add", methods=["POST"])
def api_multiroom_add():
    """Add a speaker to a multiroom group."""
    data = request.get_json() or {}
    master_id = data.get("master_device_id")
    slave_id = data.get("slave_device_id")
    if not master_id or not slave_id:
        return jsonify({"success": False, "error": "master und slave device_id erforderlich"}), 400
    ok, msg = cast.multiroom_add(master_id, slave_id)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/cast/multiroom/remove", methods=["POST"])
def api_multiroom_remove():
    """Remove a speaker from multiroom group."""
    data = request.get_json() or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"success": False, "error": "device_id erforderlich"}), 400
    ok, msg = cast.multiroom_remove(device_id)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/cast/volume/<device_id>", methods=["GET"])
def api_cast_volume_get(device_id):
    """Get current volume of a cast device."""
    vol = cast.get_volume(device_id)
    if vol is None:
        return jsonify({"error": "Volume nicht verfügbar"}), 404
    return jsonify({"volume": vol})


@app.route("/api/cast/volume/<device_id>", methods=["POST"])
def api_cast_volume_set(device_id):
    """Set volume of a cast device."""
    data = request.get_json() or {}
    level = data.get("volume")
    if level is None:
        return jsonify({"error": "volume erforderlich"}), 400
    try:
        level = int(level)
    except (TypeError, ValueError):
        return jsonify({"error": "volume muss eine Zahl sein"}), 400
    cast.set_volume(device_id, level)
    return jsonify({"ok": True, "volume": max(0, min(100, level))})


# --- Cast Queue API ---

@app.route("/api/cast/queue/<device_id>")
def api_cast_queue_get(device_id):
    """Return queue and timer info for a device."""
    queue = cast_queue.get_queue(device_id)
    timer = cast_queue.get_timer_info(device_id)
    return jsonify({"queue": queue, "timer": timer})


@app.route("/api/cast/queue/<device_id>/add", methods=["POST"])
def api_cast_queue_add(device_id):
    """Add a stream to a device's queue."""
    data = request.get_json() or {}
    stream_id = data.get("stream_id")
    if not stream_id:
        return jsonify({"success": False, "error": "stream_id erforderlich"}), 400
    stream = db.get_stream(int(stream_id))
    if not stream:
        return jsonify({"success": False, "error": "Stream nicht gefunden"}), 404
    cast_queue.add_to_queue(device_id, int(stream_id), stream["url"], stream["name"])
    return jsonify({"success": True, "queue": cast_queue.get_queue(device_id)})


@app.route("/api/cast/queue/<device_id>/<int:index>", methods=["DELETE"])
def api_cast_queue_remove(device_id, index):
    """Remove a queue item by index."""
    ok = cast_queue.remove_from_queue(device_id, index)
    return jsonify({"success": ok, "queue": cast_queue.get_queue(device_id)})


@app.route("/api/cast/queue/<device_id>/next", methods=["POST"])
def api_cast_queue_next(device_id):
    """Advance to the next stream in the queue."""
    item = cast_queue.advance_queue(device_id)
    if not item:
        return jsonify({"success": False, "error": "Warteschlange ist leer"}), 400
    return jsonify({"success": True, "playing": item, "queue": cast_queue.get_queue(device_id)})


@app.route("/api/cast/queue/<device_id>/timer", methods=["POST"])
def api_cast_queue_timer(device_id):
    """Set or cancel the auto-advance timer. minutes=0 cancels."""
    data = request.get_json() or {}
    minutes = int(data.get("minutes", 0))
    if minutes <= 0:
        cast_queue.cancel_timer(device_id)
        return jsonify({"success": True, "timer": None})
    cast_queue.set_timer(device_id, minutes)
    return jsonify({"success": True, "timer": cast_queue.get_timer_info(device_id)})


@app.route("/api/cast/queue/<device_id>", methods=["DELETE"])
def api_cast_queue_clear(device_id):
    """Clear entire queue for a device."""
    cast_queue.clear_queue(device_id)
    return jsonify({"success": True})


@app.route("/api/cast/queues")
def api_cast_queues_all():
    """Return all device queues (for the queue panel)."""
    devices = cast.discover_devices()
    result = {}
    for d in devices:
        q = cast_queue.get_queue(d["id"])
        if q:
            result[d["id"]] = {
                "device_name": d["name"],
                "queue": q,
                "timer": cast_queue.get_timer_info(d["id"]),
            }
    return jsonify(result)


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


# --- Library ---

@app.route("/library")
def library():
    return render_template("library.html")


@app.route("/api/library/tracks")
def api_library_tracks():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 200, type=int)
    sort = request.args.get("sort", "title")
    order = request.args.get("order", "asc")
    stream = request.args.get("stream", None)
    search = request.args.get("search", None)
    bpm_min = request.args.get("bpm_min", None, type=int)
    bpm_max = request.args.get("bpm_max", None, type=int)
    key_filter = request.args.get("key", None)
    tracks, total = db.get_library_tracks(
        page=page, per_page=per_page, sort=sort, order=order,
        stream=stream, search=search, bpm_min=bpm_min, bpm_max=bpm_max,
        key_filter=key_filter
    )
    return jsonify({
        "tracks": tracks, "total": total,
        "page": page, "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
    })


@app.route("/api/library/stats")
def api_library_stats():
    return jsonify(db.get_library_stats())


@app.route("/api/library/subdirs")
def api_library_subdirs():
    return jsonify({"subdirs": db.get_stream_subdirs()})


@app.route("/api/library/scan", methods=["POST"])
def api_library_scan():
    subdir = request.json.get("subdir") if request.is_json else None
    library.start_scan(subdir=subdir)
    return jsonify({"success": True})


@app.route("/api/library/scan/status")
def api_library_scan_status():
    return jsonify(library.get_scan_status())


@app.route("/api/library/playlists")
def api_library_playlists():
    return jsonify({"playlists": db.get_all_playlists()})


@app.route("/api/library/playlists", methods=["POST"])
def api_library_create_playlist():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    try:
        pid = db.create_playlist(name)
        return jsonify({"success": True, "id": pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/library/playlists/<int:playlist_id>", methods=["DELETE"])
def api_library_delete_playlist(playlist_id):
    pl = db.get_playlist(playlist_id)
    if not pl:
        return jsonify({"error": "not found"}), 404
    library.delete_m3u(pl["name"])
    db.delete_playlist(playlist_id)
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/rename", methods=["POST"])
def api_library_rename_playlist(playlist_id):
    data = request.get_json()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    old = db.get_playlist(playlist_id)
    if old:
        library.delete_m3u(old["name"])
    db.rename_playlist(playlist_id, name)
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/tracks")
def api_library_playlist_tracks(playlist_id):
    tracks = db.get_playlist_tracks(playlist_id)
    return jsonify({"tracks": tracks})


@app.route("/api/library/playlists/<int:playlist_id>/add", methods=["POST"])
@app.route("/api/library/playlists/<int:playlist_id>/tracks", methods=["POST"])
def api_library_playlist_add(playlist_id):
    data = request.get_json()
    track_ids = data.get("track_ids", [])
    if not track_ids:
        return jsonify({"error": "No tracks"}), 400
    db.add_to_playlist(playlist_id, track_ids)
    library.generate_m3u(playlist_id)
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/tracks/<int:track_id>", methods=["DELETE"])
def api_library_playlist_remove(playlist_id, track_id):
    db.remove_from_playlist(playlist_id, track_id)
    library.generate_m3u(playlist_id)
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/reorder", methods=["POST"])
def api_library_playlist_reorder(playlist_id):
    data = request.get_json()
    track_ids = data.get("track_ids", [])
    db.reorder_playlist(playlist_id, track_ids)
    library.generate_m3u(playlist_id)
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/m3u")
def api_library_playlist_m3u(playlist_id):
    pl = db.get_playlist(playlist_id)
    if not pl:
        return jsonify({"error": "not found"}), 404
    # Generate fresh M3U
    library.generate_m3u(playlist_id)
    sync_target = sync.get_sync_target()
    m3u_path = os.path.join(sync_target, pl["name"] + ".m3u")
    if not os.path.isfile(m3u_path):
        return jsonify({"error": "M3U not found"}), 404
    return send_file(m3u_path, mimetype="audio/x-mpegurl",
                     as_attachment=True, download_name=pl["name"] + ".m3u")


@app.route("/api/library/track/<int:track_id>/play")
def api_library_track_play(track_id):
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    filepath = track["filepath"]
    if not os.path.isfile(filepath):
        return jsonify({"error": "file not found"}), 404
    return send_file(filepath, mimetype="audio/mpeg")


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
            capture_output=True, text=True, timeout=15,
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
        usage = shutil.disk_usage(sync.get_sync_target())
        info["nas_free_gb"] = round(usage.free / (1024**3), 1)
        info["nas_total_gb"] = round(usage.total / (1024**3), 1)
    except OSError:
        info["nas_free_gb"] = 0
        info["nas_total_gb"] = 0

    _disk_cache["info"] = info
    _disk_cache["ts"] = now
    return info


# --- YT Download (direct URL download) ---

@app.route("/yt-download")
def yt_download():
    return render_template("yt_download.html")


@app.route("/api/yt-download/check", methods=["POST"])
def api_yt_download_check():
    """Check a YouTube URL: single video or playlist? Return metadata."""
    import subprocess
    url = request.json.get("url", "").strip() if request.is_json else ""
    if not url:
        return jsonify({"ok": False, "error": "Keine URL angegeben"}), 400

    # Find yt-dlp binary
    yt_dlp_candidates = [
        os.path.expanduser("~/.local/bin/yt-dlp"),
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
        "yt-dlp",
    ]
    yt_dlp = next((p for p in yt_dlp_candidates if os.path.isfile(p)), "yt-dlp")

    env = os.environ.copy()
    deno_path = os.path.expanduser("~/.deno/bin")
    if os.path.isdir(deno_path):
        env["PATH"] = f"{deno_path}:{env.get('PATH', '')}"

    # Strip &radio=... parameter (YouTube radio/mix indicator)
    url = re.sub(r"[&?]radio=[^&]*", "", url)

    # Extract list= ID to detect playlist type
    list_id = ""
    list_match = re.search(r"list=([^&]+)", url)
    if list_match:
        list_id = list_match.group(1)

    # Real playlist prefixes: PL (user), OL (official), FL (favorites)
    # Not a playlist: RD (radio/mix), UU (channel uploads), WL (watch later), LL (liked)
    is_real_playlist = list_id.startswith(("PL", "OL", "FL"))
    is_explicit_playlist = "/playlist" in url.split("?")[0]
    is_playlist_url = is_explicit_playlist or is_real_playlist
    has_single_video = "watch?v=" in url or "youtu.be/" in url

    # For mixes/radio: strip the list= param so yt-dlp treats it as single video
    if list_id and not is_real_playlist and not is_explicit_playlist:
        url = re.sub(r"[&?]list=[^&]*", "", url)
        url = re.sub(r"[&?]index=[^&]*", "", url)

    # Step 1: Always get single video info first (unless explicit playlist URL)
    # Use --print instead of --dump-json to skip slow format extraction
    single_info = None
    if has_single_video:
        try:
            result = subprocess.run(
                [yt_dlp, "--no-playlist", "--no-download", "--no-warnings",
                 "--print", "%(title)s\n%(duration)s\n%(uploader)s", url],
                capture_output=True, text=True, timeout=15, env=env,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                single_info = {
                    "title": lines[0] if len(lines) > 0 else "Unbekannt",
                    "duration": int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None,
                    "uploader": lines[2] if len(lines) > 2 else "",
                }
        except Exception:
            pass

    # Step 2: Only fetch playlist entries for real playlists (index= or /playlist page)
    if is_playlist_url:
        try:
            # Extract the list= parameter to check for Mixes (RD...) which are auto-generated
            list_id = ""
            if "list=" in url:
                list_id = url.split("list=")[1].split("&")[0]
            is_mix = list_id.startswith("RD") or list_id.startswith("UU")

            # Limit entries: Mixes are quasi-infinite, cap at 50; real playlists at 500
            max_entries = 50 if is_mix else 500

            result = subprocess.run(
                [yt_dlp, "--flat-playlist", "--dump-json", "--no-warnings",
                 "--playlist-end", str(max_entries), url],
                capture_output=True, text=True, timeout=60, env=env,
            )
            if result.returncode != 0:
                # Playlist fetch failed — if we have single video info, return that
                if single_info:
                    return jsonify({
                        "ok": True, "is_playlist": False,
                        "title": single_info.get("title", "Unknown"),
                        "duration": single_info.get("duration"),
                        "uploader": single_info.get("uploader", ""),
                    })
                return jsonify({"ok": False, "error": f"yt-dlp {i18n.t('general.error')}: {result.stderr[:200]}"})

            entries = []
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

            if not entries:
                if single_info:
                    return jsonify({
                        "ok": True, "is_playlist": False,
                        "title": single_info.get("title", "Unknown"),
                        "duration": single_info.get("duration"),
                        "uploader": single_info.get("uploader", ""),
                    })
                return jsonify({"ok": False, "error": "Keine Videos gefunden"})

            items = []
            for e in entries:
                items.append({
                    "id": e.get("id", ""),
                    "title": e.get("title", "Unbekannt"),
                    "duration": e.get("duration"),
                    "url": e.get("url", e.get("webpage_url", "")),
                })

            playlist_title = entries[0].get("playlist_title") or entries[0].get("playlist") or "Playlist"
            if is_mix:
                playlist_title = f"Mix: {playlist_title}"

            return jsonify({
                "ok": True,
                "is_playlist": True,
                "is_mix": is_mix,
                "has_single_video": has_single_video,
                "playlist_title": playlist_title,
                "count": len(entries),
                "capped": len(entries) >= max_entries,
                "items": items,
                # Include single video info if available
                "single_title": single_info.get("title") if single_info else None,
                "single_duration": single_info.get("duration") if single_info else None,
            })
        except subprocess.TimeoutExpired:
            if single_info:
                return jsonify({
                    "ok": True, "is_playlist": False,
                    "title": single_info.get("title", "Unknown"),
                    "duration": single_info.get("duration"),
                    "uploader": single_info.get("uploader", ""),
                })
            return jsonify({"ok": False, "error": f"Timeout: {i18n.t('general.error')}"})
        except Exception as e:
            if single_info:
                return jsonify({
                    "ok": True, "is_playlist": False,
                    "title": single_info.get("title", "Unknown"),
                    "duration": single_info.get("duration"),
                    "uploader": single_info.get("uploader", ""),
                })
            return jsonify({"ok": False, "error": str(e)[:200]})

    # No playlist — single video
    if single_info:
        return jsonify({
            "ok": True, "is_playlist": False,
            "title": single_info.get("title", "Unknown"),
            "duration": single_info.get("duration"),
            "uploader": single_info.get("uploader", ""),
        })

    # Fallback: try fetching as single video
    try:
        result = subprocess.run(
            [yt_dlp, "--no-playlist", "--no-download", "--no-warnings",
             "--print", "%(title)s\n%(duration)s\n%(uploader)s", url],
            capture_output=True, text=True, timeout=15, env=env,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": f"yt-dlp {i18n.t('general.error')}: {result.stderr[:200]}"})
        lines = result.stdout.strip().split("\n")
        return jsonify({
            "ok": True, "is_playlist": False,
            "title": lines[0] if lines else "Unknown",
            "duration": int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None,
            "uploader": lines[2] if len(lines) > 2 else "",
        })
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout beim Abrufen der Video-Infos"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]})


# Background download jobs: job_id -> {"log": [...], "done": bool}
import threading
import queue as _queue

_yt_jobs = {}
_yt_jobs_lock = threading.Lock()


def _yt_download_worker(job_id, url, mode, dest, dest_subdir):
    """Run yt-dlp download in background thread. Writes progress to job log."""
    import subprocess as _sp
    from pathlib import Path

    job = _yt_jobs[job_id]

    def log(data):
        job["log"].append(data)

    yt_dlp_candidates = [
        os.path.expanduser("~/.local/bin/yt-dlp"),
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
        "yt-dlp",
    ]
    yt_dlp = next((p for p in yt_dlp_candidates if os.path.isfile(p)), "yt-dlp")
    yt_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

    env = os.environ.copy()
    deno_path = os.path.expanduser("~/.deno/bin")
    if os.path.isdir(deno_path):
        env["PATH"] = f"{deno_path}:{env.get('PATH', '')}"

    sync_target = sync.get_sync_target()
    nas_dest = os.path.join(sync_target, dest_subdir)

    cmd = [
        yt_dlp, url,
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--format", "bestaudio",
        "--postprocessor-args", "ffmpeg:-b:a 320k",
        "--output", os.path.join(dest, "%(title)s.%(ext)s"),
        "--no-overwrites",
        "--restrict-filenames",
        "--embed-thumbnail",
        "--user-agent", yt_ua,
        "--remote-components", "ejs:github",
        "--newline",
        "--progress",
    ]
    if mode == "single":
        cmd += ["--no-playlist"]

    log({"status": "starting", "message": "Download wird gestartet..."})

    try:
        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                         text=True, env=env, bufsize=1)

        downloaded_files = []

        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue

            if "[download] Destination:" in line:
                title = os.path.basename(line.split("Destination:")[-1].strip())
                log({"status": "downloading", "message": f"Lade: {title}"})
            elif "[download]" in line and "%" in line:
                log({"status": "progress", "message": line.strip()})
            elif "[ExtractAudio]" in line:
                log({"status": "converting", "message": "Konvertiere zu MP3..."})
            elif "[EmbedThumbnail]" in line:
                log({"status": "converting", "message": "Thumbnail wird eingebettet..."})
            elif line.startswith("/") and line.endswith(".mp3"):
                downloaded_files.append(line.strip())
            elif "has already been downloaded" in line:
                log({"status": "skip", "message": "Bereits vorhanden, uebersprungen"})

        proc.wait(timeout=10)

        # Find downloaded mp3 files in dest
        if not downloaded_files:
            now = time.time()
            for f in sorted(Path(dest).glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True):
                if now - f.stat().st_mtime < 300:
                    downloaded_files.append(str(f))

        # Sync files to NAS
        synced = 0
        total = len(downloaded_files)
        if downloaded_files and sync.is_sync_enabled() and os.path.ismount(sync_target):
            log({"status": "syncing", "message": f"{i18n.t('general.sync')} {total} {i18n.t('general.files_count')}..."})
            os.makedirs(nas_dest, exist_ok=True)
            for fp in downloaded_files:
                if os.path.exists(fp):
                    dst = os.path.join(nas_dest, os.path.basename(fp))
                    if os.path.exists(dst):
                        name, ext = os.path.splitext(os.path.basename(fp))
                        dst = os.path.join(nas_dest, f"{name}_{int(time.time())}{ext}")
                    try:
                        result = _sp.run(
                            ["rsync", "-aq", "--remove-source-files", fp, dst],
                            capture_output=True, text=True, timeout=60,
                        )
                        if result.returncode == 0:
                            synced += 1
                    except Exception:
                        pass

        total = len(downloaded_files)
        msg = f"{total} Song{'s' if total != 1 else ''} {i18n.t('detail.downloaded')}"
        if synced:
            msg += f", {synced} {i18n.t('general.sync')}"
        log({"status": "done", "message": msg, "count": total, "synced": synced})

    except Exception as e:
        log({"status": "error", "message": f"{i18n.t('general.error')}: {str(e)[:200]}"})

    job["done"] = True


@app.route("/api/yt-download/start")
def api_yt_download_start():
    """Start YouTube download in background, return SSE progress stream."""
    url = request.args.get("url", "").strip()
    mode = request.args.get("mode", "single")
    dest_subdir = request.args.get("dest", "yt-downloads").strip()
    dest_subdir = re.sub(r"[^\w\s-]", "", dest_subdir).strip().replace(" ", "_") or "yt-downloads"

    if not url:
        return Response("data: " + json.dumps({"error": "Keine URL"}) + "\n\n",
                        mimetype="text/event-stream")

    dest = os.path.join(RECORDING_BASE, dest_subdir)
    os.makedirs(dest, exist_ok=True)

    # Create background job
    job_id = f"yt_{int(time.time())}_{id(url)}"
    _yt_jobs[job_id] = {"log": [], "done": False}

    t = threading.Thread(target=_yt_download_worker,
                         args=(job_id, url, mode, dest, dest_subdir), daemon=True)
    t.start()

    def generate():
        idx = 0
        while True:
            job = _yt_jobs.get(job_id)
            if not job:
                break

            # Send any new log entries
            while idx < len(job["log"]):
                entry = job["log"][idx]
                yield f"data: {json.dumps(entry)}\n\n"
                idx += 1
                # If done or error, stop
                if entry.get("status") in ("done", "error"):
                    # Cleanup old job after sending
                    _yt_jobs.pop(job_id, None)
                    return

            if job["done"] and idx >= len(job["log"]):
                _yt_jobs.pop(job_id, None)
                return

            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream")


def _shutdown():
    """Graceful shutdown: stop all streams, cleanup incomplete files."""
    print("Shutting down: stopping all streams...")
    process_manager.stop_all_streams()
    streams = db.get_all_streams()
    process_manager.cleanup_incomplete(streams)
    if scheduler:
        scheduler.stop()
    dlna_server.stop()
    print("Shutdown complete.")


if __name__ == "__main__":
    db.init_db()
    module_manager.discover_modules()

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

    # Start DLNA server if enabled
    if dlna_server.is_enabled():
        try:
            dlna_server.start()
        except Exception as e:
            print(f"DLNA server start failed: {e}")

    # Start background ICY metadata poller for cast players
    cast.start_icy_poller()

    app.run(host=HOST, port=PORT, threaded=True)
