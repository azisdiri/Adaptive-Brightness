"""Screen capture + brightness backends.

RealBackend needs Windows. DemoBackend works anywhere (fake screen, fake brightness) and is
used by `python app.py --demo` to try the UI.
"""
from __future__ import annotations

import math
import time

from brightness_logic import clamp, frame_luminance

STRIDE = 8  # sample every 8th pixel in both directions: ~64x fewer pixels, same average


def list_monitors() -> list:
    try:
        import mss

        with mss.mss() as sct:
            return [f"Display {i}  ({m['width']}x{m['height']})" for i, m in enumerate(sct.monitors[1:], 1)] or ["Display 1"]
    except Exception:
        return ["Display 1"]


class RealBackend:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._sct = None
        self._sbc = None
        self._com = False

    def open(self):
        # Brightness control talks to WMI over COM; COM must be initialised on this thread.
        try:
            import pythoncom

            pythoncom.CoInitialize()
            self._com = True
        except Exception:
            pass
        import mss
        import screen_brightness_control as sbc

        self._sct = mss.mss()      # mss handles are per-thread, so create it here
        self._sbc = sbc

    def _method(self):
        return "wmi" if self.cfg.get("builtin_only") else None

    def luminance(self, monitor: int) -> float:
        monitors = self._sct.monitors
        mon = monitors[monitor] if 1 <= monitor < len(monitors) else monitors[1]
        return frame_luminance(self._sct.grab(mon), STRIDE)

    def get_brightness(self) -> int:
        values = self._sbc.get_brightness(method=self._method())
        if isinstance(values, (int, float)):
            return int(values)
        for value in values:
            if value is not None:
                return int(value)
        raise RuntimeError("The display did not report a brightness value")

    def set_brightness(self, value: int):
        self._sbc.set_brightness(int(value), method=self._method())

    def close(self):
        if self._sct is not None:
            self._sct.close()
        if self._com:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass


class DemoBackend:
    """Fake screen that slowly swings between dark and bright, plus a fake panel."""

    def __init__(self, period: float = 24.0):
        self.period = period
        self._brightness = 70
        self._t0 = time.monotonic()

    def open(self):
        self._t0 = time.monotonic()

    def luminance(self, monitor: int) -> float:
        phase = (time.monotonic() - self._t0) / self.period
        return clamp(0.5 + 0.7 * math.sin(2 * math.pi * phase), 0.0, 1.0)

    def get_brightness(self) -> int:
        return self._brightness

    def set_brightness(self, value: int):
        self._brightness = int(value)

    def close(self):
        pass
