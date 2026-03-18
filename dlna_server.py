"""
Minimal embedded DLNA/UPnP Media Server for recorded audio files.

Serves MP3 files from the NAS sync target as a UPnP ContentDirectory.
Discovered automatically by Sonos, LMS, and other DLNA renderers.
"""

import os
import glob
import html
import socket
import struct
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import SMB_TARGET

# Defaults
DEFAULT_PORT = 9090
DEFAULT_NAME = "Streampeg Media Server"
UUID = "uuid:streampeg-dlna-media-server-001"
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


def is_enabled():
    from db import get_setting
    return get_setting("dlna_enabled") == "1"


def set_enabled(enabled):
    from db import set_setting
    set_setting("dlna_enabled", "1" if enabled else "0")


def get_port():
    from db import get_setting
    val = get_setting("dlna_port")
    try:
        return int(val)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def set_port(port):
    from db import set_setting
    set_setting("dlna_port", str(int(port)))


def get_friendly_name():
    from db import get_setting
    val = get_setting("dlna_name")
    return val if val else DEFAULT_NAME


def set_friendly_name(name):
    from db import set_setting
    set_setting("dlna_name", name.strip())


def get_media_path():
    """Get configured media path. Falls back to sync target."""
    from db import get_setting
    val = get_setting("dlna_media_path")
    if val:
        return val
    from sync import get_sync_target
    return get_sync_target()


def set_media_path(path):
    from db import set_setting
    set_setting("dlna_media_path", path.strip())


def _get_media_root():
    return get_media_path()


def _get_local_ip():
    """Get the local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# === Content scanning ===

def _scan_media():
    """Scan media root and return structured content."""
    root = _get_media_root()
    if not os.path.isdir(root):
        return {}
    content = {}
    for entry in os.scandir(root):
        if entry.is_dir():
            tracks = []
            for f in sorted(glob.glob(os.path.join(entry.path, "*.mp3"))):
                try:
                    stat = os.stat(f)
                    tracks.append({
                        "name": os.path.splitext(os.path.basename(f))[0],
                        "filename": os.path.basename(f),
                        "path": f,
                        "size": stat.st_size,
                        "subdir": entry.name,
                    })
                except OSError:
                    pass
            if tracks:
                content[entry.name] = tracks
    return content


# === SSDP responder ===

_ssdp_thread = None
_ssdp_stop = threading.Event()


def _ssdp_notify(ip):
    """Send SSDP NOTIFY alive messages."""
    location = f"http://{ip}:{_active_port}/description.xml"
    for st in ["upnp:rootdevice", "urn:schemas-upnp-org:device:MediaServer:1",
               "urn:schemas-upnp-org:service:ContentDirectory:1"]:
        msg = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {location}\r\n"
            "NTS: ssdp:alive\r\n"
            f"NT: {st}\r\n"
            "SERVER: Streampeg/1.0 UPnP/1.0\r\n"
            f"USN: {UUID}::{st}\r\n"
            "\r\n"
        ).encode()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            sock.close()
        except Exception:
            pass


def _ssdp_loop():
    """Listen for M-SEARCH and respond."""
    ip = _get_local_ip()
    location = f"http://{ip}:{_active_port}/description.xml"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass
    sock.bind(("", SSDP_PORT))
    group = socket.inet_aton(SSDP_ADDR)
    mreq = struct.pack("4sL", group, socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(2)

    # Initial notify
    _ssdp_notify(ip)
    last_notify = time.time()

    while not _ssdp_stop.is_set():
        try:
            data, addr = sock.recvfrom(1024)
            msg = data.decode(errors="ignore")
            if "M-SEARCH" in msg and ("ssdp:all" in msg or "MediaServer" in msg or "ContentDirectory" in msg):
                for st in ["upnp:rootdevice", "urn:schemas-upnp-org:device:MediaServer:1"]:
                    response = (
                        "HTTP/1.1 200 OK\r\n"
                        f"LOCATION: {location}\r\n"
                        "CACHE-CONTROL: max-age=1800\r\n"
                        "SERVER: Streampeg/1.0 UPnP/1.0\r\n"
                        f"ST: {st}\r\n"
                        f"USN: {UUID}::{st}\r\n"
                        "\r\n"
                    ).encode()
                    sock.sendto(response, addr)
        except socket.timeout:
            pass
        except Exception:
            pass

        # Periodic notify
        if time.time() - last_notify > 300:
            _ssdp_notify(ip)
            last_notify = time.time()

    sock.close()


# === HTTP server ===

class DLNAHandler(BaseHTTPRequestHandler):
    """Handle UPnP description, SOAP control, and media file requests."""

    def log_message(self, format, *args):
        pass  # Suppress default logging

    def do_GET(self):
        if self.path == "/description.xml":
            self._send_description()
        elif self.path == "/ContentDirectory.xml":
            self._send_content_directory_scpd()
        elif self.path.startswith("/media/"):
            self._send_media()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/control":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode(errors="ignore")
            self._handle_soap(body)
        else:
            self.send_error(404)

    def _send_description(self):
        ip = _get_local_ip()
        xml = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{_active_name}</friendlyName>
    <manufacturer>Streampeg</manufacturer>
    <modelName>Streampeg DLNA</modelName>
    <UDN>{UUID}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <controlURL>/control</controlURL>
        <eventSubURL/>
        <SCPDURL>/ContentDirectory.xml</SCPDURL>
      </service>
    </serviceList>
  </device>
</root>"""
        self._send_xml(xml)

    def _send_content_directory_scpd(self):
        xml = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>Browse</name>
      <argumentList>
        <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""
        self._send_xml(xml)

    def _handle_soap(self, body):
        """Handle SOAP Browse requests."""
        ip = _get_local_ip()
        base_url = f"http://{ip}:{_active_port}"

        # Extract ObjectID from SOAP body
        import re
        oid_match = re.search(r"<ObjectID>([^<]*)</ObjectID>", body)
        object_id = oid_match.group(1) if oid_match else "0"

        content = _scan_media()

        if object_id == "0":
            # Root: list stream folders
            items = []
            for i, subdir in enumerate(sorted(content.keys())):
                items.append(
                    f'<container id="{html.escape(subdir)}" parentID="0" childCount="{len(content[subdir])}" restricted="1">'
                    f'<dc:title>{html.escape(subdir)}</dc:title>'
                    f'<upnp:class>object.container.storageFolder</upnp:class>'
                    f'</container>'
                )
            result_xml = (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                + "".join(items) + '</DIDL-Lite>'
            )
            self._send_browse_response(result_xml, len(items), len(items))
        elif object_id in content:
            # Folder: list tracks
            tracks = content[object_id]
            items = []
            for t in tracks:
                encoded = urllib.parse.quote(t["filename"])
                url = f"{base_url}/media/{urllib.parse.quote(t['subdir'])}/{encoded}"
                # Parse artist - title
                name = t["name"]
                artist = ""
                title = name
                for sep in (" - ", " – ", " — "):
                    if sep in name:
                        parts = name.split(sep, 1)
                        artist = parts[0].strip()
                        title = parts[1].strip()
                        break
                items.append(
                    f'<item id="{html.escape(object_id)}/{html.escape(t["filename"])}" parentID="{html.escape(object_id)}" restricted="1">'
                    f'<dc:title>{html.escape(title)}</dc:title>'
                    + (f'<dc:creator>{html.escape(artist)}</dc:creator>' if artist else '')
                    + (f'<upnp:artist>{html.escape(artist)}</upnp:artist>' if artist else '')
                    + f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
                    f'<res protocolInfo="http-get:*:audio/mpeg:*" size="{t["size"]}">{html.escape(url)}</res>'
                    f'</item>'
                )
            result_xml = (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                + "".join(items) + '</DIDL-Lite>'
            )
            self._send_browse_response(result_xml, len(items), len(items))
        else:
            self._send_browse_response("", 0, 0)

    def _send_browse_response(self, result_xml, returned, total):
        escaped = html.escape(result_xml)
        soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <Result>{escaped}</Result>
      <NumberReturned>{returned}</NumberReturned>
      <TotalMatches>{total}</TotalMatches>
      <UpdateID>1</UpdateID>
    </u:BrowseResponse>
  </s:Body>
</s:Envelope>"""
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        data = soap.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_media(self):
        """Serve an actual MP3 file."""
        # Path: /media/<subdir>/<filename>
        parts = self.path[7:]  # strip /media/
        parts = urllib.parse.unquote(parts)
        root = _get_media_root()
        filepath = os.path.normpath(os.path.join(root, parts))

        # Security: ensure path is within media root
        if not filepath.startswith(os.path.normpath(root)):
            self.send_error(403)
            return

        if not os.path.isfile(filepath):
            self.send_error(404)
            return

        size = os.path.getsize(filepath)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break

    def _send_xml(self, xml):
        data = xml.encode()
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# === Server lifecycle ===

_http_server = None
_http_thread = None


_active_port = DEFAULT_PORT
_active_name = DEFAULT_NAME


def start():
    """Start the DLNA server (HTTP + SSDP)."""
    global _http_server, _http_thread, _ssdp_thread, _active_port, _active_name

    if _http_server:
        return  # Already running

    _active_port = get_port()
    _active_name = get_friendly_name()
    _ssdp_stop.clear()

    # Start HTTP server (allow reuse of port after restart)
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True
    try:
        _http_server = ReusableHTTPServer(("0.0.0.0", _active_port), DLNAHandler)
        _http_thread = threading.Thread(target=_http_server.serve_forever, daemon=True)
        _http_thread.start()
    except OSError as e:
        _http_server = None
        print(f"DLNA server failed to start: {e}")
        return

    # Start SSDP
    _ssdp_thread = threading.Thread(target=_ssdp_loop, daemon=True)
    _ssdp_thread.start()


def stop():
    """Stop the DLNA server."""
    global _http_server, _http_thread, _ssdp_thread

    _ssdp_stop.set()
    if _http_server:
        _http_server.shutdown()
        _http_server = None
    _http_thread = None
    _ssdp_thread = None


def get_status():
    """Return server status dict."""
    return {
        "running": _http_server is not None,
        "port": _active_port if _http_server else get_port(),
        "name": _active_name if _http_server else get_friendly_name(),
        "media_path": get_media_path(),
        "ip": _get_local_ip() if _http_server else None,
    }
