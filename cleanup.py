import os
import glob
import re


def cleanup_small_files(dest_dir, min_size_mb=2):
    """Delete audio files smaller than min_size_mb. Return count deleted."""
    min_bytes = min_size_mb * 1024 * 1024
    deleted = 0
    for ext in ("*.mp3", "*.ogg", "*.aac"):
        for f in glob.glob(os.path.join(dest_dir, "**", ext), recursive=True):
            if "/incomplete/" in f:
                continue
            try:
                if os.path.getsize(f) < min_bytes:
                    os.remove(f)
                    deleted += 1
            except OSError:
                pass
    return deleted


def cleanup_duplicates(dest_dir):
    """Remove files matching pattern *(*[1-5]*)* (duplicates). Return count deleted."""
    pattern = re.compile(r".*\([1-5]\).*")
    deleted = 0
    for root, dirs, files in os.walk(dest_dir):
        if "incomplete" in root:
            continue
        for name in files:
            if pattern.match(name):
                try:
                    os.remove(os.path.join(root, name))
                    deleted += 1
                except OSError:
                    pass
    return deleted


def cleanup_quotes(dest_dir):
    """Remove single quotes from filenames. Return count renamed."""
    renamed = 0
    for ext in ("*.mp3", "*.ogg", "*.aac"):
        for f in glob.glob(os.path.join(dest_dir, "**", ext), recursive=True):
            if "/incomplete/" in f:
                continue
            basename = os.path.basename(f)
            if "'" in basename:
                new_name = basename.replace("'", "")
                new_path = os.path.join(os.path.dirname(f), new_name)
                try:
                    os.rename(f, new_path)
                    renamed += 1
                except OSError:
                    pass
    return renamed


def run_all(dest_dir, min_size_mb=2):
    """Run all cleanups. Return summary dict."""
    return {
        "small_deleted": cleanup_small_files(dest_dir, min_size_mb),
        "dupes_deleted": cleanup_duplicates(dest_dir),
        "quotes_renamed": cleanup_quotes(dest_dir),
    }
