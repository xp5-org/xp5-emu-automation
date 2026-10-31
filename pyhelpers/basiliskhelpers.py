import os
import sys
import time
import signal
import threading
import subprocess
import numpy as np
from PIL import Image

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
M68K_DIR = os.path.join(TESTSRC_BASEDIR, "m68k")
BASILISK_BIN = os.path.join(M68K_DIR, "BasiliskII")
DEFAULT_ROM = os.path.join(M68K_DIR, "Quadra-650.ROM")
DEFAULT_BOOT_DISK = os.path.join(M68K_DIR, "system76boot.dsk")

# OCR upscale for take_screenshot() (see OCR_SCALE in box86helpers.py)
OCR_SCALE = 4


class BasiliskInstance:
    def __init__(self, name, rom=DEFAULT_ROM, boot_disk=DEFAULT_BOOT_DISK,
                 extra_disks=None, serial_path=None, ramsize_mb=64,
                 screen_w=640, screen_h=480, modelid=14, cpu=4, fpu=True,
                 screenshot_scale=None):
        self.name = name
        self.rom = rom
        self.boot_disk = boot_disk
        self.extra_disks = list(extra_disks or [])
        self.serial_path = serial_path
        self.ramsize_mb = int(ramsize_mb)
        self.screen_w = int(screen_w)
        self.screen_h = int(screen_h)
        self.modelid = int(modelid)
        self.cpu = int(cpu)
        self.fpu = bool(fpu)

        self.process = None
        self.stdout_lines = []
        self.screenshot_count = 0
        # screenshot scale; 1x default because gui_move_to() uses raw screenshot pixel coords
        try:
            self.screenshot_scale = float(screenshot_scale) if screenshot_scale not in (None, "") else 1.0
        except (TypeError, ValueError):
            self.screenshot_scale = 1.0
        self._pty_master = None
        self._serial_thread = None
        self._serial_stop = False
        self._prefs_path = None
        self._wid = None

    def _write_prefs(self):
        """Basilisk prefs file ('key value' per line; 'disk' may repeat)."""
        lines = [
            f"rom {self.rom}",
            f"disk {self.boot_disk}",
        ]
        for d in self.extra_disks:
            lines.append(f"disk {d}")
        lines += [
            f"screen win/{self.screen_w}/{self.screen_h}",
            f"ramsize {self.ramsize_mb * 1024 * 1024}",
            f"modelid {self.modelid}",
            f"cpu {self.cpu}",
            f"fpu {'true' if self.fpu else 'false'}",
            "nogui true",
            "nosound true",
            "frameskip 0",
            "init_grab false",
        ]
        if self.serial_path is not None:
            # Mac serial port A -> host pty; reader thread copies to serial_path
            master, slave = os.openpty()
            self._pty_master = master
            lines.append(f"seriala {os.ttyname(slave)}")
        self._prefs_path = f"/tmp/_basilisk_{self.name}.prefs"
        with open(self._prefs_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return self._prefs_path

    def _serial_reader(self):
        with open(self.serial_path, "ab", buffering=0) as out:
            while not self._serial_stop:
                try:
                    data = os.read(self._pty_master, 4096)
                    if data:
                        out.write(data)
                except OSError:
                    break

    def start(self):
        if self.serial_path is not None:
            open(self.serial_path, "wb").close()
        prefs = self._write_prefs()
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":14")
        try:
            self.process = subprocess.Popen(
                [BASILISK_BIN, "--config", prefs],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, cwd=M68K_DIR, env=env)
        except Exception as e:
            self.stdout_lines.append(f"Failed to launch BasiliskII: {e}")
            return False
        processreg.register(self.name, self.process, source="basilisk")
        if self._pty_master is not None:
            self._serial_thread = threading.Thread(target=self._serial_reader, daemon=True)
            self._serial_thread.start()
        time.sleep(3)
        if self.process.poll() is not None:
            out = self.process.stdout.read().decode(errors="replace") if self.process.stdout else ""
            self.stdout_lines.append(f"BasiliskII exited early: {out[:500]}")
            return False
        return True

    @staticmethod
    def _xdo(*a):
        return subprocess.run(["xdotool", *a], capture_output=True, text=True).stdout.strip()

    def _find_window(self):
        if self._wid:
            return self._wid
        for nm in ("BasiliskII", "Basilisk II", "Basilisk"):
            ids = self._xdo("search", "--name", nm).split()
            if ids:
                self._wid = ids[0]
                return self._wid
        # fallback: window owned by our pid
        if self.process:
            ids = self._xdo("search", "--pid", str(self.process.pid)).split()
            if ids:
                self._wid = ids[0]
        return self._wid

    def _screencap_array(self):
        """Grab the Basilisk window (the Mac framebuffer) as an RGB numpy array."""
        wid = self._find_window()
        if not wid:
            return None
        tmp = f"/tmp/_bsk_cap_{self.name}.png"
        r = subprocess.run(["import", "-window", wid, tmp], capture_output=True)
        if r.returncode != 0 or not os.path.exists(tmp):
            return None
        return np.asarray(Image.open(tmp).convert("RGB")).astype(np.int16)

    def take_screenshot(self, test_step=None, filename=None):
        """PNG of the Mac screen (as QemuInstance.take_screenshot) -> (ok, path)."""
        wid = self._find_window()
        if not wid:
            return False, "Basilisk window not found"
        if filename:
            path = os.path.abspath(filename)
        else:
            if APPBASE_DIR not in sys.path:
                sys.path.insert(0, APPBASE_DIR)
            from appstate import current_reports_dir
            reports = current_reports_dir()
            step = f"-{os.path.basename(str(test_step))}" if test_step else ""
            path = os.path.join(reports, f"screenshot-{self.name}{step}-{self.screenshot_count}.png")
            self.screenshot_count += 1
        r = subprocess.run(["import", "-window", wid, path], capture_output=True)
        if r.returncode != 0 or not os.path.exists(path):
            return False, f"import failed: {r.stderr.decode(errors='replace')[:200]}"
        if self.screenshot_scale != 1.0:
            try:
                img = Image.open(path).convert("RGB")
                img = img.resize(
                    (max(1, round(img.width * self.screenshot_scale)),
                     max(1, round(img.height * self.screenshot_scale))),
                    Image.LANCZOS
                )
                img.save(path)
            except Exception as e:
                return False, f"failed to upscale screenshot for OCR: {e}"
        return True, path

    def gui_move_to(self, gx, gy, do_click=True, button=1, clicks=1, **_):
        """Position the Mac cursor at window pixel (gx, gy), optional mouseclick."""
        wid = self._find_window()
        if not wid:
            return False, "Basilisk window not found"
        self._xdo("windowactivate", "--sync", wid)
        self._xdo("mousemove", "--window", wid, str(int(gx)), str(int(gy)))
        time.sleep(0.2)
        log = [f"moved to ({gx},{gy})"]
        if do_click:
            for i in range(max(1, int(clicks))):
                self._xdo("mousedown", "--window", wid, str(button))
                time.sleep(0.08)
                self._xdo("mouseup", "--window", wid, str(button))
                if i + 1 < clicks:
                    time.sleep(0.12)
            log.append(f"clicked button {button} x{max(1, int(clicks))}")
        return True, "\n".join(log)

    def send_key(self, keyname, ctrl=False, alt=False, shift=False, delay=0.1):
        wid = self._find_window()
        mods = [m for m, f in (("ctrl", ctrl), ("alt", alt), ("shift", shift)) if f]
        combo = "+".join(mods + [keyname])
        self._xdo("key", "--window", wid, combo)
        time.sleep(delay)

    def send_specialkeys(self, keystr, ctrl=False, alt=False, shift=False, delay=0.1):
        """xdotool key form ('Return', 'super+o', 'ctrl+a'); Mac Command = host Super."""
        wid = self._find_window()
        self._xdo("key", "--window", wid, str(keystr))
        time.sleep(delay)

    def stop(self):
        self._serial_stop = True
        processreg.unregister(self.name)
        if self.process and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                time.sleep(1)
                if self.process.poll() is None:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        if self._pty_master is not None:
            try:
                os.close(self._pty_master)
            except OSError:
                pass

    def collect_logs(self):
        return "\n".join(self.stdout_lines)
