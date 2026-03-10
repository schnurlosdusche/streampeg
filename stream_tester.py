"""
Stream-Tester: Prüft einen Stream und empfiehlt den besten Aufnahme-Modus.

Tests (in Reihenfolge):
  1. HTTP-Verbindung (Redirects, HTTPS, Content-Type)
  2. ICY-Metadaten (deep probe - bis zu 30s warten, Ads überspringen)
  3. Shoutcast API (/currentsong, /7.html)
  4. Icecast JSON API (/status-json.xsl)
  5. TuneIn Now-Playing API
  6. Streamripper-Kompatibilität
  7. FFmpeg-Kompatibilität

Ergebnisse werden in metadata_methods DB-Tabelle gespeichert zum Lernen.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.parse

from config import DB_PATH


def _get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _save_method(stream_host, stream_path, method, method_url="", has_titles=False, sample_title="", notes=""):
    """Save a metadata discovery result to the DB for learning."""
    try:
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO metadata_methods
            (stream_host, stream_path, method, method_url, has_titles, sample_title, tested_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
        """, (stream_host, stream_path, method, method_url, int(has_titles), sample_title[:200], notes[:200]))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_known_methods(stream_host):
    """Get previously discovered metadata methods for this host."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM metadata_methods WHERE stream_host = ? AND has_titles = 1 ORDER BY tested_at DESC",
            (stream_host,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def test_stream(url, ua="VLC/3.0.21 LibVLC/3.0.21", timeout=15):
    """Run all tests on a stream URL and return results + recommendation."""
    parsed = urllib.parse.urlparse(url)
    stream_host = parsed.hostname or ""
    stream_path = parsed.path or ""

    results = {
        "url": url,
        "host": stream_host,
        "tests": {},
        "recommendation": None,
    }

    # Check if we already know a working method for this host
    known = _get_known_methods(stream_host)
    if known:
        results["known_methods"] = known

    # --- Test 1: HTTP connection ---
    print(f"\n[1/7] HTTP-Verbindung testen...")
    http = _test_http(url, ua, timeout)
    results["tests"]["http"] = http
    if not http["ok"]:
        results["recommendation"] = "FEHLER: Stream nicht erreichbar"
        _print_results(results)
        return results

    effective_url = http.get("effective_url", url)
    eff_parsed = urllib.parse.urlparse(effective_url)
    eff_host = eff_parsed.hostname or stream_host

    # --- Test 2: ICY metadata (deep probe) ---
    print(f"[2/7] ICY-Metadaten prüfen (Deep Probe, bis 30s)...")
    icy = _test_icy_deep(effective_url, ua)
    results["tests"]["icy"] = icy
    _save_method(stream_host, stream_path, "icy", effective_url,
                 has_titles=bool(icy.get("title")),
                 sample_title=icy.get("title", ""),
                 notes=f"metaint={icy.get('metaint', 0)}, blocks={icy.get('blocks_read', 0)}")

    # --- Test 3: Shoutcast API ---
    print(f"[3/7] Shoutcast API testen...")
    api = _test_shoutcast_api(effective_url, ua)
    results["tests"]["api"] = api
    if api.get("api_url"):
        _save_method(stream_host, stream_path, "shoutcast_api", api["api_url"],
                     has_titles=api.get("ok", False),
                     sample_title=api.get("title", ""))

    # --- Test 4: Icecast JSON API ---
    print(f"[4/7] Icecast Status-API testen...")
    icecast = _test_icecast_api(effective_url, stream_path, ua)
    results["tests"]["icecast"] = icecast
    if icecast.get("api_url"):
        _save_method(stream_host, stream_path, "icecast_json", icecast["api_url"],
                     has_titles=icecast.get("ok", False),
                     sample_title=icecast.get("title", ""))

    # --- Test 5: TuneIn ---
    print(f"[5/7] TuneIn Now-Playing testen...")
    tunein = _test_tunein(url, http.get("icy_name", ""))
    results["tests"]["tunein"] = tunein
    if tunein.get("station_id"):
        _save_method(stream_host, stream_path, "tunein", tunein.get("station_id", ""),
                     has_titles=tunein.get("ok", False),
                     sample_title=tunein.get("title", ""))

    # --- Test 6: Streamripper ---
    print(f"[6/7] Streamripper testen (10s)...")
    sr = _test_streamripper(url, ua)
    results["tests"]["streamripper"] = sr

    # --- Test 7: FFmpeg ---
    print(f"[7/7] FFmpeg testen (10s)...")
    ffmpeg = _test_ffmpeg(effective_url, ua)
    results["tests"]["ffmpeg"] = ffmpeg

    # --- Recommendation ---
    results["recommendation"] = _recommend(results)
    _print_results(results)
    return results


def _test_http(url, ua, timeout):
    """Test basic HTTP connectivity, redirects, content type."""
    result = {"ok": False}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Icy-MetaData": "1",
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        result["status"] = resp.status
        result["content_type"] = resp.headers.get("Content-Type", "")
        result["effective_url"] = resp.url
        result["redirected"] = resp.url != url
        result["https"] = resp.url.startswith("https://")
        result["server"] = resp.headers.get("Server", "")

        icy_headers = {}
        for key in resp.headers:
            if key.lower().startswith("icy-"):
                icy_headers[key.lower()] = resp.headers[key]
        result["icy_headers"] = icy_headers
        result["icy_name"] = resp.headers.get("icy-name", "")

        resp.close()
        result["ok"] = "audio" in result["content_type"] or bool(icy_headers)
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def _test_icy_deep(url, ua, max_seconds=30):
    """Deep ICY probe: read stream for up to max_seconds, skip ads, find real titles."""
    result = {"ok": False, "has_metaint": False, "title": None, "blocks_read": 0}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Icy-MetaData": "1",
        })
        resp = urllib.request.urlopen(req, timeout=15)

        metaint_str = resp.headers.get("icy-metaint")
        if not metaint_str:
            result["error"] = "Kein icy-metaint Header"
            resp.close()
            return result

        metaint = int(metaint_str)
        result["has_metaint"] = True
        result["metaint"] = metaint

        titles_found = []
        start = time.time()
        blocks = 0

        while time.time() - start < max_seconds:
            audio = resp.read(metaint)
            if not audio:
                break
            meta_len_byte = resp.read(1)
            if not meta_len_byte:
                break
            length = meta_len_byte[0] * 16
            blocks += 1
            if length > 0:
                meta_raw = resp.read(length)
                if not meta_raw:
                    break
                meta = meta_raw.decode("utf-8", errors="replace").rstrip("\x00")
                m = re.search(r"StreamTitle='([^']*)'", meta)
                if m:
                    title = m.group(1).strip()
                    # Skip empty titles and ad markers
                    if title and "adw_ad" not in meta:
                        titles_found.append(title)
                        # Found a real title, we can stop
                        if " - " in title:
                            break

        resp.close()
        result["blocks_read"] = blocks
        result["seconds"] = round(time.time() - start, 1)

        if titles_found:
            result["ok"] = True
            result["title"] = titles_found[-1]
            result["titles_seen"] = len(set(titles_found))
            result["has_separator"] = " - " in titles_found[-1]
        else:
            result["ok"] = False
            result["title"] = None
            result["note"] = f"Kein StreamTitle in {blocks} Blöcken ({result['seconds']}s)"

    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def _test_shoutcast_api(url, ua):
    """Test Shoutcast v2 /currentsong and v1 /7.html APIs."""
    result = {"ok": False}
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base += f":{parsed.port}"

    # Try v2 API: /currentsong
    for sid in (1, 2):
        api_url = f"{base}/currentsong?sid={sid}"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": ua})
            resp = urllib.request.urlopen(req, timeout=5)
            title = resp.read().decode("utf-8", errors="replace").strip()
            resp.close()
            if title and len(title) > 2 and "<" not in title:
                result["ok"] = True
                result["api_url"] = api_url
                result["title"] = title
                result["has_separator"] = " - " in title
                result["api_type"] = "shoutcast_v2"
                return result
        except Exception:
            pass

    # Try v1 API: /7.html
    api_url = f"{base}/7.html"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": ua})
        resp = urllib.request.urlopen(req, timeout=5)
        html = resp.read().decode("utf-8", errors="replace").strip()
        resp.close()
        # Format: <HTML><meta http-equiv...>listeners,status,peak,...,bitrate,title</HTML>
        m = re.search(r"<body>(.+?)</body>", html, re.IGNORECASE)
        if m:
            parts = m.group(1).split(",")
            if len(parts) >= 7:
                title = ",".join(parts[6:]).strip()
                if title and len(title) > 2:
                    result["ok"] = True
                    result["api_url"] = api_url
                    result["title"] = title
                    result["has_separator"] = " - " in title
                    result["api_type"] = "shoutcast_v1"
                    return result
    except Exception:
        pass

    result["error"] = "Kein Shoutcast API verfügbar"
    return result


def _test_icecast_api(effective_url, stream_path, ua):
    """Test Icecast JSON status API."""
    result = {"ok": False}
    parsed = urllib.parse.urlparse(effective_url)

    # Try on both original and effective hosts
    hosts = set()
    hosts.add(f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else ""))

    for base in hosts:
        api_url = f"{base}/status-json.xsl"
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": ua})
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            resp.close()

            sources = data.get("icestats", {}).get("source", [])
            if isinstance(sources, dict):
                sources = [sources]

            result["api_url"] = api_url
            result["sources_count"] = len(sources)

            # Find our mountpoint
            mount = parsed.path.rstrip("/").split("/")[-1] if parsed.path else ""
            for src in sources:
                listen_url = src.get("listenurl", "")
                server_name = src.get("server_name", "")
                title = src.get("title", src.get("yp_currently_playing", ""))

                # Match by mountpoint name
                if mount and mount in listen_url or mount in server_name:
                    if title and len(title) > 2:
                        result["ok"] = True
                        result["title"] = title
                        result["has_separator"] = " - " in title
                        result["mountpoint"] = server_name
                        return result
                    else:
                        result["mountpoint"] = server_name
                        result["note"] = "Mountpoint gefunden, aber kein Titel"

            # Check any source with a title
            for src in sources:
                title = src.get("title", src.get("yp_currently_playing", ""))
                if title and " - " in title:
                    result["note"] = f"Andere Quelle hat Titel: {title[:60]}"
                    break

        except Exception:
            pass

    if not result.get("api_url"):
        result["error"] = "Kein Icecast API verfügbar"
    return result


def _test_tunein(stream_url, icy_name):
    """Search TuneIn for the station and check if it has now-playing info."""
    result = {"ok": False}

    # Build search query from ICY name or URL
    query = icy_name or urllib.parse.urlparse(stream_url).hostname or ""
    query = re.sub(r'[_\-]+', ' ', query).strip()
    if not query or len(query) < 3:
        result["error"] = "Kein Suchbegriff für TuneIn"
        return result

    try:
        search_url = f"https://opml.radiotime.com/Search.ashx?query={urllib.parse.quote(query)}&formats=mp3&render=json"
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        resp.close()

        stations = [s for s in data.get("body", []) if s.get("type") == "audio"]
        if not stations:
            result["error"] = "Nicht auf TuneIn gefunden"
            return result

        # Take the first match
        station = stations[0]
        station_id = station.get("guide_id", "")
        result["station_id"] = station_id
        result["station_name"] = station.get("text", "")

        # Check Describe endpoint for current song
        if station_id:
            describe_url = f"http://opml.radiotime.com/Describe.ashx?id={station_id}&render=json"
            req = urllib.request.Request(describe_url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=5)
            desc = json.loads(resp.read().decode("utf-8", errors="replace"))
            resp.close()

            body = desc.get("body", [{}])
            if body:
                info = body[0] if isinstance(body, list) else body
                song = info.get("current_song")
                artist = info.get("current_artist")
                has_song = info.get("has_song", False)

                if song and artist:
                    result["ok"] = True
                    result["title"] = f"{artist} - {song}"
                    result["has_separator"] = True
                elif song:
                    result["ok"] = True
                    result["title"] = song
                    result["has_separator"] = " - " in song
                elif has_song:
                    result["note"] = "TuneIn meldet has_song=true, aber aktuell kein Titel"
                else:
                    result["note"] = "Station gefunden, aber keine Titelinformationen"

    except Exception as e:
        result["error"] = str(e)[:100]
    return result


def _test_streamripper(url, ua):
    """Test if streamripper can connect and rip (10 second test)."""
    result = {"ok": False}
    try:
        sr_bin = "streamripper"
        proc = subprocess.run(
            [sr_bin, url, "-d", "/tmp/sr_test", "-l", "10", "-u", ua, "--quiet"],
            capture_output=True, text=True, timeout=20,
        )
        result["returncode"] = proc.returncode
        result["ok"] = proc.returncode == 0

        import glob
        files = glob.glob("/tmp/sr_test/**/*.mp3", recursive=True)
        result["files_created"] = len(files)

        subprocess.run(["rm", "-rf", "/tmp/sr_test"], capture_output=True)

        if proc.returncode != 0:
            result["error"] = proc.stderr.strip()[-200:]
    except FileNotFoundError:
        result["error"] = "streamripper nicht installiert"
    except subprocess.TimeoutExpired:
        subprocess.run(["pkill", "-f", "streamripper.*sr_test"], capture_output=True)
        subprocess.run(["rm", "-rf", "/tmp/sr_test"], capture_output=True)
        result["error"] = "Timeout (20s)"
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def _test_ffmpeg(url, ua):
    """Test if ffmpeg can record the stream (10 second test)."""
    result = {"ok": False}
    outfile = "/tmp/ffmpeg_test.mp3"
    try:
        os.remove(outfile)
    except OSError:
        pass

    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-user_agent", ua,
             "-i", url,
             "-t", "10", "-c:a", "libmp3lame", "-q:a", "2", outfile],
            capture_output=True, text=True, timeout=20,
        )
        result["returncode"] = proc.returncode

        if os.path.exists(outfile):
            size = os.path.getsize(outfile)
            result["file_size"] = size
            result["ok"] = size > 10000
            os.remove(outfile)
        else:
            result["ok"] = False

        if proc.returncode != 0 and not result["ok"]:
            result["error"] = proc.stderr.strip()[-200:]
    except FileNotFoundError:
        result["error"] = "ffmpeg nicht installiert"
    except subprocess.TimeoutExpired:
        subprocess.run(["pkill", "-f", "ffmpeg.*ffmpeg_test"], capture_output=True)
        result["error"] = "Timeout (20s)"
        try:
            os.remove(outfile)
        except OSError:
            pass
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def _recommend(results):
    """Determine the best recording mode based on test results."""
    t = results["tests"]
    http = t["http"]
    icy = t["icy"]
    api = t["api"]
    icecast = t.get("icecast", {})
    tunein = t.get("tunein", {})
    sr = t["streamripper"]
    ffmpeg = t["ffmpeg"]

    has_icy_titles = icy.get("ok") and icy.get("title") and icy.get("has_separator")
    has_api_titles = api.get("ok") and api.get("has_separator")
    has_icecast_titles = icecast.get("ok") and icecast.get("has_separator")
    has_tunein_titles = tunein.get("ok") and tunein.get("has_separator")
    has_any_titles = has_icy_titles or has_api_titles or has_icecast_titles or has_tunein_titles
    sr_works = sr.get("ok", False)
    ffmpeg_works = ffmpeg.get("ok", False)
    is_https = http.get("https", False)
    was_redirected = http.get("redirected", False)

    # No titles at all = unusable
    if not has_any_titles:
        # Check known methods from DB
        known = results.get("known_methods", [])
        if known:
            methods_str = ", ".join(m["method"] for m in known[:3])
            return f"NICHT GEEIGNET: Aktuell keine Titel verfügbar. Früher funktionierte: {methods_str}. Bitte später erneut testen."
        return "NICHT GEEIGNET: Keine Song-Titel verfügbar (ICY, Shoutcast, Icecast, TuneIn alle geprüft). Einzelne Songs können nicht extrahiert werden."

    reasons = []

    # Priority 1: Streamripper (simplest, native splitting)
    if sr_works and (has_icy_titles or has_api_titles):
        score = 85
        if is_https or was_redirected:
            score -= 30  # often fails with HTTPS
        if score > 50:
            reasons.append({
                "mode": "streamripper",
                "score": score,
                "reason": "Streamripper funktioniert mit Titel-Metadaten",
            })

    # Priority 2: FFmpeg + ICY
    if ffmpeg_works and has_icy_titles:
        reasons.append({
            "mode": "ffmpeg_icy",
            "score": 82,
            "reason": "FFmpeg + ICY-Metadaten: zuverlässig, auch bei HTTPS",
        })

    # Priority 3: FFmpeg + API (Shoutcast or Icecast)
    if ffmpeg_works and (has_api_titles or has_icecast_titles):
        meta_src = "Shoutcast" if has_api_titles else "Icecast"
        reasons.append({
            "mode": "ffmpeg_api",
            "score": 78,
            "reason": f"FFmpeg + {meta_src} API: schneidet Stream anhand API-Titel",
        })

    # Priority 4: YouTube (last resort, but highest quality per song)
    if has_icy_titles or has_tunein_titles:
        title_src = "ICY" if has_icy_titles else "TuneIn"
        reasons.append({
            "mode": "youtube",
            "score": 75,
            "reason": f"YouTube-Download via {title_src}-Titel: beste Einzelsong-Qualität, aber abhängig von YouTube-Verfügbarkeit",
        })

    if not reasons:
        return "NICHT GEEIGNET: Titel vorhanden, aber kein Aufnahme-Modus funktioniert technisch."

    reasons.sort(key=lambda r: r["score"], reverse=True)
    return reasons


def _print_results(results):
    """Pretty-print test results."""
    t = results["tests"]

    print("\n" + "=" * 60)
    print(f"Stream: {results['url']}")
    print("=" * 60)

    http = t.get("http", {})
    status = "OK" if http.get("ok") else "FEHLER"
    print(f"\n  HTTP:          {status}")
    if http.get("redirected"):
        print(f"                 -> Redirect zu {http['effective_url'][:80]}")
    if http.get("https"):
        print(f"                 -> HTTPS")
    if http.get("server"):
        print(f"                 -> Server: {http['server']}")

    icy = t.get("icy", {})
    if icy.get("title"):
        print(f"  ICY-Metadata:  OK ({icy['seconds']}s, Title: {icy['title'][:50]})")
    elif icy.get("has_metaint"):
        print(f"  ICY-Metadata:  KEIN TITEL ({icy.get('blocks_read', 0)} Blöcke in {icy.get('seconds', 0)}s)")
    else:
        print(f"  ICY-Metadata:  FEHLER ({icy.get('error', 'unbekannt')})")

    api = t.get("api", {})
    if api.get("ok"):
        print(f"  Shoutcast API: OK ({api.get('api_type', '')}: {api.get('title', '?')[:50]})")
    else:
        print(f"  Shoutcast API: nicht verfügbar")

    icecast = t.get("icecast", {})
    if icecast.get("ok"):
        print(f"  Icecast API:   OK (Title: {icecast.get('title', '?')[:50]})")
    elif icecast.get("mountpoint"):
        print(f"  Icecast API:   Mountpoint '{icecast['mountpoint']}' gefunden, aber kein Titel")
    else:
        print(f"  Icecast API:   nicht verfügbar")

    tunein = t.get("tunein", {})
    if tunein.get("ok"):
        print(f"  TuneIn:        OK ({tunein.get('station_name', '?')}: {tunein.get('title', '?')[:50]})")
    elif tunein.get("station_id"):
        print(f"  TuneIn:        Station gefunden ({tunein.get('station_name', '')}), aber {tunein.get('note', 'kein Titel')}")
    else:
        print(f"  TuneIn:        {tunein.get('error', 'nicht gefunden')}")

    sr = t.get("streamripper", {})
    if sr.get("ok"):
        print(f"  Streamripper:  OK ({sr.get('files_created', 0)} Dateien)")
    else:
        print(f"  Streamripper:  FEHLER ({sr.get('error', 'unbekannt')[:60]})")

    ffmpeg = t.get("ffmpeg", {})
    if ffmpeg.get("ok"):
        size_kb = round(ffmpeg.get("file_size", 0) / 1024)
        print(f"  FFmpeg:        OK ({size_kb} KB in 10s)")
    else:
        print(f"  FFmpeg:        FEHLER ({ffmpeg.get('error', 'unbekannt')[:60]})")

    # Known methods from DB
    known = results.get("known_methods", [])
    if known:
        print(f"\n  DB-Wissen:     {len(known)} bekannte Methode(n) für {results['host']}")
        for m in known[:3]:
            print(f"                 -> {m['method']}: {m['sample_title'][:50]}")

    print("\n" + "-" * 60)
    rec = results["recommendation"]
    if isinstance(rec, str):
        print(f"  Empfehlung: {rec}")
    elif isinstance(rec, list):
        print("  Empfehlung (nach Eignung sortiert):\n")
        for i, r in enumerate(rec):
            marker = " *" if i == 0 else "  "
            print(f"  {marker} {r['mode']:15s} (Score {r['score']:3d}) - {r['reason']}")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <stream-url>")
        sys.exit(1)
    test_stream(sys.argv[1])
