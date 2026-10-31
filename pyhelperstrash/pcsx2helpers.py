import os
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

PCSX2_BIN = "pcsx2"

# pcsx2-qt flags: -batch exit after shutdown; -elf <file> boot ELF (space-separated); -datapath <dir> self-contained config/BIOS tree
BATCH_ARGS = ["-batch"]

# -testconfig bootstraps a version-matched PCSX2.ini and datapath tree without the Setup Wizard; do not combine with -batch (hangs)
_TESTCONFIG_ARGS = ["-testconfig"]


class Pcsx2Instance:
    """Windowed PCSX2 (Qt) driven by xdotool, screenshotted with `import` (same surface as DosboxInstance/Box86Instance).
    Exactly one of elf_path (homebrew ELF, e.g. test_hostbuild PS2SDK output) / iso_path (disc image).
    bios_file: PS2 BIOS dump file, or a directory of them (e.g. ps2_roms/): every file is copied to datapath's bios/ on start(); the largest is the main BIOS ([Filenames] BIOS).
    Supplied out of band (copyrighted). Without a BIOS PCSX2 exits with no window, so wait_for_ready times out.
    datapath: per-instance PCSX2 config/BIOS/savestate tree (-datapath); defaults to a directory under /tmp named after the instance.
    """

    def __init__(self, name, elf_path=None, iso_path=None, bios_file=None,
                 datapath=None, display=None, binary=None, screenshot_scale=None):
        self.name = name
        self.elf_path = elf_path
        self.iso_path = iso_path
        self.bios_file = bios_file
        self.datapath = datapath or f"/tmp/pcsx2-{name}"
        self.binary = binary or PCSX2_BIN
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

    def _resolve_bios_files(self):
        """bios_file as a file or directory -> (all files to copy, main BIOS path or None); main = largest file."""
        if not self.bios_file:
            return [], None
        if os.path.isfile(self.bios_file):
            return [self.bios_file], self.bios_file
        if os.path.isdir(self.bios_file):
            files = [os.path.join(self.bios_file, e)
                    for e in sorted(os.listdir(self.bios_file))
                    if not e.startswith(".")
                    and os.path.isfile(os.path.join(self.bios_file, e))]
            main = max(files, key=os.path.getsize) if files else None
            return files, main
        return [], None

    def _prepare_datapath(self):
        """Bootstrap datapath's inis/PCSX2.ini via `-testconfig` (_TESTCONFIG_ARGS), patch it to skip the wizard and, with bios_file, copy the BIOS in and point PCSX2 at it. No-op if the ini exists."""
        import shutil as _shutil
        ini_dir = os.path.join(self.datapath, "PCSX2", "inis")
        ini_path = os.path.join(ini_dir, "PCSX2.ini")
        bios_dir = os.path.join(self.datapath, "PCSX2", "bios")

        if not os.path.exists(ini_path):
            # datapath must exist before -testconfig (it hangs otherwise)
            os.makedirs(self.datapath, exist_ok=True)
            subprocess.run([self.binary] + _TESTCONFIG_ARGS + ["-datapath", self.datapath],
                           capture_output=True, env=self._env(), timeout=30)

        if not os.path.exists(ini_path):
            self.stdout_lines.append(
                f"-testconfig did not create {ini_path}; datapath bootstrap failed")
            return False

        with open(ini_path) as f:
            ini_text = f.read()
        ini_text = ini_text.replace("SetupWizardIncomplete = true",
                                    "SetupWizardIncomplete = false")

        bios_files, main_bios = self._resolve_bios_files()
        if bios_files:
            os.makedirs(bios_dir, exist_ok=True)
            for src in bios_files:
                dest = os.path.join(bios_dir, os.path.basename(src))
                if not os.path.exists(dest):
                    _shutil.copy2(src, dest)
            if main_bios and "[Filenames]\nBIOS = \n" in ini_text:
                ini_text = ini_text.replace(
                    "[Filenames]\nBIOS = \n",
                    f"[Filenames]\nBIOS = {os.path.basename(main_bios)}\n")
        elif self.bios_file:
            self.stdout_lines.append(
                f"bios_file={self.bios_file} given but no BIOS files found there")

        with open(ini_path, "w") as f:
            f.write(ini_text)
        return True

    def _env(self):
        env = dict(os.environ)
        env["DISPLAY"] = self.display
        return env

    def _start_xvfb(self):
        for num in range(150, 180):
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

        if not self.elf_path and not self.iso_path:
            self.stdout_lines.append("Pcsx2Instance: neither elf_path nor iso_path given.")
            return False

        if not self._prepare_datapath():
            return False

        args = [self.binary] + BATCH_ARGS + ["-datapath", self.datapath]
        if self.elf_path:
            args += ["-elf", self.elf_path]
        elif self.iso_path:
            args += [self.iso_path]

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
            print("PCSX2 PID IS: ", self.pid, flush=True)
            print("start command is", args, flush=True)
            processreg.register(self.name, self.process, source="pcsx2")

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
        # windows are titled containing "PCSX2"; match with --name
        try:
            cmd = ['xdotool', 'search', '--all', '--pid', str(self.pid),
                   '--onlyvisible', '--name', 'PCSX2']
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
                return True
            time.sleep(0.5)
        return False

    def is_alive(self):
        """False once the emulator process has exited. Calling poll() reaps it."""
        return self.process is not None and self.process.poll() is None

    def send_command(self, cmd_text=None, special_keys=None, key_delay=40):
        """Type into the PCSX2 window via xdotool (host keys map to PS2 pad buttons via PCSX2's input config)."""
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
