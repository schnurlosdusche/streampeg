import os
import re
import shutil
import subprocess
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
import slimproto
import lms_compat
import library as lib_module
import bpm_analyzer
import backup

# --- Client activity tracking ---
_last_client_active = 0.0  # timestamp of last active (visible tab) request
_client_audio_playing = False  # whether browser is playing audio
_stream_test_active = False  # whether a stream test is running
_scan_override = False  # user override: run despite active client
_CLIENT_IDLE_THRESHOLD = 60  # seconds before considering no active client


def is_client_active():
    """Check if any browser client is actively viewing the page or playing audio."""
    if _scan_override:
        return False
    if _stream_test_active:
        return True
    if _client_audio_playing:
        return True
    return (time.time() - _last_client_active) < _CLIENT_IDLE_THRESHOLD
import i18n
from scheduler import SyncScheduler

VERSION = "0.0.169a"

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
    return render_template("dashboard_welcome.html")


@app.route("/recordings")
def recordings():
    streams = db.get_all_streams()
    statuses = {}
    for s in streams:
        statuses[s["id"]] = process_manager.get_status_fast(s)
    return render_template("dashboard.html", streams=streams, statuses=statuses,
                           module_icons=module_manager.get_module_icons())


@app.route("/streams-home")
def streams_home():
    bookmarks = db.get_stream_bookmarks()
    return render_template("streams_home.html", bookmarks=bookmarks)


@app.route("/api/stream-bookmarks", methods=["GET"])
def api_stream_bookmarks():
    return jsonify({"bookmarks": db.get_stream_bookmarks()})


@app.route("/api/stream-bookmarks/add", methods=["POST"])
def api_stream_bookmark_add():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    if not name or not url:
        return jsonify({"success": False, "error": "name and url required"}), 400
    db.add_stream_bookmark(
        name, url,
        tags=data.get("tags", ""),
        favicon=data.get("favicon", ""),
        codec=data.get("codec", ""),
        bitrate=data.get("bitrate", 0),
        country=data.get("country", ""),
    )
    return jsonify({"success": True})


@app.route("/api/stream-bookmarks/<int:bookmark_id>/record", methods=["POST"])
def api_stream_bookmark_record(bookmark_id):
    """Create a recording stream from a bookmark."""
    bm = None
    for b in db.get_stream_bookmarks():
        if b["id"] == bookmark_id:
            bm = b
            break
    if not bm:
        return jsonify({"success": False, "error": "Bookmark not found"}), 404
    name = bm["name"]
    url = bm["url"]
    dest = _sanitize_subdir(name)
    # Check if stream with this URL already exists
    for s in db.get_all_streams():
        if s["url"] == url:
            return jsonify({"success": False, "error": "Stream already exists", "stream_id": s["id"]}), 409
    data = request.get_json() or {}
    record_mode = data.get("record_mode", "ffmpeg_icy")
    if record_mode not in module_manager.get_all_record_modes():
        record_mode = "ffmpeg_icy"
    db.create_stream(name, url, dest, DEFAULT_MIN_SIZE_MB, DEFAULT_USER_AGENT,
                     record_mode, "", 0, 0, 0, "", 0)
    return jsonify({"success": True, "message": f"Recording stream '{name}' created"})


@app.route("/api/stream-bookmarks/<int:bookmark_id>/delete", methods=["POST"])
def api_stream_bookmark_delete(bookmark_id):
    db.delete_stream_bookmark(bookmark_id)
    return jsonify({"success": True})


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
        return redirect(url_for("recordings"))
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
        return redirect(url_for("recordings"))
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
        return redirect(url_for("recordings"))
    return render_template("stream_form.html", stream=stream, user_agents=USER_AGENTS, default_ua=DEFAULT_USER_AGENT,
                           module_options=module_manager.get_module_form_options(),
                           module_hints=module_manager.get_module_form_hints(),
                           module_hide_fields=module_manager.get_module_hide_fields())


@app.route("/stream/<int:stream_id>/delete", methods=["POST"])
def stream_delete(stream_id):
    process_manager.stop_stream(stream_id)
    db.delete_stream(stream_id)
    return redirect(url_for("recordings"))


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
            return redirect(url_for("recordings"))
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for("recordings"))


@app.route("/stream/<int:stream_id>/stop", methods=["POST"])
def stream_stop(stream_id):
    process_manager.stop_stream(stream_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True})
    return redirect(url_for("recordings"))


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
        return redirect(url_for("recordings"))
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
        global _stream_test_active
        _stream_test_active = True
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

        _stream_test_active = False

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


@app.route("/api/browse/probe-bitrate")
def api_browse_probe_bitrate():
    """Quick probe of a stream URL to get bitrate from ICY headers."""
    url = request.args.get("url", "")
    if not url:
        return jsonify({"bitrate": 0})
    try:
        import http.client
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        if parsed.scheme == "https":
            import ssl
            conn = http.client.HTTPSConnection(host, port, timeout=5, context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", path, headers={
            "User-Agent": "Streampeg/1.0",
            "Icy-MetaData": "1",
        })
        resp = conn.getresponse()
        bitrate = 0
        icy_br = resp.getheader("icy-br", "")
        if icy_br:
            bitrate = int(icy_br.split(",")[0])
        conn.close()
        return jsonify({"bitrate": bitrate})
    except Exception:
        return jsonify({"bitrate": 0})


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
    bpm_analyzer_enabled = db.get_setting("bpm_analyzer_enabled") == "1"
    bpm_backend = db.get_setting("bpm_backend") or "aubio"
    essentia_available = "essentia" in bpm_analyzer.get_available_backends()
    mb_enrichment_enabled = db.get_setting("musicbrainz_enrichment") != "0"
    skip_unusable = db.get_setting("skip_unusable") == "1"
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
                           bpm_analyzer_enabled=bpm_analyzer_enabled,
                           bpm_backend=bpm_backend,
                           essentia_available=essentia_available,
                           mb_enrichment_enabled=mb_enrichment_enabled,
                           skip_unusable=skip_unusable,
                           languages=i18n.LANGUAGES,
                           backups=backup.list_backups(),
                           crossfade_sec=int(db.get_setting("autodj_crossfade") or 10),
                           outro_threshold_pct=int(db.get_setting("autodj_outro_threshold") or 30),
                           outro_max_sec=int(db.get_setting("autodj_outro_max") or 15))


@app.route("/api/settings/autodj")
def api_settings_autodj():
    return jsonify({
        "crossfade_sec": int(db.get_setting("autodj_crossfade") or 10),
        "outro_threshold_pct": int(db.get_setting("autodj_outro_threshold") or 30),
        "outro_max_sec": int(db.get_setting("autodj_outro_max") or 15),
    })


@app.route("/settings/skip-unusable/toggle", methods=["POST"])
def settings_skip_unusable_toggle():
    current = db.get_setting("skip_unusable") == "1"
    db.set_setting("skip_unusable", "0" if current else "1")
    return jsonify({"ok": True, "enabled": not current})


@app.route("/settings/autodj/fade", methods=["POST"])
def settings_autodj_fade():
    data = request.get_json() or {}
    sec = max(3, min(30, int(data.get("seconds", 10))))
    db.set_setting("autodj_crossfade", str(sec))
    return jsonify({"ok": True, "seconds": sec})


@app.route("/settings/autodj/outro-threshold", methods=["POST"])
def settings_autodj_outro_threshold():
    data = request.get_json() or {}
    pct = max(5, min(95, int(data.get("percent", 30))))
    db.set_setting("autodj_outro_threshold", str(pct))
    return jsonify({"ok": True, "percent": pct})


@app.route("/settings/autodj/outro-max", methods=["POST"])
def settings_autodj_outro_max():
    data = request.get_json() or {}
    sec = max(3, min(60, int(data.get("seconds", 15))))
    db.set_setting("autodj_outro_max", str(sec))
    return jsonify({"ok": True, "seconds": sec})


@app.route("/api/backups")
def api_backups():
    return jsonify(backup.list_backups())


@app.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    filename = backup.create_backup()
    if filename:
        return jsonify({"ok": True, "filename": filename})
    return jsonify({"ok": False, "error": "Backup failed"}), 500


@app.route("/settings/backup/restore", methods=["POST"])
def settings_backup_restore():
    data = request.get_json()
    filename = data.get("filename", "")
    location = data.get("location", "local")
    if backup.restore_backup(filename, location):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Restore failed"}), 500


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


@app.route("/settings/musicbrainz/toggle", methods=["POST"])
def settings_musicbrainz_toggle():
    currently = db.get_setting("musicbrainz_enrichment") != "0"
    db.set_setting("musicbrainz_enrichment", "0" if currently else "1")
    return jsonify({"ok": True, "enabled": not currently})


@app.route("/settings/bpm-analyzer/toggle", methods=["POST"])
def settings_bpm_toggle():
    global _scan_override
    currently = db.get_setting("bpm_analyzer_enabled") == "1"
    db.set_setting("bpm_analyzer_enabled", "0" if currently else "1")
    _scan_override = False  # reset override when toggling
    if currently:
        pass
    else:
        lib_module.start_daemon()
    return jsonify({"ok": True, "enabled": not currently})


@app.route("/settings/bpm-analyzer/backend", methods=["POST"])
def settings_bpm_backend():
    data = request.get_json() or {}
    backend = data.get("backend", "aubio")
    if backend not in ("aubio", "essentia"):
        return jsonify({"error": "Invalid backend"}), 400
    db.set_setting("bpm_backend", backend)
    return jsonify({"ok": True, "backend": backend})


@app.route("/api/bpm-analyzer/status")
def api_bpm_status():
    global _last_client_active
    if request.headers.get("X-Tab-Visible") == "1":
        _last_client_active = time.time()
    daemon = lib_module.get_daemon_status()
    rescan = lib_module.get_rescan_status()
    enabled = db.get_setting("bpm_analyzer_enabled") == "1"
    # Count tracks still needing BPM/Key analysis
    try:
        conn = db.get_db()
        pending = conn.execute(
            "SELECT COUNT(*) FROM library_tracks WHERE trashed=0 AND (bpm IS NULL OR bpm=0 OR key IS NULL OR key='')"
        ).fetchone()[0]
        conn.close()
    except Exception:
        pending = 0
    # Check if workers are paused due to active client
    bpm_status = bpm_analyzer.get_status()
    paused = bpm_status.get("paused", False) or is_client_active()
    scan_status = lib_module.get_scan_status()
    scan_active = scan_status.get("running", False)
    return jsonify({
        "enabled": enabled,
        "running": daemon.get("running", False),
        "paused": paused and not scan_active,
        "phase": daemon.get("phase", ""),
        "current_subdir": daemon.get("current_subdir", ""),
        "rescan_running": rescan.get("running", False),
        "rescan_total": rescan.get("total", 0),
        "rescan_scanned": rescan.get("scanned", 0),
        "pending": pending,
        "scan_phase": scan_status.get("phase", ""),
        "scan_scanned": scan_status.get("files_scanned", 0),
        "scan_total": scan_status.get("files_total", 0),
        "scan_running": scan_active,
    })


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

@app.route("/api/heartbeat", methods=["POST"])
def api_heartbeat():
    """Called by browser when tab is visible. Pauses background workers."""
    global _last_client_active
    _last_client_active = time.time()
    return jsonify({"ok": True})


@app.route("/api/scan-override", methods=["POST"])
def api_scan_override():
    """Override pause: let daemon run despite active client."""
    global _scan_override
    _scan_override = True
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    global _last_client_active, _client_audio_playing
    # Track active client from status poll (header set by JS)
    _client_audio_playing = request.headers.get("X-Audio-Playing") == "1"
    if request.headers.get("X-Tab-Visible") == "1":
        _last_client_active = time.time()
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


@app.route("/api/icy")
def api_icy_generic():
    """Get ICY metadata for any stream URL."""
    url = request.args.get("url", "")
    if not url:
        return jsonify({"current_track": "", "cover_url": None})
    ct = ""
    try:
        r = req_lib.get(url, headers={"Icy-MetaData": "1"}, stream=True, timeout=5)
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
    return jsonify({"current_track": ct, "cover_url": None})


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
    """Cast a stream to a device. Accepts stream_id (recordings) or url (bookmarks)."""
    data = request.get_json() or {}
    stream_id = data.get("stream_id")
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"success": False, "error": "device_id required"}), 400

    # Bookmarks: negative stream_id = bookmark, look up URL from DB
    if stream_id and int(stream_id) < 0:
        bm_id = abs(int(stream_id))
        bms = db.get_stream_bookmarks()
        bm = next((b for b in bms if b["id"] == bm_id), None)
        if not bm:
            return jsonify({"success": False, "error": "Bookmark not found"}), 404
        stream_url = bm["url"]
    elif stream_id:
        stream = db.get_stream(int(stream_id))
        if not stream:
            return jsonify({"success": False, "error": "Stream nicht gefunden"}), 404
        stream_url = stream["url"]
    else:
        return jsonify({"success": False, "error": "stream_id required"}), 400

    # If this device is already casting something, stop it first
    active = cast.get_active_casts()
    if device_id in active:
        cast.stop_cast(device_id)
        cast.remove_active_cast_by_device(device_id)

    # Limit to 4 simultaneous casts
    active = cast.get_active_casts()
    if len(active) >= 4:
        return jsonify({"success": False, "message": i18n.t("cast.max_reached")}), 400

    ok, msg = cast.cast_stream(stream_url, device_id)
    if ok:
        cast.set_active_cast(int(stream_id), device_id)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/cast/play-url", methods=["POST"])
def api_cast_play_url():
    """Cast a URL directly to a device (for bookmarks)."""
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    device_id = data.get("device_id")
    if not url or not device_id:
        return jsonify({"success": False, "error": "url and device_id required"}), 400
    active = cast.get_active_casts()
    if device_id in active:
        cast.stop_cast(device_id)
        cast.remove_active_cast_by_device(device_id)
    bookmark_id = data.get("bookmark_id", 0)
    ok, msg = cast.cast_stream(url, device_id)
    if ok:
        # Register as active cast with negative bookmark ID
        cast.set_active_cast(-int(bookmark_id) if bookmark_id else -99999, device_id)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/cast/play-library", methods=["POST"])
def api_cast_play_library():
    """Cast a library track to a device, optionally starting at a position."""
    data = request.get_json() or {}
    track_id = data.get("track_id")
    device_id = data.get("device_id")
    position = float(data.get("position", 0) or 0)
    if not track_id or not device_id:
        return jsonify({"success": False, "error": "track_id and device_id required"}), 400

    track = db.get_library_track(int(track_id))
    if not track:
        return jsonify({"success": False, "error": "Track not found"}), 404

    # Build URL that the cast device can reach
    server_ip = _get_server_ip()
    track_url = f"http://{server_ip}:{PORT}/api/library/track/{track_id}/play"

    # Stop existing cast on this device
    active = cast.get_active_casts()
    if device_id in active:
        cast.stop_cast(device_id)
        cast.remove_active_cast_by_device(device_id)

    active = cast.get_active_casts()
    if len(active) >= 4:
        return jsonify({"success": False, "message": "Max 4 casts"}), 400

    ok, msg = cast.cast_stream(track_url, device_id)
    if ok:
        cast.set_active_cast(-int(track_id), device_id)  # negative ID = library track
        # Seek to the browser's current position so playback continues mid-song.
        # Delayed slightly so LMS has time to start the stream before seeking.
        if position > 1:
            def _delayed_seek(did, pos):
                import time as _t
                _t.sleep(0.5)
                cast.seek_device(did, pos)
            threading.Thread(target=_delayed_seek, args=(device_id, position), daemon=True).start()
    return jsonify({"success": ok, "message": msg})


@app.route("/api/cast/device-mode", methods=["GET"])
def api_cast_device_mode():
    """Get playback mode of a cast device (play/stop/pause)."""
    device_id = request.args.get("device_id", "")
    if not device_id:
        return jsonify({"mode": None})
    mode = cast.get_device_playback_mode(device_id)
    return jsonify({"mode": mode})


def _get_server_ip():
    """Get the server's LAN IP address."""
    try:
        import socket as _s
        sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.route("/api/cast/stop", methods=["POST"])
def api_cast_stop():
    """Stop casting a stream. Accepts stream_id (stops all devices) or device_id (stops one)."""
    data = request.get_json(silent=True) or {}
    if not data and request.data:
        try:
            data = json.loads(request.data)
        except Exception:
            data = {}
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


@app.route("/api/cast/stop-external", methods=["POST"])
def api_cast_stop_external():
    """Stop an externally-playing device (not a streampeg-initiated cast)."""
    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"success": False, "error": "device_id required"}), 400
    ok, msg = cast.stop_cast(device_id)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/cast/seek", methods=["POST"])
def api_cast_seek():
    """Seek a cast device to a specific position in seconds."""
    data = request.get_json() or {}
    device_id = data.get("device_id")
    position = data.get("position", 0)
    if not device_id:
        return jsonify({"success": False, "error": "device_id required"}), 400
    ok, msg = cast.seek_device(device_id, float(position))
    return jsonify({"success": ok, "message": msg})


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
    devices = cast.discover_devices()
    dev_map = {d["id"]: d for d in devices}

    # --- External playback: detect Sonos/LMS devices that are currently
    # playing something streampeg did NOT initiate, and surface them in the
    # player bar as read-only "external" players. ---
    #
    # Skip LMS devices that are sync-grouped with an active streampeg cast —
    # they're not independent sources, just sync partners.
    external_players = []
    taken_device_ids = set(active.keys())
    # Build set of LMS player_ids that are actively cast by streampeg,
    # plus their sync-group partners.
    _lms_cast_group = set()
    for did in taken_device_ids:
        dev = dev_map.get(did)
        if dev and dev.get("type") == "lms":
            pid = dev.get("player_id", "")
            _lms_cast_group.add(pid)
            # Query LMS for sync partners
            result = cast._lms_request(
                dev["host"], dev.get("port", 9000), pid, ["sync", "?"]
            )
            if result:
                sync_ids = result.get("result", {}).get("_sync", "")
                if sync_ids and sync_ids != "-":
                    for partner in sync_ids.split(","):
                        _lms_cast_group.add(partner.strip())

    for d in devices:
        if d["id"] in taken_device_ids:
            continue
        if d["type"] not in ("sonos", "lms"):
            continue
        if d["type"] == "sonos" and not d.get("enabled", False):
            continue
        # Skip LMS sync-group partners of active casts
        if d["type"] == "lms" and d.get("player_id", "") in _lms_cast_group:
            continue
        np = cast.get_device_now_playing(d)
        if not np or np.get("mode") != "play":
            continue
        artist = np.get("artist", "") or ""
        title = np.get("title", "") or ""
        track_name = (artist + " - " + title) if (artist and title) else (title or artist)
        vol = cast.get_volume(d["id"])
        external_players.append({
            "stream_id": 0,
            "stream_name": d["type"].upper(),
            "device_id": d["id"],
            "device_name": d.get("name", d["id"]),
            "device_type": d.get("type", ""),
            "current_track": track_name,
            "cover_url": np.get("cover_url"),
            "volume": vol,
            "running": True,
            "external": True,
        })

    if not active and not external_players:
        return jsonify({"active": False})

    # Get stream status for active casts
    players = []
    all_streams = db.get_all_streams()
    stream_map = {s["id"]: s for s in all_streams}

    for device_id, stream_id_raw in active.items():
        stream_id = int(stream_id_raw) if isinstance(stream_id_raw, str) else stream_id_raw
        device = dev_map.get(device_id)
        if not device:
            # Device not found in discovery (may be temporarily unreachable).
            # Try LMS fallback cache for real name/type, else use ID as name.
            fb = {fb["id"]: fb for fb in cast._lms_players_fallback}.get(device_id)
            device = fb if fb else {"id": device_id, "name": device_id, "type": "unknown"}

        # Library track cast (negative stream_id, track exists in library)
        if stream_id < 0:
            abs_id = abs(stream_id)
            track = db.get_library_track(abs_id)
            if track:
                artist = track.get("artist", "") or ""
                title = track.get("title", "") or ""
                track_name = (artist + " - " + title) if artist else title
                vol = cast.get_volume(device_id)
                players.append({
                    "stream_id": stream_id,
                    "stream_name": "library",
                    "device_id": device_id,
                    "device_name": device.get("name", device_id),
                    "device_type": device.get("type", ""),
                    "current_track": track_name,
                    "cover_url": f"/api/library/track/{abs_id}/cover",
                    "volume": vol,
                    "running": True,
                    "is_library": True,
                })
                continue

            # Bookmark cast (negative stream_id, not a library track)
            bms = db.get_stream_bookmarks()
            bm = next((b for b in bms if b["id"] == abs_id), None)
            if bm:
                # Get ICY track info
                icy_track = ""
                try:
                    r = req_lib.get(bm["url"], headers={"Icy-MetaData": "1"}, stream=True, timeout=5)
                    icy_interval = int(r.headers.get("icy-metaint", 0))
                    if icy_interval > 0:
                        r.raw.read(icy_interval)
                        meta_len = struct.unpack("B", r.raw.read(1))[0] * 16
                        if meta_len > 0:
                            meta = r.raw.read(meta_len).decode("utf-8", errors="ignore").rstrip("\0")
                            m = re.search(r"StreamTitle='([^']*)'", meta)
                            if m and m.group(1).strip():
                                icy_track = m.group(1).strip()
                    r.close()
                except Exception:
                    pass
                # Get cover art from iTunes for current track
                cover_url = bm.get("favicon") or None
                if icy_track:
                    try:
                        itunes_cover = cover_art._itunes_search(icy_track)
                        if itunes_cover:
                            cover_url = itunes_cover
                    except Exception:
                        pass
                vol = cast.get_volume(device_id)
                players.append({
                    "stream_id": stream_id,
                    "stream_name": bm["name"],
                    "device_id": device_id,
                    "device_name": device.get("name", device_id),
                    "device_type": device.get("type", ""),
                    "current_track": icy_track,
                    "cover_url": cover_url,
                    "volume": vol,
                    "running": True,
                    "is_library": False,
                })
                continue

        stream = stream_map.get(stream_id)
        if not stream:
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

    # Append external players (read-only, not tied to a streampeg cast)
    players.extend(external_players)

    # All available speakers for multiroom toggle.
    # Include devices from discovery + any active cast devices that discovery
    # may have missed (LMS discovery can be intermittent). Use the LMS
    # fallback cache to resolve names when discovery fails.
    all_device_ids = {d["id"] for d in devices}
    augmented_devices = list(devices)

    # Build a name cache from LMS fallback players (survives discovery failures)
    _lms_name_cache = {}
    for fb in cast._lms_players_fallback:
        _lms_name_cache[fb["id"]] = fb

    for did in active:
        if did not in all_device_ids:
            # Try LMS fallback cache first (has real name/type/player_id)
            cached = _lms_name_cache.get(did)
            if cached:
                fallback = dict(cached)
                fallback["enabled"] = True
            else:
                fallback = {"id": did, "name": did, "type": "unknown", "enabled": True}
                for p in players:
                    if p["device_id"] == did:
                        fallback["name"] = p.get("device_name", did)
                        fallback["type"] = p.get("device_type", "unknown")
                        break
            augmented_devices.append(fallback)

    # Determine the "master" device — the first device that streampeg cast to.
    # All other active LMS devices are sync partners and point to the master.
    master_device_id = None
    for p in players:
        if not p.get("external"):
            master_device_id = p["device_id"]
            break

    all_speakers = []
    for d in augmented_devices:
        if not d.get("enabled", True):
            continue
        in_group_of = None
        # Direct match: device is itself an active player
        for p in players:
            if d["id"] == p["device_id"]:
                # Point to master, not to itself
                in_group_of = master_device_id or p["device_id"]
                break
        # Indirect match: LMS sync-partner of an active cast
        if not in_group_of and d.get("player_id"):
            if d.get("player_id", "") in _lms_cast_group:
                in_group_of = master_device_id
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


@app.route("/api/library/folders")
def api_library_folders():
    subdirs = db.get_stream_subdirs()
    folders = []
    for sd in subdirs:
        conn = db.get_db()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM library_tracks WHERE stream_subdir = ? AND trashed = 0", (sd,)
        ).fetchone()
        conn.close()
        folders.append({"name": sd, "track_count": row["cnt"] if row else 0})
    return jsonify({"folders": folders})


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
    # Slim down response — only fields needed for the list view
    slim = []
    for t_row in tracks:
        slim.append({
            "id": t_row["id"],
            "title": t_row.get("title", ""),
            "artist": t_row.get("artist", ""),
            "bpm": t_row.get("bpm", 0),
            "key": t_row.get("key", ""),
            "duration_sec": t_row.get("duration_sec", 0),
            "rating": t_row.get("rating", 0),
            "favorited": t_row.get("favorited", 0),
            "mtime": t_row.get("mtime", 0),
            "bitrate": t_row.get("bitrate", 0),
            "size_bytes": t_row.get("size_bytes", 0),
            "stream_subdir": t_row.get("stream_subdir", ""),
            "cue_nums": t_row.get("cue_nums", ""),
            "unusable": t_row.get("unusable", 0),
        })
    return jsonify({
        "tracks": slim, "total": total,
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
    lib_module.start_scan(subdir=subdir)
    return jsonify({"success": True})


@app.route("/api/library/scan/status")
def api_library_scan_status():
    return jsonify(lib_module.get_scan_status())


@app.route("/api/library/loudness/status")
def api_library_loudness_status():
    return jsonify(lib_module.get_loudness_status())


@app.route("/api/library/rescan-tags", methods=["POST"])
def api_library_rescan_tags():
    data = request.get_json()
    subdir = (data.get("subdir") or "").strip()
    if not subdir:
        return jsonify({"error": "subdir required"}), 400
    lib_module.start_rescan_tags(subdir)
    return jsonify({"success": True})


@app.route("/api/library/rescan-tags/status")
def api_library_rescan_tags_status():
    return jsonify(lib_module.get_rescan_status())


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
    lib_module.delete_m3u(pl["name"])
    lib_module.delete_playlist_dir(pl["name"])
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
        lib_module.delete_m3u(old["name"])
        lib_module.rename_playlist_dir(old["name"], name)
    db.rename_playlist(playlist_id, name)
    lib_module.generate_m3u(playlist_id)
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/color", methods=["POST"])
def api_library_playlist_color(playlist_id):
    data = request.get_json() or {}
    color = data.get("color", "")
    conn = db.get_db()
    conn.execute("UPDATE playlists SET color = ? WHERE id = ?", (color, playlist_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/tracks")
def api_library_playlist_tracks(playlist_id):
    tracks = db.get_playlist_tracks(playlist_id)
    return jsonify({"tracks": tracks})


def _bg_sync_playlist(playlist_id):
    import threading as _th
    def _run():
        try:
            lib_module.generate_m3u(playlist_id)
        except Exception as e:
            log.error("bg sync playlist %s failed: %s", playlist_id, e)
    _th.Thread(target=_run, daemon=True).start()


@app.route("/api/library/playlists/<int:playlist_id>/add", methods=["POST"])
@app.route("/api/library/playlists/<int:playlist_id>/tracks", methods=["POST"])
def api_library_playlist_add(playlist_id):
    data = request.get_json()
    track_ids = data.get("track_ids", [])
    if not track_ids:
        return jsonify({"error": "No tracks"}), 400
    db.add_to_playlist(playlist_id, track_ids)
    # File copy + m3u generation runs in background to keep UI responsive.
    _bg_sync_playlist(playlist_id)
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/tracks/<int:track_id>", methods=["DELETE"])
def api_library_playlist_remove(playlist_id, track_id):
    db.remove_from_playlist(playlist_id, track_id)
    # File removal + m3u regen runs in background to keep UI responsive.
    def _run():
        try:
            lib_module.remove_track_from_playlist_dir(playlist_id, track_id)
            lib_module.generate_m3u(playlist_id)
        except Exception as e:
            log.error("bg remove from playlist %s failed: %s", playlist_id, e)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/reorder", methods=["POST"])
def api_library_playlist_reorder(playlist_id):
    data = request.get_json()
    track_ids = data.get("track_ids", [])
    db.reorder_playlist(playlist_id, track_ids)
    lib_module.generate_m3u(playlist_id)
    return jsonify({"success": True})


@app.route("/api/library/playlists/<int:playlist_id>/m3u")
def api_library_playlist_m3u(playlist_id):
    pl = db.get_playlist(playlist_id)
    if not pl:
        return jsonify({"error": "not found"}), 404
    # Generate fresh M3U
    lib_module.generate_m3u(playlist_id)
    pl_dir = lib_module._get_playlist_dir(pl["name"])
    safe_name = lib_module._safe_playlist_name(pl["name"])
    m3u_path = os.path.join(pl_dir, safe_name + ".m3u") if pl_dir else None
    if not m3u_path or not os.path.isfile(m3u_path):
        return jsonify({"error": "M3U not found"}), 404
    return send_file(m3u_path, mimetype="audio/x-mpegurl",
                     as_attachment=True, download_name=pl["name"] + ".m3u")


@app.route("/api/usb-devices")
def api_usb_devices():
    """List mounted USB/removable storage devices."""
    import subprocess
    devices = []
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,LABEL,FSTYPE,RM,HOTPLUG"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for dev in data.get("blockdevices", []):
                _find_usb_mounts(dev, devices)
    except Exception as e:
        log.error("USB device scan error: %s", e)
    return jsonify({"devices": devices})


def _find_usb_mounts(dev, results, parent_removable=False):
    """Recursively find mounted partitions on removable/hotplug devices."""
    is_removable = dev.get("rm") or dev.get("hotplug") or parent_removable
    mountpoint = dev.get("mountpoint")
    if is_removable and mountpoint and dev.get("type") == "part":
        label = dev.get("label") or dev.get("name", "")
        size = dev.get("size", "")
        # Check free space
        try:
            stat = os.statvfs(mountpoint)
            free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        except Exception:
            free_gb = 0
        results.append({
            "name": dev.get("name", ""),
            "label": label,
            "mountpoint": mountpoint,
            "size": size,
            "free_gb": round(free_gb, 1),
            "fstype": dev.get("fstype", ""),
        })
    for child in dev.get("children", []):
        _find_usb_mounts(child, results, is_removable)


@app.route("/api/library/playlists/<int:playlist_id>/export.zip")
def api_library_playlist_export_zip(playlist_id):
    """Stream playlist as ZIP download (M3U + all MP3s)."""
    import zipfile, io
    pl = db.get_playlist(playlist_id)
    if not pl:
        return jsonify({"error": "not found"}), 404
    tracks = db.get_playlist_tracks(playlist_id)
    safe_name = lib_module._safe_playlist_name(pl["name"])

    def generate():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
            # Generate M3U
            lines = ["#EXTM3U"]
            for t in tracks:
                dur = t.get("duration_sec", 0)
                artist = t.get("artist", "")
                title = t.get("title", "") or t.get("filename", "")
                display = f"{artist} - {title}" if artist else title
                filename = os.path.basename(t.get("filepath", ""))
                lines.append(f"#EXTINF:{dur},{display}")
                lines.append(filename)
            zf.writestr(f"{safe_name}/{safe_name}.m3u", "\n".join(lines) + "\n")
            # Add MP3 files
            for t in tracks:
                filepath = t.get("filepath", "")
                if os.path.isfile(filepath):
                    filename = os.path.basename(filepath)
                    zf.write(filepath, f"{safe_name}/{filename}")

            # Generate Mixxx XML with cue points
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
            xml += '<Playlist name="{}" tracks="{}">\n'.format(
                pl["name"].replace("&", "&amp;").replace("<", "&lt;"), len(tracks))
            for t in tracks:
                cues = db.get_cue_points(t["id"])
                fname = os.path.basename(t.get("filepath", ""))
                xml += '  <Track>\n'
                xml += '    <Location>{}</Location>\n'.format(fname.replace("&", "&amp;"))
                if t.get("title"):
                    xml += '    <Title>{}</Title>\n'.format(t["title"].replace("&", "&amp;").replace("<", "&lt;"))
                if t.get("artist"):
                    xml += '    <Artist>{}</Artist>\n'.format(t["artist"].replace("&", "&amp;").replace("<", "&lt;"))
                if t.get("bpm") and t["bpm"] > 0:
                    xml += '    <BPM>{}</BPM>\n'.format(t["bpm"])
                if t.get("key"):
                    xml += '    <Key>{}</Key>\n'.format(t["key"])
                if t.get("duration_sec"):
                    xml += '    <Duration>{}</Duration>\n'.format(t["duration_sec"])
                if cues:
                    xml += '    <CuePoints>\n'
                    for num, pos in sorted(cues.items(), key=lambda x: int(x[0])):
                        xml += '      <Cue number="{}" position="{}"/>\n'.format(num, pos)
                    xml += '    </CuePoints>\n'
                xml += '  </Track>\n'
            xml += '</Playlist>\n'
            zf.writestr(f"{safe_name}/{safe_name}.xml", xml)

            # Add import script
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "import_cues_to_mixxx.py")
            if os.path.isfile(script_path):
                zf.write(script_path, f"{safe_name}/import_cues_to_mixxx.py")

            # Add .command launcher for macOS
            cmd = '#!/bin/bash\ncd "$(dirname "$0")"\npython3 import_cues_to_mixxx.py "{}.xml"\nread -p "Press Enter to close..."\n'.format(safe_name)
            zf.writestr(f"{safe_name}/Import Cues to Mixxx.command", cmd)

        buf.seek(0)
        yield buf.read()

    return app.response_class(
        generate(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{safe_name}.zip"'}
    )


@app.route("/api/library/track/<int:track_id>")
def api_library_track_detail(track_id):
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(track))


@app.route("/api/library/track/<int:track_id>/cues")
def api_library_track_cues_get(track_id):
    cues = db.get_cue_points(track_id)
    return jsonify({"cues": cues})


@app.route("/api/library/track/<int:track_id>/cues", methods=["POST"])
def api_library_track_cues_set(track_id):
    data = request.get_json() or {}
    cues = data.get("cues", {})
    db.set_cue_points(track_id, cues)
    return jsonify({"success": True})


@app.route("/api/library/track/<int:track_id>/rescan-bitrate", methods=["POST"])
def api_library_track_rescan_bitrate(track_id):
    """Re-probe the actual bitrate of a single track via ffprobe and persist it."""
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    fp = track["filepath"]
    if not fp or not os.path.isfile(fp):
        return jsonify({"error": "file not found"}), 404
    real = lib_module._ffprobe_bitrate(fp)
    if real <= 0:
        return jsonify({"error": "ffprobe failed"}), 500
    conn = db.get_db()
    conn.execute("UPDATE library_tracks SET bitrate = ? WHERE id = ?", (real, track_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "bitrate": real})


@app.route("/api/library/rescan-bitrate-bulk", methods=["POST"])
def api_library_rescan_bitrate_bulk():
    """Re-probe the bitrate of many tracks via ffprobe.
    Body: {"subdir": "..."} (LIKE match) OR {"track_ids": [...]}.
    Runs in a background thread so the request returns immediately."""
    data = request.get_json() or {}
    subdir = (data.get("subdir") or "").strip()
    track_ids = data.get("track_ids") or []
    conn = db.get_db()
    if track_ids:
        placeholders = ",".join("?" * len(track_ids))
        rows = conn.execute(
            f"SELECT id, filepath FROM library_tracks WHERE id IN ({placeholders})",
            track_ids,
        ).fetchall()
    elif subdir:
        like = f"%{subdir}%"
        rows = conn.execute(
            "SELECT id, filepath FROM library_tracks WHERE LOWER(stream_subdir) LIKE LOWER(?) OR LOWER(filepath) LIKE LOWER(?)",
            (like, like),
        ).fetchall()
    else:
        conn.close()
        return jsonify({"error": "need subdir or track_ids"}), 400
    rows = [dict(r) for r in rows]
    conn.close()

    def _run():
        c = db.get_db()
        updated = 0
        for r in rows:
            fp = r.get("filepath")
            if not fp or not os.path.isfile(fp):
                continue
            real = lib_module._ffprobe_bitrate(fp)
            if real > 0:
                c.execute("UPDATE library_tracks SET bitrate = ? WHERE id = ?", (real, r["id"]))
                updated += 1
        c.commit()
        c.close()
        log.info("rescan-bitrate-bulk: updated %d/%d tracks", updated, len(rows))
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "queued": len(rows)})


@app.route("/api/streams/<int:stream_id>/probe-bitrate")
def api_stream_probe_bitrate(stream_id):
    """Probe the LIVE stream URL with ffprobe to find out what bitrate the
    server is actually delivering right now (independent of any claim in the
    stream's metadata)."""
    stream = db.get_stream(stream_id)
    if not stream:
        return jsonify({"error": "not found"}), 404
    url = stream.get("url") or ""
    if not url:
        return jsonify({"error": "no url"}), 400
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_name,bit_rate,sample_rate,channels",
             "-show_entries", "format=bit_rate",
             "-of", "default=nw=1", url],
            capture_output=True, text=True, timeout=15)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    info = {"codec": "", "sample_rate": 0, "channels": 0, "stream_bitrate": 0, "format_bitrate": 0}
    seen_stream_br = False
    for line in r.stdout.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if k == "codec_name":
            info["codec"] = v
        elif k == "sample_rate" and v.isdigit():
            info["sample_rate"] = int(v)
        elif k == "channels" and v.isdigit():
            info["channels"] = int(v)
        elif k == "bit_rate" and v.isdigit():
            if not seen_stream_br:
                info["stream_bitrate"] = int(v)
                seen_stream_br = True
            else:
                info["format_bitrate"] = int(v)
    real_kbps = round(max(info["stream_bitrate"], info["format_bitrate"]) / 1000) if (info["stream_bitrate"] or info["format_bitrate"]) else 0
    if not real_kbps:
        return jsonify({"error": "ffprobe returned no bitrate", "stderr": r.stderr[-300:]}), 500
    return jsonify({"success": True, "bitrate": real_kbps, "codec": info["codec"],
                    "sample_rate": info["sample_rate"], "channels": info["channels"]})


@app.route("/api/library/track/<int:track_id>/rescan-bpmkey", methods=["POST"])
def api_library_track_rescan_bpmkey(track_id):
    """Re-run BPM/Key detection for a single track.
    Uses the same backend (essentia/aubio) as the daemon scan so results
    are consistent. Falls back to autotag.detect_key() only when the
    configured backend has no key detection (aubio)."""
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    filepath = track["filepath"]
    if not os.path.isfile(filepath):
        return jsonify({"error": "file not found"}), 404
    try:
        import bpm_analyzer
        backend = db.get_setting("bpm_backend") or "aubio"
        bpm, key = bpm_analyzer._analyze_track(filepath, backend)
        # aubio has no key detection — fall back to autotag
        if not key or key == "-":
            key = autotag.detect_key(filepath)
        conn = db.get_db()
        if bpm and bpm > 0:
            conn.execute("UPDATE library_tracks SET bpm = ? WHERE id = ?", (bpm, track_id))
        if key and key != "-":
            conn.execute("UPDATE library_tracks SET key = ? WHERE id = ?", (key, track_id))
        conn.commit()
        conn.close()
        updated = db.get_library_track(track_id)
        return jsonify({"success": True, "bpm": updated.get("bpm"), "key": updated.get("key")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/library/track/<int:track_id>/playlists")
def api_library_track_playlists(track_id):
    conn = db.get_db()
    rows = conn.execute(
        """SELECT p.id, p.name, p.color FROM playlists p
           JOIN playlist_tracks pt ON pt.playlist_id = p.id
           WHERE pt.track_id = ?
           ORDER BY p.name""",
        (track_id,),
    ).fetchall()
    conn.close()
    return jsonify({"playlists": [{"id": r["id"], "name": r["name"], "color": r["color"] or ""} for r in rows]})


@app.route("/api/library/tracks/playlists", methods=["POST"])
def api_library_tracks_playlists_batch():
    """Get playlist memberships for a batch of track IDs."""
    data = request.get_json() or {}
    track_ids = data.get("track_ids", [])
    if not track_ids:
        return jsonify({})
    conn = db.get_db()
    placeholders = ",".join("?" * len(track_ids))
    rows = conn.execute(
        f"""SELECT pt.track_id, p.id, p.name, p.color FROM playlists p
            JOIN playlist_tracks pt ON pt.playlist_id = p.id
            WHERE pt.track_id IN ({placeholders})
            ORDER BY p.name""",
        track_ids,
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        tid = str(r["track_id"])
        if tid not in result:
            result[tid] = []
        result[tid].append({"id": r["id"], "name": r["name"], "color": r["color"] or ""})
    return jsonify(result)


@app.route("/api/library/track/<int:track_id>/rating", methods=["POST"])
def api_library_track_rating(track_id):
    data = request.get_json() or {}
    rating = data.get("rating", 0)
    db.set_track_rating(track_id, rating)
    return jsonify({"success": True, "rating": max(0, min(5, rating))})


@app.route("/api/library/track/<int:track_id>/favorite", methods=["POST"])
def api_library_track_favorite(track_id):
    new_val = db.toggle_favorite(track_id)
    return jsonify({"success": True, "favorited": new_val})


@app.route("/api/library/track/<int:track_id>/unusable", methods=["POST"])
def api_library_track_unusable(track_id):
    new_val = db.toggle_unusable(track_id)
    return jsonify({"success": True, "unusable": new_val})


@app.route("/api/stream-favorites", methods=["GET"])
def api_stream_favorites():
    sort = request.args.get("sort", "newest")
    return jsonify({"favorites": db.get_stream_favorites(sort=sort)})


@app.route("/api/stream-favorites/toggle", methods=["POST"])
def api_stream_favorite_toggle():
    data = request.get_json() or {}
    track_name = data.get("track_name", "").strip()
    stream_name = data.get("stream_name", "").strip()
    cover_url = data.get("cover_url", "")
    stream_id = data.get("stream_id")
    if not track_name or not stream_name:
        return jsonify({"error": "missing fields"}), 400
    existing = db.is_stream_favorite(track_name, stream_name)
    if existing:
        db.remove_stream_favorite(existing)
        return jsonify({"favorited": False, "id": None})
    new_id = db.add_stream_favorite(track_name, stream_name, stream_id, cover_url)
    return jsonify({"favorited": True, "id": new_id})


@app.route("/api/stream-favorites/<int:fav_id>", methods=["DELETE"])
def api_stream_favorite_delete(fav_id):
    db.remove_stream_favorite(fav_id)
    return jsonify({"success": True})


@app.route("/api/library/track/find", methods=["POST"])
def api_library_track_find():
    """Find a library track by current_track string and stream name."""
    data = request.get_json() or {}
    track_str = data.get("track", "").strip()
    stream_subdir = data.get("stream_subdir", "").strip()
    if not track_str:
        return jsonify({"found": False})
    conn = db.get_db()
    # Try matching by filename (most reliable)
    fname = track_str.replace(" - ", " - ").replace("/", "_")
    row = conn.execute(
        """SELECT id, favorited FROM library_tracks
           WHERE trashed=0 AND (filename LIKE ? OR title LIKE ?)
           ORDER BY CASE WHEN stream_subdir=? THEN 0 ELSE 1 END
           LIMIT 1""",
        (f"%{fname}%", f"%{track_str}%", stream_subdir),
    ).fetchone()
    conn.close()
    if row:
        return jsonify({"found": True, "id": row["id"], "favorited": row["favorited"], "mode": "library"})
    # Check stream_favorites
    sf_id = db.is_stream_favorite(track_str, stream_subdir)
    if sf_id:
        return jsonify({"found": True, "mode": "stream", "favorited": 1, "sf_id": sf_id})
    return jsonify({"found": False, "mode": "stream"})


@app.route("/api/library/playlists/<int:playlist_id>/mixxx")
def api_library_playlist_mixxx(playlist_id):
    """Export playlist as Mixxx-compatible XML with BPM, Key, and cue points."""
    pl = db.get_playlist(playlist_id)
    if not pl:
        return jsonify({"error": "not found"}), 404
    tracks = db.get_playlist_tracks(playlist_id)

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<Playlist name="{}" tracks="{}">\n'.format(
        pl["name"].replace("&", "&amp;").replace("<", "&lt;"), len(tracks))

    for t in tracks:
        cues = db.get_cue_points(t["id"])
        xml += '  <Track>\n'
        xml += '    <Location>{}</Location>\n'.format(
            t["filepath"].replace("&", "&amp;").replace("<", "&lt;"))
        if t.get("title"):
            xml += '    <Title>{}</Title>\n'.format(
                t["title"].replace("&", "&amp;").replace("<", "&lt;"))
        if t.get("artist"):
            xml += '    <Artist>{}</Artist>\n'.format(
                t["artist"].replace("&", "&amp;").replace("<", "&lt;"))
        if t.get("bpm") and t["bpm"] > 0:
            xml += '    <BPM>{}</BPM>\n'.format(t["bpm"])
        if t.get("key"):
            xml += '    <Key>{}</Key>\n'.format(t["key"])
        if t.get("duration_sec"):
            xml += '    <Duration>{}</Duration>\n'.format(t["duration_sec"])
        if t.get("rating") and t["rating"] > 0:
            xml += '    <Rating>{}</Rating>\n'.format(t["rating"])
        if cues:
            xml += '    <CuePoints>\n'
            for num, pos in sorted(cues.items(), key=lambda x: int(x[0])):
                xml += '      <Cue number="{}" position="{}"/>\n'.format(num, pos)
            xml += '    </CuePoints>\n'
        xml += '  </Track>\n'

    xml += '</Playlist>\n'

    return Response(xml, mimetype="application/xml",
                    headers={"Content-Disposition": 'attachment; filename="{}.xml"'.format(pl["name"])})


@app.route("/api/library/playlists/<int:playlist_id>/csv")
def api_library_playlist_csv(playlist_id):
    """Export playlist as CSV with BPM, Key, Duration."""
    pl = db.get_playlist(playlist_id)
    if not pl:
        return jsonify({"error": "not found"}), 404
    tracks = db.get_playlist_tracks(playlist_id)

    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Title", "Artist", "BPM", "Key", "Duration", "Rating", "Filepath"])
    for t in tracks:
        dur_min = t.get("duration_sec", 0) or 0
        dur_str = f"{dur_min // 60}:{dur_min % 60:02d}" if dur_min else ""
        writer.writerow([
            t.get("title", ""), t.get("artist", ""),
            t.get("bpm", ""), t.get("key", ""),
            dur_str, t.get("rating", ""),
            t.get("filepath", ""),
        ])

    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{pl["name"]}.csv"'})


@app.route("/api/library/random")
def api_library_random():
    """Return a random track from the library."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT id, title, artist, stream_subdir FROM library_tracks WHERE trashed=0 ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no tracks"}), 404
    return jsonify({"id": row["id"], "title": row["title"], "artist": row["artist"], "stream_subdir": row["stream_subdir"]})


@app.route("/api/autodj/next")
def api_autodj_next():
    """Auto-DJ: find the best next track by BPM/Key compatibility."""
    import autodj as _autodj
    track_id = request.args.get("track_id", 0, type=int)
    playlist_id = request.args.get("playlist_id", None, type=int)
    stream = request.args.get("stream", None)
    result = _autodj.get_next_track(track_id, playlist_id=playlist_id, stream=stream)
    if not result:
        return jsonify({"error": "no compatible track"}), 404
    return jsonify(result)


import logging
_autodj_log = logging.getLogger("autodj")

@app.route("/api/autodj/log", methods=["POST"])
def api_autodj_log():
    data = request.get_json() or {}
    msg = data.get("msg", "")
    if msg:
        _autodj_log.info(msg)
        with open("/tmp/autodj_debug.log", "a") as f:
            f.write(msg + "\n")
    return jsonify({"ok": True})


@app.route("/api/library/track/<int:track_id>/play")
def api_library_track_play(track_id):
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    filepath = track["filepath"]
    if not os.path.isfile(filepath):
        return jsonify({"error": "file not found"}), 404
    return send_file(filepath, mimetype="audio/mpeg")


@app.route("/api/cover-search")
def api_cover_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"url": None})
    import cover_art
    url = cover_art._itunes_search(q)
    return jsonify({"url": url})


@app.route("/api/library/track/<int:track_id>/waveform")
def api_library_track_waveform(track_id):
    """Return waveform peaks (256 bars, normalized 0-1). Cached in DB."""
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"peaks": []}), 404

    # Return cached waveform if available
    if track.get("waveform"):
        return jsonify({"peaks": json.loads(track["waveform"])})

    filepath = track["filepath"]
    if not os.path.isfile(filepath):
        return jsonify({"peaks": []}), 404
    try:
        peaks = lib_module.generate_waveform(filepath)
        if peaks:
            conn = db.get_db()
            conn.execute("UPDATE library_tracks SET waveform = ? WHERE id = ?",
                         (json.dumps(peaks), track_id))
            conn.commit()
            conn.close()
            return jsonify({"peaks": peaks})
        return jsonify({"peaks": []})
    except Exception as e:
        return jsonify({"peaks": [], "error": str(e)})


@app.route("/api/library/track/<int:track_id>/waveform-hd")
def api_library_track_waveform_hd(track_id):
    """Return high-resolution waveform for the track editor."""
    bars = request.args.get("bars", 2048, type=int)
    bars = max(512, min(8192, bars))
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"peaks": []}), 404
    filepath = track["filepath"]
    if not os.path.isfile(filepath):
        return jsonify({"peaks": []}), 404
    try:
        peaks = lib_module.generate_waveform(filepath, num_bars=bars)
        return jsonify({"peaks": peaks or []})
    except Exception as e:
        return jsonify({"peaks": [], "error": str(e)})


@app.route("/api/library/track/<int:track_id>/metadata", methods=["POST"])
def api_library_track_metadata(track_id):
    """Update artist and title for a track (DB + ID3 tags)."""
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    artist = data.get("artist", "").strip()
    title = data.get("title", "").strip()
    conn = db.get_db()
    conn.execute("UPDATE library_tracks SET artist = ?, title = ? WHERE id = ?",
                 (artist, title, track_id))
    conn.commit()
    conn.close()
    # Write ID3 tags to file
    filepath = track["filepath"]
    if os.path.isfile(filepath):
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, TIT2, TPE1
            try:
                tags = ID3(filepath)
            except Exception:
                tags = ID3()
            tags["TIT2"] = TIT2(encoding=3, text=[title])
            tags["TPE1"] = TPE1(encoding=3, text=[artist])
            tags.save(filepath, v2_version=4)
        except Exception:
            pass
    return jsonify({"success": True})


@app.route("/api/library/track/<int:track_id>/trim", methods=["POST"])
def api_library_track_trim(track_id):
    """Trim audio file: keep only the region between start and end (seconds)."""
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    filepath = track["filepath"]
    if not os.path.isfile(filepath):
        return jsonify({"error": "file not found"}), 404

    data = request.get_json() or {}
    start = float(data.get("start", 0))
    end = float(data.get("end", 0))
    if start >= end or end <= 0:
        return jsonify({"error": "invalid range"}), 400

    try:
        # Backup original (only first time)
        bak = filepath + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(filepath, bak)

        # Trim with ffmpeg (decode-seek for frame accuracy)
        tmpfile = os.path.join("/tmp", "streampeg_trim_" + str(track_id) + ".mp3")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", filepath, "-ss", str(start), "-to", str(end),
             "-c", "copy", "-map_metadata", "0", tmpfile],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            if os.path.exists(tmpfile):
                os.remove(tmpfile)
            return jsonify({"error": "ffmpeg failed: " + result.stderr[-200:]}), 500

        # Replace original with trimmed version
        shutil.move(tmpfile, filepath)

        # Get new duration
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10
        )
        new_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 0
        new_size = os.path.getsize(filepath)

        # Regenerate waveform
        peaks = lib_module.generate_waveform(filepath)

        # Update DB
        conn = db.get_db()
        conn.execute(
            "UPDATE library_tracks SET duration_sec = ?, size_bytes = ?, waveform = ? WHERE id = ?",
            (int(new_duration), new_size, json.dumps(peaks) if peaks else "[]", track_id)
        )
        conn.commit()
        conn.close()

        return jsonify({"success": True, "new_duration": new_duration, "peaks": peaks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/library/track/<int:track_id>/cover")
def api_library_track_cover(track_id):
    track = db.get_library_track(track_id)
    if not track:
        return "", 404
    filepath = track["filepath"]
    if not os.path.isfile(filepath):
        return "", 404
    try:
        from mutagen.id3 import ID3
        tags = ID3(filepath)
        for key in tags:
            if key.startswith("APIC"):
                apic = tags[key]
                return Response(apic.data, mimetype=apic.mime or "image/jpeg")
    except Exception:
        pass
    return "", 404


@app.route("/api/library/track/<int:track_id>/autotag", methods=["POST"])
def api_library_track_autotag(track_id):
    """Auto-tag a single library track via AcoustID/MusicBrainz, then update DB."""
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    filepath = track["filepath"]
    if not os.path.isfile(filepath):
        return jsonify({"error": "file not found"}), 404
    try:
        autotag.tag_file(filepath)
        # Re-read tags and update DB
        tags = lib_module._read_id3(filepath)
        conn = db.get_db()
        conn.execute(
            """UPDATE library_tracks SET
                title = CASE WHEN ? != '' THEN ? ELSE title END,
                artist = CASE WHEN ? != '' THEN ? ELSE artist END,
                album = ?, genre = ?, bpm = CASE WHEN ? > 0 THEN ? ELSE bpm END,
                key = CASE WHEN ? != '' THEN ? ELSE key END,
                duration_sec = CASE WHEN ? > 0 THEN ? ELSE duration_sec END
            WHERE id = ?""",
            (
                tags["title"], tags["title"],
                tags["artist"], tags["artist"],
                tags["album"], tags["genre"],
                tags["bpm"], tags["bpm"],
                tags["key"], tags["key"],
                tags["duration_sec"], tags["duration_sec"],
                track_id,
            ),
        )
        conn.commit()
        conn.close()
        # Return updated track info
        updated = db.get_library_track(track_id)
        return jsonify({"success": True, "track": updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/library/track/<int:track_id>/trash", methods=["POST"])
def api_library_track_trash(track_id):
    """Trash a track: delete file but keep DB entry to prevent re-download."""
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    filepath = track["filepath"]
    if os.path.isfile(filepath):
        try:
            os.remove(filepath)
        except OSError as e:
            return jsonify({"error": f"Could not delete file: {e}"}), 500
    db.trash_library_track(track_id)
    return jsonify({"success": True})


@app.route("/api/library/track/<int:track_id>/delete", methods=["POST"])
def api_library_track_delete(track_id):
    """Fully delete track: remove file AND DB entry (allows re-download)."""
    track = db.get_library_track(track_id)
    if not track:
        return jsonify({"error": "not found"}), 404
    filepath = track["filepath"]
    if os.path.isfile(filepath):
        try:
            os.remove(filepath)
        except OSError as e:
            return jsonify({"error": f"Could not delete file: {e}"}), 500
    db.delete_library_track(track_id)
    return jsonify({"success": True})


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


@app.route("/api/yt-search", methods=["POST"])
def api_yt_search():
    """Search YouTube for audio content via yt-dlp."""
    import subprocess
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Query required"}), 400

    yt_dlp_candidates = [
        os.path.expanduser("~/.local/bin/yt-dlp"),
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
        "yt-dlp",
    ]
    yt_dlp = next((p for p in yt_dlp_candidates if os.path.isfile(p)), "yt-dlp")

    try:
        result = subprocess.run(
            [yt_dlp, "--flat-playlist", "--no-warnings", "-j",
             f"ytsearch10:{query}"],
            capture_output=True, text=True, timeout=30
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                items.append({
                    "id": entry.get("id", ""),
                    "title": entry.get("title", "Unknown"),
                    "uploader": entry.get("uploader") or entry.get("channel", ""),
                    "duration": entry.get("duration"),
                    "url": f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                    "thumbnail": entry.get("thumbnail") or entry.get("thumbnails", [{}])[-1].get("url", ""),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return jsonify({"ok": True, "results": items})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Search timeout"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/api/yt-preview", methods=["GET"])
def api_yt_preview():
    """Get a streamable audio URL for a YouTube video (for preview)."""
    import subprocess
    video_id = request.args.get("id", "").strip()
    if not video_id:
        return jsonify({"ok": False, "error": "id required"}), 400

    yt_dlp_candidates = [
        os.path.expanduser("~/.local/bin/yt-dlp"),
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
        "yt-dlp",
    ]
    yt_dlp = next((p for p in yt_dlp_candidates if os.path.isfile(p)), "yt-dlp")

    try:
        result = subprocess.run(
            [yt_dlp, "-f", "bestaudio", "-g", "--no-warnings",
             "--no-playlist", f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=15
        )
        url = result.stdout.strip().split("\n")[0]
        if url and url.startswith("http"):
            # Return proxy URL to avoid CORS issues
            return jsonify({"ok": True, "url": f"/api/yt-preview/stream?id={video_id}"})
        return jsonify({"ok": False, "error": "Could not get audio URL"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/yt-preview/stream", methods=["GET"])
def api_yt_preview_stream():
    """Proxy-stream audio from YouTube to avoid CORS issues."""
    import subprocess
    video_id = request.args.get("id", "").strip()
    if not video_id:
        return "Missing id", 400

    yt_dlp_candidates = [
        os.path.expanduser("~/.local/bin/yt-dlp"),
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
        "yt-dlp",
    ]
    yt_dlp = next((p for p in yt_dlp_candidates if os.path.isfile(p)), "yt-dlp")

    # Use yt-dlp + ffmpeg to pipe audio as mp3
    try:
        proc = subprocess.Popen(
            [yt_dlp, "-f", "bestaudio", "-o", "-", "--no-warnings",
             "--no-playlist", f"https://www.youtube.com/watch?v={video_id}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        ffmpeg = subprocess.Popen(
            ["ffmpeg", "-i", "pipe:0", "-f", "mp3", "-q:a", "5", "-"],
            stdin=proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        proc.stdout.close()

        def generate():
            try:
                while True:
                    chunk = ffmpeg.stdout.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                ffmpeg.stdout.close()
                ffmpeg.wait()
                proc.wait()

        return app.response_class(generate(), mimetype="audio/mpeg")
    except Exception:
        return "Stream error", 502


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
    is_mix = list_id.startswith("RD") or list_id.startswith("UU")
    is_explicit_playlist = "/playlist" in url.split("?")[0]
    is_playlist_url = is_explicit_playlist or is_real_playlist or is_mix
    has_single_video = "watch?v=" in url or "youtu.be/" in url

    # Keep original URL for playlist fetch (don't strip list= anymore)
    single_url = re.sub(r"[&?]list=[^&]*", "", url)
    single_url = re.sub(r"[&?]index=[^&]*", "", single_url)

    # Step 1: Always get single video info first (unless explicit playlist URL)
    # Use --print instead of --dump-json to skip slow format extraction
    single_info = None
    if has_single_video:
        try:
            result = subprocess.run(
                [yt_dlp, "--no-playlist", "--no-download", "--no-warnings",
                 "--print", "%(title)s\n%(duration)s\n%(uploader)s", single_url],
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

    # Step 2: Fetch playlist entries for playlists and mixes
    if is_playlist_url:
        try:
            # Limit entries: Mixes are quasi-infinite, cap at 25; real playlists at 500
            max_entries = 25 if is_mix else 500

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
# URLs currently being downloaded — guarded by _yt_jobs_lock. Prevents two
# parallel downloads of the same URL from racing on the same destination file
# (which is how the corrupt Dub_Deep_Techno_Mix_Paris.mp3 came to exist).
_yt_active_urls = set()


def _yt_verify_mp3(filepath):
    """Decode-test a downloaded mp3 with ffmpeg. Returns (ok, error_msg).
    A file that ffprobe accepts but ffmpeg refuses to decode (e.g. broken
    backstep, truncated frames) is treated as corrupt."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", filepath, "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "decode timeout"
    except Exception as e:
        return False, f"decode error: {e}"
    if r.returncode != 0 or "invalid" in r.stderr.lower() or "error" in r.stderr.lower():
        msg = r.stderr.strip().splitlines()[0] if r.stderr.strip() else f"rc={r.returncode}"
        return False, msg[:200]
    return True, ""


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

    # Per-job temp dir under dest. yt-dlp writes here first; only files that
    # pass the ffmpeg decode test are atomically moved into `dest`. This makes
    # parallel downloads of the same URL safe (each job has its own temp dir),
    # and crashes/interruptions can't leave half-written .mp3 files in dest.
    tmp_dir = os.path.join(dest, ".tmp_" + job_id)
    os.makedirs(tmp_dir, exist_ok=True)

    cmd = [
        yt_dlp, url,
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--format", "bestaudio",
        "--postprocessor-args", "ffmpeg:-b:a 320k",
        "--output", os.path.join(tmp_dir, "%(title)s.%(ext)s"),
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

        produced_in_tmp = []

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
            elif line.startswith(tmp_dir) and line.endswith(".mp3"):
                produced_in_tmp.append(line.strip())
            elif "has already been downloaded" in line:
                log({"status": "skip", "message": "Bereits vorhanden, uebersprungen"})

        proc.wait(timeout=10)

        # Pick up everything yt-dlp left in tmp_dir, even if --print didn't catch it
        for f in Path(tmp_dir).glob("*.mp3"):
            sp = str(f)
            if sp not in produced_in_tmp:
                produced_in_tmp.append(sp)

        # Verify each file with ffmpeg, then atomically move good ones to dest.
        downloaded_files = []
        for src in produced_in_tmp:
            if not os.path.isfile(src):
                continue
            log({"status": "verifying", "message": f"Pruefe: {os.path.basename(src)}"})
            ok, err = _yt_verify_mp3(src)
            if not ok:
                log({"status": "verify_fail",
                     "message": f"Datei korrupt, verworfen ({err}): {os.path.basename(src)}"})
                try:
                    os.remove(src)
                except OSError:
                    pass
                continue
            # Atomic move into dest, with collision-handling rename
            base = os.path.basename(src)
            target = os.path.join(dest, base)
            if os.path.exists(target):
                name, ext = os.path.splitext(base)
                target = os.path.join(dest, f"{name}_{int(time.time())}{ext}")
            try:
                os.replace(src, target)
                downloaded_files.append(target)
            except OSError as e:
                log({"status": "error", "message": f"Move-Fehler: {e}"})

        # Clean up temp dir (now empty unless a verify_fail left orphans)
        try:
            for leftover in Path(tmp_dir).iterdir():
                try:
                    leftover.unlink()
                except OSError:
                    pass
            os.rmdir(tmp_dir)
        except OSError:
            pass

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

    finally:
        with _yt_jobs_lock:
            _yt_active_urls.discard(url)

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

    # Refuse a duplicate of the exact same URL while one is still running.
    # Without this guard, two parallel jobs race on the same destination filename
    # and yt-dlp + ffmpeg can produce a corrupt mp3.
    with _yt_jobs_lock:
        if url in _yt_active_urls:
            return Response(
                "data: " + json.dumps({
                    "status": "error",
                    "error": "already_downloading",
                    "message": "Diese URL wird bereits heruntergeladen",
                }) + "\n\n",
                mimetype="text/event-stream",
            )
        _yt_active_urls.add(url)

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
    """Graceful shutdown: save running streams, stop all, cleanup."""
    print("Shutting down: saving running stream IDs...")
    running_ids = [sid for sid in process_manager._processes
                   if process_manager._processes[sid].get("proc") and
                   (hasattr(process_manager._processes[sid]["proc"], 'poll') and process_manager._processes[sid]["proc"].poll() is None
                    or hasattr(process_manager._processes[sid]["proc"], 'is_running') and process_manager._processes[sid]["proc"].is_running())]
    db.set_setting("running_streams_on_shutdown", json.dumps(running_ids))
    print(f"  Saved {len(running_ids)} running stream IDs: {running_ids}")
    print("Shutting down: stopping background workers...")
    lib_module.stop_daemon()
    print("Shutting down: stopping all streams...")
    process_manager.stop_all_streams()
    streams = db.get_all_streams()
    process_manager.cleanup_incomplete(streams)
    print("Shutdown complete.")
    if scheduler:
        scheduler.stop()
    dlna_server.stop()
    lms_compat.stop()
    slimproto.stop()
    print("Shutdown complete.")


if __name__ == "__main__":
    db.init_db()
    cast._load_lms_fallback()
    module_manager.discover_modules()

    # Register shutdown handler
    atexit.register(_shutdown)
    def _sigterm_handler(sig, frame):
        # Save running stream IDs before exit
        print("SIGTERM received, saving running streams...")
        running_ids = [sid for sid in process_manager._processes
                       if process_manager._processes[sid].get("proc") and
                       hasattr(process_manager._processes[sid]["proc"], 'poll') and
                       process_manager._processes[sid]["proc"].poll() is None]
        db.set_setting("running_streams_on_shutdown", json.dumps(running_ids))
        print(f"  Saved {len(running_ids)} stream IDs: {running_ids}")
        # Stop background workers
        lib_module.stop_daemon()
        # Exit — daemon threads are killed automatically
        print("Exiting.")
        os._exit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Cleanup incomplete files from previous run
    streams = db.get_all_streams()
    process_manager.cleanup_incomplete(streams)

    # Adopt existing streamripper processes
    if streams:
        process_manager.adopt_existing_processes(streams)

    # Restore streams that were running before shutdown
    saved_ids = db.get_setting("running_streams_on_shutdown")
    if saved_ids:
        try:
            running_ids = json.loads(saved_ids)
            streams_by_id = {s["id"]: s for s in streams}
            for sid in running_ids:
                if sid in streams_by_id and sid not in process_manager._processes:
                    try:
                        process_manager.start_stream(streams_by_id[sid])
                        print(f"  Restored stream: {streams_by_id[sid]['name']} (ID {sid})")
                    except Exception as e:
                        print(f"  Failed to restore stream {sid}: {e}")
            db.set_setting("running_streams_on_shutdown", "[]")
            print(f"Restored {len(running_ids)} streams from previous session.")
        except Exception as e:
            print(f"Failed to restore streams: {e}")

    # Start background scheduler
    scheduler = SyncScheduler(app)
    scheduler.start()

    # Start SlimProto server (embedded Squeezelite server, replaces LMS)
    try:
        slimproto.start()
        print("SlimProto server started on port 3483")
    except Exception as e:
        print(f"SlimProto server start failed: {e}")

    # Start LMS compatibility layer (JSON-RPC on 9000, CLI on 9090 — for Jivelite)
    try:
        lms_compat.start()
        print("LMS compat server started (HTTP:9000, CLI:9090)")
    except Exception as e:
        print(f"LMS compat server start failed: {e}")

    # Start DLNA server if enabled
    if dlna_server.is_enabled():
        try:
            dlna_server.start()
        except Exception as e:
            print(f"DLNA server start failed: {e}")

    # Start library background daemon (auto scan + auto rescan tags + BPM/Key analysis)
    try:
        lib_module.start_daemon()
    except Exception as e:
        print(f"Library daemon start failed: {e}")

    # Start loudness normalization thread (parallel, independent)
    try:
        lib_module.start_loudness_daemon()
    except Exception as e:
        print(f"Loudness normalization thread start failed: {e}")

    # Start background ICY metadata poller for cast players
    cast.start_icy_poller()

    app.run(host=HOST, port=PORT, threaded=True)
