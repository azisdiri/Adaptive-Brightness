"""Pure logic for Adaptive Brightness.

Nothing in here touches the OS, the screen or the GUI, so it can be unit-tested anywhere.
"""
from __future__ import annotations

import copy
from collections import deque
from datetime import datetime
from typing import Optional

DEFAULT_CONFIG = {
    "mode": "auto",                       # "day" | "night" | "auto" (auto switches by time)
    "day": {"white": 50, "dark": 90},     # screen brightness % for bright / dark content
    "night": {"white": 20, "dark": 50},
    "threshold": 5,                       # ignore brightness changes smaller than this (%)
    "smoothing": 1.5,                     # seconds of screen content averaged together
    "speed": 3,                           # max brightness change per update (%)
    "night_start": "19:00",
    "night_end": "07:00",
    "monitor": 1,                         # which display to watch (1 = primary)
    "builtin_only": True,                 # only touch the laptop's own panel
    "start_with_windows": False,
    "minimize_to_tray": True,
    "restore_on_exit": True,
    "enabled": True,
}

LIMITS = {
    "brightness": (1, 100),
    "threshold": (1, 30),
    "smoothing": (0.0, 5.0),
    "speed": (1, 10),
    "monitor": (1, 16),
}

# Real screens almost never reach 0 % or 100 % average luminance: a dark theme still has
# some lit pixels, a "white" page still has grey window chrome. Treat <= LUM_LOW as fully
# dark and >= LUM_HIGH as fully white, so the user's "white" and "dark" settings are what
# they really get on typical content.
LUM_LOW = 0.08
LUM_HIGH = 0.85

BOOL_KEYS = ("builtin_only", "start_with_windows", "minimize_to_tray", "restore_on_exit", "enabled")


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _coerce(value, lo, hi, default, integer=True):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if x != x:  # NaN
        return default
    x = clamp(x, lo, hi)
    return int(round(x)) if integer else round(x, 2)


def parse_hhmm(text) -> Optional[tuple]:
    """'7:30' or '07:30' -> (7, 30); None when it is not a valid 24-hour time."""
    try:
        hours, minutes = str(text).strip().split(":")
        hours, minutes = int(hours), int(minutes)
    except ValueError:
        return None
    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return hours, minutes
    return None


def normalize_hhmm(text, default: str) -> str:
    parsed = parse_hhmm(text)
    return f"{parsed[0]:02d}:{parsed[1]:02d}" if parsed else default


def merge_config(loaded) -> dict:
    """Overlay a loaded (possibly damaged or outdated) config on the defaults."""
    d = DEFAULT_CONFIG
    cfg = copy.deepcopy(d)
    if not isinstance(loaded, dict):
        return cfg

    if loaded.get("mode") in ("day", "night", "auto"):
        cfg["mode"] = loaded["mode"]
    for profile in ("day", "night"):
        source = loaded.get(profile)
        if isinstance(source, dict):
            for key in ("white", "dark"):
                cfg[profile][key] = _coerce(source.get(key), *LIMITS["brightness"], d[profile][key])
    cfg["threshold"] = _coerce(loaded.get("threshold"), *LIMITS["threshold"], d["threshold"])
    cfg["smoothing"] = _coerce(loaded.get("smoothing"), *LIMITS["smoothing"], d["smoothing"], integer=False)
    cfg["speed"] = _coerce(loaded.get("speed"), *LIMITS["speed"], d["speed"])
    cfg["monitor"] = _coerce(loaded.get("monitor"), *LIMITS["monitor"], d["monitor"])
    for key in ("night_start", "night_end"):
        cfg[key] = normalize_hhmm(loaded.get(key), d[key])
    for key in BOOL_KEYS:
        if isinstance(loaded.get(key), bool):
            cfg[key] = loaded[key]
    return cfg


def resolve_profile(cfg: dict, now: datetime) -> str:
    """Which profile ("day" or "night") applies right now."""
    mode = cfg["mode"]
    if mode in ("day", "night"):
        return mode
    start, end = parse_hhmm(cfg["night_start"]), parse_hhmm(cfg["night_end"])
    if not start or not end or start == end:
        return "day"
    current = (now.hour, now.minute)
    if start < end:                       # e.g. 13:00 -> 15:00
        is_night = start <= current < end
    else:                                 # wraps midnight, e.g. 19:00 -> 07:00
        is_night = current >= start or current < end
    return "night" if is_night else "day"


def target_brightness(luminance: float, white: float, dark: float) -> float:
    """Map content luminance (0 = black, 1 = white) to a screen brightness in %.

    Bright content -> `white` level, dark content -> `dark` level, linear in between.
    """
    t = clamp((luminance - LUM_LOW) / (LUM_HIGH - LUM_LOW), 0.0, 1.0)
    return dark + (white - dark) * t


def frame_luminance(bgra, stride: int = 8) -> float:
    """Average perceived luminance (0..1) of a BGRA frame, sampling every `stride`-th pixel."""
    import numpy as np

    pixels = np.asarray(bgra)[::stride, ::stride, :3].astype(np.float32)
    luma = pixels @ np.array([0.114, 0.587, 0.299], dtype=np.float32)  # B, G, R weights
    return float(luma.mean()) / 255.0


class Smoother:
    """Moving average over the last `window` seconds of samples."""

    def __init__(self):
        self._items = deque()

    def clear(self):
        self._items.clear()

    def add(self, t: float, value: float, window: float) -> float:
        self._items.append((t, value))
        while len(self._items) > 1 and t - self._items[0][0] > window:
            self._items.popleft()
        return sum(v for _, v in self._items) / len(self._items)


class BrightnessController:
    """Decides when and how far to move the real brightness toward a target.

    * Changes smaller than `threshold` are ignored (no flicker, no needless hardware calls).
    * Once a move starts it keeps going until the target is reached, in steps of at most
      `max_step`, so transitions are smooth and don't stall just short of the target.
    """

    def __init__(self, threshold: int = 5, max_step: int = 3):
        self.threshold = threshold
        self.max_step = max_step
        self.current: Optional[int] = None
        self.moving = False

    def reset(self, current: Optional[int] = None):
        self.current = None if current is None else int(current)
        self.moving = False

    def update(self, target: float) -> Optional[int]:
        """Return the brightness to apply now, or None if nothing should change."""
        target = int(clamp(round(target), 1, 100))
        if self.current is None:
            self.current = target
            return target
        diff = target - self.current
        if diff == 0:
            self.moving = False
            return None
        if not self.moving and abs(diff) < self.threshold:
            return None
        self.moving = True
        self.current += int(clamp(diff, -self.max_step, self.max_step))
        if self.current == target:
            self.moving = False
        return self.current
