import threading
import time
import cleanup
import sync
import process_manager
from db import get_all_streams
from config import SYNC_INTERVAL


class SyncScheduler:
    def __init__(self, app):
        self.app = app
        self.interval = SYNC_INTERVAL
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                break
            self._tick()

    def _tick(self):
        try:
            with self.app.app_context():
                streams = get_all_streams()
                for stream in streams:
                    if not stream["enabled"]:
                        continue

                    # Health check: restart crashed streams
                    process_manager.check_and_restart(stream)

                    # Cleanup
                    import os
                    from config import RECORDING_BASE, SMB_TARGET
                    dest = os.path.join(RECORDING_BASE, stream["dest_subdir"])
                    if os.path.isdir(dest):
                        cleanup.run_all(dest, stream["min_size_mb"])

                    # Sync
                    sync.sync_stream(stream)

                    # YouTube DB cleanup: remove entries for deleted files
                    record_mode = stream["record_mode"] if "record_mode" in stream.keys() else "streamripper"
                    if record_mode == "youtube":
                        try:
                            from youtube_recorder import YouTubeSongDB, YT_DATA_DIR
                            db_name = f"yt_songs_{stream['dest_subdir']}.db"
                            db_path = os.path.join(YT_DATA_DIR, db_name)
                            if os.path.exists(db_path):
                                song_db = YouTubeSongDB(db_path)
                                nas_dest = os.path.join(SMB_TARGET, stream["dest_subdir"])
                                removed = song_db.cleanup_missing(dest, nas_dest)
                                if removed > 0:
                                    from db import log_event
                                    log_event(stream["id"], "cleanup",
                                              f"DB bereinigt: {removed} fehlende Dateien entfernt")
                        except Exception:
                            pass
        except Exception as e:
            print(f"Scheduler error: {e}")
