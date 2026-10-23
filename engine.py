"""The worker that watches the screen and nudges the brightness."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from brightness_logic import BrightnessController, Smoother, resolve_profile, target_brightness

CHECKS_PER_SECOND = 4


@dataclass
class Status:
    content: Optional[float] = None      # smoothed content luminance, 0..1
    target: Optional[int] = None         # brightness the content is asking for (%)
    brightness: Optional[int] = None     # brightness we last applied / read (%)
    capture_error: str = ""
    set_error: str = ""

    @property
    def error(self) -> str:
        return self.capture_error or self.set_error


def describe_error(exc: Exception) -> str:
    name = type(exc).__name__
    text = str(exc).strip()
    if "NoValidDisplay" in name or "no valid display" in text.lower():
        return ("No adjustable display found. On a desktop PC or with an external monitor, untick "
                "'Control the built-in display only' (external monitors need DDC/CI enabled).")
    return f"{name}: {text}" if text else name


class Engine:
    """Reads `cfg` live, so the GUI can change settings while it runs.

    `backend` needs: open(), close(), luminance(monitor), get_brightness(), set_brightness(value).
    """

    def __init__(self, cfg: dict, backend, clock=time.monotonic, now_fn=datetime.now):
        self.cfg = cfg
        self.backend = backend
        self.clock = clock
        self.now_fn = now_fn
        self.smoother = Smoother()
        self.controller = BrightnessController()
        self.status = Status()
        self.original: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- one step of work (no threads, no sleeping: easy to test) ---------------------------
    def process(self, lum: float, t: Optional[float] = None, now: Optional[datetime] = None):
        cfg = self.cfg
        t = self.clock() if t is None else t
        now = self.now_fn() if now is None else now

        avg = self.smoother.add(t, lum, cfg["smoothing"])
        levels = cfg[resolve_profile(cfg, now)]
        target = int(round(target_brightness(avg, levels["white"], levels["dark"])))

        ctl = self.controller
        ctl.threshold, ctl.max_step = cfg["threshold"], cfg["speed"]
        prev_current, prev_moving = ctl.current, ctl.moving
        new = ctl.update(target)

        st = self.status
        st.content, st.target, st.capture_error = avg, target, ""
        if new is None:
            return None
        try:
            self.backend.set_brightness(new)
        except Exception as exc:
            ctl.current, ctl.moving = prev_current, prev_moving   # we did not actually move
            st.set_error = describe_error(exc)
            return None
        st.brightness, st.set_error = new, ""
        return new

    def resync(self) -> Optional[int]:
        """Forget history and re-read the real brightness (start-up, or after a pause)."""
        self.smoother.clear()
        try:
            actual = self.backend.get_brightness()
        except Exception as exc:
            actual = None
            self.status.set_error = describe_error(exc)
        self.controller.reset(actual)
        if actual is not None:
            self.status.brightness = actual
        return actual

    # -- thread management ------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="brightness-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        try:
            self.backend.open()
        except Exception as exc:
            self.status.capture_error = f"Could not start screen capture: {describe_error(exc)}"
            return
        try:
            self.original = self.resync()
            was_enabled = True
            while not self._stop.is_set():
                started = self.clock()
                enabled = self.cfg["enabled"]
                if enabled:
                    if not was_enabled:
                        self.resync()
                    lum = None
                    try:
                        lum = self.backend.luminance(self.cfg["monitor"])
                    except Exception as exc:
                        self.status.capture_error = describe_error(exc)
                    if lum is not None:
                        try:
                            self.process(lum)
                        except Exception as exc:
                            self.status.set_error = describe_error(exc)
                was_enabled = enabled
                spare = 1.0 / CHECKS_PER_SECOND - (self.clock() - started)
                self._stop.wait(max(0.05, spare))
        finally:
            try:
                if self.cfg["restore_on_exit"] and self.original is not None:
                    self.backend.set_brightness(self.original)
            except Exception:
                pass
            try:
                self.backend.close()
            except Exception:
                pass
