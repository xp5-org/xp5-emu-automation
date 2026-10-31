import subprocess
import os
import sys
import time
import threading

from PIL import Image

from displayhelpers import resolve_display
import windowlayout
from qemuhelpers import TESSERACT_TESSDATA_ARGS

# read_screen() upscale factor before tesseract; matches "ocr_scale" in qemuhelpers.ocr_word_find
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

DOSBOX_BIN = "dosbox-x"

# -nomenu: keep the 17px menu bar out of screenshots
WINDOW_ARGS = ["-fastlaunch", "-nogui", "-nomenu"]


def run_headless(config_path, timeout=120, extra_args=None):
    """Run a config to completion with no X (-silent runs [autoexec] then exits; blind: no window to capture or send keys to) -> (ok, log)."""
    args = [DOSBOX_BIN, "-conf", config_path, "-silent", "-fastlaunch"]
    if extra_args:
        args += extra_args

    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env["SDL_VIDEODRIVER"] = "dummy"

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            env=env, start_new_session=True
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s: {' '.join(args)}"

    log = proc.stdout + proc.stderr
    if proc.returncode != 0:
        return False, f"exit={proc.returncode}\n{log}"
    return True, log


class DosboxInstance:
    """Windowed dosbox-x driven by xdotool, screenshotted with `import`. The window opens on the runner's X session; display="auto" gives a private Xvfb."""

    def __init__(self, name, config_path=None, display=None, window_slot=None,
                 screenshot_scale=None):
        self.name = name
        self.process = None
        self.pid = None
        self.stdout_lines = []
        self.screenshot_count = 0
        self.config_path = config_path
        self.xvfb = None
        # screenshot scale; defaults to OCR_SCALE
        try:
            self.screenshot_scale = float(screenshot_scale) if screenshot_scale not in (None, "") else float(OCR_SCALE)
        except (TypeError, ValueError):
            self.screenshot_scale = float(OCR_SCALE)
        # display: "auto" -> private Xvfb in start(); else the runner's xfce/RDP session
        self.display = display if display == "auto" else resolve_display(display)
        # tile slot (dispatch_functions._next_window_slot / windowlayout.tile_position)
        self.window_slot = window_slot


    def _env(self):
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        return env


    def _start_xvfb(self):
        # free display number
        for num in range(90, 120):
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
        if self.display == "auto" and not self._start_xvfb():
            self.stdout_lines.append("Failed to start a private Xvfb.")
            return False

        args = [DOSBOX_BIN] + WINDOW_ARGS
        if self.config_path:
            args += ["-conf", self.config_path]
        try:
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=self._env()
            )
            self.pid = self.process.pid
            print("DOSBOX PID IS: ", self.pid, flush=True)
            print("start command is", args, flush=True)
            processreg.register(self.name, self.process, source="dosbox")

            self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
            self.stdout_thread.start()
            return True
        except Exception as e:
            self.stdout_lines.append(str(e))
            return False

    def _read_stdout(self):
        if self.process and self.process.stdout:
            for line in self.process.stdout:
                self.stdout_lines.append(line.strip())

    def get_window_id(self):
        # window id for this instance's PID
        try:
            cmd = ['xdotool', 'search', '--pid', str(self.pid), '--onlyvisible', '--class', 'dosbox']
            result = subprocess.run(cmd, capture_output=True, text=True, env=self._env(), timeout=5)
            if result.returncode == 0:
                return result.stdout.strip().split('\n')[0]
        except:
            pass
        return None

    def _move_to_tile(self, wid):
        """Snap the window onto its round-robin tile slot (clamped to the session edge)."""
        win_size = windowlayout.window_geometry(wid, self.display)
        x, y = windowlayout.tile_position(self.window_slot, win_size, self.display)
        try:
            subprocess.run(['xdotool', 'windowmove', wid, str(x), str(y)],
                           capture_output=True, env=self._env(), timeout=5)
        except Exception as e:
            print(f"WARNING: {self.name} failed to move window {wid} to ({x},{y}): {e}")

    def wait_for_ready(self, timeout=5):
        start = time.time()
        while time.time() - start < timeout:
            if self.process.poll() is not None: return False
            wid = self.get_window_id()
            if wid:
                time.sleep(0.5)
                self._move_to_tile(wid)
                return True
            time.sleep(0.5)
        return False

    def is_alive(self):
        """False once the process has exited; poll() also reaps it so liveview doesn't see a zombie."""
        return self.process is not None and self.process.poll() is None

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

    def wait_for_screen(self, expect, timeout=60, poll=2.0):
        """Poll OCR until `expect` (case-insensitive) appears -> (ok, last_ocr_text). Ends early if the emulator exits."""
        needle = expect.lower()
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            if not self.is_alive():
                return False, f"dosbox-x exited (window closed?)\n{text}"
            ok, text = self.read_screen()
            if ok and needle in text.lower():
                return True, text
            if not self._sleep_while_alive(float(poll), deadline):
                return False, f"dosbox-x exited (window closed?)\n{text}"
        return False, text

    def send_command(self, cmd_text, special_keys=None, key_delay=12):
        # SDL2 drops key events unless the window has focus
        wid = self.get_window_id()
        if not wid:
            return False

        env = self._env()
        try:
            subprocess.run(['xdotool', 'windowactivate', '--sync', wid],
                           capture_output=True, env=env, timeout=5)
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
                    subprocess.run(['xdotool', 'key', key], env=env, timeout=5)
        except subprocess.TimeoutExpired:
            print(f"WARNING: {self.name} window {wid} stopped responding "
                  f"while sending input (closed mid-command?)")
            return False
        return True


    def take_screenshot(self, test_step=None, filename=None):
        if APPBASE_DIR not in sys.path:
            sys.path.insert(0, APPBASE_DIR)
        from appstate import current_reports_dir
        reports_dir = current_reports_dir()

        step_str = f"-{test_step}" if test_step else ""
        name = filename if filename else f"screenshot-{self.name}{step_str}-{self.screenshot_count}.png"
        path = os.path.join(reports_dir, name) if not filename else os.path.abspath(filename)

        wid = self.get_window_id()
        if not wid:
            return False, "Window ID not found"

        try:
            subprocess.run(['import', '-window', wid, path], check=True, env=self._env(), timeout=10)
            if self.screenshot_scale != 1.0:
                img = Image.open(path).convert("RGB")
                img = img.resize(
                    (max(1, round(img.width * self.screenshot_scale)),
                     max(1, round(img.height * self.screenshot_scale))),
                    Image.LANCZOS
                )
                img.save(path)
            self.screenshot_count += 1
            print("DEBUG: SCREENSHOT TAKEN AT: ", path)
            return True, path
        except Exception as e:
            print("FAILED TO TAKE SCREENSHOT", e)
            return False, str(e)


    def read_screen(self):
        """OCR the current screen -> (ok, text). --psm 6 + TESSERACT_TESSDATA_ARGS as ocr_word_find (qemuhelpers.py); take_screenshot() already upscales by screenshot_scale."""
        ok, path = self.take_screenshot(filename=f"/tmp/{self.name}-ocr.png")
        if not ok:
            return False, path
        cmd = ['tesseract', path, 'stdout', '--psm', '6'] + TESSERACT_TESSDATA_ARGS.split()
        try:
            result = subprocess.run(cmd,
                                    capture_output=True, text=True, check=True)
            return True, result.stdout
        except Exception as e:
            return False, str(e)


    def stop(self):
        processreg.unregister(self.name)
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.xvfb:
            self.xvfb.terminate()
            try:
                self.xvfb.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.xvfb.kill()
                self.xvfb.wait()
            self.xvfb = None


class DosboxConf:
    def __init__(self, filepath):
        self.filepath = filepath
        # newline="": keep template line endings, store lines without terminators
        with open(filepath, "r", newline="") as f:
            raw = f.read()
        self.lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()

    def autoexec_insert(self, insert_line):
        self.section_insert("[autoexec]", insert_line)

    def section_insert(self, section_header, insert_line):
        """Insert insert_line as the first line of section_header (e.g. "[dosbox]"), appending the section if absent; case-insensitive section match."""
        new_lines = []
        inserted = False
        for line in self.lines:
            new_lines.append(line)
            if not inserted and line.strip().lower() == section_header.lower():
                new_lines.append(insert_line)
                inserted = True
        if not inserted:
            new_lines.append(section_header)
            new_lines.append(insert_line)
        self.lines = new_lines

    def disable_quit_warning(self):
        """Disable dosbox-x's `quit warning=auto` confirmation dialog."""
        self.section_insert("[dosbox]", "quit warning=false")


    def autotype_insert(self, buttons, wait=1.0, pace=0.2):
        """Queue keystrokes into the emulated keyboard from [autoexec] (AUTOTYPE builtin; no X or focus needed)."""
        seq = " ".join(buttons)
        self.autoexec_insert(f"autotype -w {wait} -p {pace} {seq}")

    def save(self, outpath=None):
        path = outpath if outpath else self.filepath
        print('SAVING NEW CONF TO PATH: ', path)
        # newline="": write the joined \r\n literally
        with open(path, "w", newline="") as f:
            f.write("\r\n".join(self.lines) + "\r\n")
