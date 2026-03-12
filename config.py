import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDING_BASE = "/recording"
SMB_TARGET = "/mnt/unraid-streams"
DB_PATH = os.environ.get("SR_UI_DB_PATH", os.path.join(BASE_DIR, "streamripper-ui.db"))
STREAMRIPPER_BIN = "streamripper"
SYNC_INTERVAL = 300  # seconds

# User-Agent presets for streamripper (key -> user-agent string)
USER_AGENTS = {
    "lyrion": "Lyrion Music Server (9.1.1 - 1771596440)",
    "vlc": "VLC/3.0.21 LibVLC/3.0.21",
    "chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "firefox": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "winamp": "WinampMPEG/5.666",
    "foobar": "foobar2000/2.1.4",
    "itunes": "iTunes/12.13 (Windows; Microsoft Windows 10 x64 Professional Edition)",
}
DEFAULT_USER_AGENT = "lyrion"
METADATA_POLL_INTERVAL = 3  # seconds between Shoutcast API polls
DEFAULT_MIN_SIZE_MB = 2
MIN_BITRATE = 128  # Minimum stream bitrate in kbps — refuse to record/download below this
HOST = "0.0.0.0"
PORT = 5000
SECRET_KEY = os.environ.get("SR_UI_SECRET", "sr-ui-secret-key-change-me")
AUTH_PASSWORD = os.environ.get("SR_UI_PASSWORD", "streaming")
