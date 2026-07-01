"""
AutoClicker - a self-contained click/macro sequencer for Windows.

Pure standard library: tkinter (GUI) + ctypes (Win32 API). No pip installs,
no third-party tools. Multi-monitor and DPI aware.

Requests administrator rights on launch (UAC). This is needed to send clicks
and keystrokes to programs that themselves run as administrator - Windows
blocks input from a lower-privilege process otherwise.

Run:  python autoclicker.py

Default hotkeys (global - work even when this window is NOT focused, and
are re-bindable in the GUI):
    F6  - start / stop playback
    F7  - panic stop
    F8  - toggle rapid clicker (spam-click at the cursor)
    F9  - start / stop recording your real clicks
"""

import ctypes
import ctypes.wintypes as wt
import threading
import time
import json
import os
import sys
import random
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
gdi32.GetPixel.restype = ctypes.c_uint
gdi32.GetPixel.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]

MOUSEEVENTF_LEFTDOWN   = 0x0002
MOUSEEVENTF_LEFTUP     = 0x0004
MOUSEEVENTF_RIGHTDOWN  = 0x0008
MOUSEEVENTF_RIGHTUP    = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP   = 0x0040
MOUSEEVENTF_WHEEL      = 0x0800

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04

WM_HOTKEY = 0x0312

# Re-bindable hotkey choices: label -> virtual key code
VK_CHOICES = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
    "Pause": 0x13, "ScrollLock": 0x91, "Insert": 0x2D, "Home": 0x24,
    "End": 0x23, "PageUp": 0x21, "PageDown": 0x22, "`": 0xC0,
}

PUL = ctypes.POINTER(ctypes.c_ulong)

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]

class _IUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _IUNION)]

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin():
    """Relaunch elevated if we're not already admin. Returns True if a new
    elevated instance was started (so the caller should exit).

    Elevation is needed to send synthetic input to windows that themselves run
    as administrator (Windows UIPI blocks it otherwise)."""
    if is_admin():
        return False
    frozen = getattr(sys, "frozen", False) or "__compiled__" in globals()
    if frozen:
        target, params = sys.argv[0], " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        target = sys.executable
        params = " ".join(f'"{a}"' for a in sys.argv)
    try:
        # ShellExecuteW with "runas" triggers the UAC prompt; >32 means success.
        return int(ctypes.windll.shell32.ShellExecuteW(
            None, "runas", target, params, None, 1)) > 32
    except Exception:
        return False


def get_cursor_pos():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def virtual_screen_rect():
    """(x, y, w, h) spanning ALL monitors (the virtual desktop), in physical px."""
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def click_at(x, y, button="left", double=False):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.012)
    pairs = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
             "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
             "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}
    down, up = pairs.get(button, pairs["left"])
    for _ in range(2 if double else 1):
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
        if double:
            time.sleep(0.05)


def move_to(x, y):
    user32.SetCursorPos(int(x), int(y))


_CLICK_PAIRS = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}


def click_here(button="left"):
    """Click at the cursor's current position without moving it."""
    down, up = _CLICK_PAIRS.get(button, _CLICK_PAIRS["left"])
    user32.mouse_event(down, 0, 0, 0, 0)
    user32.mouse_event(up, 0, 0, 0, 0)


def get_pixel_color(x, y):
    """Return (r, g, b) of the screen pixel at (x, y), or None if unreadable."""
    hdc = user32.GetDC(0)
    try:
        c = gdi32.GetPixel(hdc, int(x), int(y))
    finally:
        user32.ReleaseDC(0, hdc)
    if c == 0xFFFFFFFF:  # CLR_INVALID
        return None
    return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % (rgb[0], rgb[1], rgb[2])


def hex_to_rgb(s):
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def color_dist(a, b):
    """Max per-channel difference (0-255); intuitive tolerance scale."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def scroll_at(x, y, notches):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.01)
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(notches) * 120, 0)


def type_text(text):
    for ch in text:
        for fl in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD,
                        u=_IUNION(ki=KEYBDINPUT(0, ord(ch), fl, 0, None)))
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.006)


def key_down(vk):
    return user32.GetAsyncKeyState(vk) & 0x8000 != 0


BG, PANEL2, BORDER = "#15171c", "#23272f", "#2e323b"
TEXT, MUTED = "#e7e9ee", "#9aa1ad"
ACCENT, ACCENT_D = "#4c8bf5", "#3a6fd0"
GO, GO_D, STOP = "#37b87a", "#2c9462", "#e2625c"

PAD = {"padx": 5, "pady": 3}


def apply_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.background", PANEL2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Header.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 16))
    style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid")
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=("Segoe UI Semibold", 9))
    style.configure("TButton", background=PANEL2, foreground=TEXT, bordercolor=BORDER,
                    focuscolor=BG, relief="flat", padding=(10, 6))
    style.map("TButton", background=[("active", "#2c313a"), ("pressed", "#333845")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", padding=(12, 7))
    style.map("Accent.TButton", background=[("active", ACCENT_D), ("pressed", ACCENT_D)])
    style.configure("Go.TButton", background=GO, foreground="#08120c", padding=(12, 7))
    style.map("Go.TButton", background=[("active", GO_D), ("pressed", GO_D)])
    style.configure("Stop.TButton", background=PANEL2, foreground=STOP, padding=(12, 7))
    style.map("Stop.TButton", background=[("active", "#3a2424")])
    style.configure("TEntry", fieldbackground=PANEL2, foreground=TEXT,
                    bordercolor=BORDER, insertcolor=TEXT, padding=4)
    style.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                    foreground=TEXT, bordercolor=BORDER, arrowcolor=MUTED, padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", TEXT)])
    style.configure("TCheckbutton", background=BG, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
    style.configure("TNotebook.Tab", background=PANEL2, foreground=MUTED,
                    bordercolor=BORDER, padding=(22, 6), borderwidth=0,
                    font=("Segoe UI Semibold", 10))
    style.map("TNotebook.Tab",
              background=[("selected", ACCENT), ("active", "#2c313a")],
              foreground=[("selected", "#ffffff"), ("active", TEXT)],
              # selected tab grows (wider + taller) so it reads as the active one
              expand=[("selected", (4, 5, 4, 0))])
    return style


class Tooltip:
    """Hover help: shows a small popup after the cursor rests on a widget."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=self.text, justify="left", bg="#2b2f37", fg=TEXT,
                 relief="solid", borderwidth=1, font=("Segoe UI", 9),
                 wraplength=270, padx=8, pady=5).pack()

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class AutoClicker:
    KINDS = ["click", "move", "scroll", "type", "random-area", "wait-color"]

    def __init__(self, root):
        self.root = root
        root.title("AutoClicker")
        root.resizable(False, False)
        apply_theme(root)
        self._set_window_icon()

        self.steps = []
        self.stop_flag = threading.Event()
        self.running = False
        self.recording = False
        self.rapid_active = False

        # current hotkey bindings (labels)
        self.hk_start = tk.StringVar(value="F6")
        self.hk_stop = tk.StringVar(value="F7")
        self.hk_rec = tk.StringVar(value="F9")
        self.hk_rapid = tk.StringVar(value="F8")

        self._build_ui()
        self._start_hotkey_listener()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self._poll_hotkey_queue)

    def _set_window_icon(self):
        """Set the titlebar/taskbar icon from icon.png (works from source and
        from the bundled exe, where the png is included as a data file)."""
        for base in (os.path.dirname(os.path.abspath(__file__)),
                     os.path.dirname(os.path.abspath(sys.argv[0])), os.getcwd()):
            path = os.path.join(base, "icon.png")
            if os.path.exists(path):
                try:
                    self._icon_img = tk.PhotoImage(file=path)   # keep a reference
                    self.root.iconphoto(True, self._icon_img)
                    return
                except Exception:
                    pass

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        frm = ttk.Frame(self.root, padding=14)
        frm.grid()

        head = ttk.Frame(frm)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="AutoClicker", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.hint = ttk.Label(head, text="", style="Sub.TLabel")
        self.hint.grid(row=1, column=0, sticky="w")

        nb = ttk.Notebook(frm)
        self.nb = nb
        nb.grid(row=1, column=0, sticky="ew")
        tab_run = ttk.Frame(nb, padding=10)
        tab_set = ttk.Frame(nb, padding=10)
        nb.add(tab_run, text="Run")
        nb.add(tab_set, text="Settings")
        self._build_run_tab(tab_run)
        self._build_settings_tab(tab_set)

        foot = ttk.Frame(frm)
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(foot, text="Save…", command=self.save).grid(row=0, column=0, **PAD)
        ttk.Button(foot, text="Load…", command=self.load).grid(row=0, column=1, **PAD)
        self.status = tk.StringVar(value="Idle")
        ttk.Label(foot, textvariable=self.status, style="Muted.TLabel")\
            .grid(row=0, column=2, sticky="w", padx=12)

        self._refresh_hint()
        self._on_kind()

    def _build_run_tab(self, tab):
        # editor variables
        self.kind_var = tk.StringVar(value="click")
        self.wait_var = tk.StringVar(value="1.0")
        self.x_var = tk.StringVar(value="500")
        self.y_var = tk.StringVar(value="500")
        self.btn_var = tk.StringVar(value="left")
        self.dbl_var = tk.BooleanVar(value=False)
        self.times_var = tk.StringVar(value="1")
        self.x2_var = tk.StringVar(value="900")
        self.y2_var = tk.StringVar(value="700")
        self.amt_var = tk.StringVar(value="-3")
        self.text_var = tk.StringVar(value="")
        self.color_var = tk.StringVar(value="#ffffff")
        self.tol_var = tk.StringVar(value="10")
        self.match_var = tk.StringVar(value="matches")
        self.timeout_var = tk.StringVar(value="0")

        ed = ttk.LabelFrame(tab, text="ADD / EDIT STEP", padding=10)
        ed.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        top = ttk.Frame(ed)
        top.grid(row=0, column=0, sticky="w")
        ttk.Label(top, text="Type").grid(row=0, column=0, **PAD)
        kc = ttk.Combobox(top, textvariable=self.kind_var, values=self.KINDS,
                          width=12, state="readonly")
        kc.grid(row=0, column=1, **PAD)
        kc.bind("<<ComboboxSelected>>", lambda e: self._on_kind())
        ttk.Label(top, text="Wait after (s)").grid(row=0, column=2, **PAD)
        we = ttk.Entry(top, textvariable=self.wait_var, width=8)
        we.grid(row=0, column=3, **PAD)
        self._tip(kc, "What this step does:\n• click / move / scroll at a point\n"
                      "• type text\n• random-area: click random spots in a box\n"
                      "• wait-color: pause until a pixel matches a colour")
        self._tip(we, "Seconds to pause after this step before the next one runs.")

        # dynamic parameter area: one panel per kind, only the active one shown
        self.param_holder = ttk.Frame(ed)
        self.param_holder.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.kind_panels = {k: ttk.Frame(self.param_holder) for k in self.KINDS}
        for f in self.kind_panels.values():
            f.grid(row=0, column=0, sticky="nw")
        self._build_panels()

        btns = ttk.Frame(ed)
        btns.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        add_b = ttk.Button(btns, text="Add step", style="Accent.TButton", command=self.add_step)
        add_b.grid(row=0, column=0, sticky="ew", **PAD)
        upd_b = ttk.Button(btns, text="Update selected", command=self.update_step)
        upd_b.grid(row=0, column=1, sticky="ew", **PAD)
        self._tip(add_b, "Add the step above as a new entry at the end of the sequence.")
        self._tip(upd_b, "Overwrite the selected sequence row with the values above.")

        lf = ttk.LabelFrame(tab, text="SEQUENCE  (double-click a row to edit)", padding=10)
        lf.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.listbox = tk.Listbox(lf, width=54, height=9, activestyle="none",
                                  bg=PANEL2, fg=TEXT, selectbackground=ACCENT,
                                  selectforeground="#ffffff", highlightthickness=0,
                                  borderwidth=0, font=("Consolas", 10))
        self.listbox.grid(row=0, column=0, rowspan=5, padx=(0, 8), pady=2)
        self.listbox.bind("<<ListboxSelect>>", self.on_select)
        self.listbox.bind("<Double-Button-1>", self.on_double)
        for i, (txt, cmd) in enumerate([("Delete", self.delete_step),
                                        ("Up", lambda: self.move(-1)),
                                        ("Down", lambda: self.move(1)),
                                        ("Clear", self.clear_steps)]):
            ttk.Button(lf, text=txt, command=cmd).grid(row=i, column=1, sticky="ew", pady=2)

        run = ttk.Frame(tab)
        run.grid(row=2, column=0, sticky="ew")
        ttk.Label(run, text="Repeat").grid(row=0, column=0, **PAD)
        self.loops_var = tk.StringVar(value="5")
        rep = ttk.Entry(run, textvariable=self.loops_var, width=7)
        rep.grid(row=0, column=1, **PAD)
        ttk.Label(run, text="0 = forever", style="Muted.TLabel").grid(row=0, column=2, sticky="w", **PAD)
        self.start_btn = ttk.Button(run, text="▶ Start", style="Go.TButton", command=self.toggle)
        self.start_btn.grid(row=0, column=3, sticky="ew", padx=10)
        stop_b = ttk.Button(run, text="■ Stop", style="Stop.TButton", command=self.stop)
        stop_b.grid(row=0, column=4, sticky="ew")
        self._tip(rep, "How many times to run the whole sequence. 0 = loop forever.")
        self._tip(self.start_btn, "Run the sequence (or stop it if already running).")
        self._tip(stop_b, "Stop playback immediately.")

    def _pos_widgets(self, parent, capture=True):
        ttk.Label(parent, text="X").grid(row=0, column=0, **PAD)
        ttk.Entry(parent, textvariable=self.x_var, width=8).grid(row=0, column=1, **PAD)
        ttk.Label(parent, text="Y").grid(row=0, column=2, **PAD)
        ttk.Entry(parent, textvariable=self.y_var, width=8).grid(row=0, column=3, **PAD)
        if capture:
            cap = ttk.Button(parent, text="Capture (click)", command=self.capture)
            cap.grid(row=0, column=4, columnspan=2, sticky="ew", **PAD)
            self._tip(cap, "Hide the window, then click anywhere on screen to grab "
                           "that exact position into X/Y. Esc cancels.")

    def _build_panels(self):
        P = self.kind_panels

        # click
        f = P["click"]
        pos = ttk.Frame(f); pos.grid(row=0, column=0, sticky="w")
        self._pos_widgets(pos)
        r = ttk.Frame(f); r.grid(row=1, column=0, sticky="w")
        ttk.Label(r, text="Button").grid(row=0, column=0, **PAD)
        ttk.Combobox(r, textvariable=self.btn_var, values=["left", "right", "middle"],
                     width=7, state="readonly").grid(row=0, column=1, **PAD)
        ttk.Checkbutton(r, text="Double", variable=self.dbl_var).grid(row=0, column=2, sticky="w", **PAD)
        ttk.Label(r, text="Times").grid(row=0, column=3, sticky="e", **PAD)
        ttk.Entry(r, textvariable=self.times_var, width=6).grid(row=0, column=4, **PAD)

        # move
        f = P["move"]
        pos = ttk.Frame(f); pos.grid(row=0, column=0, sticky="w")
        self._pos_widgets(pos)

        # scroll
        f = P["scroll"]
        pos = ttk.Frame(f); pos.grid(row=0, column=0, sticky="w")
        self._pos_widgets(pos)
        r = ttk.Frame(f); r.grid(row=1, column=0, sticky="w")
        ttk.Label(r, text="Scroll notches").grid(row=0, column=0, **PAD)
        ttk.Entry(r, textvariable=self.amt_var, width=8).grid(row=0, column=1, **PAD)
        ttk.Label(r, text="(+ up / - down)", style="Muted.TLabel").grid(row=0, column=2, sticky="w", **PAD)

        # type
        f = P["type"]
        ttk.Label(f, text="Text").grid(row=0, column=0, **PAD)
        ttk.Entry(f, textvariable=self.text_var, width=40).grid(row=0, column=1, sticky="ew", **PAD)

        # random-area
        f = P["random-area"]
        r = ttk.Frame(f); r.grid(row=0, column=0, sticky="w")
        ttk.Label(r, text="Top-left X").grid(row=0, column=0, **PAD)
        ttk.Entry(r, textvariable=self.x_var, width=8).grid(row=0, column=1, **PAD)
        ttk.Label(r, text="Y").grid(row=0, column=2, **PAD)
        ttk.Entry(r, textvariable=self.y_var, width=8).grid(row=0, column=3, **PAD)
        ttk.Label(r, text="  Bottom-right X").grid(row=0, column=4, **PAD)
        ttk.Entry(r, textvariable=self.x2_var, width=8).grid(row=0, column=5, **PAD)
        ttk.Label(r, text="Y").grid(row=0, column=6, **PAD)
        ttk.Entry(r, textvariable=self.y2_var, width=8).grid(row=0, column=7, **PAD)
        r2 = ttk.Frame(f); r2.grid(row=1, column=0, sticky="w")
        sel = ttk.Button(r2, text="Select area on screen", command=self.select_area)
        sel.grid(row=0, column=0, sticky="w", **PAD)
        self._tip(sel, "Hide the window and drag a rectangle on screen to set the "
                       "random-click area (fills both corners). Esc cancels.")
        ttk.Label(r2, text="Button").grid(row=0, column=1, **PAD)
        ttk.Combobox(r2, textvariable=self.btn_var, values=["left", "right", "middle"],
                     width=7, state="readonly").grid(row=0, column=2, **PAD)
        ttk.Label(r2, text="Times").grid(row=0, column=3, sticky="e", **PAD)
        ttk.Entry(r2, textvariable=self.times_var, width=6).grid(row=0, column=4, **PAD)

        # wait-color
        f = P["wait-color"]
        pos = ttk.Frame(f); pos.grid(row=0, column=0, sticky="w")
        self._pos_widgets(pos)
        r = ttk.Frame(f); r.grid(row=1, column=0, sticky="w")
        ttk.Label(r, text="Color").grid(row=0, column=0, **PAD)
        ttk.Entry(r, textvariable=self.color_var, width=10).grid(row=0, column=1, **PAD)
        self.swatch = tk.Label(r, text="  ", bg="#ffffff", width=3, relief="solid", bd=1)
        self.swatch.grid(row=0, column=2, **PAD)
        pick = ttk.Button(r, text="Pick color (3s)", command=self.pick_color)
        pick.grid(row=0, column=3, **PAD)
        self._tip(pick, "Hover the mouse over a target pixel; after a 3s countdown "
                        "its colour and position are captured.")
        r2 = ttk.Frame(f); r2.grid(row=2, column=0, sticky="w")
        ttk.Label(r2, text="Tolerance").grid(row=0, column=0, **PAD)
        tol = ttk.Entry(r2, textvariable=self.tol_var, width=6)
        tol.grid(row=0, column=1, **PAD)
        self._tip(tol, "How close the pixel must be to the colour to count as a match "
                       "(0 = exact; higher = looser, 0–255).")
        ttk.Label(r2, text="When pixel").grid(row=0, column=2, **PAD)
        mt = ttk.Combobox(r2, textvariable=self.match_var, values=["matches", "differs"],
                          width=8, state="readonly")
        mt.grid(row=0, column=3, **PAD)
        self._tip(mt, "Wait until the pixel matches the colour, or until it differs from it.")
        ttk.Label(r2, text="Timeout s").grid(row=0, column=4, sticky="e", **PAD)
        to = ttk.Entry(r2, textvariable=self.timeout_var, width=6)
        to.grid(row=0, column=5, **PAD)
        self._tip(to, "Give up waiting after this many seconds (0 = wait forever).")
        self.color_var.trace_add("write", lambda *a: self._update_swatch())

    def _cfg_cols(self, frame):
        """Shared column metrics so fields line up across every settings frame."""
        for col, minsize in ((0, 104), (1, 84), (2, 96), (3, 110)):
            frame.columnconfigure(col, minsize=minsize)
        frame.columnconfigure(4, weight=1)   # trailing spacer keeps content left-aligned

    def _tip(self, widget, text):
        Tooltip(widget, text)

    def _build_settings_tab(self, tab):
        keys = list(VK_CHOICES.keys())
        tab.columnconfigure(0, weight=1)
        self.rec_append = tk.BooleanVar(value=False)
        self.delay_var = tk.StringVar(value="0")
        self.jpx_var = tk.StringVar(value="0")
        self.jpct_var = tk.StringVar(value="0")
        self.cps_var = tk.StringVar(value="12")
        self.rapid_btn_var = tk.StringVar(value="left")

        # ---- RECORD & PLAYBACK ----
        opt = ttk.LabelFrame(tab, text="RECORD & PLAYBACK", padding=12)
        opt.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._cfg_cols(opt)
        self.rec_btn = ttk.Button(opt, text="● Record", command=self.toggle_record)
        self.rec_btn.grid(row=0, column=0, columnspan=2, sticky="ew", **PAD)
        ap = ttk.Checkbutton(opt, text="Append", variable=self.rec_append)
        ap.grid(row=0, column=2, columnspan=2, sticky="w", **PAD)
        ttk.Label(opt, text="Start delay (s)").grid(row=1, column=0, sticky="e", **PAD)
        de = ttk.Entry(opt, textvariable=self.delay_var, width=8)
        de.grid(row=1, column=1, sticky="w", **PAD)
        ttk.Label(opt, text="Jitter ±px").grid(row=2, column=0, sticky="e", **PAD)
        je = ttk.Entry(opt, textvariable=self.jpx_var, width=8)
        je.grid(row=2, column=1, sticky="w", **PAD)
        ttk.Label(opt, text="Jitter ±time%").grid(row=2, column=2, sticky="e", **PAD)
        jt = ttk.Entry(opt, textvariable=self.jpct_var, width=8)
        jt.grid(row=2, column=3, sticky="w", **PAD)

        # ---- RAPID CLICKER ----
        rp = ttk.LabelFrame(tab, text="RAPID CLICKER", padding=12)
        rp.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self._cfg_cols(rp)
        ttk.Label(rp, text="Clicks/sec").grid(row=0, column=0, sticky="e", **PAD)
        ce = ttk.Entry(rp, textvariable=self.cps_var, width=8)
        ce.grid(row=0, column=1, sticky="w", **PAD)
        ttk.Label(rp, text="Button").grid(row=0, column=2, sticky="e", **PAD)
        rb = ttk.Combobox(rp, textvariable=self.rapid_btn_var, values=["left", "right", "middle"],
                          width=9, state="readonly")
        rb.grid(row=0, column=3, sticky="w", **PAD)
        self.rapid_status = ttk.Label(rp, text="rapid: off", style="Muted.TLabel")
        self.rapid_status.grid(row=1, column=0, columnspan=5, sticky="w", **PAD)

        # ---- HOTKEYS ----
        hk = ttk.LabelFrame(tab, text="HOTKEYS  (work even when the window isn't focused)", padding=12)
        hk.grid(row=2, column=0, sticky="ew")
        self._cfg_cols(hk)
        ttk.Label(hk, text="Start/Stop").grid(row=0, column=0, sticky="e", **PAD)
        c1 = ttk.Combobox(hk, textvariable=self.hk_start, values=keys, width=10, state="readonly")
        c1.grid(row=0, column=1, sticky="w", **PAD)
        ttk.Label(hk, text="Panic").grid(row=0, column=2, sticky="e", **PAD)
        c2 = ttk.Combobox(hk, textvariable=self.hk_stop, values=keys, width=10, state="readonly")
        c2.grid(row=0, column=3, sticky="w", **PAD)
        ttk.Label(hk, text="Record").grid(row=1, column=0, sticky="e", **PAD)
        c3 = ttk.Combobox(hk, textvariable=self.hk_rec, values=keys, width=10, state="readonly")
        c3.grid(row=1, column=1, sticky="w", **PAD)
        ttk.Label(hk, text="Rapid").grid(row=1, column=2, sticky="e", **PAD)
        c4 = ttk.Combobox(hk, textvariable=self.hk_rapid, values=keys, width=10, state="readonly")
        c4.grid(row=1, column=3, sticky="w", **PAD)
        ttk.Button(hk, text="Apply hotkeys", command=self.apply_hotkeys)\
            .grid(row=2, column=0, columnspan=4, sticky="ew", padx=5, pady=(10, 3))

        # ---- tooltips ----
        self._tip(self.rec_btn, "Record your real mouse clicks (with timing) into the sequence.")
        self._tip(ap, "Add recorded clicks onto the current sequence instead of replacing it.")
        self._tip(de, "Seconds to wait after you press Start before playback begins.")
        self._tip(je, "Randomly shift each click up to this many pixels for natural variation.")
        self._tip(jt, "Randomly vary each step's wait time by up to this percent.")
        self._tip(ce, "How many clicks per second the rapid clicker fires (1–200).")
        self._tip(rb, "Which mouse button the rapid clicker uses.")
        for cb, what in ((c1, "start/stop playback"), (c2, "panic-stop everything"),
                         (c3, "start/stop recording"), (c4, "toggle the rapid clicker")):
            self._tip(cb, f"Global hotkey to {what}. Click Apply hotkeys after changing.")

    def _refresh_hint(self):
        self.hint.config(text=f"{self.hk_start.get()} play/stop  ·  "
                              f"{self.hk_stop.get()} panic  ·  {self.hk_rec.get()} record  ·  "
                              f"{self.hk_rapid.get()} rapid")
        self.rec_btn.config(text=f"● Record ({self.hk_rec.get()})")
        if not self.running:
            self.start_btn.config(text="▶ Start")

    # ---------------------------------------------------------------- kind logic
    def _on_kind(self):
        for f in self.kind_panels.values():
            f.grid_remove()
        self.kind_panels[self.kind_var.get()].grid()

    # ---------------------------------------------------------------- steps
    def _fmt(self, s):
        k = s["kind"]
        if k == "click":
            t = " x2" if s.get("double") else ""
            n = f" ×{s.get('times', 1)}" if s.get("times", 1) > 1 else ""
            head = f"click {s['button']}{t}{n} @ ({s['x']}, {s['y']})"
        elif k == "move":
            head = f"move → ({s['x']}, {s['y']})"
        elif k == "scroll":
            head = f"scroll {s.get('amount', 0):+d} @ ({s['x']}, {s['y']})"
        elif k == "random-area":
            head = (f"random {s.get('times', 1)}× {s['button']} in "
                    f"[{s['x']},{s['y']}]–[{s['x2']},{s['y2']}]")
        elif k == "wait-color":
            to = f" ≤{s['timeout']:g}s" if s.get("timeout") else ""
            head = (f"wait pixel {s.get('match', 'matches')} {s.get('color', '')} "
                    f"±{s.get('tolerance', 0)} @ ({s['x']}, {s['y']}){to}")
        else:
            p = s.get("text", "")
            p = p[:18] + "…" if len(p) > 18 else p
            head = f"type \"{p}\""
        return f"{head}   wait {round(s['wait'], 2)}s"

    def refresh_list(self, keep=None):
        self.listbox.delete(0, tk.END)
        for i, s in enumerate(self.steps, 1):
            self.listbox.insert(tk.END, f"{i:>2}. {self._fmt(s)}")
        if keep is not None and 0 <= keep < len(self.steps):
            self.listbox.selection_set(keep)
            self.listbox.see(keep)

    def _read_editor(self):
        k = self.kind_var.get()
        s = {"kind": k, "wait": max(0.0, float(self.wait_var.get()))}
        if k in ("click", "move", "scroll", "random-area", "wait-color"):
            s["x"] = int(float(self.x_var.get()))
            s["y"] = int(float(self.y_var.get()))
        if k == "click":
            s["button"] = self.btn_var.get()
            s["double"] = bool(self.dbl_var.get())
            s["times"] = max(1, int(float(self.times_var.get())))
        if k == "scroll":
            s["amount"] = int(float(self.amt_var.get()))
        if k == "type":
            s["text"] = self.text_var.get()
        if k == "random-area":
            s["x2"] = int(float(self.x2_var.get()))
            s["y2"] = int(float(self.y2_var.get()))
            s["button"] = self.btn_var.get()
            s["times"] = max(1, int(float(self.times_var.get())))
        if k == "wait-color":
            hex_to_rgb(self.color_var.get())   # validate; raises ValueError if bad
            s["color"] = self.color_var.get().strip()
            s["tolerance"] = max(0, int(float(self.tol_var.get())))
            s["match"] = self.match_var.get()
            s["timeout"] = max(0.0, float(self.timeout_var.get()))
        return s

    def _load_editor(self, s):
        self.kind_var.set(s["kind"])
        self._on_kind()
        self.wait_var.set(str(round(s["wait"], 2)))
        if "x" in s:
            self.x_var.set(str(s["x"]))
            self.y_var.set(str(s["y"]))
        if s["kind"] == "click":
            self.btn_var.set(s.get("button", "left"))
            self.dbl_var.set(bool(s.get("double")))
            self.times_var.set(str(s.get("times", 1)))
        if s["kind"] == "scroll":
            self.amt_var.set(str(s.get("amount", 0)))
        if s["kind"] == "type":
            self.text_var.set(s.get("text", ""))
        if s["kind"] == "random-area":
            self.x2_var.set(str(s.get("x2", 0)))
            self.y2_var.set(str(s.get("y2", 0)))
            self.btn_var.set(s.get("button", "left"))
            self.times_var.set(str(s.get("times", 1)))
        if s["kind"] == "wait-color":
            self.color_var.set(s.get("color", "#ffffff"))
            self.tol_var.set(str(s.get("tolerance", 10)))
            self.match_var.set(s.get("match", "matches"))
            self.timeout_var.set(str(s.get("timeout", 0)))

    def add_step(self):
        try:
            self.steps.append(self._read_editor())
        except ValueError:
            messagebox.showerror("Invalid", "Numeric fields must be numbers.")
            return
        self.refresh_list(keep=len(self.steps) - 1)

    def update_step(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("No selection", "Pick a step first.")
            return
        try:
            self.steps[sel[0]] = self._read_editor()
        except ValueError:
            messagebox.showerror("Invalid", "Numeric fields must be numbers.")
            return
        self.refresh_list(keep=sel[0])
        self.status.set(f"Updated step {sel[0] + 1}")

    def on_select(self, _e):
        sel = self.listbox.curselection()
        if sel:
            self._load_editor(self.steps[sel[0]])

    def on_double(self, _e):
        sel = self.listbox.curselection()
        if sel:
            self._load_editor(self.steps[sel[0]])
            self.status.set(f"Editing step {sel[0] + 1} — change values, then Update selected")

    def delete_step(self):
        sel = self.listbox.curselection()
        if sel:
            del self.steps[sel[0]]
            self.refresh_list()

    def move(self, d):
        sel = self.listbox.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + d
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self.refresh_list(keep=j)

    def clear_steps(self):
        self.steps.clear()
        self.refresh_list()

    def _open_overlay(self, prompt):
        """Full virtual-desktop transparent overlay spanning ALL monitors.
        Returns (toplevel, canvas). Caller binds events and calls destroy."""
        self.root.withdraw()
        time.sleep(0.15)
        ov = tk.Toplevel()
        ov.overrideredirect(True)         # borderless: lets us span every monitor
        vx, vy, vw, vh = virtual_screen_rect()
        ov.geometry(f"{vw}x{vh}+{vx}+{vy}")
        ov.attributes("-alpha", 0.25)
        ov.attributes("-topmost", True)
        ov.configure(bg="#000000")
        ov.config(cursor="crosshair")
        cv = tk.Canvas(ov, bg="#000000", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        cv.create_text(vw // 2, 40, text=prompt, fill="#ffffff", font=("Segoe UI", 14))
        ov.focus_force()
        return ov, cv

    def capture(self):
        ov, cv = self._open_overlay("Click the target position  —  Esc to cancel")

        def click(e):
            self.x_var.set(str(e.x_root))
            self.y_var.set(str(e.y_root))
            self.status.set(f"Captured ({e.x_root}, {e.y_root})")
            finish()

        def cancel(_e):
            self.status.set("Capture cancelled")
            finish()

        def finish():
            ov.destroy()
            self.root.deiconify()

        cv.bind("<Button-1>", click)
        ov.bind("<Escape>", cancel)
        cv.bind("<Escape>", cancel)

    def _update_swatch(self):
        try:
            self.swatch.config(bg=rgb_to_hex(hex_to_rgb(self.color_var.get())))
        except Exception:
            pass

    def pick_color(self):
        self._color_countdown(3)

    def _color_countdown(self, n):
        if n > 0:
            self.status.set(f"Hover over target pixel… {n}")
            self.root.after(1000, lambda: self._color_countdown(n - 1))
        else:
            x, y = get_cursor_pos()
            c = get_pixel_color(x, y)
            if c is None:
                self.status.set("Could not read pixel")
                return
            self.x_var.set(str(x))
            self.y_var.set(str(y))
            self.color_var.set(rgb_to_hex(c))
            self.status.set(f"Picked {rgb_to_hex(c)} @ ({x}, {y})")

    # ---------------------------------------------------------------- area selection overlay
    def select_area(self):
        ov, cv = self._open_overlay("Drag to select an area  —  Esc to cancel")
        # canvas (0,0) maps to the virtual-desktop origin, so e.x/e.y are
        # canvas coords for drawing while e.x_root/e.y_root are true screen coords.
        st = {"cx0": 0, "cy0": 0, "x0": 0, "y0": 0, "rect": None}

        def down(e):
            st["x0"], st["y0"] = e.x_root, e.y_root
            st["cx0"], st["cy0"] = e.x, e.y
            st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT, width=2)

        def drag(e):
            if st["rect"] is not None:
                cv.coords(st["rect"], st["cx0"], st["cy0"], e.x, e.y)

        def up(e):
            x1, y1 = st["x0"], st["y0"]
            x2, y2 = e.x_root, e.y_root
            self.x_var.set(str(min(x1, x2)))
            self.y_var.set(str(min(y1, y2)))
            self.x2_var.set(str(max(x1, x2)))
            self.y2_var.set(str(max(y1, y2)))
            self.status.set(f"Area set [{min(x1,x2)},{min(y1,y2)}]–[{max(x1,x2)},{max(y1,y2)}]")
            finish()

        def cancel(_e):
            self.status.set("Area selection cancelled")
            finish()

        def finish():
            ov.destroy()
            self.root.deiconify()

        cv.bind("<Button-1>", down)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", up)
        ov.bind("<Escape>", cancel)
        cv.bind("<Escape>", cancel)

    # ---------------------------------------------------------------- record
    def toggle_record(self):
        if self.recording:
            self.recording = False
        else:
            if self.running:
                self.stop()
            self.rapid_active = False
            if not self.rec_append.get():
                self.steps.clear()
                self.refresh_list()
            self.recording = True
            self.rec_btn.config(text=f"■ Stop rec ({self.hk_rec.get()})")
            self.status.set("Recording… click anywhere.")
            threading.Thread(target=self._record_loop, daemon=True).start()

    def _record_loop(self):
        buttons = {VK_LBUTTON: "left", VK_RBUTTON: "right", VK_MBUTTON: "middle"}
        prev = {vk: False for vk in buttons}
        last = None
        while self.recording:
            for vk, name in buttons.items():
                now = key_down(vk)
                if now and not prev[vk]:
                    x, y = get_cursor_pos()
                    t = time.time()
                    if last is not None and self.steps:
                        self.steps[-1]["wait"] = round(t - last, 2)
                    last = t
                    self.steps.append({"kind": "click", "x": x, "y": y, "wait": 0.5,
                                       "button": name, "double": False, "times": 1})
                    self.root.after(0, lambda: self.refresh_list(keep=len(self.steps) - 1))
                prev[vk] = now
            time.sleep(0.008)
        self.root.after(0, self._record_done)

    def _record_done(self):
        self.recording = False
        self.rec_btn.config(text=f"● Record ({self.hk_rec.get()})")
        self.status.set(f"Recorded {len(self.steps)} step(s)")

    # ---------------------------------------------------------------- run
    def toggle(self):
        self.stop() if self.running else self.start()

    def start(self):
        if self.running or self.recording:
            return
        self.rapid_active = False
        if not self.steps:
            messagebox.showinfo("No steps", "Add or record at least one step first.")
            return
        try:
            loops = int(float(self.loops_var.get()))
            self.j_px = max(0, int(float(self.jpx_var.get())))
            self.j_pct = max(0.0, float(self.jpct_var.get())) / 100.0
            self.delay = max(0.0, float(self.delay_var.get()))
        except ValueError:
            messagebox.showerror("Invalid", "Repeat / jitter / delay must be numbers.")
            return
        self.stop_flag.clear()
        self.running = True
        self.start_btn.config(text="■ Stop")
        threading.Thread(target=self._run_loop, args=(loops,), daemon=True).start()

    def stop(self):
        self.stop_flag.set()

    def _sleep(self, secs):
        waited = 0.0
        while waited < secs and not self.stop_flag.is_set():
            time.sleep(0.04)
            waited += 0.04

    def _jit_xy(self, x, y):
        if self.j_px:
            return x + random.randint(-self.j_px, self.j_px), y + random.randint(-self.j_px, self.j_px)
        return x, y

    def _jit_wait(self, w):
        if self.j_pct:
            return max(0.0, w * (1 + random.uniform(-self.j_pct, self.j_pct)))
        return w

    def _run_loop(self, loops):
        count = 0
        try:
            if self.delay > 0:
                self.root.after(0, lambda: self.status.set(f"Starting in {self.delay:g}s…"))
                self._sleep(self.delay)
            self.root.after(0, lambda: self.status.set("Running…"))
            while not self.stop_flag.is_set():
                if loops != 0 and count >= loops:
                    break
                for s in self.steps:
                    if self.stop_flag.is_set():
                        break
                    k = s["kind"]
                    if k == "click":
                        for _ in range(s.get("times", 1)):
                            if self.stop_flag.is_set():
                                break
                            x, y = self._jit_xy(s["x"], s["y"])
                            click_at(x, y, s["button"], s.get("double", False))
                            time.sleep(0.04)
                    elif k == "move":
                        x, y = self._jit_xy(s["x"], s["y"])
                        move_to(x, y)
                    elif k == "scroll":
                        x, y = self._jit_xy(s["x"], s["y"])
                        scroll_at(x, y, s.get("amount", 0))
                    elif k == "type":
                        type_text(s.get("text", ""))
                    elif k == "random-area":
                        x1, x2 = sorted((s["x"], s["x2"]))
                        y1, y2 = sorted((s["y"], s["y2"]))
                        for _ in range(s.get("times", 1)):
                            if self.stop_flag.is_set():
                                break
                            rx = random.randint(x1, x2) if x2 > x1 else x1
                            ry = random.randint(y1, y2) if y2 > y1 else y1
                            click_at(rx, ry, s.get("button", "left"))
                            time.sleep(0.04)
                    elif k == "wait-color":
                        self._wait_color(s)
                    self._sleep(self._jit_wait(s["wait"]))
                count += 1
        finally:
            self.root.after(0, self._on_finished, count)

    def _on_finished(self, count):
        self.running = False
        self.start_btn.config(text="▶ Start")
        self.status.set(f"Stopped after {count} loop(s)")

    def _wait_color(self, s):
        """Block until the pixel at (x, y) matches/differs from the target colour,
        or the optional timeout elapses. Respects the stop flag."""
        try:
            target = hex_to_rgb(s.get("color", "#000000"))
        except ValueError:
            return
        tol = s.get("tolerance", 0)
        want_match = s.get("match", "matches") == "matches"
        timeout = s.get("timeout", 0)
        t0 = time.time()
        while not self.stop_flag.is_set():
            cur = get_pixel_color(s["x"], s["y"])
            is_match = cur is not None and color_dist(cur, target) <= tol
            if is_match == want_match:
                return
            if timeout > 0 and (time.time() - t0) >= timeout:
                return
            time.sleep(0.03)

    # ---------------------------------------------------------------- rapid clicker
    def toggle_rapid(self):
        if self.rapid_active:
            self.rapid_active = False
            return
        if self.running or self.recording:
            return
        try:
            cps = float(self.cps_var.get())
        except ValueError:
            cps = 12.0
        cps = min(200.0, max(1.0, cps))
        self.rapid_interval = 1.0 / cps
        self.stop_flag.clear()
        self.rapid_active = True
        self.rapid_status.config(text=f"rapid: ON ({cps:g} cps)")
        threading.Thread(target=self._rapid_loop, daemon=True).start()

    def _rapid_loop(self):
        btn = self.rapid_btn_var.get()
        while self.rapid_active and not self.stop_flag.is_set():
            click_here(btn)
            time.sleep(self.rapid_interval)
        self.rapid_active = False
        self.root.after(0, lambda: self.rapid_status.config(text="rapid: off"))

    # ---------------------------------------------------------------- save/load
    def save(self):
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("Click sequence", "*.json")])
        if path:
            with open(path, "w") as f:
                json.dump({"loops": self.loops_var.get(),
                           "jitter_px": self.jpx_var.get(),
                           "jitter_pct": self.jpct_var.get(),
                           "delay": self.delay_var.get(),
                           "hk_start": self.hk_start.get(),
                           "hk_stop": self.hk_stop.get(),
                           "hk_rec": self.hk_rec.get(),
                           "hk_rapid": self.hk_rapid.get(),
                           "cps": self.cps_var.get(),
                           "rapid_button": self.rapid_btn_var.get(),
                           "steps": self.steps}, f, indent=2)
            self.status.set("Saved")

    def load(self):
        path = filedialog.askopenfilename(filetypes=[("Click sequence", "*.json")])
        if path:
            with open(path) as f:
                data = json.load(f)
            self.steps = data.get("steps", [])
            self.loops_var.set(str(data.get("loops", "5")))
            self.jpx_var.set(str(data.get("jitter_px", "0")))
            self.jpct_var.set(str(data.get("jitter_pct", "0")))
            self.delay_var.set(str(data.get("delay", "0")))
            self.hk_start.set(data.get("hk_start", "F6"))
            self.hk_stop.set(data.get("hk_stop", "F7"))
            self.hk_rec.set(data.get("hk_rec", "F9"))
            self.hk_rapid.set(data.get("hk_rapid", "F8"))
            self.cps_var.set(str(data.get("cps", "12")))
            self.rapid_btn_var.set(data.get("rapid_button", "left"))
            self.apply_hotkeys(silent=True)
            self.refresh_list()
            self.status.set("Loaded")

    # ---------------------------------------------------------------- hotkeys
    def apply_hotkeys(self, silent=False):
        chosen = [self.hk_start.get(), self.hk_stop.get(),
                  self.hk_rec.get(), self.hk_rapid.get()]
        if len(set(chosen)) < 4:
            if not silent:
                messagebox.showerror("Conflict", "Pick four different keys.")
            return
        self._hk_rebind = True   # signal listener thread to re-register
        self._refresh_hint()
        if not silent:
            self.status.set("Hotkeys updated")

    def _start_hotkey_listener(self):
        self._hk_q = []
        self._hk_run = True
        self._hk_rebind = False
        threading.Thread(target=self._hotkey_loop, daemon=True).start()

    def _register(self):
        ids = {1: self.hk_start.get(), 2: self.hk_stop.get(),
               3: self.hk_rec.get(), 4: self.hk_rapid.get()}
        for hid, label in ids.items():
            user32.RegisterHotKey(None, hid, 0, VK_CHOICES.get(label, 0x75))

    def _unregister(self):
        for hid in (1, 2, 3, 4):
            user32.UnregisterHotKey(None, hid)

    def _hotkey_loop(self):
        self._register()
        msg = wt.MSG()
        while self._hk_run:
            if self._hk_rebind:
                self._unregister()
                self._register()
                self._hk_rebind = False
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_HOTKEY:
                    self._hk_q.append(msg.wParam)
            time.sleep(0.02)
        self._unregister()

    def _poll_hotkey_queue(self):
        while self._hk_q:
            wid = self._hk_q.pop(0)
            if wid == 1:
                self.toggle()
            elif wid == 2:
                self.stop()
            elif wid == 3:
                self.toggle_record()
            elif wid == 4:
                self.toggle_rapid()
        self.root.after(120, self._poll_hotkey_queue)

    def on_close(self):
        self._hk_run = False
        self.recording = False
        self.stop_flag.set()
        self.root.after(120, self.root.destroy)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This build targets Windows (Win32 API).")
    elif run_as_admin():
        sys.exit(0)   # handed off to the elevated instance; quit this one
    root = tk.Tk()
    AutoClicker(root)
    root.mainloop()
