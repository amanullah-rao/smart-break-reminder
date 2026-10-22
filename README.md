# Smart Break

A simple desktop reminder that helps you rest your eyes. Set a work time (for
example 60 minutes). When it ends, a pop-up appears on top of everything with a
sound and a countdown telling you to take a short break (for example 5 minutes).

## Features

- Circular countdown timer with Start / Pause / Resume / Reset
- Break pop-up that stays on top of your work, with sound and progress bar
- Snooze a break for 5 minutes (up to 2 times in a row) or skip it
- Quick presets: 25/5, 50/10, 60/5, plus custom durations
- Hide to system tray, or exit completely (choose when closing, or remember it)
- Start automatically when Windows opens
- Handles sleep: if the computer was away for at least a break's length, a fresh
  focus cycle starts; a shorter interruption pauses the timer
- Light, Dark and Auto themes (Auto follows the Windows setting, including the title bar)
- Sharp on high-DPI (125% / 150%) displays
- Break history (latest 500 kept) and "breaks completed today" counter
- Keyboard shortcuts: `Ctrl+Space` start/pause, `Ctrl+S` save, `Ctrl+H` history

## Download (Windows)

Get `SmartBreak.exe` from the [Releases](../../releases) page and double-click it.
No Python needed.

## Run from source

Requires Python 3.9+.

```bash
pip install -r requirements.txt
python Break_Reminder.py
```

`pystray` and `pillow` are optional. Without them the app still works, but the
system tray option is disabled.

## Run the tests

```bash
python -m unittest discover tests
```

## Build the .exe yourself

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --clean --name SmartBreak ^
  --icon assets/smart_break.ico --add-data "assets;assets" ^
  --collect-submodules pystray --hidden-import pystray._win32 ^
  --hidden-import PIL._tkinter_finder Break_Reminder.py
```

The executable will be in the `dist` folder.

The icon lives in `assets/`. To change it, edit and run `python tools/make_icon.py`.

## Settings location

Settings and history are saved in your user folder:
`.break_reminder_config.json` and `.break_reminder_history.json`.

## License

MIT. See [LICENSE](LICENSE).

*Smart Break is a wellness reminder, not medical advice.*
