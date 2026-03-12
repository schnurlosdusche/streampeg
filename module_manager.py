"""Simple plugin/module system for streampeg.

Modules are .py files in the modules/ directory that expose a MODULE_INFO dict.
Discovery happens once at startup via discover_modules().
"""

import os
import importlib
import logging

log = logging.getLogger(__name__)

# Built-in record modes (always available)
BUILTIN_MODES = {"streamripper", "ffmpeg_api", "ffmpeg_icy"}

# Discovered modules: {name: MODULE_INFO dict}
_modules = {}


def discover_modules():
    """Scan modules/ directory and import all valid modules."""
    _modules.clear()
    modules_dir = os.path.join(os.path.dirname(__file__), "modules")
    if not os.path.isdir(modules_dir):
        return

    for filename in sorted(os.listdir(modules_dir)):
        if filename.startswith("_") or not filename.endswith(".py"):
            continue
        module_name = filename[:-3]
        try:
            mod = importlib.import_module(f"modules.{module_name}")
            info = getattr(mod, "MODULE_INFO", None)
            if info and isinstance(info, dict) and "name" in info:
                _modules[info["name"]] = info
                log.info("Module loaded: %s", info["name"])
            else:
                log.warning("Module %s has no valid MODULE_INFO", module_name)
        except Exception as e:
            log.error("Failed to load module %s: %s", module_name, e)


def get_all_modules():
    """Return dict of all discovered modules."""
    return dict(_modules)


def _is_enabled(name):
    """Check if a module is enabled in settings. Default: enabled."""
    from db import get_setting
    val = get_setting(f"module:{name}:enabled")
    return val != "0"  # enabled by default (None or "1")


def get_enabled_modules():
    """Return list of MODULE_INFO dicts for enabled modules."""
    return [m for m in _modules.values() if _is_enabled(m["name"])]


def get_all_record_modes():
    """Return set of all available record modes (built-in + enabled modules)."""
    modes = set(BUILTIN_MODES)
    for mod in get_enabled_modules():
        modes.update(mod.get("record_modes", []))
    return modes


def get_recorder_class(record_mode):
    """Return recorder class for a module-provided record mode, or None."""
    for mod in get_enabled_modules():
        if record_mode in mod.get("record_modes", []):
            return mod.get("recorder_class")
    return None


def is_mode_available(record_mode):
    """Check if a record mode is available (built-in or enabled module)."""
    return record_mode in get_all_record_modes()


def get_module_icons():
    """Return dict {record_mode: icon_html} for all enabled modules."""
    icons = {}
    for mod in get_enabled_modules():
        html = mod.get("icon_html", "")
        for mode in mod.get("record_modes", []):
            icons[mode] = html
    return icons


def get_module_form_options():
    """Return list of form option dicts for enabled modules."""
    options = []
    for mod in get_enabled_modules():
        opt = mod.get("form_option")
        if opt:
            options.append(opt)
    return options


def get_module_form_hints():
    """Return dict {record_mode: hint_text} for enabled modules."""
    hints = {}
    for mod in get_enabled_modules():
        hints.update(mod.get("form_hints", {}))
    return hints


def get_module_hide_fields():
    """Return dict {record_mode: [field_ids]} for enabled modules."""
    fields = {}
    for mod in get_enabled_modules():
        for mode in mod.get("record_modes", []):
            fields[mode] = mod.get("hide_fields", [])
    return fields


def set_module_enabled(name, enabled):
    """Enable or disable a module."""
    from db import set_setting
    set_setting(f"module:{name}:enabled", "1" if enabled else "0")
