# K's Macro Launcher

![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey.svg)

## Features

- **Vision-based minigame control** - follows the fish line with a PD controller
- **Smart bait selection** - uses the best bait in stock (default Rare, then Legendary, then Common)
- **Auto-buy bait** - on a loop interval, and right away when every bait tier has run out
- **Devil fruit tracking** - counts drops from GPO's drop notice, split by rarity, with a session history
- **Auto-store devil fruits** - to storage or the backpack; duplicates are kept unless you opt in to dropping them
- **Anti-macro detection** - handles black screens automatically
- **Megalodon sound detection** - optional
- **Real-time stats** - dashboard and an optional overlay: fish, fruits, time, fish/hour
- **Discord webhook** notifications

## Installation
```bash
git clone https://github.com/K3nD4rk-Code-Developer/Grand-Piece-Online-Fishing.git
cd gpo-fishing-macro
pip install -r requirements.txt
```

## Usage

1. Run the app:
```bash
   npm run tauri dev -- --no-watch
```

In dev builds the app runs `src-tauri/backend.py` directly, so after editing it just restart the app.
`--no-watch` stops Tauri from restarting the app whenever the backend writes its settings or logs.

Start and stop fishing with `F1` or the Start button in the sidebar.

## Hotkeys

- `F1` - Start/Stop
- `F3` - Exit

You can rebind these in the UI.

## Configuration

The bot auto-saves to `Auto Fish Settings.json`. Everything is configurable through the interface.

## Requirements
```
flask
flask-cors
keyboard
mss
numpy
pyautogui
pynput
pywin32
```

Windows-only packages (`pywin32`, `keyboard`, `PyAudioWPatch`) and macOS-only ones (`pyobjc-*`) are marked in
`requirements.txt`, so `pip install -r requirements.txt` picks the right set.

## macOS

Windows is unchanged; macOS runs the same macro with platform equivalents (Quartz for mouse input, pynput for keys
and hotkeys, AppKit to focus Roblox).

- **Permissions** - in System Settings > Privacy & Security, allow the app (or your terminal, in dev) under
  **Accessibility**, **Input Monitoring** and **Screen Recording**, then restart it. The sidebar shows
  "Input Access Granted" once Accessibility and Input Monitoring are both allowed.
- **Tkinter** - region selectors use Tk. python.org and python-build-standalone builds include it; with Homebrew,
  `brew install python-tk`.
- **Megalodon sound** - macOS can't record app audio directly. Install [BlackHole](https://github.com/ExistentialAudio/BlackHole),
  create a Multi-Output Device (speakers + BlackHole) in Audio MIDI Setup and use it as the output. The macro finds
  BlackHole automatically, or pick the input in the audio device list.
- **Hotkeys** - `F1`/`F3` may need `fn` held, depending on your keyboard settings. You can rebind them.
- **Release builds** - set `GPO_PYTHON_DIR` to a relocatable Python 3.14 with the requirements installed (e.g. a
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone) build), then `npm run tauri build`.
  This produces a `.app` and `.dmg`. The app is unsigned, so open it the first time with right-click > Open.
  Settings and logs live in `~/Library/Application Support/com.gpo.ksmacro`.

## License

Personal Use LC - Do whatever you want with it, just credit. Don't publish or release partially modified verisons though.