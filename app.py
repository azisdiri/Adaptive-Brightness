"""Adaptive Brightness - GUI, tray icon and entry point.

    python app.py              run (Windows). Add --minimized to start hidden in the tray.
    python app.py --demo       any OS: fake screen + fake brightness, to try the interface.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from backends import DemoBackend, RealBackend, list_monitors
from brightness_logic import merge_config, normalize_hhmm, parse_hhmm, resolve_profile
from engine import Engine
from icon_art import make_icon

APP_NAME = "Adaptive Brightness"
IS_WIN = sys.platform == "win32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_mutex_handle = None  # kept alive for the lifetime of the process


# ---------------------------------------------------------------------------------------------
# Settings file, start-with-Windows, single instance
# ---------------------------------------------------------------------------------------------
def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "AdaptiveBrightness")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            return merge_config(json.load(fh))
    except (OSError, ValueError):
        return merge_config(None)


def save_config(cfg: dict) -> None:
    try:
        path = config_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _startup_command() -> str:
    if getattr(sys, "frozen", False):                       # packaged .exe
        return f'"{sys.executable}" --minimized'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    interpreter = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{interpreter}" "{os.path.abspath(__file__)}" --minimized'


def set_autostart(enabled: bool) -> None:
    if not IS_WIN:
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "AdaptiveBrightness", 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, "AdaptiveBrightness")
            except FileNotFoundError:
                pass


def already_running() -> bool:
    """Two copies would fight over the brightness, so only allow one."""
    global _mutex_handle
    if not IS_WIN:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\AdaptiveBrightnessSingleton")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------------------------
class App:
    TICK_MS = 250

    def __init__(self, root: tk.Tk, cfg: dict, engine: Engine, hidden: bool, demo: bool):
        self.root, self.cfg, self.engine, self.demo = root, cfg, engine, demo
        self.tray = None
        self.events: "queue.Queue" = queue.Queue()   # tray thread -> Tk thread
        self._save_job = None
        self._flash_text = ""
        self._flash_job = None
        self._closing = False

        root.title(APP_NAME + ("  (demo)" if demo else ""))
        root.resizable(False, False)
        self._set_window_icon()
        self._build()
        self._start_tray()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if hidden and self.tray:
            root.withdraw()
        self.tick()

    # -- config helpers ---------------------------------------------------------------------
    def _get(self, path):
        value = self.cfg
        for key in path:
            value = value[key]
        return value

    def _set(self, path, value):
        target = self.cfg
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    def save_soon(self):
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(400, lambda: save_config(self.cfg))

    def flash(self, text: str, ms: int = 4000):
        self._flash_text = text
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self._flash_job = self.root.after(ms, lambda: setattr(self, "_flash_text", ""))

    # -- building the UI --------------------------------------------------------------------
    def _set_window_icon(self):
        try:
            from PIL import ImageTk

            self._icon = ImageTk.PhotoImage(make_icon(64))
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    def _build(self):
        style = ttk.Style()
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Big.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Cap.TLabel", foreground="#666666")
        style.configure("Err.TLabel", foreground="#b00020")

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        # header ------------------------------------------------------------------------
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(side="left")
        self.toggle_btn = ttk.Button(head, width=10, command=self.toggle)
        self.toggle_btn.pack(side="right")

        # live readout ------------------------------------------------------------------
        live = ttk.LabelFrame(outer, text="Live", padding=10)
        live.pack(fill="x", pady=(10, 0))
        self.stat = {}
        captions = (("content", "Screen content"), ("target", "Wanted brightness"), ("screen", "Brightness now"))
        for col, (key, caption) in enumerate(captions):
            live.columnconfigure(col, weight=1)
            cell = ttk.Frame(live)
            cell.grid(row=0, column=col, sticky="w")
            self.stat[key] = ttk.Label(cell, text="-", style="Big.TLabel")
            self.stat[key].pack(anchor="w")
            ttk.Label(cell, text=caption, style="Cap.TLabel").pack(anchor="w")
        self.bar = ttk.Progressbar(live, maximum=100, mode="determinate")
        self.bar.grid(row=1, column=0, columnspan=3, sticky="we", pady=(8, 0))
        self.state_lbl = ttk.Label(live, style="Cap.TLabel")
        self.state_lbl.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # two tabs keep the window short enough for small laptop screens -----------------
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="x", pady=(10, 0))
        main_tab = ttk.Frame(self.notebook, padding=(0, 10, 0, 0))
        settings_tab = ttk.Frame(self.notebook, padding=(0, 10, 0, 0))
        self.notebook.add(main_tab, text="Brightness")
        self.notebook.add(settings_tab, text="Settings")

        # mode + schedule ---------------------------------------------------------------
        modef = ttk.LabelFrame(main_tab, text="Mode", padding=10)
        modef.pack(fill="x")
        radios = ttk.Frame(modef)
        radios.pack(fill="x")
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        for text, value in (("Day", "day"), ("Night", "night"), ("Auto (by time of day)", "auto")):
            ttk.Radiobutton(radios, text=text, value=value, variable=self.mode_var,
                            command=self.on_mode).pack(side="left", padx=(0, 18))
        sched = ttk.Frame(modef)
        sched.pack(fill="x", pady=(8, 0))
        ttk.Label(sched, text="Auto mode: night from").pack(side="left")
        self._time_entry(sched, "night_start")
        ttk.Label(sched, text="to").pack(side="left", padx=6)
        self._time_entry(sched, "night_end")
        ttk.Label(sched, text="(24-hour HH:MM)", style="Cap.TLabel").pack(side="left", padx=8)

        # day / night brightness levels -------------------------------------------------
        profiles = ttk.Frame(main_tab)
        profiles.pack(fill="x", pady=(10, 0))
        profiles.columnconfigure(0, weight=1, uniform="profile")
        profiles.columnconfigure(1, weight=1, uniform="profile")
        self.profile_frames = {}
        for col, (key, title) in enumerate((("day", "Day mode"), ("night", "Night mode"))):
            frame = ttk.LabelFrame(profiles, text=title, padding=10)
            frame.grid(row=0, column=col, sticky="nsew", padx=(0, 6) if col == 0 else (6, 0))
            self.slider(frame, "White / light content", (key, "white"), 1, 100, "{}%")
            self.slider(frame, "Dark content", (key, "dark"), 1, 100, "{}%")
            self.profile_frames[key] = (frame, title)

        # behaviour ---------------------------------------------------------------------
        behaviour = ttk.LabelFrame(settings_tab, text="Behaviour", padding=10)
        behaviour.pack(fill="x")
        self.slider(behaviour, "Change threshold (ignore smaller changes)", ("threshold",), 1, 30, "{}%")
        self.slider(behaviour, "Smoothing (average content over)", ("smoothing",), 0, 5, "{:.1f} s", as_float=True)
        self.slider(behaviour, "Transition speed (max step per update)", ("speed",), 1, 10, "{}%")

        # options -----------------------------------------------------------------------
        options = ttk.LabelFrame(settings_tab, text="Options", padding=10)
        options.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(options)
        row.pack(fill="x")
        ttk.Label(row, text="Display to watch").pack(side="left")
        names = list_monitors()
        index = min(max(self.cfg["monitor"], 1), len(names)) - 1
        self.monitor_var = tk.StringVar(value=names[index])
        combo = ttk.Combobox(row, values=names, textvariable=self.monitor_var, state="readonly", width=26)
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", lambda _e: self.on_monitor(names))
        self.check(options, "Control the built-in display only", "builtin_only")
        self.check(options, "Start with Windows", "start_with_windows", self.on_autostart)
        self.check(options, "Keep running in the tray when the window is closed", "minimize_to_tray")
        self.check(options, "Restore the original brightness on exit", "restore_on_exit")

        # messages ----------------------------------------------------------------------
        self.msg = ttk.Label(outer, style="Err.TLabel", wraplength=460, justify="left")
        self.msg.pack(fill="x", pady=(8, 0))

    def slider(self, parent, text, path, lo, hi, fmt, as_float=False):
        box = ttk.Frame(parent)
        box.pack(fill="x", pady=(0, 6))
        top = ttk.Frame(box)
        top.pack(fill="x")
        ttk.Label(top, text=text).pack(side="left")
        value_label = ttk.Label(top, width=7, anchor="e")
        value_label.pack(side="right")
        value_label.config(text=fmt.format(self._get(path)))
        var = tk.DoubleVar(value=self._get(path))

        def changed(_value=None):
            v = var.get()
            v = round(v, 1) if as_float else int(round(v))
            self._set(path, v)
            value_label.config(text=fmt.format(v))
            self.save_soon()

        ttk.Scale(box, from_=lo, to=hi, variable=var, command=changed).pack(fill="x")

    def check(self, parent, text, key, extra=None):
        var = tk.BooleanVar(value=self.cfg[key])

        def changed():
            self.cfg[key] = bool(var.get())
            if extra:
                extra()
            self.save_soon()

        ttk.Checkbutton(parent, text=text, variable=var, command=changed).pack(anchor="w", pady=(6, 0))

    def _time_entry(self, parent, key):
        var = tk.StringVar(value=self.cfg[key])
        entry = ttk.Entry(parent, textvariable=var, width=6, justify="center")
        entry.pack(side="left", padx=(6, 0))

        def commit(_event=None):
            if parse_hhmm(var.get()):
                self.cfg[key] = normalize_hhmm(var.get(), self.cfg[key])
                self.save_soon()
            else:
                self.flash("Please enter a 24-hour time like 19:30.")
            var.set(self.cfg[key])

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)

    # -- actions ----------------------------------------------------------------------------
    def on_mode(self):
        self.cfg["mode"] = self.mode_var.get()
        self.save_soon()
        self.refresh()

    def on_monitor(self, names):
        self.cfg["monitor"] = names.index(self.monitor_var.get()) + 1
        self.save_soon()

    def on_autostart(self):
        try:
            set_autostart(self.cfg["start_with_windows"])
        except Exception as exc:
            self.flash(f"Could not change the start-up setting: {exc}")

    def toggle(self):
        self.cfg["enabled"] = not self.cfg["enabled"]
        self.save_soon()
        self.refresh()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.tray and self.cfg["minimize_to_tray"]:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        if self._closing:
            return
        self._closing = True
        if self._save_job:
            self.root.after_cancel(self._save_job)
        save_config(self.cfg)
        self.engine.stop()                    # also restores the original brightness
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # -- tray icon --------------------------------------------------------------------------
    def _start_tray(self):
        try:
            import pystray

            post = self.events.put
            modes = pystray.Menu(*[
                pystray.MenuItem(label, lambda value=value: post(("mode", value)),
                                 checked=lambda item, value=value: self.cfg["mode"] == value, radio=True)
                for label, value in (("Day", "day"), ("Night", "night"), ("Auto", "auto"))
            ])
            menu = pystray.Menu(
                pystray.MenuItem("Open", lambda: post("show"), default=True),
                pystray.MenuItem(lambda item: "Pause" if self.cfg["enabled"] else "Resume",
                                 lambda: post("toggle")),
                pystray.MenuItem("Mode", modes),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda: post("quit")),
            )
            self.tray = pystray.Icon("AdaptiveBrightness", make_icon(64), APP_NAME, menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None                  # no tray available: closing the window quits

    def _handle(self, event):
        if event == "show":
            self.show()
        elif event == "toggle":
            self.toggle()
        elif event == "quit":
            self.quit()
        elif isinstance(event, tuple) and event[0] == "mode":
            self.cfg["mode"] = event[1]
            self.save_soon()

    # -- periodic refresh -------------------------------------------------------------------
    def tick(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if self._closing:
            return
        self.refresh()
        self.root.after(self.TICK_MS, self.tick)

    def refresh(self):
        st = self.engine.status
        enabled = self.cfg["enabled"]
        profile = resolve_profile(self.cfg, datetime.now())

        self.toggle_btn.config(text="Pause" if enabled else "Resume")
        self.mode_var.set(self.cfg["mode"])

        def pct(value):
            return "-" if value is None else f"{round(value)}%"

        content = None if st.content is None else st.content * 100
        self.stat["content"].config(text=pct(content))
        self.stat["target"].config(text=pct(st.target))
        self.stat["screen"].config(text=pct(st.brightness))
        self.bar["value"] = content or 0

        for key, (frame, title) in self.profile_frames.items():
            frame.config(text=title + ("   (active)" if enabled and key == profile else ""))

        if enabled:
            suffix = " (chosen by the clock)" if self.cfg["mode"] == "auto" else ""
            self.state_lbl.config(text=f"Adjusting brightness using the {profile} levels{suffix}.")
        else:
            self.state_lbl.config(text="Paused - brightness is left alone.")

        self.msg.config(text=self._flash_text or st.error)


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    demo = "--demo" in argv or not IS_WIN
    hidden = "--minimized" in argv

    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on high-DPI screens
        except Exception:
            pass

    root = tk.Tk()
    if already_running():
        root.withdraw()
        messagebox.showinfo(APP_NAME, "Adaptive Brightness is already running - look for its icon in the system tray.")
        return

    cfg = load_config()
    cfg["enabled"] = True                      # always start active, even if it was paused last time
    if IS_WIN and cfg["start_with_windows"]:
        try:
            set_autostart(True)                # keep the registry entry pointing at this copy
        except Exception:
            pass

    backend = DemoBackend() if demo else RealBackend(cfg)
    engine = Engine(cfg, backend)
    app = App(root, cfg, engine, hidden=hidden, demo=demo)
    engine.start()
    root.mainloop()
    del app


if __name__ == "__main__":
    main()
