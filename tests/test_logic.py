import os
import sys
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import engine as engine_mod  # noqa: E402
from brightness_logic import (  # noqa: E402
    DEFAULT_CONFIG,
    BrightnessController,
    Smoother,
    frame_luminance,
    merge_config,
    normalize_hhmm,
    parse_hhmm,
    resolve_profile,
    target_brightness,
)

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

NOON = datetime(2026, 1, 1, 12, 0)


class ConfigTests(unittest.TestCase):
    def test_defaults_when_nothing_loaded(self):
        self.assertEqual(merge_config(None), DEFAULT_CONFIG)
        self.assertEqual(merge_config("junk"), DEFAULT_CONFIG)

    def test_values_are_clamped_and_garbage_ignored(self):
        cfg = merge_config({
            "threshold": 999, "smoothing": "abc", "speed": -4,
            "day": {"white": 0, "dark": 500}, "mode": "weird",
            "night_start": "25:99", "monitor": 3.7, "enabled": "yes",
        })
        self.assertEqual(cfg["threshold"], 30)
        self.assertEqual(cfg["smoothing"], DEFAULT_CONFIG["smoothing"])
        self.assertEqual(cfg["speed"], 1)
        self.assertEqual(cfg["day"], {"white": 1, "dark": 100})
        self.assertEqual(cfg["mode"], DEFAULT_CONFIG["mode"])
        self.assertEqual(cfg["night_start"], DEFAULT_CONFIG["night_start"])
        self.assertEqual(cfg["monitor"], 4)
        self.assertEqual(cfg["enabled"], DEFAULT_CONFIG["enabled"])

    def test_defaults_are_not_shared_between_calls(self):
        a = merge_config(None)
        a["day"]["white"] = 1
        self.assertEqual(merge_config(None)["day"]["white"], DEFAULT_CONFIG["day"]["white"])

    def test_time_parsing(self):
        self.assertEqual(parse_hhmm("7:30"), (7, 30))
        self.assertEqual(parse_hhmm(" 07:05 "), (7, 5))
        for bad in ("24:00", "7", "ab:cd", "", None, "12:60"):
            self.assertIsNone(parse_hhmm(bad))
        self.assertEqual(normalize_hhmm("7:5", "00:00"), "07:05")
        self.assertEqual(normalize_hhmm("nope", "19:00"), "19:00")


class ProfileTests(unittest.TestCase):
    def test_fixed_modes(self):
        self.assertEqual(resolve_profile(merge_config({"mode": "day"}), datetime(2026, 1, 1, 23, 0)), "day")
        self.assertEqual(resolve_profile(merge_config({"mode": "night"}), NOON), "night")

    def test_auto_overnight_window(self):
        cfg = merge_config({"mode": "auto", "night_start": "19:00", "night_end": "07:00"})
        cases = [(12, 0, "day"), (18, 59, "day"), (19, 0, "night"), (23, 30, "night"),
                 (0, 0, "night"), (6, 59, "night"), (7, 0, "day")]
        for hour, minute, expected in cases:
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(resolve_profile(cfg, datetime(2026, 1, 1, hour, minute)), expected)

    def test_auto_same_day_window(self):
        cfg = merge_config({"mode": "auto", "night_start": "13:00", "night_end": "15:00"})
        self.assertEqual(resolve_profile(cfg, datetime(2026, 1, 1, 14, 0)), "night")
        self.assertEqual(resolve_profile(cfg, datetime(2026, 1, 1, 16, 0)), "day")
        self.assertEqual(resolve_profile(cfg, datetime(2026, 1, 1, 12, 0)), "day")

    def test_equal_start_and_end_means_day(self):
        cfg = merge_config({"mode": "auto", "night_start": "08:00", "night_end": "08:00"})
        self.assertEqual(resolve_profile(cfg, NOON), "day")


class MappingTests(unittest.TestCase):
    def test_extremes_hit_the_users_levels(self):
        self.assertAlmostEqual(target_brightness(0.0, white=30, dark=90), 90)
        self.assertAlmostEqual(target_brightness(1.0, white=30, dark=90), 30)
        self.assertAlmostEqual(target_brightness(0.05, white=30, dark=90), 90)   # typical dark theme
        self.assertAlmostEqual(target_brightness(0.9, white=30, dark=90), 30)    # typical white page

    def test_brighter_content_never_raises_brightness(self):
        values = [target_brightness(i / 100, white=30, dark=90) for i in range(101)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_midpoint(self):
        self.assertAlmostEqual(target_brightness(0.465, white=40, dark=90), 65, places=1)


class SmootherTests(unittest.TestCase):
    def test_average_over_window(self):
        s = Smoother()
        self.assertEqual(s.add(0.0, 0.0, 1.0), 0.0)
        self.assertEqual(s.add(0.5, 1.0, 1.0), 0.5)
        self.assertAlmostEqual(s.add(1.2, 1.0, 1.0), 2 / 2)   # t=0.0 sample has aged out

    def test_zero_window_uses_latest_only(self):
        s = Smoother()
        s.add(0.0, 0.2, 0.0)
        self.assertEqual(s.add(1.0, 0.8, 0.0), 0.8)


class ControllerTests(unittest.TestCase):
    def test_first_update_applies_directly(self):
        self.assertEqual(BrightnessController().update(70), 70)

    def test_small_changes_ignored(self):
        c = BrightnessController(threshold=5, max_step=3)
        c.reset(50)
        self.assertIsNone(c.update(53))
        self.assertIsNone(c.update(46))

    def test_steps_toward_target_then_stops(self):
        c = BrightnessController(threshold=5, max_step=3)
        c.reset(50)
        seen = []
        for _ in range(20):
            v = c.update(62)
            if v is None:
                break
            seen.append(v)
        self.assertEqual(seen, [53, 56, 59, 62])

    def test_keeps_moving_once_started_even_below_threshold(self):
        c = BrightnessController(threshold=5, max_step=3)
        c.reset(50)
        self.assertEqual(c.update(56), 53)
        self.assertEqual(c.update(56), 56)      # remaining diff 3 < threshold, but already moving
        self.assertIsNone(c.update(58))         # arrived: back to ignoring small changes

    def test_output_is_clamped(self):
        c = BrightnessController(threshold=1, max_step=10)
        c.reset(5)
        self.assertEqual(c.update(-20), 1)


class FakeBackend:
    def __init__(self, start=80, lum=1.0):
        self.value, self.lum, self.calls, self.fail, self.closed = start, lum, [], False, False

    def open(self): pass
    def close(self): self.closed = True
    def luminance(self, monitor): return self.lum
    def get_brightness(self): return self.value

    def set_brightness(self, v):
        if self.fail:
            raise RuntimeError("boom")
        self.value = v
        self.calls.append(v)


def make_engine(**overrides):
    cfg = merge_config({"mode": "day", "smoothing": 0, "threshold": 5, "speed": 3,
                        "day": {"white": 40, "dark": 90}, **overrides})
    backend = FakeBackend()
    eng = engine_mod.Engine(cfg, backend)
    eng.resync()
    return eng, backend


class EngineTests(unittest.TestCase):
    def test_white_content_lowers_brightness_to_white_level(self):
        eng, b = make_engine()
        for i in range(40):
            eng.process(1.0, t=i, now=NOON)
        self.assertEqual(b.value, 40)
        steps = [abs(x - y) for x, y in zip([80] + b.calls, b.calls)]
        self.assertTrue(all(s <= 3 for s in steps), steps)
        self.assertEqual(eng.status.target, 40)

    def test_dark_content_raises_brightness_to_dark_level(self):
        eng, b = make_engine()
        for i in range(40):
            eng.process(0.0, t=i, now=NOON)
        self.assertEqual(b.value, 90)

    def test_small_target_change_is_ignored(self):
        eng, b = make_engine(threshold=10)
        for i in range(5):
            eng.process(0.2, t=i, now=NOON)     # target ~ 79: within threshold of 80
        self.assertEqual(b.calls, [])

    def test_night_profile_is_used(self):
        eng, b = make_engine(mode="night", night={"white": 20, "dark": 50})
        for i in range(40):
            eng.process(1.0, t=i, now=NOON)
        self.assertEqual(b.value, 20)

    def test_failed_set_is_retried_and_reported(self):
        eng, b = make_engine()
        b.fail = True
        eng.process(1.0, t=0, now=NOON)
        self.assertIn("boom", eng.status.error)
        self.assertEqual(eng.controller.current, 80)
        b.fail = False
        eng.process(1.0, t=1, now=NOON)
        self.assertEqual(b.value, 77)
        self.assertEqual(eng.status.error, "")


class EngineThreadTests(unittest.TestCase):
    def setUp(self):
        old = engine_mod.CHECKS_PER_SECOND
        engine_mod.CHECKS_PER_SECOND = 100
        self.addCleanup(setattr, engine_mod, "CHECKS_PER_SECOND", old)

    def test_runs_adjusts_and_restores_original_on_exit(self):
        cfg = merge_config({"mode": "day", "smoothing": 0, "restore_on_exit": True,
                            "day": {"white": 40, "dark": 90}})
        b = FakeBackend(start=80, lum=1.0)
        eng = engine_mod.Engine(cfg, b)
        eng.start()
        time.sleep(0.8)
        eng.stop()
        self.assertEqual(min(b.calls), 40)
        self.assertEqual(b.value, 80)          # restored on exit
        self.assertTrue(b.closed)

    def test_paused_engine_leaves_brightness_alone(self):
        cfg = merge_config({"mode": "day", "enabled": False, "restore_on_exit": False})
        b = FakeBackend(start=80, lum=1.0)
        eng = engine_mod.Engine(cfg, b)
        eng.start()
        time.sleep(0.3)
        eng.stop()
        self.assertEqual(b.calls, [])

    def test_resume_after_pause_resyncs_to_real_brightness(self):
        cfg = merge_config({"mode": "day", "smoothing": 0, "enabled": False, "restore_on_exit": False,
                            "day": {"white": 40, "dark": 90}})
        b = FakeBackend(start=80, lum=1.0)
        eng = engine_mod.Engine(cfg, b)
        eng.start()
        time.sleep(0.2)
        b.value = 60                           # user changed brightness by hand while paused
        cfg["enabled"] = True
        time.sleep(0.15)
        eng.stop()
        self.assertEqual(b.calls[0], 57)       # stepped from 60, not from the stale 80


@unittest.skipIf(np is None, "numpy not installed")
class FrameLuminanceTests(unittest.TestCase):
    def test_white_and_black(self):
        self.assertAlmostEqual(frame_luminance(np.full((100, 200, 4), 255, dtype=np.uint8)), 1.0, places=3)
        self.assertAlmostEqual(frame_luminance(np.zeros((100, 200, 4), dtype=np.uint8)), 0.0)

    def test_channel_order_is_bgra(self):
        green = np.zeros((16, 16, 4), dtype=np.uint8)
        green[..., 1] = 255
        red = np.zeros((16, 16, 4), dtype=np.uint8)
        red[..., 2] = 255
        self.assertAlmostEqual(frame_luminance(green, stride=1), 0.587, places=3)
        self.assertAlmostEqual(frame_luminance(red, stride=1), 0.299, places=3)


if __name__ == "__main__":
    unittest.main()
