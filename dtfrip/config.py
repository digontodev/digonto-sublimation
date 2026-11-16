"""Paths and persistent settings."""
import json
import os
from pathlib import Path

import sys

from .flavor import CONFIG_DIR as APP_DIR, CONFIG_NAME as APP, DEFAULT_PROFILE, PORT as DEFAULT_PORT
HERE = Path(__file__).resolve().parent


def _root():
    """(config root, data root)"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
        return base / APP_DIR, Path(os.environ.get("LOCALAPPDATA") or base) / APP_DIR
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_DIR
        return base, base
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / APP
    data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / APP
    return cfg, data


def config_dir():
    d = Path(os.environ.get("DTFRIP_CONFIG") or _root()[0])
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir():
    d = Path(os.environ.get("DTFRIP_DATA") or _root()[1])
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundled_profiles_dir():
    """colour profiles shipped with the program (copied from the AcroRIP / DTFRIP installer)"""
    for p in (HERE.parent / "profiles", HERE / "profiles"):
        if p.is_dir():
            return p
    return None


def profiles_dir():
    d = data_dir() / "profiles"; d.mkdir(parents=True, exist_ok=True); return d


def uploads_dir():
    d = data_dir() / "uploads"; d.mkdir(parents=True, exist_ok=True); return d


def spool_dir():
    d = data_dir() / "spool"; d.mkdir(parents=True, exist_ok=True); return d


def _profile_dirs():
    out = [profiles_dir()]
    b = bundled_profiles_dir()
    if b:
        out.append(b)
    return out


def list_profiles():
    names = set()
    for d in _profile_dirs():
        names.update(p.name for p in d.iterdir() if p.suffix.lower() in (".icc", ".icm"))
    return sorted(names)


def find_profile(name):
    if not name:
        return None
    for d in _profile_dirs():
        p = d / name
        if p.exists():
            return p
    p = Path(name)
    return p if p.is_absolute() and p.exists() else None


DEFAULTS = {
    "device": "",                 # "" = auto-detect (USB device on Linux, print queue on Windows / macOS)
    "listen": "0.0.0.0",
    "port": DEFAULT_PORT,
    "password": "",
    "rip": {"profile": DEFAULT_PROFILE},
    "keepalive": {"enabled": False, "hours": 24, "white_only": False, "height_mm": 4,
                  "quiet_from": 22, "quiet_to": 8},
    "last_print": 0,
    "alignment": [],
}


def load():
    f = config_dir() / "settings.json"
    data = json.loads(json.dumps(DEFAULTS))
    if f.exists():
        try:
            user = json.loads(f.read_text())
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(data.get(k), dict):
                    data[k].update(v)
                else:
                    data[k] = v
        except Exception:
            pass
    return data


def save(data):
    f = config_dir() / "settings.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(f)
