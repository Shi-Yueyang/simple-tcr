"""
Persistent settings for the TCR engine.

Stores a JSON file in a platform-appropriate config directory:
  - Linux / macOS: ~/.config/simple-tcr/settings.json
  - Windows: %APPDATA%\\simple-tcr\\settings.json
"""

import json
import os
import pathlib
import platform

DEFAULTS = {
    "allow_101_without_lock": False,
}


def _default_path() -> pathlib.Path:
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return pathlib.Path(base) / "simple-tcr" / "settings.json"


DEFAULT_PATH = _default_path()


def load_settings(path=None):
    """Return merged settings (file values over defaults).

    Returns a copy so the caller cannot mutate the module-level DEFAULTS.
    """
    path = pathlib.Path(path) if path else DEFAULT_PATH
    settings = dict(DEFAULTS)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            settings.update(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return settings


def save_settings(path, settings):
    """Write *settings* dict to *path* as JSON, creating parent dirs."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    tmp.replace(path)
