"""Smart Break - reliable desktop work/break reminder (Python 3.9+).
Run: python Break_Reminder.py
Optional tray: python -m pip install -r requirements.txt
Tests: python -m unittest discover tests
Windows executable:
python -m PyInstaller --onefile --windowed --clean --name SmartBreak --icon assets/smart_break.ico --add-data "assets;assets" --collect-submodules pystray --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder Break_Reminder.py
"""
import json
import math
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk, messagebox

NAME = "Smart Break"
VERSION = "1.1.0"
SYSTEM = platform.system()
CONFIG = Path.home() / ".break_reminder_config.json"
HISTORY = Path.home() / ".break_reminder_history.json"
LOCK = Path.home() / ".smart_break.lock"
REGKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULTS = dict(work_minutes=60, break_minutes=5, repeat=True,
                startup=False, sound="chime", close_action="ask", theme="system")
CLOSE_LABELS = {"ask": "Ask me every time",
                "tray": "Hide to system tray",
                "exit": "Exit the program"}
THEME_LABELS = {"light": "☀ Light", "dark": "☾ Dark", "system": "Auto"}
THEMES = {
    "light": dict(
        bg="#eef3f7", card="#ffffff", border="#d9e3eb", text="#16324f", muted="#5f7487",
        accent="#0f9d8a", accent_hover="#0b8474", accent_fg="#ffffff",
        button="#f1f5f8", button_hover="#e2eaf0", field="#ffffff", track="#e2ecf3",
        header="#16324f", header_text="#ffffff", header_sub="#b9d6e8",
        header_button="#244b73", header_hover="#2f5d8a",
        warn="#d97706", danger="#c0392b", dark=False),
    "dark": dict(
        bg="#0e1621", card="#17212e", border="#2a3a4d", text="#e6edf3", muted="#8fa3b6",
        accent="#14b8a6", accent_hover="#2dd4bf", accent_fg="#04201d",
        button="#213043", button_hover="#2b3e55", field="#0f1924", track="#24364a",
        header="#0a111a", header_text="#e6edf3", header_sub="#7fb3cf",
        header_button="#1c2b3c", header_hover="#2a3f57",
        warn="#f59e0b", danger="#e5534b", dark=True),
}
FONT = "Segoe UI" if SYSTEM == "Windows" else "Helvetica"
# assets/ sits next to this file, or inside the PyInstaller bundle (--add-data)
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
APP_ICON = ASSETS / "smart_break.ico"
TRAY_ICON = ASSETS / "smart_break.png"
HISTORY_LIMIT = 500          # entries kept on disk
SNOOZE_SECONDS = 300
MAX_SNOOZES = 2              # in a row, per break


def valid_minutes(value):
    number = float(value)
    if not math.isfinite(number) or not 0.05 <= number <= 1440:
        raise ValueError("Minutes must be between 0.05 and 1440.")
    return number


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def load_settings():
    result = DEFAULTS.copy()
    saved = read_json(CONFIG, {})
    if not isinstance(saved, dict):
        return result
    for key in ("work_minutes", "break_minutes"):
        try:
            result[key] = valid_minutes(saved.get(key, result[key]))
        except (ValueError, TypeError, OverflowError):
            pass
    for key in ("repeat", "startup"):
        if isinstance(saved.get(key), bool):
            result[key] = saved[key]
    if saved.get("sound") in ("off", "beep", "chime"):
        result["sound"] = saved["sound"]
    if saved.get("close_action") in CLOSE_LABELS:
        result["close_action"] = saved["close_action"]
    elif saved.get("close_to_tray") is True:      # migrate older settings file
        result["close_action"] = "tray"
    if saved.get("theme") in THEME_LABELS:
        result["theme"] = saved["theme"]
    return result


def load_history():
    raw = read_json(HISTORY, [])
    result = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        try:
            if isinstance(item, str):  # migrate earlier app history
                stamp, label = item.split(" - ", 1)
                stamp = datetime.strptime(stamp, "%d-%m-%Y %H:%M").isoformat()
                event = {"Break completed": "completed", "Break skipped": "skipped"}.get(label)
            elif isinstance(item, dict):
                stamp, event = item["timestamp"], item["event"]
                datetime.fromisoformat(stamp)
            else:
                continue
            if event in ("completed", "skipped", "snoozed"):
                result.append(dict(timestamp=stamp, event=event))
        except (KeyError, ValueError, TypeError):
            continue
    return result[:HISTORY_LIMIT]


def completed_today(history, today=None):
    today = today or datetime.now().date().isoformat()
    return sum(h["event"] == "completed" and h["timestamp"][:10] == today for h in history)


def system_prefers_dark():
    """True when Windows apps are set to dark mode."""
    if SYSTEM != "Windows":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def resolve_theme(choice, system_dark):
    """'system' follows Windows; returns 'light' or 'dark'."""
    if choice == "system":
        return "dark" if system_dark else "light"
    return choice


def windows_setup():
    """Sharp text on scaled (125% / 150%) displays, and our own taskbar icon
    instead of python.exe's when run from source."""
    if SYSTEM != "Windows":
        return
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SmartBreak.App")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def style_title_bar(window, dark):
    """Dark or light Windows title bar (Windows 10 20H1+ / 11; ignored elsewhere)."""
    if SYSTEM != "Windows":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE, and its pre-20H1 number
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                         ctypes.sizeof(value)) == 0:
                break
        # NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED: repaint the frame now
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x37)
    except Exception:
        pass


class InstanceLock:
    """OS-released file lock; no network or background server needed."""
    def __init__(self):
        self.file = None

    def acquire(self):
        self.file = LOCK.open("a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if SYSTEM == "Windows":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            self.file = None
            return False

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def startup_command():
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--autostart"]
    else:
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        args = [str(pythonw if pythonw.exists() else exe), str(Path(__file__).resolve()), "--autostart"]
    return subprocess.list2cmdline(args)


def startup_enabled():
    if SYSTEM != "Windows":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGKEY) as key:
            return winreg.QueryValueEx(key, "BreakReminder")[0] == startup_command()
    except OSError:
        return False


def set_startup(enabled):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGKEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "BreakReminder", 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, "BreakReminder")
            except FileNotFoundError:
                pass


def fmt(seconds):
    minutes, seconds = divmod(max(0, math.ceil(seconds)), 60)
    return f"{minutes:02d}:{seconds:02d}"


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.settings["startup"] = startup_enabled()
        self.history = load_history()
        self.state = "idle"
        self.phase = "work"
        self.remaining = self.settings["work_minutes"] * 60
        self.total = self.remaining
        self.break_total = self.settings["break_minutes"] * 60
        self.deadline = 0
        self.last_wall = time.time()
        self.overlay = None
        self.tray = None
        self.tray_thread = None
        self.tray_error = ""
        self.commands = queue.Queue()   # thread-safe messages from tray / sound threads
        self.closing = False
        self.notice_until = 0
        self.sound_busy = threading.Lock()
        self.tick_id = None
        self.snoozes = 0
        self.history_win = None
        self.history_box = None
        self.next_theme_check = 0
        self.system_dark = system_prefers_dark()
        self.c = THEMES[resolve_theme(self.settings["theme"], self.system_dark)]
        # 1.0 at 100% display scaling, 1.5 at 150%, ... (fonts scale on their own).
        # On short screens, shrink spacing so the window still fits above the taskbar.
        dpi_scale = max(1.0, root.winfo_fpixels("1i") / 96)
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        self.scale = max(0.8, min(dpi_scale, (screen_h - 120 * dpi_scale) / 730))

        # Form values live outside build() so a theme switch keeps unsaved typing.
        self.work_var = tk.StringVar(value=f"{self.settings['work_minutes']:g}")
        self.break_var = tk.StringVar(value=f"{self.settings['break_minutes']:g}")
        self.repeat_var = tk.BooleanVar(value=self.settings["repeat"])
        self.startup_var = tk.BooleanVar(value=self.settings["startup"])
        self.close_var = tk.StringVar(value=CLOSE_LABELS[self.settings["close_action"]])
        self.sound_var = tk.StringVar(value=self.settings["sound"])

        root.title(NAME)
        try:
            if SYSTEM == "Windows":
                root.iconbitmap(default=str(APP_ICON))
            else:
                root.iconphoto(True, tk.PhotoImage(file=str(TRAY_ICON)))
        except (tk.TclError, OSError):
            pass   # icon file missing; Tk keeps its default
        self.build()
        root.update_idletasks()   # size the window to fit its content, capped to the screen
        width = min(max(self.px(960), root.winfo_reqwidth()), screen_w - 40)
        height = min(max(self.px(730), root.winfo_reqheight()), screen_h - 80)
        root.geometry(f"{width}x{height}")
        root.minsize(min(self.px(860), width), min(self.px(690), height))
        self.setup_tray()
        root.protocol("WM_DELETE_WINDOW", self.close_window)
        root.bind("<Control-space>", lambda e: self.toggle())
        root.bind("<Control-s>", lambda e: self.save())
        root.bind("<Control-h>", lambda e: self.show_history())
        self.tick()
        if "--autostart" in sys.argv:
            root.after(600, self.autostart)

    def px(self, pixels):
        """Scale a 100%-display pixel size to the current display."""
        return round(pixels * self.scale)

    # ------------------------------------------------------------ theme
    def apply_style(self):
        c, px = self.c, self.px
        style = ttk.Style(self.root)
        style.theme_use("clam")
        flat = dict(bordercolor=c["border"], lightcolor=c["button"], darkcolor=c["button"])
        style.configure(".", background=c["card"], foreground=c["text"], font=(FONT, 11),
                        troughcolor=c["track"], focuscolor=c["accent"], insertcolor=c["text"])
        style.configure("TFrame", background=c["bg"])
        style.configure("Card.TFrame", background=c["card"], borderwidth=1, relief="solid",
                        bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"])
        style.configure("Panel.TFrame", background=c["card"])
        style.configure("TLabel", background=c["card"], foreground=c["text"], font=(FONT, 11))
        style.configure("Muted.TLabel", foreground=c["muted"])

        style.configure("TButton", background=c["button"], foreground=c["text"], font=(FONT, 11),
                        padding=(px(14), px(9)), relief="flat", **flat)
        style.map("TButton",
                  background=[("disabled", c["button"]), ("pressed", c["button_hover"]), ("active", c["button_hover"])],
                  lightcolor=[("active", c["button_hover"])], darkcolor=[("active", c["button_hover"])],
                  foreground=[("disabled", c["muted"])],
                  bordercolor=[("focus", c["accent"])])
        style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_fg"],
                        bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"],
                        font=(FONT, 11, "bold"))
        style.map("Accent.TButton",
                  background=[("disabled", c["track"]), ("pressed", c["accent_hover"]), ("active", c["accent_hover"])],
                  lightcolor=[("active", c["accent_hover"])], darkcolor=[("active", c["accent_hover"])],
                  foreground=[("disabled", c["muted"])],
                  bordercolor=[("focus", c["text"]), ("active", c["accent_hover"])])
        style.configure("Link.TButton", background=c["card"], foreground=c["accent"], font=(FONT, 10, "bold"),
                        padding=(px(6), px(2)), bordercolor=c["card"], lightcolor=c["card"], darkcolor=c["card"])
        style.map("Link.TButton", background=[("active", c["button"])], lightcolor=[("active", c["button"])],
                  darkcolor=[("active", c["button"])], bordercolor=[("focus", c["accent"]), ("active", c["button"])])

        style.configure("TCheckbutton", background=c["card"], foreground=c["text"], font=(FONT, 10), padding=4,
                        indicatorbackground=c["field"], indicatorforeground=c["accent"],
                        upperbordercolor=c["border"], lowerbordercolor=c["border"])
        style.map("TCheckbutton", background=[("active", c["card"])],
                  indicatorbackground=[("pressed", c["button_hover"]), ("selected", c["field"])])
        style.configure("TEntry", padding=px(7), fieldbackground=c["field"], foreground=c["text"],
                        bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"])
        style.map("TEntry", bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["accent"])])
        style.configure("TCombobox", padding=px(6), fieldbackground=c["field"], foreground=c["text"],
                        background=c["button"], arrowcolor=c["text"], **flat)
        style.map("TCombobox",
                  fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["text"])],
                  selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["text"])],
                  background=[("active", c["button_hover"])], bordercolor=[("focus", c["accent"])])
        # the combobox drop-down list is a plain Tk listbox
        for option, value in (("background", c["field"]), ("foreground", c["text"]),
                              ("selectBackground", c["accent"]), ("selectForeground", c["accent_fg"])):
            self.root.option_add(f"*TCombobox*Listbox.{option}", value)
        style.configure("Horizontal.TProgressbar", troughcolor=c["track"], background=c["accent"],
                        bordercolor=c["track"], lightcolor=c["accent"], darkcolor=c["accent"])
        style.configure("Vertical.TScrollbar", background=c["button"], troughcolor=c["card"], arrowcolor=c["muted"],
                        **flat)
        style.map("Vertical.TScrollbar", background=[("active", c["button_hover"])])
        style.configure("TSeparator", background=c["border"])

    def set_theme(self, choice):
        self.settings["theme"] = choice
        self.persist()
        self.rebuild()

    def rebuild(self):
        """Redraw every window in the current theme; timer state is untouched."""
        self.c = THEMES[resolve_theme(self.settings["theme"], self.system_dark)]
        history_open, overlay_open = self.history_win is not None, self.overlay is not None
        self.close_history()
        self.close_overlay()
        for child in self.root.winfo_children():
            child.destroy()
        self.build()
        self.render()
        style_title_bar(self.root, self.c["dark"])
        if overlay_open:
            self.show_overlay()
        if history_open:
            self.show_history()

    def header_button(self, parent, text, command, selected=False, danger=False):
        c = self.c
        base = c["accent"] if selected else c["header_button"]
        fg = c["accent_fg"] if selected else c["header_text"]
        hover = c["danger"] if danger else (c["accent_hover"] if selected else c["header_hover"])
        hover_fg = "#ffffff" if danger else fg
        button = tk.Button(parent, text=text, command=command, bg=base, fg=fg, font=(FONT, 10, "bold"),
                           activebackground=hover, activeforeground=hover_fg, relief="flat", bd=0,
                           highlightthickness=2, highlightbackground=parent["bg"], highlightcolor=c["accent"],
                           padx=self.px(14), pady=self.px(7), cursor="hand2")
        button.bind("<Enter>", lambda e: button.configure(bg=hover, fg=hover_fg))
        button.bind("<Leave>", lambda e: button.configure(bg=base, fg=fg))
        button.bind("<Return>", lambda e: command())
        return button

    # ------------------------------------------------------------ layout
    def build(self):
        c, px = self.c, self.px
        self.apply_style()
        self.root.configure(bg=c["bg"])

        header = tk.Frame(self.root, bg=c["header"], padx=px(26), pady=px(18))
        header.pack(fill="x")
        titles = tk.Frame(header, bg=c["header"])
        titles.pack(side="left")
        tk.Label(titles, text="Smart Break", bg=c["header"], fg=c["header_text"], font=(FONT, 24, "bold")).pack(anchor="w")
        tk.Label(titles, text="Focus well. Rest often. Feel better.", bg=c["header"], fg=c["header_sub"],
                 font=(FONT, 11)).pack(anchor="w", pady=(px(2), 0))

        self.header_button(header, "Exit", self.quit, danger=True).pack(side="right")
        self.header_button(header, "About", self.show_about).pack(side="right", padx=(0, px(8)))
        themes = tk.Frame(header, bg=c["header_button"], padx=px(3), pady=px(3))
        themes.pack(side="right", padx=(0, px(16)))
        for key, label in THEME_LABELS.items():
            self.header_button(themes, label, lambda k=key: self.set_theme(k),
                               selected=self.settings["theme"] == key).pack(side="left")

        body = ttk.Frame(self.root, padding=px(20))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body, style="Card.TFrame", padding=px(20))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(14)))
        right = ttk.Frame(body, style="Card.TFrame", padding=px(20))
        right.grid(row=0, column=1, sticky="nsew")

        self.canvas = tk.Canvas(left, width=px(300), height=px(300), bg=c["card"], highlightthickness=0)
        self.canvas.pack(pady=(px(8), px(12)))
        ring = (px(22), px(22), px(278), px(278))
        self.canvas.create_oval(*ring, outline=c["track"], width=px(14))
        self.arc = self.canvas.create_arc(*ring, start=90, extent=-359.9, style="arc", outline=c["accent"], width=px(14))
        self.timer_text = self.canvas.create_text(px(150), px(142), text="", fill=c["text"], font=(FONT, 42, "bold"))
        self.phase_text = self.canvas.create_text(px(150), px(188), text="READY", fill=c["muted"], font=(FONT, 11, "bold"))
        self.status = ttk.Label(left, text="", wraplength=px(300), anchor="center", justify="center")
        self.status.pack(fill="x", pady=10)
        buttons = ttk.Frame(left, style="Panel.TFrame")
        buttons.pack(fill="x", pady=10)
        self.start_button = ttk.Button(buttons, text="Start focus", style="Accent.TButton", command=self.toggle)
        self.start_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(buttons, text="Reset", command=self.reset).pack(side="left")
        self.stats = ttk.Label(left, style="Muted.TLabel", anchor="center")
        self.stats.pack(fill="x", pady=14)
        ttk.Label(left, text="Ctrl+Space to start or pause", style="Muted.TLabel", font=(FONT, 9)).pack(pady=5)

        ttk.Label(right, text="Your routine", font=(FONT, 16, "bold")).pack(anchor="w")
        presets = ttk.Frame(right, style="Panel.TFrame")
        presets.pack(fill="x", pady=(12, 8))
        for w, b in ((25, 5), (50, 10), (60, 5)):
            ttk.Button(presets, text=f"{w} / {b}", command=lambda w=w, b=b: self.preset(w, b)).pack(side="left", expand=True, fill="x", padx=2)
        for label, var in (("Work (minutes)", self.work_var), ("Break (minutes)", self.break_var)):
            row = ttk.Frame(right, style="Panel.TFrame")
            row.pack(fill="x", pady=7)
            ttk.Label(row, text=label).pack(side="left")
            entry = ttk.Entry(row, textvariable=var, width=9, justify="center")
            entry.pack(side="right")
            # Tk entries treat Ctrl+H as Backspace; open history without deleting a digit.
            entry.bind("<Control-h>", lambda e: (self.show_history(), "break")[1])
        ttk.Button(right, text="Save durations", command=self.save, style="Accent.TButton").pack(fill="x", pady=(7, 3))
        ttk.Label(right, text="Duration changes apply to the next focus cycle.", style="Muted.TLabel",
                  font=(FONT, 9), wraplength=px(330)).pack(anchor="w", pady=(0, 8))
        ttk.Checkbutton(right, text="Repeat work / break cycle", variable=self.repeat_var, command=self.preferences).pack(anchor="w")
        startup = ttk.Checkbutton(right, text="Start at Windows login", variable=self.startup_var, command=self.change_startup)
        startup.pack(anchor="w")
        if SYSTEM != "Windows":
            startup.configure(state="disabled")

        close_row = ttk.Frame(right, style="Panel.TFrame")
        close_row.pack(fill="x", pady=(8, 0))
        ttk.Label(close_row, text="When closing", font=(FONT, 10)).pack(side="left")
        close_combo = ttk.Combobox(close_row, textvariable=self.close_var, values=list(CLOSE_LABELS.values()),
                                   state="readonly", width=20)
        close_combo.pack(side="right")
        close_combo.bind("<<ComboboxSelected>>", lambda e: self.preferences())

        sound = ttk.Frame(right, style="Panel.TFrame")
        sound.pack(fill="x", pady=8)
        ttk.Label(sound, text="Sound").pack(side="left")
        combo = ttk.Combobox(sound, textvariable=self.sound_var, values=("off", "beep", "chime"), state="readonly", width=8)
        combo.pack(side="left", padx=10)
        combo.bind("<<ComboboxSelected>>", lambda e: self.preferences())
        ttk.Button(sound, text="Test", command=self.play_sound).pack(side="right")
        ttk.Separator(right).pack(fill="x", pady=8)
        recent_row = ttk.Frame(right, style="Panel.TFrame")
        recent_row.pack(fill="x")
        ttk.Label(recent_row, text="Recent activity", font=(FONT, 12, "bold")).pack(side="left")
        ttk.Button(recent_row, text="View all  ›", style="Link.TButton", command=self.show_history).pack(side="right")
        self.recent = ttk.Label(right, text="", style="Muted.TLabel", wraplength=px(330), font=(FONT, 10))
        self.recent.pack(anchor="w", pady=7)
        self.refresh_history()

    def persist(self):
        try:
            write_json(CONFIG, self.settings)
            return True
        except OSError as exc:
            messagebox.showwarning("Settings not saved", f"Changes work for this session only.\n\n{exc}", parent=self.root)
            return False

    def tray_missing_popup(self):
        messagebox.showinfo(
            NAME,
            "System tray support is missing.\n\n"
            f"Reason: {self.tray_error or 'unknown'}\n\n"
            f"Python used by this app:\n{sys.executable}\n\n"
            "Install it with:\n"
            f"\"{sys.executable}\" -m pip install --user pystray six pillow\n\n"
            "Then restart the app.",
            parent=self.root,
        )

    def preferences(self):
        label_to_key = {label: key for key, label in CLOSE_LABELS.items()}
        action = label_to_key.get(self.close_var.get(), "ask")
        if action == "tray" and not self.tray:
            action = "ask"
            self.close_var.set(CLOSE_LABELS["ask"])
            self.tray_missing_popup()
        self.settings.update(repeat=self.repeat_var.get(), sound=self.sound_var.get(),
                             close_action=action)
        self.persist()

    def change_startup(self):
        old = self.settings["startup"]
        try:
            set_startup(self.startup_var.get())
        except OSError as exc:
            self.startup_var.set(old)
            messagebox.showerror("Startup change failed", str(exc), parent=self.root)
            return
        self.settings["startup"] = self.startup_var.get()
        self.persist()

    def save(self, quiet=False):
        try:
            w, b = valid_minutes(self.work_var.get()), valid_minutes(self.break_var.get())
        except (ValueError, TypeError, OverflowError):
            messagebox.showerror("Invalid duration", "Enter finite minutes between 0.05 and 1440.", parent=self.root)
            return False
        self.settings.update(work_minutes=w, break_minutes=b)
        saved = self.persist()
        if self.state == "idle":
            self.remaining = w * 60
            self.total = self.remaining
        if saved and not quiet:
            self.notice("Durations saved. Active timer is unchanged." if self.state != "idle" else "Durations saved.")
        return True  # valid in-memory values remain usable if disk save fails

    def preset(self, work, brk):
        self.work_var.set(str(work))
        self.break_var.set(str(brk))
        self.save()

    def notice(self, text):
        self.notice_until = time.monotonic() + 4
        self.status.configure(text=text)

    # ------------------------------------------------------------ timer
    def start_work(self):
        self.phase = "work"
        self.total = self.settings["work_minutes"] * 60
        self.break_total = self.settings["break_minutes"] * 60
        self.remaining = self.total
        self.deadline = time.monotonic() + self.remaining
        self.last_wall = time.time()
        self.state = "work"

    def toggle(self):
        now = time.monotonic()
        if self.state == "idle":
            if not self.save(quiet=True):
                return
            self.start_work()
        elif self.state == "work":
            self.remaining = max(0, self.deadline - now)
            self.state = "paused"
        elif self.state == "paused":
            self.state = self.phase
            self.deadline = now + self.remaining
            self.last_wall = time.time()
            if self.phase == "break":
                self.show_overlay()
        self.notice_until = 0
        self.render()

    def reset(self):
        if self.state != "idle" and not messagebox.askyesno("Reset timer?", "Discard this timer and return to Ready?", parent=self.root):
            return
        self.close_overlay()
        self.go_idle()
        self.notice_until = 0
        self.render()

    def go_idle(self):
        self.state = "idle"
        self.phase = "work"
        self.snoozes = 0
        self.total = self.remaining = self.settings["work_minutes"] * 60

    def handle_commands(self):
        """Run messages sent from the tray / sound threads on the Tk thread."""
        while not self.closing:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            if command == "show":
                self.show_window()
            elif command == "toggle":
                self.toggle()
            elif command == "bell":
                self.root.bell()
            elif command == "quit":
                self.quit()

    def follow_system_theme(self):
        """In Auto mode, switch when Windows switches between light and dark."""
        now = time.monotonic()
        if self.settings["theme"] != "system" or now < self.next_theme_check:
            return
        self.next_theme_check = now + 3
        dark = system_prefers_dark()
        if dark != self.system_dark and self.root.grab_current() is None:   # not while a dialog is open
            self.system_dark = dark
            self.rebuild()

    def tick(self):
        if self.closing:
            return
        try:
            self.handle_commands()
            if self.closing:
                return
            self.follow_system_theme()
            wall, now = time.time(), time.monotonic()
            gap = wall - self.last_wall
            self.last_wall = wall
            if self.state in ("work", "break"):
                if gap > 30:  # computer slept or the app was frozen
                    self.wake_up(gap)
                else:
                    self.remaining = max(0, self.deadline - now)
                    if self.remaining <= 0:
                        if self.state == "work":
                            self.begin_break()
                        else:
                            self.finish_break("completed")
            self.render()
        finally:
            if not self.closing:
                self.tick_id = self.root.after(200, self.tick)

    def wake_up(self, gap):
        """Away at least a full break: that was rest, so start over. Shorter: pause."""
        self.close_overlay()
        if gap >= self.break_total:
            self.snoozes = 0
            if self.repeat_var.get():
                self.start_work()
                self.notice("Welcome back. A fresh focus cycle has started.")
            else:
                self.go_idle()
                self.notice("Welcome back. Start a focus cycle when ready.")
        else:
            # keep the last displayed balance
            self.phase = self.state
            self.state = "paused"
            self.show_window()
            self.notice("Interruption detected. Resume when ready.")

    def render(self):
        c = self.c
        fraction = min(1, max(0, self.remaining / max(1, self.total)))
        color = c["warn"] if self.state == "paused" else c["accent"]
        self.canvas.itemconfigure(self.arc, outline=color, extent=-359.9 * fraction)
        self.canvas.itemconfigure(self.timer_text, text=fmt(self.remaining))
        labels = {"idle": "READY", "work": "FOCUS", "paused": "PAUSED", "break": "RECHARGE"}
        self.canvas.itemconfigure(self.phase_text, text=labels[self.state])
        self.start_button.configure(text={"idle": "Start focus", "work": "Pause", "paused": "Resume", "break": "On break"}[self.state], state="disabled" if self.state == "break" else "normal")
        if time.monotonic() >= self.notice_until:
            text = {"idle": "Ready when you are.", "paused": "Paused. Resume when ready.", "break": "Look away, stretch and breathe."}.get(self.state)
            if self.state == "work":
                at = datetime.now() + timedelta(seconds=self.remaining)
                text = f"Next break at {at:%H:%M}"
            self.status.configure(text=text)
        count = completed_today(self.history)
        self.stats.configure(text=f"Today: {count} completed break{'s' if count != 1 else ''}")
        if self.overlay:
            self.break_label.configure(text=fmt(self.remaining))
            self.break_progress.configure(value=100 * fraction)

    def begin_break(self):
        self.state = self.phase = "break"
        self.total = self.break_total
        self.remaining = self.total
        self.deadline = time.monotonic() + self.total
        self.show_overlay()
        self.play_sound()

    # ------------------------------------------------------------ windows
    def place_window(self, win, over_root=False):
        """Center a pop-up on the main window (or the screen), style its title bar, raise it."""
        win.update_idletasks()
        width, height = win.winfo_reqwidth(), win.winfo_reqheight()
        if over_root and self.root.winfo_viewable():
            x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        else:
            x = (win.winfo_screenwidth() - width) // 2
            y = (win.winfo_screenheight() - height) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        style_title_bar(win, self.c["dark"])
        win.lift()

    def show_overlay(self):
        if self.overlay:
            return
        c = self.c
        ov = self.overlay = tk.Toplevel(self.root)
        ov.title("Time to recharge")
        ov.configure(bg=c["card"])
        ov.resizable(False, False)
        ov.attributes("-topmost", True)
        # X / Escape do nothing: a break should be snoozed or skipped on purpose.
        ov.protocol("WM_DELETE_WINDOW", lambda: None)
        frame = ttk.Frame(ov, style="Panel.TFrame", padding=self.px(34))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Time to recharge", font=(FONT, 25, "bold")).pack(pady=(8, 12))
        ttk.Label(frame, text="Look away from the screen.\nRelax your shoulders and breathe.", justify="center",
                  style="Muted.TLabel").pack()
        self.break_label = ttk.Label(frame, text=fmt(self.remaining), font=(FONT, 50, "bold"), foreground=c["accent"])
        self.break_label.pack(pady=20)
        self.break_progress = ttk.Progressbar(frame, maximum=100, value=100 * self.remaining / self.total, length=self.px(340))
        self.break_progress.pack(fill="x", pady=8)
        row = ttk.Frame(frame, style="Panel.TFrame")
        row.pack(pady=(20, 8))
        left = MAX_SNOOZES - self.snoozes
        snooze = ttk.Button(row, text=f"Snooze {SNOOZE_SECONDS // 60} min ({left} left)" if left else "No snoozes left",
                            command=self.snooze)
        snooze.pack(side="left", padx=5)
        if not left:
            snooze.state(["disabled"])
        ttk.Button(row, text="Skip break", command=lambda: self.finish_break("skipped")).pack(side="left", padx=5)
        self.place_window(ov)

    def close_overlay(self):
        if self.overlay:
            self.overlay.destroy()
            self.overlay = None

    def record(self, event):
        self.history.insert(0, dict(timestamp=datetime.now().isoformat(timespec="seconds"), event=event))
        del self.history[HISTORY_LIMIT:]
        try:
            write_json(HISTORY, self.history)
        except OSError:
            self.notice("Activity could not be saved to disk.")
        self.refresh_history()

    def snooze(self):
        if self.state != "break" or self.snoozes >= MAX_SNOOZES:
            return
        self.snoozes += 1
        self.close_overlay()
        self.record("snoozed")
        self.state = self.phase = "work"
        self.total = self.remaining = SNOOZE_SECONDS
        self.deadline = time.monotonic() + SNOOZE_SECONDS
        self.last_wall = time.time()
        # Preserve the planned break duration; snooze is not a fresh work cycle.
        self.render()

    def finish_break(self, event):
        if self.state != "break":
            return
        self.close_overlay()
        self.record(event)
        self.snoozes = 0
        if event == "completed":
            self.play_sound()
        if self.repeat_var.get():
            self.start_work()
        else:
            self.go_idle()
        self.render()

    def history_line(self, entry):
        stamp = datetime.fromisoformat(entry["timestamp"])
        return f"{stamp:%d %b, %H:%M}  —  Break {entry['event']}"

    def refresh_history(self):
        self.recent.configure(text="\n".join(self.history_line(h) for h in self.history[:3]) or "Your first break starts a healthier habit.")
        if self.history_box:
            box = self.history_box
            box.delete(0, "end")
            for h in self.history[:200]:
                box.insert("end", self.history_line(h))
            if not self.history:
                box.insert("end", "No activity yet.")

    def show_history(self):
        if self.history_win:   # one history window; bring it forward
            self.history_win.deiconify()
            self.history_win.lift()
            self.history_win.focus_force()
            return
        c = self.c
        win = self.history_win = tk.Toplevel(self.root)
        win.title("Activity history — latest 200")
        win.configure(bg=c["card"])
        win.geometry(f"{self.px(540)}x{self.px(400)}")
        frame = ttk.Frame(win, style="Panel.TFrame", padding=self.px(15))
        frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(frame)
        scroll.pack(side="right", fill="y")
        box = self.history_box = tk.Listbox(frame, font=(FONT, 11), relief="flat", bd=0, highlightthickness=0,
                                            bg=c["card"], fg=c["text"], selectbackground=c["accent"],
                                            selectforeground=c["accent_fg"], activestyle="none",
                                            yscrollcommand=scroll.set)
        box.pack(fill="both", expand=True)
        scroll.configure(command=box.yview)
        win.protocol("WM_DELETE_WINDOW", self.close_history)
        win.bind("<Escape>", lambda e: self.close_history())
        self.refresh_history()
        style_title_bar(win, c["dark"])

    def close_history(self):
        if self.history_win:
            self.history_win.destroy()
            self.history_win = self.history_box = None

    def show_about(self):
        dlg = tk.Toplevel(self.root)
        dlg.title(f"About {NAME}")
        dlg.configure(bg=self.c["card"])
        dlg.resizable(False, False)
        dlg.transient(self.root)
        frame = ttk.Frame(dlg, style="Panel.TFrame", padding=self.px(26))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=NAME, font=(FONT, 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text=f"Version {VERSION}  ·  Original app by Amanullah", style="Muted.TLabel",
                  font=(FONT, 10)).pack(anchor="w", pady=(2, 14))
        keys = ttk.Frame(frame, style="Panel.TFrame")
        keys.pack(anchor="w")
        for row, (key, action) in enumerate((("Ctrl+Space", "start / pause"), ("Ctrl+S", "save durations"),
                                             ("Ctrl+H", "history"))):
            ttk.Label(keys, text=key, font=(FONT, 10, "bold")).grid(row=row, column=0, sticky="w", padx=(0, self.px(18)))
            ttk.Label(keys, text=action, font=(FONT, 10)).grid(row=row, column=1, sticky="w")
        ttk.Label(frame, text="A wellness reminder, not medical advice.", style="Muted.TLabel",
                  font=(FONT, 9)).pack(anchor="w", pady=(14, 14))
        row = ttk.Frame(frame, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="Developer GitHub", command=lambda: webbrowser.open("https://github.com/amanullah-rao")).pack(side="left")
        close = ttk.Button(row, text="Close", style="Accent.TButton", command=dlg.destroy)
        close.pack(side="right", padx=(self.px(10), 0))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self.place_window(dlg, over_root=True)
        close.focus_set()
        try:
            dlg.grab_set()
        except tk.TclError:
            pass

    def play_sound(self):
        sound = self.sound_var.get()
        if sound == "off" or not self.sound_busy.acquire(blocking=False):
            return

        def worker():
            try:
                if SYSTEM == "Windows":
                    import winsound
                    winsound.Beep(880 if sound == "chime" else 1000, 220)
                    if sound == "chime":
                        winsound.Beep(1175, 260)
                elif SYSTEM == "Darwin":
                    subprocess.run(
                        ["afplay", "/System/Library/Sounds/" + ("Glass.aiff" if sound == "chime" else "Ping.aiff")],
                        check=True, timeout=6, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    filename = "complete.oga" if sound == "chime" else "bell.oga"
                    subprocess.run(
                        ["paplay", "/usr/share/sounds/freedesktop/stereo/" + filename],
                        check=True, timeout=6, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.commands.put("bell")
            finally:
                self.sound_busy.release()

        threading.Thread(target=worker, daemon=True).start()

    def setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            try:
                image = Image.open(TRAY_ICON)
            except OSError:   # fallback: simple drawn clock
                image = Image.new("RGB", (64, 64), THEMES["light"]["header"])
                draw = ImageDraw.Draw(image)
                draw.ellipse((9, 9, 55, 55), outline=THEMES["light"]["accent"], width=6)
                draw.line((32, 18, 32, 33, 43, 39), fill="white", width=4)

            def make_action(name):
                def action(icon, item):
                    self.commands.put(name)   # handled safely on the Tk thread
                return action

            menu = pystray.Menu(
                pystray.MenuItem("Show Smart Break", make_action("show"), default=True),
                pystray.MenuItem("Start / Pause / Resume", make_action("toggle")),
                pystray.MenuItem("Exit", make_action("quit")),
            )
            self.tray = pystray.Icon("smart_break", image, NAME, menu)

            def tray_loop():
                try:
                    self.tray.run()
                except Exception:
                    pass

            self.tray_thread = threading.Thread(target=tray_loop, daemon=True)
            self.tray_thread.start()
        except Exception as exc:
            self.tray_error = f"{type(exc).__name__}: {exc}"
            if self.tray:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.tray = None
            self.tray_thread = None

    def show_window(self):
        if self.closing:
            return
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def autostart(self):
        self.toggle()
        if self.tray and self.settings["close_action"] == "tray":
            self.root.withdraw()
        else:
            self.root.iconify()

    # ------------------------------------------------------------ closing
    def ask_close_action(self):
        """Popup shown when the user closes the window. Returns (choice, remember)."""
        result = {"choice": None, "remember": False}
        dlg = tk.Toplevel(self.root)
        dlg.title("Close Smart Break?")
        dlg.configure(bg=self.c["card"])
        dlg.resizable(False, False)
        dlg.transient(self.root)
        remember = tk.BooleanVar(value=False)

        def choose(choice):
            result["choice"] = choice
            result["remember"] = remember.get()
            dlg.destroy()

        frame = ttk.Frame(dlg, style="Panel.TFrame", padding=self.px(26))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Close Smart Break?", font=(FONT, 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Hide it to the system tray so your reminders\nkeep running, or exit the program completely.",
                  style="Muted.TLabel", font=(FONT, 10), justify="left").pack(anchor="w", pady=(6, 14))

        tray_button = ttk.Button(frame, text="Hide to system tray", style="Accent.TButton",
                                 command=lambda: choose("tray"))
        tray_button.pack(fill="x", pady=3)
        if not self.tray:
            tray_button.state(["disabled"])
            ttk.Label(frame, text="System tray is not available in this install.",
                      style="Muted.TLabel", font=(FONT, 9)).pack(anchor="w")
        ttk.Button(frame, text="Exit completely", command=lambda: choose("exit")).pack(fill="x", pady=3)
        ttk.Button(frame, text="Cancel", command=lambda: choose(None)).pack(fill="x", pady=3)
        ttk.Checkbutton(frame, text="Remember my choice", variable=remember).pack(anchor="w", pady=(10, 0))

        dlg.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        dlg.bind("<Escape>", lambda e: choose(None))
        self.place_window(dlg, over_root=True)
        dlg.focus_force()
        try:
            dlg.grab_set()
        except tk.TclError:
            pass
        self.root.wait_window(dlg)
        return result["choice"], result["remember"]

    def close_window(self):
        """Called when the user clicks the window's X button."""
        action = self.settings.get("close_action", "ask")
        if action == "tray" and not self.tray:
            action = "ask"          # tray not available, so ask instead
        if action == "ask":
            choice, remember = self.ask_close_action()
            if self.closing or choice is None:
                return              # cancelled
            if remember:
                self.settings["close_action"] = choice
                self.close_var.set(CLOSE_LABELS[choice])
                self.persist()
        else:
            choice = action

        if choice == "tray":
            self.root.withdraw()
        else:
            # No second question if the user just clicked "Exit completely".
            self.quit(confirm=(action == "exit"))

    def quit(self, confirm=True):
        if confirm and self.state != "idle":
            self.show_window()   # make sure the question is visible even if hidden to tray
            if not messagebox.askyesno("Exit Smart Break?", "The current timer will stop. Exit?", parent=self.root):
                return
        self.closing = True
        if self.tick_id:
            self.root.after_cancel(self.tick_id)
        self.close_overlay()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.tray_thread = None
        self.root.destroy()


def main():
    windows_setup()
    lock = InstanceLock()
    root = tk.Tk()
    root.withdraw()
    try:
        if not lock.acquire():
            messagebox.showinfo(NAME, "Smart Break is already running. Check its window or system tray.", parent=root)
            root.destroy()
            return
        app = App(root)
        root.deiconify()
        style_title_bar(root, app.c["dark"])
        root.mainloop()
    finally:
        lock.close()


if __name__ == "__main__":
    main()
