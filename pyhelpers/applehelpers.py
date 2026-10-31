import time
import os
import sys
import shutil
import subprocess
import threading
import re

from appstate import process_registry


class AppleInstance:
    def __init__(self, name=None, disk1_path=None, disk2_path=None,
                 autoboot=True, fullscreen=False, config_path=None):
        self.name = name if name else "apple1"
        self.disk1_path = disk1_path
        self.disk2_path = disk2_path
        self.autoboot = autoboot
        self.fullscreen = fullscreen
        self.config_path = config_path
        self.ready_event = threading.Event()
        self._stop_reading = threading.Event()
        self.proc = None
        self.window_id = None
        self.screenshot_count = 0
        self.startup_error = ""
        self.cmd = []
        self._output_lines = []
        self._output_lock = threading.Lock()

    def start(self):
        env = os.environ.copy()

        # linapple segfaults during audio init if it cannot find the PulseAudio
        # socket, which it locates via XDG_RUNTIME_DIR. Inheriting the runner's
        # environment normally supplies it; default it so a runner started
        # without one doesn't turn into a mystery crash at startup.
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1001")

        # That default only helps when XDG_RUNTIME_DIR was simply unset --
        # if pulseaudio/pipewire isn't actually running (sound_en.sh never
        # launched), the socket path still resolves but nothing answers on
        # it, and linapple segfaults the same way right after "Unable to
        # open SDL audio". Screenshots/keystrokes don't need real audio, so
        # force SDL's no-op driver instead of gambling on a live sound
        # server being up.
        env.setdefault("SDL_AUDIODRIVER", "dummy")

        # The emulator is built into the image by the Dockerfile's linapple
        # stage. Say so plainly rather than letting Popen raise a bare
        # FileNotFoundError, which reads like a bug in the test.
        if not shutil.which("linapple"):
            self.startup_error = ("linapple binary not found on PATH -- rebuild the "
                                  "container image so the Dockerfile's linapple "
                                  "build stage runs")
            return False

        self.cmd = ["linapple"]

        if self.disk1_path:
            if not os.path.exists(self.disk1_path):
                self.startup_error = f"Disk image not found: {self.disk1_path}"
                return False
            self.cmd.extend(["--d1", self.disk1_path])

        if self.disk2_path:
            if not os.path.exists(self.disk2_path):
                self.startup_error = f"Disk image not found: {self.disk2_path}"
                return False
            self.cmd.extend(["--d2", self.disk2_path])

        if self.config_path:
            self.cmd.extend(["--conf", self.config_path])

        if self.autoboot:
            self.cmd.append("-b")

        if self.fullscreen:
            self.cmd.append("-f")

        try:
            self.proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env
            )
        except OSError as e:
            self.startup_error = f"Could not launch linapple: {e}"
            return False

        for _ in range(15):
            ret = self.proc.poll()
            if ret is not None:
                output = self.proc.stdout.read()
                self.startup_error = f"Exit code {ret}. LinApple Output: {output.strip()[:500]}"
                return False

            time.sleep(0.2)
            self.window_id = self.find_window_id_by_pid(self.proc.pid, env)
            if self.window_id:
                break

        if not self.window_id:
            self.startup_error = "Window ID not found (X11 connection or xdotool issue)"
            return False

        self.ready_event.set()
        threading.Thread(target=self._reader, daemon=True).start()
        process_registry.register(self.name, self.proc)
        return True

    def stop(self, timeout=5):
        if not self.proc:
            return True

        self._stop_reading.set()

        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass

        self.proc = None
        self.window_id = None
        process_registry.unregister(self.name)
        return True

    def get_output(self):
        with self._output_lock:
            return list(self._output_lines)

    def find_window_id_by_pid(self, pid, env):
        try:
            out = subprocess.check_output(["xdotool", "search", "--pid", str(pid)], text=True, env=env)
            ids = out.strip().splitlines()
            if ids: return ids[-1]
        except:
            pass
        return None

    def _reader(self):
        while not self._stop_reading.is_set() and self.proc:
            line = self.proc.stdout.readline()
            if not line: break
            print(f"[{self.name}] {line.strip()}")

    def _activate_window(self):
        win_id = getattr(self, "window_id", None)
        if not win_id:
            return False, "No window ID cached"
        try:
            subprocess.run(["xdotool", "windowmap", win_id], check=True)
            subprocess.run(["xdotool", "windowactivate", win_id], check=True)
            return True, win_id
        except subprocess.CalledProcessError as e:
            return False, f"Subprocess failed: {e}"

    def send_keys(self, text, delay_ms=50):
        """Type a literal string into the emulator window via synthetic X11
        key events (the SDL window reads real X11 keyboard input, there is no
        binary monitor like VICE's to poke the keyboard buffer directly).

        Newlines are sent as explicit Return keypresses rather than being left
        in the typed string: `xdotool type` does not deliver an embedded
        newline as RETURN to this SDL app, so a multi-line BASIC program typed
        in one go silently lands on a single line and is never entered."""
        if not self.proc or self.proc.poll() is not None:
            return False, "LinApple process not running"

        ok, info = self._activate_window()
        if not ok:
            return False, info

        delay_ms = str(int(delay_ms))
        lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")

        try:
            for idx, line in enumerate(lines):
                if line:
                    subprocess.run(["xdotool", "type", "--delay", delay_ms, line], check=True)
                # every "\n" in the input becomes a RETURN; a trailing newline
                # produces the final one, which is what submits the last line
                if idx < len(lines) - 1:
                    subprocess.run(["xdotool", "key", "Return"], check=True)
                    time.sleep(0.2)
            returns = len(lines) - 1
            return True, f"typed {len(text)} chars ({returns} RETURN)"
        except subprocess.CalledProcessError as e:
            return False, f"Subprocess failed: {e}"

    def send_key(self, keysym):
        """Send a single xdotool keysym (e.g. 'Return', 'Escape', 'Up')."""
        if not self.proc or self.proc.poll() is not None:
            return False, "LinApple process not running"

        ok, info = self._activate_window()
        if not ok:
            return False, info

        try:
            subprocess.run(["xdotool", "key", keysym], check=True)
            return True, f"sent key {keysym}"
        except subprocess.CalledProcessError as e:
            return False, f"Subprocess failed: {e}"

    def take_screenshot(self, test_step=None, filename=None, window="default"):
        """Grab the emulator window.

        Caveat worth knowing before writing assertions against these images:
        linapple repaints only the character cells it believes changed, and
        under the SDL1.2->SDL2 compat layer in this image a lot of those
        repaints are dropped. The capture is therefore a composite of current
        and stale cells -- LinApple's own splash never clears, and output
        produced while the 6502 is in a tight loop may not appear at all. The
        emulation underneath is correct; only the picture lags. Treat these as
        an eyeball aid, not ground truth, and prefer programs that finish and
        return to the prompt over ones that loop forever."""
        _helperdir = "/testrunnersrc/pyhelpers"
        if _helperdir not in sys.path:
            sys.path.insert(0, _helperdir)

        try:
            from appstate import progress_state
            stepnum = progress_state.step
            stepnum = re.match(r'\d+', stepnum).group(0)
            if not test_step:
                test_step = stepnum
        except Exception:
            if not test_step:
                test_step = "unknown"

        if not self.proc or self.proc.poll() is not None:
            return False, "LinApple process not running"

        win_id = getattr(self, "window_id", None)
        if not win_id:
            return False, "No window ID cached"

        screenshot_base_dir = "/testrunnerapp/"
        reports_dir = os.path.join(screenshot_base_dir, "reports")
        if not os.path.exists(reports_dir):
            os.makedirs(reports_dir)

        self.screenshot_count += 1
        if filename is None:
            filename = f"screenshot-{self.name}-{test_step}-{self.screenshot_count}.png"

        filepath = os.path.join(reports_dir, filename)

        try:
            subprocess.run(["xdotool", "windowmap", win_id], check=True)
            subprocess.run(["xdotool", "windowactivate", win_id], check=True)
            subprocess.run(["import", "-window", win_id, filepath], check=True)
            return True, filepath
        except subprocess.CalledProcessError as e:
            return False, f"Subprocess failed: {e}"
        except Exception as e:
            return False, str(e)
