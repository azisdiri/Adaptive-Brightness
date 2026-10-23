# Adaptive Brightness

A small Windows tray app that watches what is on your screen and sets the laptop brightness to match:
bright content (white pages, light themes) lowers the brightness, dark content raises it.

## Run it

1. Install Python 3.10+ from python.org (tick "Add python.exe to PATH").
2. In this folder: `pip install -r requirements.txt`
3. `python app.py`  (add `--minimized` to start hidden in the tray)

Want to look at the interface without touching your real screen? `python app.py --demo` uses a fake
screen and fake brightness and works on any OS.

## Make a single .exe to share

Double-click **build.bat**. When it finishes, `dist\AdaptiveBrightness.exe` is a standalone program
that needs no Python on the other person's PC.

## How it works

Four times a second the app grabs a frame of the chosen display in memory (nothing is written to disk),
averages its perceived brightness, and maps that to a screen brightness between your two levels:

- content at or below ~8% brightness counts as fully **dark** -> your *Dark content* level
- content at or above ~85% counts as fully **white** -> your *White / light content* level
- anything in between is blended linearly

Because the capture reads what the computer is drawing, changing the backlight never changes the
reading, so there is no feedback loop.

## Settings

**Brightness tab**
- **Mode** - *Day*, *Night*, or *Auto*, which switches to Night between the two times you set
  (works across midnight, e.g. 19:00 to 07:00).
- **Day / Night mode** - the screen brightness you want for white content and for dark content in each mode.

**Settings tab**
- **Change threshold** - brightness changes smaller than this are ignored, so it doesn't twitch.
  Once a change starts, it finishes smoothly.
- **Smoothing** - how many seconds of screen content are averaged. Higher = calmer, slower to react.
- **Transition speed** - the biggest step per update (4 updates a second). Lower = gentler fades.
- **Display to watch** - which monitor's content is measured.
- **Control the built-in display only** - leave this on for laptops. Untick it to also try external
  monitors (they need DDC/CI enabled in the monitor's own menu).
- **Start with Windows**, **Keep running in the tray**, **Restore the original brightness on exit**.

Closing the window keeps the app running in the tray (right-click the icon for Pause/Resume, Mode, Quit).
Settings are saved in `%APPDATA%\AdaptiveBrightness\config.json`.

## Troubleshooting

- **"No adjustable display found"** - the laptop panel doesn't expose brightness control to Windows, or
  this is a desktop PC. Try unticking *Control the built-in display only*.
- **Brightness keys fight the app** - if you change brightness by hand, the app takes over again once
  the screen content changes enough. Use *Pause* in the tray while you want manual control.
- **Black reading on some video** - protected (DRM) video can appear black to screen capture, so
  the app will treat it as dark content.

## Sharing notes

- Windows SmartScreen may warn "Windows protected your PC" for an unsigned .exe: click *More info*,
  then *Run anyway*. Some antivirus programs also flag any PyInstaller one-file .exe. Sharing the
  source folder and `build.bat` (or code-signing the .exe) avoids this.
- Only one copy runs at a time.

## Tests

`python -m unittest discover -s tests` runs the logic and engine tests (no Windows needed).
