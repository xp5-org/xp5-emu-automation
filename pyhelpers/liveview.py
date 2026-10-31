"""Read-only live view of what this project has running; backs the runner's `/api/v1/instances` surface (runner loads this module by name).
Discovery, merged:
  * run   live objects from the context dict the runner publishes (appstate.live_context): QemuInstance / DosboxInstance / NovaSimhInstance, including process-only state (NovaSimhInstance.buf).
  * scan  /proc walk for known emulator binaries (hand-launched start-qemu-*.sh): only X window and QMP port are reachable.
Capabilities are reported per instance, not per project (Nova under SimH: `tty`, no screenshot; scanned QEMU: `screenshot`, no `tty`). Read-only except `media`.
Capability vocabulary (shared with other projects' liveview.py):
    screenshot  PNG of the current display
    ocr         text read off the current display
    tty         captured console/serial text
    media       list + swap removable media
    stats       JSON counters/gauges/events the program publishes about itself (Verilator testbenches, vstats.hpp)
"""

import os
import re
import subprocess
import sys
import time

_HELPERDIR = os.path.dirname(os.path.abspath(__file__))
if _HELPERDIR not in sys.path:
    sys.path.insert(0, _HELPERDIR)

import displayhelpers
import qemuhelpers


# live captures go in their own dir, kept out of the reports dir's screenshot-<name>-<step>.png scan
LIVE_DIR = "/testrunnerapp/reports/live"

CAPABILITY_DOC = {
    "screenshot": "GET the current display as a PNG.",
    "ocr":        "GET the text on the current display (tesseract).",
    "tty":        "GET the console/serial text captured so far.",
    "media":      "List removable drive slots, and attach/detach media.",
    "screentext": "GET an exact character-cell read of the screen via the VICE binary monitor (no OCR guessing).",
    "stats":      "GET the counters, gauges and recent events the program publishes about itself.",
    "keys":       "POST keystrokes to the guest: a string to type, a single key "
                  "token, or a sequence. Writes to the guest — sending keys "
                  "into a VM that a test is driving will corrupt that test.",
}

# character -> QEMU key name (+ shift flag) for `sendkey`; names per qemu/ui/input-keymap.c, shifted forms assume a US guest layout

_KEYMAP = {
    ' ': 'spc',  '\n': 'ret', '\r': 'ret', '\t': 'tab',
    '.': 'dot',  ',': 'comma', '-': 'minus', '=': 'equal',
    '/': 'slash', '\\': 'backslash', ';': 'semicolon', "'": 'apostrophe',
    '[': 'bracket_left', ']': 'bracket_right', '`': 'grave_accent',
}
_KEYMAP.update({c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"})

# character -> unshifted key pressed with shift held
_SHIFTED = {
    ':': 'semicolon', '"': 'apostrophe', '<': 'comma',  '>': 'dot',
    '?': 'slash',     '_': 'minus',      '+': 'equal',  '|': 'backslash',
    '{': 'bracket_left', '}': 'bracket_right', '~': 'grave_accent',
    '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
    '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
}

# caller-friendly aliases -> QEMU key names
_KEY_SYNONYMS = {
    'enter': 'ret', 'return': 'ret', 'newline': 'ret', 'cr': 'ret',
    'escape': 'esc', 'space': 'spc', 'del': 'delete', 'backspace': 'backspace',
    'pgup': 'pgup', 'pgdn': 'pgdn', 'ins': 'insert',
}


def _char_to_token(ch):
    """One character -> a QEMU sendkey token, or None if it can't be typed."""
    if ch in _SHIFTED:
        return f"shift-{_SHIFTED[ch]}"
    if ch.isupper() and ch.lower() in _KEYMAP:
        return f"shift-{_KEYMAP[ch.lower()]}"
    if ch in _KEYMAP:
        return _KEYMAP[ch]
    return None


def _normalise_token(tok):
    """Normalise one key token keeping modifier prefixes: 'ctrl-alt-del' -> 'ctrl-alt-delete', 'Enter' -> 'ret', 'F3' -> 'f3'."""
    parts = str(tok).strip().lower().split("-")
    # trailing empty part = token ended in '-' (e.g. "shift--"): the '-' is the key
    if parts and parts[-1] == "":
        parts = parts[:-1] + ["minus"]
    if not parts:
        return None
    key = parts[-1]
    key = _KEY_SYNONYMS.get(key, key)
    if key == "del":
        key = "delete"
    return "-".join(parts[:-1] + [key])


def _build_tokens(text=None, key=None, keys=None, enter=False):
    """Caller input -> ordered sendkey tokens: (tokens, unsupported_chars)."""
    tokens, bad = [], []

    if text:
        for ch in str(text):
            tok = _char_to_token(ch)
            if tok is None:
                bad.append(ch)
            else:
                tokens.append(tok)

    if key:
        tok = _normalise_token(key)
        if tok:
            tokens.append(tok)

    if keys:
        if isinstance(keys, str):
            keys = [k for k in re.split(r"[,\s]+", keys.strip()) if k]
        for k in keys:
            tok = _normalise_token(k)
            if tok:
                tokens.append(tok)

    if enter:
        tokens.append("ret")

    return tokens, bad


def _live_dir():
    os.makedirs(LIVE_DIR, exist_ok=True)
    return LIVE_DIR


def _capture_path(iid, ext="png"):
    """One capture file per instance, overwritten each capture."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", iid)
    return os.path.join(_live_dir(), f"live-{safe}.{ext}")


def _env():
    e = os.environ.copy()
    e["DISPLAY"] = displayhelpers.resolve_display(e.get("DISPLAY"))
    return e


def _xdo(*args):
    try:
        return subprocess.run(["xdotool", *args], capture_output=True,
                              text=True, env=_env()).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _window_for_pid(pid, min_width=200, name_hint=None):
    """Emulator's on-screen window by pid, filtered on width (skips tiny utility windows); `name_hint` picks among several (VICE C128: 40col vs 80col)."""
    if not pid:
        return None
    best = None
    for wid in _xdo("search", "--pid", str(pid)).split():
        geo = dict(l.split("=", 1) for l in
                   _xdo("getwindowgeometry", "--shell", wid).splitlines() if "=" in l)
        try:
            width = int(geo.get("WIDTH", 0))
        except ValueError:
            continue
        if width < min_width:
            continue
        if name_hint and name_hint.lower() in (_xdo("getwindowname", wid) or "").lower():
            return wid
        if best is None:
            best = wid
    return best


def _grab_window(pid, path, name_hint=None, activate=False):
    """Screenshot an X window with `import`. Returns (ok, path_or_error). `activate` raises it first: an obscured SDL window grabs as garbage."""
    wid = _window_for_pid(pid, name_hint=name_hint)
    if not wid:
        return False, ("no X window for pid %s — the emulator is headless, or is "
                       "not on DISPLAY %s" % (pid, _env().get("DISPLAY")))
    try:
        if activate:
            _xdo("windowactivate", wid)
            time.sleep(0.2)
        subprocess.run(["import", "-window", wid, path],
                       check=True, capture_output=True, env=_env())
    except subprocess.CalledProcessError as e:
        return False, f"import failed: {e.stderr.decode('utf-8', 'replace').strip() or e}"
    except OSError as e:
        return False, f"import failed: {e}"
    return (os.path.exists(path) and os.path.getsize(path) > 0), path


def _ocr_png(path):
    """Text of a PNG via tesseract -> (ok, text_or_error); --psm 6 and TESSERACT_TESSDATA_ARGS as test_ocrwordsearch (qemuhelpers.py)."""
    cmd = ["tesseract", path, "stdout", "--psm", "6"] + qemuhelpers.TESSERACT_TESSDATA_ARGS.split()
    try:
        r = subprocess.run(cmd,
                           capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        return False, f"tesseract failed: {(e.stderr or '').strip() or e}"
    except OSError as e:
        return False, f"tesseract not available: {e}"
    return True, r.stdout


def _pid_of(obj):
    """OS pid behind an instance object (.process for QemuInstance/DosboxInstance, .proc for ViceInstance, bare .pid for NovaSimhInstance)."""
    for attr in ("process", "proc"):
        p = getattr(obj, attr, None)
        if p is not None and getattr(p, "pid", None):
            return p.pid
    pid = getattr(obj, "pid", None)
    return pid if isinstance(pid, int) else None


def _alive(pid, obj=None):
    """True if this pid is a running process. Prefers the instance's own is_alive()/.process.poll() (no /proc needed: Windows/macOS); /proc state letter check ('Z' = zombie) only for scan entries on Linux."""
    if obj is not None:
        is_alive = getattr(obj, "is_alive", None)
        if callable(is_alive):
            try:
                return bool(is_alive())
            except Exception:
                pass
        proc = getattr(obj, "process", None) or getattr(obj, "proc", None)
        if proc is not None and hasattr(proc, "poll"):
            try:
                return proc.poll() is None
            except Exception:
                pass
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # comm (field 2) may contain spaces/parens: index past its close
        state = data[data.rindex(")") + 2]
    except (OSError, ValueError, IndexError):
        return False
    return state != "Z"


# per-kind adapter: matches(ctx), scan(), caps + capability methods

class _Adapter:
    kind = "?"

    @classmethod
    def matches(cls, obj):
        return False

    @classmethod
    def scan(cls):
        return []

    @classmethod
    def caps(cls, entry):
        return []

    @classmethod
    def label(cls, entry):
        return entry.get("label") or cls.kind

    @classmethod
    def screenshot(cls, entry, path):
        return False, f"{cls.kind}: no screenshot capability"

    @classmethod
    def tty(cls, entry):
        return False, f"{cls.kind}: no tty capability"

    @classmethod
    def stats(cls, entry):
        return False, f"{cls.kind}: no stats capability"


class _QemuAdapter(_Adapter):
    kind = "qemu"

    @classmethod
    def matches(cls, obj):
        # 86Box also has monitor_port; exclude it from the QEMU match
        tname = type(obj).__name__.lower()
        if "box86" in tname or "86box" in tname:
            return False
        return hasattr(obj, "monitor_port") and hasattr(obj, "take_screenshot")

    @classmethod
    def scan(cls):
        out = []
        for inst in qemuhelpers.list_instances():
            out.append({
                "kind": cls.kind,
                "pid": inst["pid"],
                "label": inst.get("label"),
                "source": "scan",
                "detail": inst,
            })
        return out

    @classmethod
    def caps(cls, entry):
        caps = ["screenshot", "ocr"]
        detail = entry.get("detail") or {}
        obj = entry.get("obj")
        # media swap needs a QMP port (runner VMs hold the HMP socket)
        if detail.get("qmp_port") or detail.get("controllable"):
            caps.append("media")
        if obj is not None and getattr(obj, "serial_path", None):
            caps.append("tty")
        # typing needs a monitor channel
        if (obj is not None and getattr(obj, "sock", None)) or \
                detail.get("qmp_port") or detail.get("monitor_port"):
            caps.append("keys")
        return caps

    @classmethod
    def send_keys(cls, entry, tokens, delay=0.05):
        """Emit sendkey tokens on the usable monitor channel: live object (holds the single-client HMP socket), then QMP, then direct HMP."""
        detail = entry.get("detail") or {}
        obj = entry.get("obj")

        if obj is not None and getattr(obj, "sock", None):
            for tok in tokens:
                obj.send_key(tok, delay=delay)
            return True, "instance-monitor"

        qmp_port = detail.get("qmp_port")
        if qmp_port:
            for tok in tokens:
                ok, text = qemuhelpers._hmp_via_qmp(qmp_port, f"sendkey {tok}")
                if not ok:
                    return False, f"qmp sendkey {tok}: {text}"
                time.sleep(delay)
            return True, "qmp"

        port = detail.get("monitor_port")
        if port:
            for tok in tokens:
                ok, text = qemuhelpers._hmp(port, f"sendkey {tok}")
                if not ok:
                    return False, f"hmp sendkey {tok}: {text}"
                time.sleep(delay)
            return True, "hmp"

        return False, "no monitor channel to send keys on"

    @classmethod
    def screenshot(cls, entry, path):
        """Capture ladder: QMP `screendump` (concurrency-safe), else GTK window grab off X, else HMP (when nothing else holds it)."""
        detail = entry.get("detail") or {}
        obj = entry.get("obj")
        errors = []

        # live object owns the monitor socket
        if obj is not None and hasattr(obj, "take_screenshot"):
            try:
                ok, res = obj.take_screenshot(test_step="live", filename=path)
                if ok:
                    return True, path
                errors.append(f"instance.take_screenshot: {res}")
            except Exception as e:
                errors.append(f"instance.take_screenshot raised {type(e).__name__}: {e}")

        qmp_port = detail.get("qmp_port")
        if qmp_port:
            ppm = path[:-4] + ".ppm" if path.endswith(".png") else path + ".ppm"
            _unlink(ppm)
            ok, text = qemuhelpers._hmp_via_qmp(qmp_port, f'screendump "{ppm}"')
            if ok and _wait_for_file(ppm):
                conv_ok, conv = _ppm_to_png(ppm, path)
                if conv_ok:
                    return True, path
                errors.append(conv)
            else:
                errors.append(f"qmp screendump: {text}")

        pid = entry.get("pid")
        ok, res = _grab_window(pid, path)
        if ok:
            return True, path
        errors.append(res)

        port = detail.get("monitor_port")
        if port and not detail.get("_hmp_held"):
            ppm = path[:-4] + ".ppm" if path.endswith(".png") else path + ".ppm"
            _unlink(ppm)
            ok, text = qemuhelpers._hmp(port, f'screendump "{ppm}"')
            if ok and _wait_for_file(ppm):
                conv_ok, conv = _ppm_to_png(ppm, path)
                if conv_ok:
                    return True, path
                errors.append(conv)
            else:
                errors.append(f"hmp screendump: {text}")

        return False, "; ".join(errors) or "no capture channel available"

    @classmethod
    def tty(cls, entry):
        obj = entry.get("obj")
        path = getattr(obj, "serial_path", None) if obj is not None else None
        if not path or not os.path.exists(path):
            return False, "no serial capture file for this instance"
        try:
            with open(path, "r", errors="replace") as f:
                return True, f.read()
        except OSError as e:
            return False, f"cannot read {path}: {e}"


class _XWindowAdapter(_Adapter):
    """Emulators driven through an X window (DOSBox-X, 86Box, Basilisk II): screenshot with `import`, screen by OCR."""
    binaries = ()
    attrs = ()

    @classmethod
    def matches(cls, obj):
        return all(hasattr(obj, a) for a in cls.attrs)

    @classmethod
    def scan(cls):
        out = []
        for pid, argv in _iter_procs():
            binary = os.path.basename(argv[0])
            if binary not in cls.binaries:
                continue
            out.append({
                "kind": cls.kind,
                "pid": pid,
                "label": cls.kind,
                "source": "scan",
                "detail": {"binary": binary, "cmdline": " ".join(argv)},
            })
        return out

    @classmethod
    def caps(cls, entry):
        return ["screenshot", "ocr"]

    @classmethod
    def screenshot(cls, entry, path):
        obj = entry.get("obj")
        if obj is not None and hasattr(obj, "take_screenshot"):
            try:
                ok, res = obj.take_screenshot(test_step="live", filename=path)
                if ok:
                    return True, path
            except Exception as e:
                res = f"{type(e).__name__}: {e}"
            # still try a plain window grab if the instance grab fails
            ok, res2 = _grab_window(entry.get("pid"), path)
            return (True, path) if ok else (False, f"{res}; {res2}")
        return _grab_window(entry.get("pid"), path)


class _DosboxAdapter(_XWindowAdapter):
    kind = "dosbox"
    binaries = ("dosbox-x", "dosbox")
    attrs = ("get_window_id", "read_screen", "conf_path")

    @classmethod
    def matches(cls, obj):
        return (hasattr(obj, "get_window_id") and hasattr(obj, "read_screen")
                and "dosbox" in type(obj).__name__.lower())


class _Box86Adapter(_XWindowAdapter):
    kind = "86box"
    binaries = ("86box", "86Box")

    @classmethod
    def matches(cls, obj):
        return "box86" in type(obj).__name__.lower() or "86box" in type(obj).__name__.lower()


class _BasiliskAdapter(_XWindowAdapter):
    kind = "basilisk"
    binaries = ("BasiliskII",)

    @classmethod
    def matches(cls, obj):
        return "basilisk" in type(obj).__name__.lower()


class _SdlAdapter(_XWindowAdapter):
    """8-bit emulators run by the CC65 projects (VICE, linapple, Hatari): X window grab, tty from the instance's own output buffer."""
    name_hint = None

    @classmethod
    def caps(cls, entry):
        caps = ["screenshot", "ocr"]
        if entry.get("obj") is not None:
            caps.append("tty")
        return caps

    @classmethod
    def screenshot(cls, entry, path):
        obj = entry.get("obj")
        if obj is not None and hasattr(obj, "take_screenshot"):
            try:
                ok, res = obj.take_screenshot(test_step="live", filename=path)
                if ok:
                    # some write to their own reports path and return it, ignoring `filename`
                    return True, res if isinstance(res, str) and os.path.exists(res) else path
                err = str(res)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            # the instance's own grab can fail where a plain window grab works (stale cached window id)
            ok, res2 = _grab_window(entry.get("pid"), path, cls.name_hint, activate=True)
            return (True, path) if ok else (False, f"{err}; {res2}")
        return _grab_window(entry.get("pid"), path, cls.name_hint, activate=True)

    @classmethod
    def tty(cls, entry):
        obj = entry.get("obj")
        if obj is None:
            return False, ("this instance was not started by the loaded run, so "
                           "its buffered output is not in this process")
        try:
            out = obj.get_output()
        except Exception as e:
            return False, f"get_output failed: {type(e).__name__}: {e}"
        return True, "".join(out) if isinstance(out, (list, tuple)) else str(out or "")


# VICE ships one binary per machine; any of them may be the one running
VICE_BINARIES = ("x64sc", "x64", "x128", "xvic", "xplus4", "xpet",
                 "x64dtv", "xcbm2", "xcbm5x0", "xscpu64")


class _ViceAdapter(_SdlAdapter):
    kind = "vice"
    binaries = VICE_BINARIES
    name_hint = "VICE"

    @classmethod
    def matches(cls, obj):
        return (hasattr(obj, "screentextdump") and hasattr(obj, "take_screenshot")
                and hasattr(obj, "port"))

    @classmethod
    def scan(cls):
        out = super().scan()
        for e in out:
            e["label"] = e["detail"]["binary"]
        return out

    @classmethod
    def label(cls, entry):
        obj = entry.get("obj")
        if obj is not None:
            return f"{getattr(obj, 'name', 'vice')} ({getattr(obj, 'archtype', '?')})"
        return entry.get("label") or cls.kind

    @classmethod
    def caps(cls, entry):
        caps = super().caps(entry)
        # screentextdump drives the binary monitor, which a running test owns
        if entry.get("obj") is not None and not _run_busy():
            caps.append("screentext")
        return caps

    @classmethod
    def screentext(cls, entry):
        obj = entry.get("obj")
        if obj is None:
            return False, "not a run instance"
        try:
            sys.path.insert(0, "/testrunnerapp")
            from appstate import live_context
            text = obj.screentextdump(live_context.context())
        except Exception as e:
            return False, f"screentextdump failed: {type(e).__name__}: {e}"
        if text is None:
            return False, "screentextdump returned nothing (emulator not running?)"
        return True, text


class _AppleAdapter(_SdlAdapter):
    kind = "apple2"
    binaries = ("linapple",)
    name_hint = "apple"

    @classmethod
    def matches(cls, obj):
        return "apple" in type(obj).__name__.lower() and hasattr(obj, "take_screenshot")


class _AtariAdapter(_SdlAdapter):
    kind = "atari_st"
    binaries = ("hatari",)
    name_hint = "hatari"

    @classmethod
    def matches(cls, obj):
        return "hatari" in type(obj).__name__.lower()

    @classmethod
    def caps(cls, entry):
        caps = super().caps(entry)
        # hatari is launched with --rs232-out, so there is a serial log on disk even for an instance this process did not start
        if entry.get("obj") is None and os.path.exists("/tmp/atariout.txt"):
            caps.append("tty")
        return caps

    @classmethod
    def tty(cls, entry):
        if entry.get("obj") is None:
            try:
                with open("/tmp/atariout.txt", "r", errors="replace") as f:
                    return True, f.read()
            except OSError as e:
                return False, f"cannot read /tmp/atariout.txt: {e}"
        return super().tty(entry)


class _SimhAdapter(_Adapter):
    """SimH (Nova/RDOS): teletype only; screen = pty buffer in the runner's memory, so a scanned instance is listed but unreadable."""
    kind = "simh"

    @classmethod
    def matches(cls, obj):
        return hasattr(obj, "buf") and hasattr(obj, "master_fd")

    @classmethod
    def scan(cls):
        out = []
        for pid, argv in _iter_procs():
            if "dgnova" not in os.path.basename(argv[0]):
                continue
            out.append({
                "kind": cls.kind,
                "pid": pid,
                "label": "nova",
                "source": "scan",
                "detail": {"binary": os.path.basename(argv[0]),
                           "cmdline": " ".join(argv),
                           "note": "console text is only readable while the run "
                                   "that started it is loaded — the pty buffer "
                                   "lives in the runner process."},
            })
        return out

    @classmethod
    def caps(cls, entry):
        return ["tty"] if entry.get("obj") is not None else []

    @classmethod
    def tty(cls, entry):
        obj = entry.get("obj")
        if obj is None:
            return False, ("this SimH instance was not started by the loaded run, "
                           "so its console buffer is not in this process")
        return True, getattr(obj, "buf", "") or ""


class _HostBuildAdapter(_Adapter):
    """test_hostbuild subprocess (dispatch_functions.HostBuildInstance): host shell script/build, no display."""
    kind = "hostbuild"

    @classmethod
    def matches(cls, obj):
        return hasattr(obj, "log_path") and hasattr(obj, "process")

    @classmethod
    def caps(cls, entry):
        return ["tty"]

    @classmethod
    def tty(cls, entry):
        obj = entry.get("obj")
        path = getattr(obj, "log_path", None) if obj is not None else None
        if not path or not os.path.exists(path):
            return False, "no output captured yet for this host build"
        try:
            with open(path, "r", errors="replace") as f:
                return True, f.read()
        except OSError as e:
            return False, f"cannot read {path}: {e}"


class _FbMonitorAdapter(_Adapter):
    """pygame window fed by a live framebuffer sink (NovaFbMonitorProcess, VerilatorFbMonitorProcess); matched by class name."""
    classnames = ()

    @classmethod
    def matches(cls, obj):
        return type(obj).__name__ in cls.classnames

    @classmethod
    def caps(cls, entry):
        return ["screenshot"]

    @classmethod
    def screenshot(cls, entry, path):
        return _grab_window(entry.get("pid"), path)


class _NovaFbMonitorAdapter(_FbMonitorAdapter):
    kind = "novafb_monitor"
    classnames = ("NovaFbMonitorProcess",)


class _VerilatorFbMonitorAdapter(_FbMonitorAdapter):
    kind = "verilatorfb_monitor"
    classnames = ("VerilatorFbMonitorProcess",)


class _VerilatorAdapter(_Adapter):
    """Compiled Verilator testbench (VerilatorLiveInstance): headless; shown as running only."""
    kind = "verilator"

    @classmethod
    def matches(cls, obj):
        return type(obj).__name__ == "VerilatorLiveInstance"

    @classmethod
    def scan(cls):
        """Testbenches publishing a vstats snapshot (hand-launched or from another run), found by their stats file."""
        import glob
        import verilatorhelpers
        out = []
        for path in sorted(glob.glob(os.path.join(verilatorhelpers.DEFAULT_STATS_DIR, "vstats-*.json"))):
            data = verilatorhelpers.StatsReader(path).read()
            if not data or not data.get("alive"):
                continue
            out.append({
                "kind": cls.kind,
                "pid": data.get("pid"),
                "label": data.get("name"),
                "source": "scan",
                "detail": {"stats_path": path, "top": data.get("top")},
            })
        return out

    @classmethod
    def _path(cls, entry):
        obj = entry.get("obj")
        return getattr(obj, "stats_path", None) or (entry.get("detail") or {}).get("stats_path")

    @classmethod
    def caps(cls, entry):
        return ["stats"] if cls._path(entry) else []

    @classmethod
    def stats(cls, entry):
        import verilatorhelpers
        path = cls._path(entry)
        if not path:
            return False, "this testbench was started without --stats"
        data = verilatorhelpers.StatsReader(path).read()
        if data is None:
            return False, f"no stats snapshot at {path} yet"
        return True, data


ADAPTERS = (_QemuAdapter, _DosboxAdapter, _Box86Adapter, _BasiliskAdapter, _SimhAdapter,
            _HostBuildAdapter, _NovaFbMonitorAdapter, _VerilatorFbMonitorAdapter, _VerilatorAdapter,
            _ViceAdapter, _AppleAdapter, _AtariAdapter)


def _iter_procs():
    """(pid, argv) for every readable process."""
    for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):
        argv = qemuhelpers._read_cmdline(pid)
        if not argv:
            continue
        yield int(pid), [a.decode("utf-8", "replace") for a in argv]


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _wait_for_file(path, timeout=5.0):
    """screendump returns before QEMU has finished writing the file."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            time.sleep(0.05)
            return True
        time.sleep(0.05)
    return False


def _ppm_to_png(ppm, png):
    try:
        from PIL import Image
        Image.open(ppm).save(png)
    except Exception as e:
        return False, f"cannot convert {ppm}: {e}"
    finally:
        _unlink(ppm)
    return True, png


def _live_entries():
    """Instances of the loaded run, from the published context (duck-typed: `context[name] = instance`)."""
    try:
        sys.path.insert(0, "/testrunnerapp")
        from appstate import live_context
    except Exception:
        return []

    entries = []
    seen = set()
    for key, obj in list(live_context.context().items()):
        if key in ("sock", "abort") or obj is None or isinstance(obj, (str, int, float, bool, dict, list)):
            continue
        if id(obj) in seen:
            continue
        for ad in ADAPTERS:
            try:
                if not ad.matches(obj):
                    continue
            except Exception:
                continue
            pid = _pid_of(obj)
            if not _alive(pid, obj):
                break
            seen.add(id(obj))
            entries.append({
                "kind": ad.kind,
                "pid": pid,
                "label": getattr(obj, "name", key),
                "source": "run",
                "context_key": key,
                "obj": obj,
                "detail": {},
            })
            break
    return entries


def _all_entries():
    """Live entries first, then scanned ones not already covered (live wins)."""
    entries = _live_entries()
    live_pids = {e["pid"] for e in entries if e.get("pid")}

    for ad in ADAPTERS:
        try:
            scanned = ad.scan()
        except Exception:
            continue
        for e in scanned:
            if e.get("pid") in live_pids:
                # keep scan detail (monitor/qmp ports) on the live entry
                for live in entries:
                    if live.get("pid") == e.get("pid"):
                        live.setdefault("detail", {}).update(e.get("detail") or {})
                continue
            entries.append(e)
    return entries


def _entry_id(entry):
    if entry.get("source") == "run":
        return f"run:{entry.get('context_key') or entry.get('label')}"
    return f"{entry['kind']}:{entry['pid']}"


def _public(entry):
    """The API-visible view of an entry — never leaks the instance object."""
    ad = _adapter_for(entry)
    detail = dict(entry.get("detail") or {})
    detail.pop("cmdline", None)
    return {
        "id":           _entry_id(entry),
        "kind":         entry["kind"],
        "label":        entry.get("label") or entry["kind"],
        "pid":          entry.get("pid"),
        "source":       entry.get("source"),
        "capabilities": ad.caps(entry) if ad else [],
        "detail":       detail,
    }


def _adapter_for(entry):
    for ad in ADAPTERS:
        if ad.kind == entry.get("kind"):
            return ad
    return None


def _resolve(iid):
    """Accept 'run:<key>', '<kind>:<pid>', or a bare pid."""
    entries = _all_entries()
    for e in entries:
        if _entry_id(e) == iid:
            return e
    if str(iid).isdigit():
        for e in entries:
            if e.get("pid") == int(iid):
                return e
    return None


def list_instances():
    """Every instance this project can currently see. Read-only."""
    return [_public(e) for e in _all_entries()]


def get_instance(iid):
    entry = _resolve(iid)
    if not entry:
        return None
    data = _public(entry)
    data["cmdline"] = (entry.get("detail") or {}).get("cmdline")
    return data


def screenshot(iid):
    """Capture the instance's display. Returns (ok, png_path_or_error)."""
    entry = _resolve(iid)
    if not entry:
        return False, f"unknown instance: {iid}"
    ad = _adapter_for(entry)
    if "screenshot" not in ad.caps(entry):
        return False, (f"instance {iid} ({entry['kind']}) has no screenshot "
                       f"capability; it offers: "
                       f"{', '.join(ad.caps(entry)) or 'nothing'}")
    path = _capture_path(_entry_id(entry))
    _unlink(path)
    return ad.screenshot(entry, path)


def ocr(iid, phrase=None):
    """Text on the instance's display -> (ok, {text, image, found?}); `phrase` is an optional match."""
    ok, res = screenshot(iid)
    if not ok:
        return False, res
    ok, text = _ocr_png(res)
    if not ok:
        return False, text
    data = {"text": text, "image": res}
    if phrase:
        data["phrase"] = phrase
        data["found"] = phrase.lower() in text.lower()
    return True, data


def tty(iid, tail=None):
    """The console/serial text captured so far. Returns (ok, text_or_error)."""
    entry = _resolve(iid)
    if not entry:
        return False, f"unknown instance: {iid}"
    ad = _adapter_for(entry)
    if "tty" not in ad.caps(entry):
        return False, (f"instance {iid} ({entry['kind']}) has no tty capability; "
                       f"it offers: {', '.join(ad.caps(entry)) or 'nothing'}")
    ok, text = ad.tty(entry)
    if ok and tail:
        try:
            text = "\n".join(text.splitlines()[-int(tail):])
        except (TypeError, ValueError):
            pass
    return ok, text


def screentext(iid):
    """Exact character-cell read of the screen (VICE only). Returns (ok, text_or_error)."""
    entry = _resolve(iid)
    if not entry:
        return False, f"unknown instance: {iid}"
    ad = _adapter_for(entry)
    if "screentext" not in ad.caps(entry):
        return False, (f"instance {iid} ({entry['kind']}) has no screentext capability "
                       f"(it needs a VICE run instance while no test is executing); "
                       f"it offers: {', '.join(ad.caps(entry)) or 'nothing'}")
    return ad.screentext(entry)


def stats(iid):
    """The stats snapshot the instance publishes. Returns (ok, dict_or_error)."""
    entry = _resolve(iid)
    if not entry:
        return False, f"unknown instance: {iid}"
    ad = _adapter_for(entry)
    if "stats" not in ad.caps(entry):
        return False, (f"instance {iid} ({entry['kind']}) has no stats capability; "
                       f"it offers: {', '.join(ad.caps(entry)) or 'nothing'}")
    return ad.stats(entry)


def verilator_projects():
    """Every Verilator project (verilator.json) with per-binary build state and last stats; the manager's overview."""
    import verilatorhelpers
    return verilatorhelpers.overview()


def send_keys(iid, text=None, key=None, keys=None, enter=False, delay=0.05):
    """Type into a running guest; any combination of:
      text   string typed character by character ("dir /w")
      key    one key token ("f3", "ret", "ctrl-alt-del")
      keys   sequence of tokens (list, or comma/space separated string)
      enter  append a Return after everything else
    No newline is implied (enter=True, key="ret", or a newline inside `text`).
    Returns (ok, {sent, count, channel, unsupported, run_busy}).
    """
    entry = _resolve(iid)
    if not entry:
        return False, f"unknown instance: {iid}"
    ad = _adapter_for(entry)
    caps = ad.caps(entry)
    if "keys" not in caps:
        return False, (f"instance {iid} ({entry['kind']}) has no keys capability; "
                       f"it offers: {', '.join(caps) or 'nothing'}")

    tokens, bad = _build_tokens(text=text, key=key, keys=keys, enter=enter)
    if not tokens:
        return False, ("nothing to send — provide 'text', 'key', 'keys', "
                       "or enter=true")

    try:
        delay = float(delay)
    except (TypeError, ValueError):
        delay = 0.05

    ok, channel = ad.send_keys(entry, tokens, delay=delay)
    if not ok:
        return False, channel

    result = {
        "sent":     tokens,
        "count":    len(tokens),
        "channel":  channel,
        "instance": _entry_id(entry),
    }
    if bad:
        # report, not fail: the rest of the string was sent
        result["unsupported"] = "".join(bad)
        result["warning"] = ("these characters have no sendkey mapping and were "
                             "skipped; the rest was typed")
    busy = _run_busy()
    if busy:
        result["run_busy"] = True
        result["warning_run"] = ("a test is running on this runner — keys sent "
                                 "now compete with the steps driving the guest")
    return True, result


def _run_busy():
    try:
        sys.path.insert(0, "/testrunnerapp")
        from appstate import progress_state
        return (progress_state.step or "Idle") not in ("Idle", "Done")
    except Exception:
        return False


# Media (mirrors /qemuctl)

def _media_target(iid):
    entry = _resolve(iid)
    if not entry:
        return None, f"unknown instance: {iid}"
    ad = _adapter_for(entry)
    if "media" not in ad.caps(entry):
        return None, (f"instance {iid} ({entry['kind']}) has no media capability. "
                      "A QEMU the runner started is swappable only if it was "
                      "given a QMP port — its HMP monitor is held by the run.")
    return entry, None


def media_list(iid):
    entry, e = _media_target(iid)
    if e:
        return False, e
    return qemuhelpers.list_blocks(pid=entry["pid"])


def media_attach(iid, device, path, read_only=None):
    entry, e = _media_target(iid)
    if e:
        return False, e
    return qemuhelpers.attach_media(entry["pid"], device, path, read_only=read_only)


def media_detach(iid, device, force=False):
    entry, e = _media_target(iid)
    if e:
        return False, e
    return qemuhelpers.detach_media(entry["pid"], device, force=force)


def media_sources():
    """Images that may be attached — the same two roots the Disk Builder offers."""
    return list(qemuhelpers.ALLOWED_ROOTS)
