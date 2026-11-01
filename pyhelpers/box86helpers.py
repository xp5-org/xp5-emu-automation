import ctypes
import os
import socket
import subprocess
import sys
import threading
import time

from PIL import Image

from displayhelpers import resolve_display
import windowlayout
from qemuhelpers import TESSERACT_TESSDATA_ARGS, TESSERACT_BIN

# read_screen() upscale factor before tesseract (see qemuhelpers.ocr_word_find)
OCR_SCALE = 4

HELPERDIR = os.path.dirname(os.path.abspath(__file__))
if HELPERDIR not in sys.path:
    sys.path.insert(0, HELPERDIR)
APPBASE_DIR = "/testrunnerapp"
if APPBASE_DIR not in sys.path:
    sys.path.insert(0, APPBASE_DIR)
try:
    import appstate as processreg
except ImportError:
    class processreg:
        register = staticmethod(lambda *a, **k: None)
        unregister = staticmethod(lambda *a, **k: None)

TESTSRC_BASEDIR = os.environ.get("TESTSRC_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

# Windows: tesseract from MSYS2 (ucrt64/mingw64); fall back to the known install dir if PATH is stale
TESSERACT_BIN_WIN = next(
    (p for p in (r"C:\msys64\ucrt64\bin\tesseract.exe", r"C:\msys64\mingw64\bin\tesseract.exe")
     if os.path.isfile(p)),
    r"C:\msys64\ucrt64\bin\tesseract.exe")

if IS_WINDOWS:
    import win32api
    import win32con
    import win32gui
    import win32process
    try:
        # win32ui is unavailable on MSYS2; import must not fail (capture uses ctypes GDI)
        import win32ui
    except ImportError:
        win32ui = None

BOX86_BIN = "86box"
# Windows has no PATH "86box": testlists pass an explicit `binary`
BOX86_BIN_WIN = r"C:\Users\me\Desktop\86Box.exe"


# 86Box --monitor-port key names <- testlist/xdotool/QEMU spellings
MONITOR_KEY_ALIASES = {
    "return": "enter", "kp_enter": "kpenter", "kpenter": "kpenter",
    "escape": "esc", "esc": "esc",
    "delete": "del", "insert": "ins",
    "prior": "pgup", "next": "pgdn", "page_up": "pgup", "page_down": "pgdn",
    "control": "ctrl", "super": "lwin", "meta": "lwin",
    "bracketleft": "[", "bracketright": "]",
    "minus": "-", "equal": "=", "semicolon": ";", "apostrophe": "'",
    "grave": "`", "backslash": "\\", "comma": ",", "period": ".", "slash": "/",
    "spc": "space", "ret": "enter",
}


def monitor_key(name):
    """Map one key token (possibly a 'ctrl+alt+Delete' combo) to monitor names."""
    parts = []
    for part in str(name).split("+"):
        part = part.strip()
        if not part:
            continue
        parts.append(MONITOR_KEY_ALIASES.get(part.lower(), part.lower()))
    return "+".join(parts)


def _xdotool_key(name):
    """xdotool keysyms are case-sensitive: lowercase 'space'; accept 'spc'/'ret'."""
    parts = []
    for part in str(name).split("+"):
        part = part.strip()
        low = part.lower()
        if low in ("space", "spc"):
            part = "space"
        elif low == "ret":
            part = "Return"
        parts.append(part)
    return "+".join(parts)


def free_tcp_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Windows input/window plumbing: SendInput with hardware scan codes; window lookup by title "<vm dir basename> - 86Box <version>" (86Box.exe may relaunch as a child, so PID is unreliable)
if IS_WINDOWS:
    _user32 = ctypes.windll.user32

    _PUL = ctypes.POINTER(ctypes.c_ulong)

    class _KeyBdInput(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", _PUL)]

    class _MouseInput(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]

    class _InputUnion(ctypes.Union):
        _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput)]

    class _Input(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("ii", _InputUnion)]

    _INPUT_KEYBOARD = 1
    _INPUT_MOUSE = 0
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_SCANCODE = 0x0008
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004

    # Set-1 hardware scan codes, US QWERTY
    _SCANCODES = {
        '1': 0x02, '2': 0x03, '3': 0x04, '4': 0x05, '5': 0x06,
        '6': 0x07, '7': 0x08, '8': 0x09, '9': 0x0A, '0': 0x0B,
        '-': 0x0C, '=': 0x0D, 'backspace': 0x0E, 'tab': 0x0F,
        'q': 0x10, 'w': 0x11, 'e': 0x12, 'r': 0x13, 't': 0x14,
        'y': 0x15, 'u': 0x16, 'i': 0x17, 'o': 0x18, 'p': 0x19,
        '[': 0x1A, ']': 0x1B, 'enter': 0x1C, 'return': 0x1C, 'ctrl': 0x1D,
        'a': 0x1E, 's': 0x1F, 'd': 0x20, 'f': 0x21, 'g': 0x22,
        'h': 0x23, 'j': 0x24, 'k': 0x25, 'l': 0x26, ';': 0x27,
        "'": 0x28, '`': 0x29, 'shift': 0x2A, '\\': 0x2B,
        'z': 0x2C, 'x': 0x2D, 'c': 0x2E, 'v': 0x2F, 'b': 0x30,
        'n': 0x31, 'm': 0x32, ',': 0x33, '.': 0x34, '/': 0x35,
        'space': 0x39, ' ': 0x39, 'capslock': 0x3A,
        'f1': 0x3B, 'f2': 0x3C, 'f3': 0x3D, 'f4': 0x3E, 'f5': 0x3F,
        'f6': 0x40, 'f7': 0x41, 'f8': 0x42, 'f9': 0x43, 'f10': 0x44,
        'f11': 0x57, 'f12': 0x58,
        'esc': 0x01, 'escape': 0x01,
        'del': 0x53, 'delete': 0x53, 'ins': 0x52, 'insert': 0x52,
        'home': 0x47, 'end': 0x4F, 'pgup': 0x49, 'pgdn': 0x51,
        'up': 0x48, 'down': 0x50, 'left': 0x4B, 'right': 0x4D,
        'alt': 0x38,
    }
    # navigation keys share Set-1 codes with the keypad: E0 prefix (KEYEVENTF_EXTENDEDKEY) distinguishes them
    _EXTENDED_SCANCODES = {0x48, 0x50, 0x4B, 0x4D, 0x47, 0x4F, 0x49, 0x51,
                           0x52, 0x53}
    # characters typed with Shift on a US layout
    _SHIFT_CHARS = {
        '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6',
        '&': '7', '*': '8', '(': '9', ')': '0', '_': '-', '+': '=',
        '{': '[', '}': ']', ':': ';', '"': "'", '~': '`', '|': '\\',
        '<': ',', '>': '.', '?': '/',
    }

    def _win_send_scancode(scancode, keyup=False, extended=False):
        flags = _KEYEVENTF_SCANCODE | (_KEYEVENTF_KEYUP if keyup else 0)
        if extended:
            flags |= 0x0001
        extra = ctypes.c_ulong(0)
        ki = _KeyBdInput(0, scancode, flags, 0, ctypes.pointer(extra))
        inp = _Input(_INPUT_KEYBOARD, _InputUnion(ki=ki))
        _user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(inp))

    def _win_press_scancode(scancode, hold=0.03, shift=False, ctrl=False, alt=False):
        for mod in (('ctrl', ctrl), ('alt', alt), ('shift', shift)):
            if mod[1]:
                _win_send_scancode(_SCANCODES[mod[0]], keyup=False)
        extended = scancode in _EXTENDED_SCANCODES
        _win_send_scancode(scancode, keyup=False, extended=extended)
        time.sleep(hold)
        _win_send_scancode(scancode, keyup=True, extended=extended)
        for mod in (('shift', shift), ('alt', alt), ('ctrl', ctrl)):
            if mod[1]:
                _win_send_scancode(_SCANCODES[mod[0]], keyup=True)
        # 20ms between keys dropped characters
        time.sleep(0.06)

    def _win_press_key_name(name, key_delay=40):
        """Send one xdotool-style key token ('F1', 'Return', 'ctrl+alt+Delete')."""
        parts = [p.strip().lower() for p in str(name).split("+") if p.strip()]
        mods = {"ctrl": False, "alt": False, "shift": False}
        base = None
        for p in parts:
            alias = MONITOR_KEY_ALIASES.get(p, p)
            if alias in ("ctrl", "alt", "shift"):
                mods[alias] = True
            else:
                base = alias
        if base is None:
            return
        sc = _SCANCODES.get(base)
        if sc is None:
            print(f"WARNING: no Windows scancode mapped for key {name!r}, skipping")
            return
        _win_press_scancode(sc, hold=max(key_delay, 30) / 1000.0, **mods)

    def _win_type_text(text, key_delay=40):
        for ch in text:
            base = ch.lower()
            shift = ch.isupper() or ch in _SHIFT_CHARS
            if ch in _SHIFT_CHARS:
                base = _SHIFT_CHARS[ch]
            sc = _SCANCODES.get(base)
            if sc is None:
                print(f"WARNING: no Windows scancode mapped for character {ch!r}, skipping")
                continue
            _win_press_scancode(sc, hold=max(key_delay, 30) / 1000.0, shift=shift)

    def _win_click(x, y):
        extra = ctypes.c_ulong(0)
        down = _Input(_INPUT_MOUSE, _InputUnion(mi=_MouseInput(0, 0, 0, _MOUSEEVENTF_LEFTDOWN, 0, ctypes.pointer(extra))))
        up = _Input(_INPUT_MOUSE, _InputUnion(mi=_MouseInput(0, 0, 0, _MOUSEEVENTF_LEFTUP, 0, ctypes.pointer(extra))))
        win32api.SetCursorPos((x, y))
        time.sleep(0.1)
        _user32.SendInput(1, ctypes.pointer(down), ctypes.sizeof(down))
        time.sleep(0.05)
        _user32.SendInput(1, ctypes.pointer(up), ctypes.sizeof(up))

    def _win_find_window_by_title(needle, timeout=25):
        deadline = time.time() + timeout
        found = []

        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and needle.lower() in win32gui.GetWindowText(hwnd).lower():
                found.append(hwnd)
            return True

        while time.time() < deadline:
            found.clear()
            win32gui.EnumWindows(cb, None)
            if found:
                return found[0]
            time.sleep(0.5)
        return None

    def _win_force_foreground(hwnd, timeout=5):
        """Windows foreground: AttachThreadInput borrows the foreground thread's input state, ALT is tapped first (a process that produced the last input may take focus); verifies it took."""
        _user32.keybd_event(win32con.VK_MENU, 0, 0, 0)
        _user32.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        cur_fg = win32gui.GetForegroundWindow()
        cur_thread = win32process.GetWindowThreadProcessId(cur_fg)[0] if cur_fg else 0
        target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
        this_thread = win32api.GetCurrentThreadId()

        attached_a = attached_b = False
        try:
            if cur_thread and cur_thread != this_thread:
                attached_a = _user32.AttachThreadInput(this_thread, cur_thread, True)
            if target_thread and target_thread != this_thread:
                attached_b = _user32.AttachThreadInput(this_thread, target_thread, True)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetActiveWindow(hwnd)
        finally:
            if attached_a:
                _user32.AttachThreadInput(this_thread, cur_thread, False)
            if attached_b:
                _user32.AttachThreadInput(this_thread, target_thread, False)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.2)
        return win32gui.GetForegroundWindow() == hwnd

    def _win_capture_window(hwnd):
        """Capture a window's own rendered content via PrintWindow (PW_RENDERFULLCONTENT for GPU-composited windows; ctypes GDI, no win32ui) - correct under occlusion, unlike a screen-region grab."""
        from ctypes import wintypes
        gdi32 = ctypes.windll.gdi32
        user32 = _user32
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        user32.GetWindowDC.restype = wintypes.HDC
        user32.GetWindowDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]

        class _BitmapInfoHeader(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = max(1, right - left), max(1, bottom - top)
        hwnd_dc = user32.GetWindowDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old = gdi32.SelectObject(mem_dc, bitmap)
        try:
            user32.PrintWindow(hwnd, mem_dc, 2)
            bih = _BitmapInfoHeader()
            bih.biSize = ctypes.sizeof(_BitmapInfoHeader)
            bih.biWidth, bih.biHeight = width, -height
            bih.biPlanes, bih.biBitCount = 1, 32
            buf = ctypes.create_string_buffer(width * height * 4)
            gdi32.SelectObject(mem_dc, old)
            if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(bih), 0):
                raise RuntimeError("_win_capture_window: GetDIBits failed")
            img = Image.frombuffer('RGB', (width, height), buf.raw, 'raw', 'BGRX', 0, 1)
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, hwnd_dc)
        return img


# macOS input/window plumbing (pyobjc Quartz): window lookup by owner PID (CGWindowListCopyWindowInfo); capture via `screencapture -l <window number>` (needs Screen Recording); keyboard via CGEventPostToPid with virtual keycodes and NX_DEVICE* modifier bits (needs Accessibility)
if IS_MACOS:
    try:
        import Quartz
        import ApplicationServices as _AS
        from AppKit import NSRunningApplication
    except ImportError:
        Quartz = None

    # name shown in System Settings > Privacy (the real python path TCC records)
    _mac_py = os.path.realpath(sys.executable)
    _MAC_PERM_HINT = (f"turn on {_mac_py} (listed as "
                      f"\"{os.path.basename(_mac_py)}\") under System Settings > "
                      f"Privacy & Security > Screen Recording and > Accessibility "
                      f"-- test_start_86box requests both, so the entries appear "
                      f"there after its first run -- then restart com.testrunner.app")

    # kVK_* virtual keycodes (HIToolbox Events.h), US ANSI
    _MAC_KEYCODES = {
        'a': 0x00, 's': 0x01, 'd': 0x02, 'f': 0x03, 'h': 0x04, 'g': 0x05,
        'z': 0x06, 'x': 0x07, 'c': 0x08, 'v': 0x09, 'b': 0x0B, 'q': 0x0C,
        'w': 0x0D, 'e': 0x0E, 'r': 0x0F, 'y': 0x10, 't': 0x11, '1': 0x12,
        '2': 0x13, '3': 0x14, '4': 0x15, '6': 0x16, '5': 0x17, '=': 0x18,
        '9': 0x19, '7': 0x1A, '-': 0x1B, '8': 0x1C, '0': 0x1D, ']': 0x1E,
        'o': 0x1F, 'u': 0x20, '[': 0x21, 'i': 0x22, 'p': 0x23, 'l': 0x25,
        'j': 0x26, "'": 0x27, 'k': 0x28, ';': 0x29, '\\': 0x2A, ',': 0x2B,
        '/': 0x2C, 'n': 0x2D, 'm': 0x2E, '.': 0x2F, '`': 0x32,
        'enter': 0x24, 'return': 0x24, 'tab': 0x30, 'space': 0x31,
        'backspace': 0x33, 'esc': 0x35, 'escape': 0x35,
        'kpenter': 0x4C,
        'f1': 0x7A, 'f2': 0x78, 'f3': 0x63, 'f4': 0x76, 'f5': 0x60,
        'f6': 0x61, 'f7': 0x62, 'f8': 0x64, 'f9': 0x65, 'f10': 0x6D,
        'f11': 0x67, 'f12': 0x6F,
        'ins': 0x72, 'insert': 0x72, 'home': 0x73, 'pgup': 0x74,
        'del': 0x75, 'delete': 0x75, 'end': 0x77, 'pgdn': 0x79,
        'left': 0x7B, 'right': 0x7C, 'down': 0x7D, 'up': 0x7E,
    }
    # (keycode, generic CG mask | NX_DEVICEL*KEYMASK) per modifier
    _MAC_MODIFIERS = {
        'shift': (0x38, 0x00020000 | 0x00000002),
        'ctrl':  (0x3B, 0x00040000 | 0x00000001),
        'alt':   (0x3A, 0x00080000 | 0x00000020),
    }
    _MAC_SHIFT_CHARS = {
        '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6',
        '&': '7', '*': '8', '(': '9', ')': '0', '_': '-', '+': '=',
        '{': '[', '}': ']', ':': ';', '"': "'", '~': '`', '|': '\\',
        '<': ',', '>': '.', '?': '/',
    }

    def _mac_windows_for_pid(pid):
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        infos = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
        return [w for w in infos
                if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0]

    def _mac_find_window(pid):
        """Largest normal-layer on-screen window owned by pid (the main 86Box window)."""
        if Quartz is None or not pid:
            return None
        wins = _mac_windows_for_pid(pid)
        if not wins:
            return None
        def area(w):
            b = w.get("kCGWindowBounds") or {}
            return b.get("Width", 0) * b.get("Height", 0)
        best = max(wins, key=area)
        return int(best["kCGWindowNumber"]) if area(best) > 0 else None

    _mac_perms_requested = False

    def _mac_request_permissions():
        """Request Screen Recording and Accessibility from THIS process (pops the system prompt once per binary; returns immediately; once per runner process)."""
        global _mac_perms_requested
        if Quartz is None or _mac_perms_requested:
            return
        _mac_perms_requested = True
        if not Quartz.CGPreflightScreenCaptureAccess():
            Quartz.CGRequestScreenCaptureAccess()
        if not _AS.AXIsProcessTrusted():
            _AS.AXIsProcessTrustedWithOptions({_AS.kAXTrustedCheckOptionPrompt: True})

    def _mac_capture_window(wid, path):
        if not Quartz.CGPreflightScreenCaptureAccess():
            raise RuntimeError(f"no Screen Recording permission -- {_MAC_PERM_HINT}")
        # -x no sound, -o no drop shadow
        subprocess.run(['screencapture', '-x', '-o', '-l', str(wid), path],
                       check=True, capture_output=True, timeout=10)
        img = Image.open(path).convert("RGB")
        # Retina captures at 2x: scale back to 1x
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow, wid) or []
        if info:
            b = info[0].get("kCGWindowBounds") or {}
            w, h = int(b.get("Width", 0)), int(b.get("Height", 0))
            if w > 0 and h > 0 and img.width > w:
                img = img.resize((w, h), Image.LANCZOS)
        img.save(path)
        return img

    def _mac_bring_to_front(pid, timeout=8):
        """Make pid the frontmost app via Accessibility so its key window receives keystrokes -> True once frontmost.
        Re-issues the AX frontmost/raise calls every ~0.3s (a single attempt loses races); timeout 8s for CPU contention.
        Success is read from the app's kAXFrontmostAttribute, not NSWorkspace.frontmostApplication() (stale without a Cocoa run loop).
        """
        app = _AS.AXUIElementCreateApplication(pid)
        deadline = time.time() + timeout
        while time.time() < deadline:
            _AS.AXUIElementSetAttributeValue(app, _AS.kAXFrontmostAttribute, True)
            err, win = _AS.AXUIElementCopyAttributeValue(app, _AS.kAXMainWindowAttribute, None)
            if err == 0 and win is not None:
                _AS.AXUIElementPerformAction(win, _AS.kAXRaiseAction)
            if _mac_is_frontmost(pid):
                return True
            time.sleep(0.3)
        return False

    def _mac_is_frontmost(pid):
        """Fresh per-call AX query (not NSWorkspace; see _mac_bring_to_front)."""
        err, front = _AS.AXUIElementCopyAttributeValue(
            _AS.AXUIElementCreateApplication(pid), _AS.kAXFrontmostAttribute, None)
        return err == 0 and bool(front)

    def _mac_frontmost_desc():
        """Best-effort name of the real frontmost app, for warnings only."""
        err, app = _AS.AXUIElementCopyAttributeValue(
            _AS.AXUIElementCreateSystemWide(), _AS.kAXFocusedApplicationAttribute, None)
        if err != 0 or app is None:
            return f"<unknown, AX error {err}>"
        err, fpid = _AS.AXUIElementGetPid(app, None)
        if err != 0:
            return f"<unknown, AX error {err}>"
        # fresh NSRunningApplication per call (frontmostApplication goes stale)
        ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(fpid)
        name = ra.localizedName() if ra is not None else "?"
        return f"{name} (pid {fpid})"

    def _mac_post_key(pid, keycode, down, flags):
        ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPostToPid(pid, ev)

    def _mac_press(pid, keycode, mods=(), hold=0.03):
        flags = 0
        for m in mods:
            mk, mflag = _MAC_MODIFIERS[m]
            flags |= mflag
            _mac_post_key(pid, mk, True, flags)
            time.sleep(0.02)
        _mac_post_key(pid, keycode, True, flags)
        time.sleep(hold)
        _mac_post_key(pid, keycode, False, flags)
        for m in reversed(list(mods)):
            mk, mflag = _MAC_MODIFIERS[m]
            flags &= ~mflag
            _mac_post_key(pid, mk, False, flags)
            time.sleep(0.02)
        time.sleep(0.02)

    def _mac_press_key_name(pid, name, key_delay=40):
        """Send one xdotool-style key token ('F1', 'Return', 'ctrl+alt+Delete')."""
        mods, base = [], None
        for p in (p.strip().lower() for p in str(name).split("+") if p.strip()):
            alias = MONITOR_KEY_ALIASES.get(p, p)
            if alias in _MAC_MODIFIERS:
                mods.append(alias)
            else:
                base = alias
        if base is None:
            return
        kc = _MAC_KEYCODES.get(base)
        if kc is None:
            print(f"WARNING: no macOS keycode mapped for key {name!r}, skipping")
            return
        _mac_press(pid, kc, mods, hold=max(key_delay, 30) / 1000.0)

    def _mac_type_text(pid, text, key_delay=40):
        for ch in text:
            base = _MAC_SHIFT_CHARS.get(ch, ch.lower())
            shift = ch.isupper() or ch in _MAC_SHIFT_CHARS
            kc = _MAC_KEYCODES.get('space' if ch == ' ' else base)
            if kc is None:
                print(f"WARNING: no macOS keycode mapped for character {ch!r}, skipping")
                continue
            _mac_press(pid, kc, ('shift',) if shift else (),
                       hold=max(key_delay, 30) / 1000.0)


class Box86Monitor:
    """Client for 86Box's remote monitor (--monitor-port): one command per line, one reply line (starts with OK or ERR). Only builds with the remote-monitor patch; opt-in per testlist."""

    def __init__(self, port, host="127.0.0.1", timeout=20):
        self.port = int(port)
        self.host = host
        self.timeout = timeout
        self.sock = None
        self.fp = None
        self.last_error = None

    def connect(self):
        if self.fp:
            return True
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.fp = self.sock.makefile("rwb")
            self.fp.readline()
            return True
        except OSError as e:
            self.last_error = str(e)
            self.close()
            return False

    def wait_ready(self, timeout=30):
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if self.connect():
                return True
            time.sleep(0.5)
        return False

    def cmd(self, line):
        """Send one command. Returns the reply line, or None if the link broke."""
        if not self.connect():
            return None
        try:
            self.fp.write((line + "\n").encode())
            self.fp.flush()
            reply = self.fp.readline().decode(errors="replace").rstrip("\r\n")
        except OSError as e:
            self.last_error = str(e)
            self.close()
            return None
        if not reply:
            self.last_error = "connection closed by 86Box"
            self.close()
            return None
        if reply.startswith("ERR "):
            self.last_error = reply[4:]
        return reply

    def ok(self, line):
        reply = self.cmd(line)
        return bool(reply) and reply.startswith("OK")

    def close(self):
        for h in (self.fp, self.sock):
            try:
                if h:
                    h.close()
            except OSError:
                pass
        self.fp = None
        self.sock = None


# instance name -> Box86Instance (Windows only; set by start()/stop(), used by win_start_stdio_bridge)
_box86_instances = {}
_box86_instances_lock = threading.Lock()


def _find_instance_by_vm_path(vm_path):
    """Registered Windows instance by vm_path (serial_bridge.py passes --vm-a/--vm-b as VM directory paths, as Box86Instance.vm_path), not by instance `name`."""
    target = os.path.abspath(vm_path)
    with _box86_instances_lock:
        for inst in _box86_instances.values():
            if os.path.abspath(inst.vm_path) == target:
                return inst
    return None


def win_start_stdio_bridge(vm_path_a, vm_path_b):
    """Cross-wire two running Windows 86Box instances' COM1 stdio. Windows 86Box binds COM1 to the launching process's stdin/stdout (no PTY), which Box86Instance.start() redirects, so the bridge must run in the runner process.
    serial_bridge.py's Windows branch calls this over the runner's HTTP API. Forwarding is inline in each instance's _read_stdout thread (one reader per pipe).
    """
    inst_a = _find_instance_by_vm_path(vm_path_a)
    inst_b = _find_instance_by_vm_path(vm_path_b)
    if not inst_a or not inst_b:
        missing = vm_path_a if not inst_a else vm_path_b
        return False, f"no running Windows 86Box instance registered for vm_path {missing!r}"
    if not (inst_a.process and inst_a.process.stdin and inst_b.process and inst_b.process.stdin):
        return False, "one or both instances were not started with stdin/stdout redirected"
    inst_a._bridge_partner_stdin = inst_b.process.stdin
    inst_b._bridge_partner_stdin = inst_a.process.stdin
    return True, f"bridging {vm_path_a} <-> {vm_path_b}"


def win_stop_stdio_bridge(vm_path_a, vm_path_b):
    inst_a = _find_instance_by_vm_path(vm_path_a)
    inst_b = _find_instance_by_vm_path(vm_path_b)
    if not inst_a and not inst_b:
        return False, "neither instance is currently registered"
    if inst_a:
        inst_a._bridge_partner_stdin = None
    if inst_b:
        inst_b._bridge_partner_stdin = None
    return True, f"stopped bridging {vm_path_a} <-> {vm_path_b}"


class Box86Instance:
    def __init__(self, name, vm_path, display=None, binary=None, monitor_port=None,
                 logfile=None, window_slot=None, screenshot_scale=None):
        self.name = name
        # tile slot (dispatch_functions._next_window_slot / windowlayout.tile_position)
        self.window_slot = window_slot
        self.vm_path = vm_path
        self.binary = binary or (BOX86_BIN_WIN if IS_WINDOWS else BOX86_BIN)
        # screenshot scale; defaults to OCR_SCALE
        try:
            self.screenshot_scale = float(screenshot_scale) if screenshot_scale not in (None, "") else float(OCR_SCALE)
        except (TypeError, ValueError):
            self.screenshot_scale = float(OCR_SCALE)
        # 86Box log file (-L); off by default; lets test_filecontains gate on emulator-reported values
        self.logfile = logfile
        self.process = None
        self.pid = None
        self.stdout_lines = []
        self.screenshot_count = 0
        # GIF recorder, OCR wait and step screenshots share one window grab
        self._shot_lock = threading.RLock()
        self.xvfb = None
        self.display = display if display == "auto" else resolve_display(display)
        # opt-in: only builds with the remote-monitor patch accept --monitor-port
        if monitor_port in (None, "", False):
            self.monitor_port = None
        elif monitor_port in ("auto", True, 0, "0"):
            self.monitor_port = free_tcp_port()
        else:
            self.monitor_port = int(monitor_port)
        self.monitor = Box86Monitor(self.monitor_port) if self.monitor_port else None

        # 86Box VM-manager IPC (Unix socket path via 86BOX_MANAGER_SOCKET): "shutdownnoprompt" runs the real quit cleanup, which SIGTERM/SIGINT never reach; Linux only, Windows falls back to terminate()/taskkill
        self._manager_sock_path = None if IS_WINDOWS else os.path.join(
            "/tmp", f"86box-mgr-{name}-{os.getpid()}.sock")
        self._manager_listener = None
        self._manager_conn = None
        self._manager_accept_thread = None

        # set by win_start_stdio_bridge(): _read_stdout forwards stdout bytes to the partner's stdin
        self._bridge_partner_stdin = None

    def _env(self):
        env = dict(os.environ)
        if IS_WINDOWS:
            # Windows: prepend the MSYS2 UCRT64 bin dir to PATH so 86Box.exe finds its DLLs
            for d in (r"C:\msys64\ucrt64\bin", r"C:\msys64\mingw64\bin"):
                if os.path.isdir(d):
                    env["PATH"] = d + os.pathsep + env.get("PATH", "")
                    break
            return env
        env["DISPLAY"] = self.display
        env["86BOX_MANAGER_SOCKET"] = self._manager_sock_path
        # cv2 (imported by the runner for OCR) points these at its bundled Qt 5.15.19 xcb plugin; 86Box links the system Qt, and mixing the two aborts at startup
        for k in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR", "QT_PLUGIN_PATH"):
            env.pop(k, None)
        return env

    def _accept_manager_connection(self):
        try:
            conn, _ = self._manager_listener.accept()
            self._manager_conn = conn
        except OSError:
            pass

    def request_graceful_shutdown(self, timeout=10):
        """Ask 86Box to quit via the manager socket (see __init__), waiting up to `timeout` s for exit. False (no exception) if there is no listener, it never connected, or it didn't exit; caller falls back to stop()."""
        if not self.is_alive():
            return True
        deadline = time.time() + timeout
        while self._manager_conn is None and time.time() < deadline:
            time.sleep(0.1)
        if self._manager_conn is None:
            return False
        try:
            self._manager_conn.sendall(b"shutdownnoprompt\n")
        except OSError:
            return False
        while time.time() < deadline:
            if not self.is_alive():
                return True
            time.sleep(0.2)
        return False

    def _start_xvfb(self):
        for num in range(120, 150):
            if os.path.exists(f"/tmp/.X11-unix/X{num}"):
                continue
            proc = subprocess.Popen(
                ["Xvfb", f":{num}", "-screen", "0", "1024x768x24"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            deadline = time.time() + 10
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                if os.path.exists(f"/tmp/.X11-unix/X{num}"):
                    self.xvfb = proc
                    self.display = f":{num}"
                    return True
                time.sleep(0.1)
            if proc.poll() is None:
                proc.terminate()
        return False

    def start(self):
        if IS_MACOS:
            _mac_request_permissions()
        if not IS_WINDOWS and not IS_MACOS and self.display == "auto" and not self._start_xvfb():
            self.stdout_lines.append("Failed to start a private Xvfb.")
            return False

        # listener must exist before launch (86Box connects once, early, no retry); Linux only
        if not IS_WINDOWS:
            try:
                if os.path.exists(self._manager_sock_path):
                    os.remove(self._manager_sock_path)
                self._manager_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._manager_listener.bind(self._manager_sock_path)
                self._manager_listener.listen(1)
                self._manager_accept_thread = threading.Thread(
                    target=self._accept_manager_connection, daemon=True)
                self._manager_accept_thread.start()
            except OSError as e:
                self.stdout_lines.append(f"could not set up manager socket: {e}")
                self._manager_listener = None

        # -N: no quit confirmation dialog; -R: ROM path when bypassing the wrapper (omitted on Windows)
        if IS_WINDOWS:
            args = [self.binary, "-P", self.vm_path, "-N"]
        else:
            args = [self.binary, "-P", self.vm_path, "-R", "/opt/86box/roms", "-N"]
        if self.monitor_port:
            args += ["--monitor-port", str(self.monitor_port)]
        if self.logfile:
            # 86Box appends across runs: start from an empty file
            try:
                os.makedirs(os.path.dirname(self.logfile), exist_ok=True)
                open(self.logfile, "w").close()
            except OSError as e:
                self.stdout_lines.append(f"could not truncate logfile: {e}")
            args += ["-L", self.logfile]
        try:
            if IS_WINDOWS:
                # Windows: pipe stdin and stdout as binary (no PTY; a "stdio" COM device binds to them, see win_start_stdio_bridge); stdout may be raw COM bytes
                self.process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=self._env()
                )
            else:
                self.process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=self._env()
                )
            # pid is 86Box itself (wrapper execs)
            self.pid = self.process.pid
            print("86BOX PID IS: ", self.pid, flush=True)
            print("start command is", args, flush=True)
            processreg.register(self.name, self.process, source="86box")
            if IS_WINDOWS:
                with _box86_instances_lock:
                    _box86_instances[self.name] = self

            self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
            self.stdout_thread.start()
            return True
        except Exception as e:
            self.stdout_lines.append(str(e))
            return False

    def _read_stdout(self):
        if not (self.process and self.process.stdout):
            return
        if IS_WINDOWS:
            # byte-at-a-time: also the read side of win_start_stdio_bridge's relay; bytes are logged to stdout_lines
            buf = bytearray()
            while True:
                chunk = self.process.stdout.read(1)
                if not chunk:
                    break
                partner_stdin = self._bridge_partner_stdin
                if partner_stdin is not None:
                    try:
                        partner_stdin.write(chunk)
                        partner_stdin.flush()
                    except (OSError, ValueError):
                        pass
                buf += chunk
                if chunk == b"\n":
                    self.stdout_lines.append(bytes(buf).decode("utf-8", errors="replace").strip())
                    buf.clear()
            if buf:
                self.stdout_lines.append(bytes(buf).decode("utf-8", errors="replace").strip())
        else:
            for line in self.process.stdout:
                self.stdout_lines.append(line.strip())

    def get_window_id(self):
        if IS_WINDOWS:
            # match by title containing " - 86Box" (not the PID, not the bare VM name, which also matches the "<vm> [COM1]" window)
            vm_name = os.path.basename(self.vm_path.rstrip("\\/"))
            hwnd = _win_find_window_by_title(f"{vm_name} - 86Box", timeout=0.1)
            return hwnd if hwnd else None
        if IS_MACOS:
            return _mac_find_window(self.pid)
        try:
            cmd = ['xdotool', 'search', '--all', '--pid', str(self.pid),
                   '--onlyvisible', '--name', '86Box']
            result = subprocess.run(cmd, capture_output=True, text=True, env=self._env(), timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')[0]
        except Exception:
            pass
        return None

    def _stable_window_geometry(self, wid, tries=10, interval=0.3):
        """Poll window_geometry() until two consecutive reads agree (the Qt window starts at a small pre-POST size); last read (maybe None) after `tries`."""
        last = None
        for _ in range(tries):
            size = windowlayout.window_geometry(wid, self.display)
            if size is not None and size == last:
                return size
            last = size
            time.sleep(interval)
        return last

    def _move_to_origin(self, wid):
        """Snap the window onto its round-robin tile slot (windowlayout.tile_position), clamped by its own size from _stable_window_geometry(), so it stays on screen on WM-less Xvfb and `import -window` doesn't clip."""
        if IS_WINDOWS or IS_MACOS:
            # no tiling on Windows/macOS: window stays where Qt opens it
            return
        win_size = self._stable_window_geometry(wid)
        x, y = windowlayout.tile_position(self.window_slot, win_size, self.display)
        try:
            subprocess.run(['xdotool', 'windowmove', wid, str(x), str(y)],
                           capture_output=True, env=self._env(), timeout=5)
        except Exception as e:
            print(f"WARNING: {self.name} failed to move window {wid} to ({x},{y}): {e}")
            return
        # windowmove places the frame; record the client offset it implies
        time.sleep(0.2)
        after = windowlayout.window_rect(wid, self.display)
        if after is not None and 0 <= after[0] - x <= 64 and 0 <= after[1] - y <= 64:
            self._wm_off = (after[0] - x, after[1] - y)

    def _ensure_onscreen(self, wid):
        """Pull the window back fully onto the display if needed; re-clamps before every capture (86Box can resize after boot)."""
        if IS_WINDOWS or IS_MACOS:
            return
        rect = windowlayout.window_rect(wid, self.display)
        if rect is None:
            return
        x, y, w, h = rect
        screen_w, screen_h = windowlayout.screen_size(self.display)
        # keep the client at least (ox, oy) in so the frame edge stays on screen
        ox, oy = self._wm_offset(wid)
        new_x = max(ox, min(x, screen_w - w))
        new_y = max(oy, min(y, screen_h - h))
        if (new_x, new_y) == (x, y):
            return
        # single placement attempt: repeated moves drift the window by the frame width
        tried = self.__dict__.setdefault("_clamp_tries", {})
        if tried.get((new_x, new_y), 0) >= 2:
            return
        tried[(new_x, new_y)] = tried.get((new_x, new_y), 0) + 1
        # target from the original position (the offset probe nudges the window)
        try:
            subprocess.run(['xdotool', 'windowmove', wid, str(new_x - ox), str(new_y - oy)],
                           capture_output=True, env=self._env(), timeout=5)
        except Exception as e:
            print(f"WARNING: {self.name} failed to re-clamp window {wid} onscreen: {e}")

    def _wm_offset(self, wid):
        """(dx, dy): where windowmove(x, y) really lands the client rect relative to (x, y); measured once per instance; (0, 0) without a WM. A move to client (X, Y) asks for (X-dx, Y-dy)."""
        cached = self.__dict__.get("_wm_off")
        if cached is not None:
            return cached
        off = (0, 0)
        rect = windowlayout.window_rect(wid, self.display)
        if rect is not None:
            try:
                subprocess.run(['xdotool', 'windowmove', wid, str(rect[0]), str(rect[1])],
                               capture_output=True, env=self._env(), timeout=5)
                time.sleep(0.3)
                after = windowlayout.window_rect(wid, self.display)
                if after is not None:
                    off = (after[0] - rect[0], after[1] - rect[1])
                    # restore the position shifted by the probe
                    subprocess.run(['xdotool', 'windowmove', wid,
                                    str(rect[0] - off[0]), str(rect[1] - off[1])],
                                   capture_output=True, env=self._env(), timeout=5)
            except Exception as e:
                print(f"WARNING: {self.name} could not measure the window manager offset: {e}")
        self._wm_off = off
        return off

    def wait_for_ready(self, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            if self.process.poll() is not None:
                return False
            wid = self.get_window_id()
            if wid:
                time.sleep(0.5)
                self._move_to_origin(wid)
                return True
            time.sleep(0.5)
        return False

    def is_alive(self):
        """False once the emulator process has exited. Calling poll() reaps it"""
        return self.process is not None and self.process.poll() is None

    # snapshot timeline (savevm/loadvm/delvm over the remote monitor; driven by snaptimeline.py): snapshots live in the 86Box process; needs monitor_port=, else can_snapshot is False

    @property
    def can_snapshot(self):
        return self.monitor is not None

    def _snapshot_cmd(self, line):
        """Monitor reply, or an "Error: ..." line for snaptimeline's hmp_error()."""
        if not self.monitor:
            return "Error: this 86Box instance has no remote monitor (start it with monitor_port)"
        reply = self.monitor.cmd(line)
        if reply is None:
            return f"Error: remote monitor link failed ({self.monitor.last_error})"
        if reply.startswith("OK"):
            return reply
        return "Error: " + (reply[3:].strip() if reply.startswith("ERR") else reply)

    def save_snapshot(self, tag="quicksave"):
        return self._snapshot_cmd(f"savevm {tag}")

    def load_snapshot(self, tag="quicksave"):
        return self._snapshot_cmd(f"loadvm {tag}")

    def delete_snapshot(self, tag):
        return self._snapshot_cmd(f"delvm {tag}")

    # snapshot files: exportvm/loadfile <path> (path may hold spaces)

    def export_snapshot(self, tag, path):
        return self._snapshot_cmd(f"exportvm {tag} {path}")

    def load_snapshot_file(self, path):
        """Load and restore a snapshot file. The reply is "OK <tag> <state>" or "Error: ..."."""
        return self._snapshot_cmd(f"loadfile {path}")

    def _sleep_while_alive(self, seconds, deadline=None):
        """Sleep up to `seconds`, waking early if the process exits (checked each second) -> False if it exited."""
        end = time.time() + seconds
        if deadline is not None:
            end = min(end, deadline)
        while time.time() < end:
            if not self.is_alive():
                return False
            time.sleep(min(1.0, max(0.0, end - time.time())))
        return self.is_alive()

    def wait_for_post(self, expect, timeout=60, poll=2.0):
        """Poll OCR until `expect` (case-insensitive) shows up."""
        needle = expect.lower()
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            if not self.is_alive():
                return False, f"86Box exited (window closed?)\n{text}"
            ok, text = self.read_screen()
            if ok and needle in text.lower():
                return True, text
            if not self._sleep_while_alive(float(poll), deadline):
                return False, f"86Box exited (window closed?)\n{text}"
        return False, text

    def send_command(self, cmd_text, special_keys=None, key_delay=40, settle=0.0):
        """Type into the 86Box VM.
        settle: seconds to wait after the focus click before the first key (guests drop early keystrokes); xdotool / Win32 / Quartz paths only.
        Prefers the remote monitor when started with one (keyboard controller directly: no focus needed, works with several instances on a display); falls back to xdotool.
        """
        if self.monitor:
            if self._send_via_monitor(cmd_text, special_keys, key_delay):
                return True
            print(f"WARNING: {self.name} monitor input failed "
                  f"({self.monitor.last_error}); falling back to "
                  f"{'Win32 SendInput' if IS_WINDOWS else 'Quartz' if IS_MACOS else 'xdotool'}")
        if IS_WINDOWS:
            return self._send_via_windows(cmd_text, special_keys, key_delay, settle)
        if IS_MACOS:
            return self._send_via_macos(cmd_text, special_keys, key_delay, settle)
        return self._send_via_xdotool(cmd_text, special_keys, key_delay, settle)

    def _send_via_windows(self, cmd_text, special_keys=None, key_delay=40, settle=0.0):
        hwnd = self.get_window_id()
        if not hwnd:
            return False

        if not _win_force_foreground(hwnd):
            print(f"WARNING: {self.name} window {hwnd} did not take the "
                  f"foreground; input would likely be dropped")
            return False

        rect = win32gui.GetWindowRect(hwnd)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        _win_click(cx, cy)
        time.sleep(0.2 + float(settle or 0))

        if cmd_text:
            # line-oriented, as the xdotool path
            lines = str(cmd_text).split("\n")
            for i, line in enumerate(lines):
                if line:
                    _win_type_text(line, key_delay=key_delay)
                if i < len(lines) - 1:
                    _win_press_key_name("Return", key_delay=key_delay)
        if special_keys:
            for key in special_keys:
                _win_press_key_name(key, key_delay=key_delay)
        return True

    def _send_via_macos(self, cmd_text, special_keys=None, key_delay=40, settle=0.0):
        if Quartz is None:
            print(f"WARNING: {self.name} pyobjc (Quartz) not importable; cannot send input", flush=True)
            return False
        if not self.get_window_id():
            # distinguish from _mac_bring_to_front timing out
            print(f"WARNING: {self.name} (pid {self.pid}) has no discoverable "
                  f"on-screen window; cannot send input", flush=True)
            return False
        if not _AS.AXIsProcessTrusted():
            print(f"WARNING: {self.name} no Accessibility permission -- {_MAC_PERM_HINT}", flush=True)
            return False
        if not _mac_bring_to_front(self.pid):
            print(f"WARNING: {self.name} (pid {self.pid}) did not become the "
                  f"frontmost app; input would likely be dropped "
                  f"(frontmost was {_mac_frontmost_desc()})", flush=True)
            return False
        if settle:
            time.sleep(float(settle))
        if cmd_text:
            # line-oriented, as the monitor/xdotool paths
            lines = str(cmd_text).split("\n")
            for i, line in enumerate(lines):
                if line:
                    _mac_type_text(self.pid, line, key_delay)
                if i < len(lines) - 1:
                    _mac_press_key_name(self.pid, "Return", key_delay)
        for key in (special_keys or []):
            _mac_press_key_name(self.pid, key, key_delay)
        return True

    def _send_via_monitor(self, cmd_text, special_keys, key_delay=0):
        """key_delay (ms) is slept between special keys (as the other paths)."""
        if not self.monitor.connect():
            return False

        if cmd_text:
            # monitor 'type' is line-oriented; Enter is separate
            lines = str(cmd_text).split("\n")
            for i, line in enumerate(lines):
                if line and not self.monitor.ok("type " + line):
                    return False
                if (i < len(lines) - 1) and not self.monitor.ok("sendkey enter"):
                    return False

        for i, key in enumerate(special_keys or []):
            if i and key_delay:
                time.sleep(float(key_delay) / 1000.0)
            if not self.monitor.ok("sendkey " + monitor_key(key)):
                return False

        return True

    def _send_via_xdotool(self, cmd_text, special_keys=None, key_delay=40, settle=0.0):
        wid = self.get_window_id()
        if not wid:
            return False

        env = self._env()
        try:
            # windowfocus, not windowactivate (no WM: windowactivate clicks at screen (0,0))
            subprocess.run(['xdotool', 'windowfocus', '--sync', wid],
                           capture_output=True, env=env, timeout=5)

            # click inside our window at a fixed (200, 150) client offset to force focus (computed center can be (0,0))
            subprocess.run(['xdotool', 'mousemove', '--window', wid, '200', '150',
                            'click', '1'], capture_output=True, env=env, timeout=5)
            subprocess.run(['xdotool', 'windowfocus', '--sync', wid],
                           capture_output=True, env=env, timeout=5)

            focused = subprocess.run(['xdotool', 'getwindowfocus'],
                                     capture_output=True, text=True, env=env, timeout=5)
        except subprocess.TimeoutExpired:
            print(f"WARNING: {self.name} window {wid} did not respond to "
                  f"xdotool (closed mid-command?); treating as focus failure")
            return False

        if focused.stdout.strip() != wid:
            print(f"WARNING: {self.name} window {wid} did not take focus "
                  f"(focus is {focused.stdout.strip()}); input would be dropped")
            return False

        if settle:
            time.sleep(float(settle))

        try:
            if cmd_text:
                # xdotool type does not send "\n" as Enter: split cmd_text per line
                lines = str(cmd_text).split("\n")
                for i, line in enumerate(lines):
                    if line:
                        subprocess.run(['xdotool', 'type', '--delay', str(key_delay), line],
                                       env=env, timeout=30)
                    if i < len(lines) - 1:
                        subprocess.run(['xdotool', 'key', 'Return'], env=env, timeout=5)
            if special_keys:
                for key in special_keys:
                    subprocess.run(['xdotool', 'key', _xdotool_key(key)], env=env, timeout=5)
        except subprocess.TimeoutExpired:
            print(f"WARNING: {self.name} window {wid} stopped responding "
                  f"while sending input (closed mid-command?)")
            return False
        return True

    def take_screenshot(self, test_step=None, filename=None):
        """Serialized: a concurrent grab (GIF recorder) would otherwise swap last_image / bump screenshot_count under the caller, desyncing its PNG from its OCR text."""
        with self._shot_lock:
            return self._take_screenshot_locked(test_step, filename)

    def _take_screenshot_locked(self, test_step=None, filename=None):
        if APPBASE_DIR not in sys.path:
            sys.path.insert(0, APPBASE_DIR)
        from appstate import current_reports_dir
        reports_dir = current_reports_dir()

        step_str = f"-{test_step}" if test_step else ""
        name = filename if filename else f"screenshot-{self.name}{step_str}-{self.screenshot_count}.png"
        path = os.path.join(reports_dir, name) if not filename else os.path.abspath(filename)

        self.last_image = None
        wid = self.get_window_id()
        if not wid:
            return False, "Window ID not found"

        self._ensure_onscreen(wid)

        try:
            if IS_WINDOWS:
                img = _win_capture_window(wid).convert("RGB")
                img.save(path)
            elif IS_MACOS:
                img = _mac_capture_window(wid, path)
            else:
                subprocess.run(['import', '-window', wid, path], check=True, env=self._env(), timeout=10)
                img = Image.open(path).convert("RGB")
            if self.screenshot_scale != 1.0:
                img = img.resize(
                    (max(1, round(img.width * self.screenshot_scale)),
                     max(1, round(img.height * self.screenshot_scale))),
                    Image.LANCZOS
                )
                img.save(path)
            self.last_image = img
            self.screenshot_count += 1
            print("DEBUG: SCREENSHOT TAKEN AT: ", path)
            return True, path
        except Exception as e:
            print("FAILED TO TAKE SCREENSHOT", e)
            return False, str(e)

    def read_screen(self):
        """OCR the current screen -> (ok, text); take_screenshot() already upscales by screenshot_scale."""
        import tempfile
        ocr_path = os.path.join(tempfile.gettempdir(), f"{self.name}-ocr.png")
        ok, path = self.take_screenshot(filename=ocr_path)
        if not ok:
            return False, path
        tesseract_bin = TESSERACT_BIN
        if IS_WINDOWS and os.path.isfile(TESSERACT_BIN_WIN):
            tesseract_bin = TESSERACT_BIN_WIN
        cmd = [tesseract_bin, path, 'stdout', '--psm', '6'] + TESSERACT_TESSDATA_ARGS.split()
        try:
            result = subprocess.run(cmd,
                                    capture_output=True, text=True, check=True)
            return True, result.stdout
        except Exception as e:
            return False, str(e)

    def stop(self):
        if self.monitor:
            self.monitor.close()
        processreg.unregister(self.name)
        if IS_WINDOWS:
            with _box86_instances_lock:
                _box86_instances.pop(self.name, None)
            self._bridge_partner_stdin = None
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if IS_WINDOWS:
            # process may be a launcher stub: kill the window's process tree by PID (never taskkill /IM 86Box.exe)
            hwnd = self.get_window_id()
            if hwnd:
                try:
                    _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
                    subprocess.run(['taskkill', '/F', '/PID', str(win_pid), '/T'],
                                    capture_output=True, timeout=10)
                except Exception as e:
                    print(f"WARNING: {self.name} failed to taskkill window process: {e}")
        if self.xvfb:
            self.xvfb.terminate()
            try:
                self.xvfb.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.xvfb.kill()
                self.xvfb.wait()
            self.xvfb = None
        if self._manager_conn:
            try:
                self._manager_conn.close()
            except OSError:
                pass
            self._manager_conn = None
        if self._manager_listener:
            try:
                self._manager_listener.close()
            except OSError:
                pass
            self._manager_listener = None
        try:
            if self._manager_sock_path and os.path.exists(self._manager_sock_path):
                os.remove(self._manager_sock_path)
        except OSError:
            pass


class Box86Conf:
    """Minimal editor for 86box.cfg"""

    def __init__(self, filepath):
        self.filepath = filepath

        with open(filepath, "r", newline="") as f:
            raw = f.read()
        # convert line endings
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = raw.splitlines(keepends=True)

    def set(self, section, key, value):
        out, in_section, done = [], False, False
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith("["):
                if in_section and not done:
                    out.append(f"{key} = {value}\n")
                    done = True
                in_section = stripped == f"[{section}]"
            elif in_section and stripped.split("=")[0].strip() == key:
                line = f"{key} = {value}\n"
                done = True
            out.append(line)
        if not done:
            if not any(l.strip() == f"[{section}]" for l in out):
                out.append(f"\n[{section}]\n")
            else:
                idx = next(i for i, l in enumerate(out) if l.strip() == f"[{section}]")
                out.insert(idx + 1, f"{key} = {value}\n")
                self.lines = out
                return
            out.append(f"{key} = {value}\n")
        self.lines = out

    def save(self, outpath=None):
        path = outpath if outpath else self.filepath
        print('SAVING NEW 86BOX CONF TO PATH: ', path)
        # newline="": write \r\n literally
        with open(path, "w", newline="") as f:
            f.write("".join(self.lines).replace("\n", "\r\n"))
