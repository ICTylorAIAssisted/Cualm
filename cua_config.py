"""Shared configuration and calibration for CUA tools.

Config search order:
  1. $CUA_CONFIG
  2. ./config.ini
  3. ~/.config/cua/config.ini
  4. /etc/cua/config.ini

Calibration search order:
  1. Env vars CUA_SCALE_X + CUA_SCALE_Y (both must be set)
  2. Calibration file from config [calibration] file
  3. None → tools that need coords should refuse
"""

import configparser
import json
import os
import sys
from typing import Optional

_SEARCH_PATHS = [
    os.environ.get("CUA_CONFIG", ""),
    os.path.join(os.getcwd(), "config.ini"),
    os.path.expanduser("~/.config/cua/config.ini"),
    "/etc/cua/config.ini",
]

_DEFAULTS = {
    "screen": {"width": "1280", "height": "800", "display": ":99"},
    "calibration": {"file": "/tmp/cua_calibration.json"},
    "mouse": {
        "move_delay": "0.1",
        "default_button": "1",
        "double_click_repeat": "2",
        "double_click_delay_ms": "100",
    },
    "keyboard": {"type_delay_ms": "30"},
    "scroll": {"default_clicks": "3"},
    "screenshot": {
        "path": "/tmp/screen.png",
        "tool": "scrot",
        "tool_args": "--pointer {path} --overwrite",
    },
    "run": {"max_output_bytes": "5000", "output_dir": "/tmp/cua_runs"},
    "agent": {
        "max_steps": "50",
        "post_action_delay": "0.8",
        "wait_default_ms": "1000",
    },
    "llm": {
        "max_tokens": "8192",
        "temperature": "0",
        "max_api_errors": "3",
        "max_parse_errors": "5",
        "max_history_pairs": "4",
        "preamble_size": "4",
    },
    "audit": {"base_dir": "/app/audit"},
}


def load_config() -> configparser.ConfigParser:
    """Load configuration, merging file values over built-in defaults."""
    cfg = configparser.ConfigParser()
    for section, kvs in _DEFAULTS.items():
        cfg.setdefault(section, {})
        for k, v in kvs.items():
            cfg.set(section, k, v)

    for path in _SEARCH_PATHS:
        if path and os.path.isfile(path):
            cfg.read(path)
            break

    return cfg


def get_display(cfg: configparser.ConfigParser) -> dict[str, str]:
    """Return an environ dict with DISPLAY set."""
    env = dict(os.environ)
    env["DISPLAY"] = cfg.get("screen", "display")
    return env


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ── Calibration ────────────────────────────────────────────

_CAL_NO_CALIBRATION_MSG = (
    "No calibration found. The agent must complete calibration first.\n"
    "Run:  cua-click <x> <y>  on the calibration target.\n"
    "This will establish coordinate mapping for all tools."
)


def _cal_file(cfg: configparser.ConfigParser) -> str:
    return cfg.get("calibration", "file")


def load_calibration(
    cfg: configparser.ConfigParser,
) -> Optional[dict]:
    """Load calibration (env vars override file). Returns None if absent."""
    sx = os.environ.get("CUA_SCALE_X")
    sy = os.environ.get("CUA_SCALE_Y")
    if sx is not None and sy is not None:
        try:
            return {"scale_x": float(sx), "scale_y": float(sy)}
        except ValueError:
            pass

    path = _cal_file(cfg)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if "scale_x" in data and "scale_y" in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass

    return None


def save_calibration(
    cfg: configparser.ConfigParser,
    scale_x: float,
    scale_y: float,
) -> str:
    """Persist calibration to the file. Returns the file path."""
    path = _cal_file(cfg)
    data = {"scale_x": scale_x, "scale_y": scale_y}
    with open(path, "w") as f:
        json.dump(data, f)
    return path


def require_calibration(cfg: configparser.ConfigParser) -> dict:
    """Load calibration or die with an instructive message."""
    cal = load_calibration(cfg)
    if cal is None:
        die(_CAL_NO_CALIBRATION_MSG)
    return cal  # type: ignore[return-value]


def map_coords(
    cfg: configparser.ConfigParser,
    cal: dict,
    x: int,
    y: int,
) -> tuple[int, int]:
    """Apply calibration scaling and clamp to screen bounds."""
    w = cfg.getint("screen", "width")
    h = cfg.getint("screen", "height")
    sx = int(x * cal["scale_x"])
    sy = int(y * cal["scale_y"])
    return max(0, min(w - 1, sx)), max(0, min(h - 1, sy))
