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
DEFAULT_PORT = 9091
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
    for st in ["upnp:rootdevice", UUID,
               "urn:schemas-upnp-org:device:MediaServer:1",
               "urn:schemas-upnp-org:service:ContentDirectory:1",
               "urn:schemas-upnp-org:service:ConnectionManager:1"]:
        usn = UUID if st == UUID else f"{UUID}::{st}"
        msg = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {location}\r\n"
            f"NT: {st}\r\n"
            "NTS: ssdp:alive\r\n"
            f"SERVER: Linux UPnP/1.0 DLNADOC/1.50 Streampeg/1.0\r\n"
            f"USN: {usn}\r\n"
            "\r\n"
        ).encode()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            sock.close()
        except Exception:
            pass


def _ssdp_loop():
    """Listen for M-SEARCH and respond."""
    import sys
    ip = _get_local_ip()
    location = f"http://{ip}:{_active_port}/description.xml"

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        # Bind to multicast address to receive only multicast traffic
        sock.bind((SSDP_ADDR, SSDP_PORT))
        # Join multicast group on the specific local interface
        local_ip = socket.inet_aton(ip)
        group = socket.inet_aton(SSDP_ADDR)
        mreq = struct.pack("4s4s", group, local_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        # Set outgoing multicast interface
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, local_ip)
        sock.settimeout(2)
        print(f"[DLNA] SSDP listening on {SSDP_ADDR}:{SSDP_PORT}, interface={ip}, location={location}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[DLNA] SSDP socket setup FAILED: {e}", file=sys.stderr, flush=True)
        return

    # Initial notify
    _ssdp_notify(ip)
    last_notify = time.time()

    while not _ssdp_stop.is_set():
        try:
            data, addr = sock.recvfrom(4096)
            msg = data.decode(errors="ignore")
            if "M-SEARCH" in msg:
                # Extract requested ST (must start at beginning of line)
                import re as _re
                st_match = _re.search(r"(?m)^ST:\s*(.+?)\s*$", msg)
                req_st = st_match.group(1).strip() if st_match else ""

                # Determine which STs to respond with
                respond_sts = []
                if req_st == "ssdp:all" or req_st == "upnp:rootdevice":
                    respond_sts = ["upnp:rootdevice",
                                   "urn:schemas-upnp-org:device:MediaServer:1",
                                   "urn:schemas-upnp-org:service:ContentDirectory:1",
                                   "urn:schemas-upnp-org:service:ConnectionManager:1"]
                elif "MediaServer" in req_st:
                    respond_sts = ["urn:schemas-upnp-org:device:MediaServer:1"]
                elif "ContentDirectory" in req_st:
                    respond_sts = ["urn:schemas-upnp-org:service:ContentDirectory:1"]
                elif "ConnectionManager" in req_st:
                    respond_sts = ["urn:schemas-upnp-org:service:ConnectionManager:1"]

                from email.utils import formatdate
                date_str = formatdate(usegmt=True)
                for st in respond_sts:
                    usn = UUID if st == UUID else f"{UUID}::{st}"
                    response = (
                        "HTTP/1.1 200 OK\r\n"
                        f"CACHE-CONTROL: max-age=1800\r\n"
                        f"DATE: {date_str}\r\n"
                        "EXT:\r\n"
                        f"LOCATION: {location}\r\n"
                        f"SERVER: Linux UPnP/1.0 DLNADOC/1.50 Streampeg/1.0\r\n"
                        f"ST: {st}\r\n"
                        f"USN: {usn}\r\n"
                        "\r\n"
                    ).encode()
                    # Reply via unicast from local IP
                    try:
                        reply_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        reply_sock.sendto(response, addr)
                        reply_sock.close()
                    except Exception:
                        sock.sendto(response, addr)
        except socket.timeout:
            pass
        except Exception:
            pass

        # Periodic notify
        if time.time() - last_notify > 60:
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
        elif self.path == "/ConnectionManager.xml":
            self._send_connection_manager_scpd()
        elif self.path.startswith("/media/"):
            self._send_media()
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode(errors="ignore")
        if self.path == "/control":
            self._handle_soap(body)
        elif self.path == "/cm-control":
            self._handle_cm_soap(body)
        else:
            self.send_error(404)

    def _send_description(self):
        ip = _get_local_ip()
        xml = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>
    <friendlyName>{html.escape(_active_name)}</friendlyName>
    <manufacturer>Streampeg</manufacturer>
    <manufacturerURL>http://{ip}:{get_port()}</manufacturerURL>
    <modelDescription>Streampeg DLNA Media Server</modelDescription>
    <modelName>Streampeg</modelName>
    <modelNumber>1.0</modelNumber>
    <UDN>{UUID}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <controlURL>/control</controlURL>
        <eventSubURL/>
        <SCPDURL>/ContentDirectory.xml</SCPDURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <controlURL>/cm-control</controlURL>
        <eventSubURL/>
        <SCPDURL>/ConnectionManager.xml</SCPDURL>
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
    <action>
      <name>GetSystemUpdateID</name>
      <argumentList>
        <argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetSearchCapabilities</name>
      <argumentList>
        <argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetSortCapabilities</name>
      <argumentList>
        <argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument>
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
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""
        self._send_xml(xml)

    def _send_connection_manager_scpd(self):
        xml = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>GetProtocolInfo</name>
      <argumentList>
        <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
        <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""
        self._send_xml(xml)

    def _handle_cm_soap(self, body):
        """Handle ConnectionManager SOAP requests (GetProtocolInfo)."""
        soap = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetProtocolInfoResponse xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">
      <Source>http-get:*:audio/mpeg:*,http-get:*:audio/mp3:*</Source>
      <Sink></Sink>
    </u:GetProtocolInfoResponse>
  </s:Body>
</s:Envelope>"""
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        data = soap.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_soap(self, body):
        """Handle SOAP Browse and GetSystemUpdateID requests."""
        import re

        # Detect action from SOAPAction header or body
        if "GetSystemUpdateID" in body:
            self._send_system_update_id()
            return
        if "GetSortCapabilities" in body:
            self._send_sort_capabilities()
            return
        if "GetSearchCapabilities" in body:
            self._send_search_capabilities()
            return

        ip = _get_local_ip()
        base_url = f"http://{ip}:{_active_port}"

        # Extract ObjectID, StartingIndex, RequestedCount from SOAP body
        oid_match = re.search(r"<ObjectID>([^<]*)</ObjectID>", body)
        object_id = oid_match.group(1) if oid_match else "0"

        si_match = re.search(r"<StartingIndex>(\d+)</StartingIndex>", body)
        starting_index = int(si_match.group(1)) if si_match else 0

        rc_match = re.search(r"<RequestedCount>(\d+)</RequestedCount>", body)
        requested_count = int(rc_match.group(1)) if rc_match else 0

        content = _scan_media()

        if object_id == "0":
            # Root: list stream folders
            all_items = []
            for i, subdir in enumerate(sorted(content.keys())):
                all_items.append(
                    f'<container id="{html.escape(subdir)}" parentID="0" childCount="{len(content[subdir])}" restricted="1">'
                    f'<dc:title>{html.escape(subdir)}</dc:title>'
                    f'<upnp:class>object.container.storageFolder</upnp:class>'
                    f'</container>'
                )
            total = len(all_items)
            # Apply paging
            if requested_count > 0:
                page = all_items[starting_index:starting_index + requested_count]
            else:
                page = all_items[starting_index:]
            result_xml = (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                + "".join(page) + '</DIDL-Lite>'
            )
            self._send_browse_response(result_xml, len(page), total)
        elif object_id in content:
            # Folder: list tracks
            tracks = content[object_id]
            all_items = []
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
                all_items.append(
                    f'<item id="{html.escape(object_id)}/{html.escape(t["filename"])}" parentID="{html.escape(object_id)}" restricted="1">'
                    f'<dc:title>{html.escape(title)}</dc:title>'
                    + (f'<dc:creator>{html.escape(artist)}</dc:creator>' if artist else '')
                    + (f'<upnp:artist>{html.escape(artist)}</upnp:artist>' if artist else '')
                    + f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
                    f'<res protocolInfo="http-get:*:audio/mpeg:*" size="{t["size"]}">{html.escape(url)}</res>'
                    f'</item>'
                )
            total = len(all_items)
            # Apply paging
            if requested_count > 0:
                page = all_items[starting_index:starting_index + requested_count]
            else:
                page = all_items[starting_index:]
            result_xml = (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                + "".join(page) + '</DIDL-Lite>'
            )
            self._send_browse_response(result_xml, len(page), total)
        else:
            self._send_browse_response("", 0, 0)

    def _send_system_update_id(self):
        """Respond to GetSystemUpdateID."""
        soap = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetSystemUpdateIDResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <Id>1</Id>
    </u:GetSystemUpdateIDResponse>
  </s:Body>
</s:Envelope>"""
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        data = soap.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sort_capabilities(self):
        """Respond to GetSortCapabilities."""
        soap = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetSortCapabilitiesResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <SortCaps>dc:title</SortCaps>
    </u:GetSortCapabilitiesResponse>
  </s:Body>
</s:Envelope>"""
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        data = soap.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_search_capabilities(self):
        """Respond to GetSearchCapabilities."""
        soap = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetSearchCapabilitiesResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <SearchCaps></SearchCaps>
    </u:GetSearchCapabilitiesResponse>
  </s:Body>
</s:Envelope>"""
        self.send_response(200)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        data = soap.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
        """Serve an actual MP3 file with Range request support."""
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
        range_header = self.headers.get("Range")

        if range_header:
            # Parse Range: bytes=start-end
            import re
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(filepath, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        remaining -= len(chunk)
                return

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
