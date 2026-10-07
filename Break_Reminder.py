"""Smart Break - reliable desktop work/break reminder (Python 3.9+).
Run: py -3.13 Break_Reminder.py
Optional tray: py -3.13 -m pip install --user pystray six pillow
Windows executable:
py -3.13 -m PyInstaller --onefile --windowed --clean --name SmartBreak --collect-submodules pystray --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder Break_Reminder.py
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
SYSTEM = platform.system()
CONFIG = Path.home() / ".break_reminder_config.json"
HISTORY = Path.home() / ".break_reminder_history.json"
LOCK = Path.home() / ".smart_break.lock"
REGKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULTS = dict(work_minutes=60, break_minutes=5, repeat=True,
                startup=False, sound="chime", close_action="ask")
CLOSE_LABELS = {"ask": "Ask me every time",
                "tray": "Hide to system tray",
                "exit": "Exit the program"}
BG, CARD, NAVY, TEAL, MUTED = "#eef4f8", "#ffffff", "#16324f", "#0f9d8a", "#60758a"
FONT = "Segoe UI" if SYSTEM == "Windows" else "Helvetica"
APP_ICON = Path.home() / ".smart_break_icon.ico"


def ensure_app_icon(path=APP_ICON):
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    if path.exists():
        return path

    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((16, 16, 240, 240), radius=38, fill=(22, 50, 79, 255), outline=(255, 255, 255, 160), width=6)
    draw.ellipse((48, 48, 208, 208), fill=(15, 157, 138, 255), outline=(255, 255, 255, 220), width=8)
    draw.rounded_rectangle((96, 84, 160, 172), radius=18, fill=(255, 255, 255, 245), outline=(255, 255, 255, 245), width=4)
    draw.line((128, 96, 128, 146), fill=(15, 157, 138, 255), width=12)
    draw.line((128, 146, 154, 146), fill=(15, 157, 138, 255), width=12)
    draw.arc((104, 104, 152, 152), start=220, end=360, fill=(15, 157, 138, 255), width=10)

    try:
        image.save(path, format="ICO")
        return path
    except Exception:
        return None


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
    return result


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
        self.icon_path = ensure_app_icon()
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

        root.title(NAME)
        if self.icon_path and self.icon_path.exists():
            try:
                root.iconbitmap(default=str(self.icon_path))
            except Exception:
                pass
        root.geometry("960x730")
        root.minsize(860, 690)
        root.configure(bg=BG)
        self.build()
        self.setup_tray()
        root.protocol("WM_DELETE_WINDOW", self.close_window)
        root.bind("<Control-space>", lambda e: self.toggle())
        root.bind("<Control-s>", lambda e: self.save())
        root.bind("<Control-h>", lambda e: self.show_history())
        self.tick()
        if "--autostart" in sys.argv:
            root.after(600, self.autostart)

    def build(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=CARD, foreground=NAVY, font=(FONT, 11))
        style.configure("TButton", font=(FONT, 11), padding=(14, 10))
        style.configure("Accent.TButton", background=TEAL, foreground="white")
        style.map("Accent.TButton", background=[("active", "#087e70"), ("disabled", "#9eafb9")])
        style.configure("TCheckbutton", background=CARD, foreground=NAVY, font=(FONT, 10), padding=4)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("TEntry", padding=7)
        style.configure("TCombobox", padding=6)

        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        for label, command in (("Start / Pause    Ctrl+Space", self.toggle),
                               ("Reset", self.reset), ("Save settings    Ctrl+S", self.save),
                               ("Exit", self.quit)):
            file_menu.add_command(label=label, command=command)
        menu.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Activity history    Ctrl+H", command=self.show_history)
        help_menu.add_command(label="Developer GitHub", command=lambda: webbrowser.open("https://github.com/amanullah-rao"))
        help_menu.add_command(label="About", command=lambda: messagebox.showinfo(NAME,
            "Smart Break 2.0\nOriginal app by Amanullah\n\nCtrl+Space: start / pause\nCtrl+S: save durations\nCtrl+H: history\n\nA wellness reminder, not medical advice.", parent=self.root))
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menu)

        header = tk.Frame(self.root, bg=NAVY, padx=26, pady=20)
        header.pack(fill="x")
        titles = tk.Frame(header, bg=NAVY)
        titles.pack(side="left")
        tk.Label(titles, text="Smart Break", bg=NAVY, fg="white", font=(FONT, 25, "bold")).pack(anchor="w")
        tk.Label(titles, text="Focus well. Rest often. Feel better.", bg=NAVY, fg="#cce7f4", font=(FONT, 11)).pack(anchor="w", pady=(4, 0))
        exit_button = tk.Label(header, text="Exit", bg="#244b73", fg="white", font=(FONT, 11, "bold"),
                               padx=22, pady=9, cursor="hand2")
        exit_button.pack(side="right")
        exit_button.bind("<Enter>", lambda e: exit_button.configure(bg="#c0392b"))
        exit_button.bind("<Leave>", lambda e: exit_button.configure(bg="#244b73"))
        exit_button.bind("<Button-1>", lambda e: self.quit())

        body = ttk.Frame(self.root, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body, style="Card.TFrame", padding=20)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        right = ttk.Frame(body, style="Card.TFrame", padding=20)
        right.grid(row=0, column=1, sticky="nsew")

        self.canvas = tk.Canvas(left, width=300, height=300, bg=CARD, highlightthickness=0)
        self.canvas.pack(pady=(8, 12))
        self.canvas.create_oval(22, 22, 278, 278, outline="#e2ecf3", width=14)
        self.arc = self.canvas.create_arc(22, 22, 278, 278, start=90, extent=-359.9, style="arc", outline=TEAL, width=14)
        self.timer_text = self.canvas.create_text(150, 142, text="", fill=NAVY, font=(FONT, 42, "bold"))
        self.phase_text = self.canvas.create_text(150, 188, text="READY", fill=MUTED, font=(FONT, 11, "bold"))
        self.status = ttk.Label(left, text="", wraplength=300, anchor="center", justify="center")
        self.status.pack(fill="x", pady=10)
        buttons = ttk.Frame(left, style="Card.TFrame")
        buttons.pack(fill="x", pady=10)
        self.start_button = ttk.Button(buttons, text="Start focus", style="Accent.TButton", command=self.toggle)
        self.start_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(buttons, text="Reset", command=self.reset).pack(side="left")
        self.stats = ttk.Label(left, foreground=MUTED, anchor="center")
        self.stats.pack(fill="x", pady=14)
        ttk.Label(left, text="Ctrl+Space to start or pause", foreground=MUTED, font=(FONT, 9)).pack(pady=5)

        ttk.Label(right, text="Your routine", font=(FONT, 16, "bold")).pack(anchor="w")
        presets = ttk.Frame(right, style="Card.TFrame")
        presets.pack(fill="x", pady=(12, 8))
        for w, b in ((25, 5), (50, 10), (60, 5)):
            ttk.Button(presets, text=f"{w} / {b}", command=lambda w=w, b=b: self.preset(w, b)).pack(side="left", expand=True, fill="x", padx=2)
        self.work_var = tk.StringVar(value=f"{self.settings['work_minutes']:g}")
        self.break_var = tk.StringVar(value=f"{self.settings['break_minutes']:g}")
        for label, var in (("Work (minutes)", self.work_var), ("Break (minutes)", self.break_var)):
            row = ttk.Frame(right, style="Card.TFrame")
            row.pack(fill="x", pady=7)
            ttk.Label(row, text=label).pack(side="left")
            ttk.Entry(row, textvariable=var, width=9, justify="center").pack(side="right")
        ttk.Button(right, text="Save durations", command=self.save, style="Accent.TButton").pack(fill="x", pady=(7, 3))
        ttk.Label(right, text="Duration changes apply to the next focus cycle.", font=(FONT, 9), foreground=MUTED, wraplength=330).pack(anchor="w", pady=(0, 8))
        self.repeat_var = tk.BooleanVar(value=self.settings["repeat"])
        self.startup_var = tk.BooleanVar(value=self.settings["startup"])
        ttk.Checkbutton(right, text="Repeat work / break cycle", variable=self.repeat_var, command=self.preferences).pack(anchor="w")
        startup = ttk.Checkbutton(right, text="Start at Windows login", variable=self.startup_var, command=self.change_startup)
        startup.pack(anchor="w")
        if SYSTEM != "Windows":
            startup.configure(state="disabled")

        close_row = ttk.Frame(right, style="Card.TFrame")
        close_row.pack(fill="x", pady=(8, 0))
        ttk.Label(close_row, text="When closing", font=(FONT, 10)).pack(side="left")
        self.close_var = tk.StringVar(value=CLOSE_LABELS[self.settings["close_action"]])
        close_combo = ttk.Combobox(close_row, textvariable=self.close_var, values=list(CLOSE_LABELS.values()),
                                   state="readonly", width=20)
        close_combo.pack(side="right")
        close_combo.bind("<<ComboboxSelected>>", lambda e: self.preferences())

        sound = ttk.Frame(right, style="Card.TFrame")
        sound.pack(fill="x", pady=8)
        ttk.Label(sound, text="Sound").pack(side="left")
        self.sound_var = tk.StringVar(value=self.settings["sound"])
        combo = ttk.Combobox(sound, textvariable=self.sound_var, values=("off", "beep", "chime"), state="readonly", width=8)
        combo.pack(side="left", padx=10)
        combo.bind("<<ComboboxSelected>>", lambda e: self.preferences())
        ttk.Button(sound, text="Test", command=self.play_sound).pack(side="right")
        ttk.Separator(right).pack(fill="x", pady=8)
        ttk.Label(right, text="Recent activity", font=(FONT, 12, "bold")).pack(anchor="w")
        self.recent = ttk.Label(right, text="", foreground=MUTED, wraplength=330, font=(FONT, 10))
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
        self.state = "idle"
        self.phase = "work"
        self.remaining = self.settings["work_minutes"] * 60
        self.total = self.remaining
        self.notice_until = 0
        self.render()

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

    def tick(self):
        if self.closing:
            return
        try:
            self.handle_commands()
            if self.closing:
                return
            wall, now = time.time(), time.monotonic()
            gap = wall - self.last_wall
            self.last_wall = wall
            if self.state in ("work", "break"):
                if gap > 30:  # preserve last displayed balance after sleep / long interruption
                    self.phase = self.state
                    self.state = "paused"
                    self.close_overlay()
                    self.show_window()
                    self.notice("Long interruption detected. Resume when ready.")
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

    def render(self):
        fraction = min(1, max(0, self.remaining / max(1, self.total)))
        color = "#f59e0b" if self.state == "paused" else TEAL
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
        today = datetime.now().date().isoformat()
        count = sum(h["event"] == "completed" and h["timestamp"][:10] == today for h in self.history)
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

    def show_overlay(self):
        if self.overlay:
            return
        ov = self.overlay = tk.Toplevel(self.root)
        ov.title("Time to recharge")
        ov.configure(bg=CARD)
        ov.resizable(False, False)
        ov.attributes("-topmost", True)
        ov.protocol("WM_DELETE_WINDOW", self.snooze)
        frame = ttk.Frame(ov, style="Card.TFrame", padding=30)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Time to recharge", font=(FONT, 25, "bold")).pack(pady=(8, 12))
        ttk.Label(frame, text="Look away from the screen.\nRelax your shoulders and breathe.", justify="center", foreground=MUTED).pack()
        self.break_label = ttk.Label(frame, text=fmt(self.remaining), font=(FONT, 50, "bold"), foreground=TEAL)
        self.break_label.pack(pady=20)
        self.break_progress = ttk.Progressbar(frame, maximum=100, value=100 * self.remaining / self.total, length=340)
        self.break_progress.pack(fill="x", pady=8)
        row = ttk.Frame(frame, style="Card.TFrame")
        row.pack(pady=(20, 8))
        ttk.Button(row, text="Snooze 5 min", command=self.snooze).pack(side="left", padx=5)
        ttk.Button(row, text="Skip break", command=lambda: self.finish_break("skipped")).pack(side="left", padx=5)
        ov.bind("<Escape>", lambda e: self.snooze())
        ov.update_idletasks()
        width, height = ov.winfo_reqwidth(), ov.winfo_reqheight()
        x = max(0, (ov.winfo_screenwidth() - width) // 2)
        y = max(0, (ov.winfo_screenheight() - height) // 2)
        ov.geometry(f"+{x}+{y}")
        ov.lift()

    def close_overlay(self):
        if self.overlay:
            self.overlay.destroy()
            self.overlay = None

    def record(self, event):
        self.history.insert(0, dict(timestamp=datetime.now().isoformat(timespec="seconds"), event=event))
        try:
            write_json(HISTORY, self.history)
        except OSError:
            self.notice("Activity could not be saved to disk.")
        self.refresh_history()

    def snooze(self):
        if self.state != "break":
            return
        self.close_overlay()
        self.record("snoozed")
        self.state = self.phase = "work"
        self.total = self.remaining = 300
        self.deadline = time.monotonic() + 300
        self.last_wall = time.time()
        # Preserve the planned break duration; snooze is not a fresh work cycle.
        self.render()

    def finish_break(self, event):
        if self.state != "break":
            return
        self.close_overlay()
        self.record(event)
        if event == "completed":
            self.play_sound()
        if self.repeat_var.get():
            self.start_work()
        else:
            self.state = "idle"
            self.phase = "work"
            self.total = self.remaining = self.settings["work_minutes"] * 60
        self.render()

    def history_line(self, entry):
        stamp = datetime.fromisoformat(entry["timestamp"])
        return f"{stamp:%d %b, %H:%M}  \u2014  Break {entry['event']}"

    def refresh_history(self):
        self.recent.configure(text="\n".join(self.history_line(h) for h in self.history[:3]) or "Your first break starts a healthier habit.")

    def show_history(self):
        win = tk.Toplevel(self.root)
        win.title("Activity history \u2014 latest 200")
        win.geometry("540x400")
        frame = ttk.Frame(win, padding=15)
        frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(frame)
        scroll.pack(side="right", fill="y")
        box = tk.Listbox(frame, font=(FONT, 11), relief="flat", bg=CARD, fg=NAVY, yscrollcommand=scroll.set)
        box.pack(fill="both", expand=True)
        scroll.configure(command=box.yview)
        for h in self.history[:200]:
            box.insert("end", self.history_line(h))
        if not self.history:
            box.insert("end", "No activity yet.")

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
            image = Image.new("RGB", (64, 64), NAVY)
            draw = ImageDraw.Draw(image)
            draw.ellipse((9, 9, 55, 55), outline=TEAL, width=6)
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
        dlg.configure(bg=CARD)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        remember = tk.BooleanVar(value=False)

        def choose(choice):
            result["choice"] = choice
            result["remember"] = remember.get()
            dlg.destroy()

        frame = ttk.Frame(dlg, style="Card.TFrame", padding=26)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Close Smart Break?", font=(FONT, 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Hide it to the system tray so your reminders\nkeep running, or exit the program completely.",
                  foreground=MUTED, font=(FONT, 10), justify="left").pack(anchor="w", pady=(6, 14))

        tray_button = ttk.Button(frame, text="Hide to system tray", style="Accent.TButton",
                                 command=lambda: choose("tray"))
        tray_button.pack(fill="x", pady=3)
        if not self.tray:
            tray_button.state(["disabled"])
            ttk.Label(frame, text="System tray is not available in this install.",
                      foreground=MUTED, font=(FONT, 9)).pack(anchor="w")
        ttk.Button(frame, text="Exit completely", command=lambda: choose("exit")).pack(fill="x", pady=3)
        ttk.Button(frame, text="Cancel", command=lambda: choose(None)).pack(fill="x", pady=3)
        ttk.Checkbutton(frame, text="Remember my choice", variable=remember).pack(anchor="w", pady=(10, 0))

        dlg.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        dlg.bind("<Escape>", lambda e: choose(None))
        dlg.update_idletasks()
        width, height = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.lift()
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
    lock = InstanceLock()
    root = tk.Tk()
    root.withdraw()
    try:
        if not lock.acquire():
            messagebox.showinfo(NAME, "Smart Break is already running. Check its window or system tray.", parent=root)
            root.destroy()
            return
        App(root)
        root.deiconify()
        root.mainloop()
    finally:
        lock.close()


if __name__ == "__main__":
    main()