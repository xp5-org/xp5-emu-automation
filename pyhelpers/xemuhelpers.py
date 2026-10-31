import os
import socket
import subprocess
import sys
import threading
import time

from PIL import Image

from displayhelpers import resolve_display
from qemuhelpers import TESSERACT_TESSDATA_ARGS

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

XEMU_BIN = "xemu"


def free_tcp_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class XemuMonitor:
    """Client for the QEMU HMP monitor xemu exposes via `-monitor tcp:host:port,server,nowait` (status / sendkey / quit; not screendump, see XemuInstance). Protocol as QemuInstance._monitor_send (drain to the "(qemu) " prompt)."""

    def __init__(self, port, host="127.0.0.1", timeout=20):
        self.port = int(port)
        self.host = host
        self.timeout = timeout
        self.sock = None
        self.last_error = None
        self.lock = threading.RLock()

    def connect(self):
        if self.sock:
            return True
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            # drain the monitor banner and first prompt
            prev = self.sock.gettimeout()
            self.sock.settimeout(2.0)
            try:
                buf = b""
                while b"(qemu)" not in buf:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                pass
            finally:
                self.sock.settimeout(prev)
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

    def cmd(self, line, drain_timeout=2.0):
        """Send one HMP command, draining to the next (qemu) prompt -> reply text (echo and prompt included) or None if the link is down."""
        if not self.connect():
            return None
        try:
            with self.lock:
                self.sock.sendall((line + "\n").encode("utf-8"))
                prev = self.sock.gettimeout()
                self.sock.settimeout(drain_timeout)
                buf = b""
                try:
                    while b"(qemu)" not in buf:
                        chunk = self.sock.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                except socket.timeout:
                    pass
                finally:
                    self.sock.settimeout(prev)
            return buf.decode("utf-8", errors="replace")
        except OSError as e:
            self.last_error = str(e)
            self.close()
            return None

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


class XemuInstance:
    """xemu (Xbox emulator, embedded QEMU 10.2.0) driven by xdotool + ImageMagick `import` + tesseract (same surface as DosboxInstance/Pcsx2Instance).
    HMP `screendump` and `-vnc` do not work in xemu, so this is not a QemuInstance subclass; the HMP monitor is always opened internally (xemu starts "paused (prelaunch)"; `cont` in wait_for_ready starts it), whatever monitor_port is.
    bootrom_file -> `-machine xbox,bootrom=<file>,...`; flashrom_file -> flashrom_path in [sys.files] (config_spec.yml); hdd_path -> `-drive index=0,media=disk,file=<file>,locked=on`; dvd_path.
    Firmware is copyrighted: supplied out of band (as the PS2 BIOS in pcsx2helpers.py).
    datadir: per-instance config/eeprom tree via XDG_DATA_HOME (xemu.toml at $XDG_DATA_HOME/xemu/xemu/xemu.toml); defaults to /tmp/xemu-{name}.
    """

    def __init__(self, name, dvd_path=None, hdd_path=None, bootrom_file=None,
                 flashrom_file=None, datadir=None, monitor_port=None,
                 display=None, binary=None, screenshot_scale=None):
        self.name = name
        self.dvd_path = dvd_path
        self.hdd_path = hdd_path
        self.bootrom_file = bootrom_file
        self.flashrom_file = flashrom_file
        self.datadir = datadir or f"/tmp/xemu-{name}"
        self.binary = binary or XEMU_BIN
        self.process = None
        self.pid = None
        self.stdout_lines = []
        self.screenshot_count = 0
        self.xvfb = None
        self.display = display if display == "auto" else resolve_display(display)
        # screenshot scale; defaults to OCR_SCALE
        try:
            self.screenshot_scale = float(screenshot_scale) if screenshot_scale not in (None, "") else float(OCR_SCALE)
        except (TypeError, ValueError):
            self.screenshot_scale = float(OCR_SCALE)

        # xemu starts paused (prelaunch): the monitor is always opened so wait_for_ready can send `cont`
        self.monitor_port = int(monitor_port) if monitor_port not in (None, "", "auto") \
            else free_tcp_port()
        self.monitor = XemuMonitor(self.monitor_port)

    def _prepare_datadir(self):
        """Write xemu.toml with [sys.files] pointing at bootrom_file/flashrom_file/hdd_path/dvd_path (whichever given) plus a private eeprom.bin path. Always overwrites (the datadir is reused across runs); needs no bootstrap run."""
        toml_dir = os.path.join(self.datadir, "xemu", "xemu")
        toml_path = os.path.join(toml_dir, "xemu.toml")

        os.makedirs(toml_dir, exist_ok=True)
        eeprom_path = os.path.join(toml_dir, "eeprom.bin")

        # show_welcome=false suppresses the onboarding overlay; display.window.startup_size="center" gives a native 640x480 window
        lines = ["[general]", "show_welcome = false", "",
                 "[display.window]", "startup_size = '640x480'",
                 "fullscreen_on_startup = false", "",
                 "[display.ui]", "fit = 'center'", "",
                 "[sys.files]"]
        if self.bootrom_file:
            lines.append(f"bootrom_path = '{self.bootrom_file}'")
        if self.flashrom_file:
            lines.append(f"flashrom_path = '{self.flashrom_file}'")
        if self.hdd_path:
            lines.append(f"hdd_path = '{self.hdd_path}'")
        if self.dvd_path:
            lines.append(f"dvd_path = '{self.dvd_path}'")
        lines.append(f"eeprom_path = '{eeprom_path}'")

        with open(toml_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return True

    def _env(self):
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        env["XDG_DATA_HOME"] = self.datadir

        # force SDL audio to pulseaudio via PULSE_SERVER (PipeWire-pulse socket); xemu's default ALSA has no device
        env.setdefault("SDL_AUDIODRIVER", "pulseaudio")
        real_runtime_dir = f"/run/user/{os.getuid()}"
        env.setdefault("XDG_RUNTIME_DIR", real_runtime_dir)
        env.setdefault("PULSE_SERVER", f"unix:{real_runtime_dir}/pulse/native")
        return env

    def _start_xvfb(self):
        for num in range(180, 210):
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

        if not self._prepare_datadir():
            return False

        args = [self.binary]
        if self.monitor_port:
            args += ["-monitor", f"tcp:127.0.0.1:{self.monitor_port},server,nowait"]

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
            print("XEMU PID IS: ", self.pid, flush=True)
            print("start command is", args, flush=True)
            processreg.register(self.name, self.process, source="xemu")

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
        # window title "xemu | v<version>": find by --name, not --pid
        try:
            cmd = ['xdotool', 'search', '--all', '--onlyvisible', '--name', 'xemu']
            result = subprocess.run(cmd, capture_output=True, text=True, env=self._env(), timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')[0]
        except Exception:
            pass
        return None

    def _move_to_origin(self, wid):
        try:
            subprocess.run(['xdotool', 'windowmove', wid, '0', '0'],
                           capture_output=True, env=self._env(), timeout=5)
        except Exception as e:
            print(f"WARNING: {self.name} failed to move window {wid} to origin: {e}")

    def wait_for_ready(self, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            if self.process.poll() is not None:
                return False
            wid = self.get_window_id()
            if wid:
                time.sleep(0.5)
                self._move_to_origin(wid)
                if self.monitor.wait_ready(timeout=max(1, timeout - (time.time() - start))):
                    # `cont` starts execution from paused (prelaunch)
                    self.monitor.cmd("cont")
                else:
                    self.stdout_lines.append(
                        "WARNING: xemu monitor never came up; VM was not "
                        "resumed and will stay paused (prelaunch)")
                return True
            time.sleep(0.5)
        return False

    def is_alive(self):
        """False once the emulator process has exited. Calling poll() reaps it."""
        return self.process is not None and self.process.poll() is None

    def send_command(self, cmd_text=None, special_keys=None, key_delay=40):
        """Type into the xemu window via xdotool (host keys map to a virtual Xbox controller via xemu's input config)."""
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
                subprocess.run(['xdotool', 'type', '--delay', str(key_delay), cmd_text],
                               env=env, timeout=30)
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
        """OCR the current screen -> (ok, text); take_screenshot() already upscales by screenshot_scale."""
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
        if self.monitor:
            self.monitor.close()
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
