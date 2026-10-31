import subprocess
import socket
import threading
import os, sys
import json
import shutil
import time
from PIL import Image, ImageOps
import math
import tempfile
import time

HELPERDIR = os.path.dirname(os.path.abspath(__file__))
if HELPERDIR not in sys.path:
    sys.path.insert(0, HELPERDIR)
import windowlayout
APPBASE_DIR = "/testrunnerapp"
if APPBASE_DIR not in sys.path:
    sys.path.insert(0, APPBASE_DIR)
try:
    import appstate as processreg
except ImportError:
    class processreg:
        register = staticmethod(lambda *a, **k: None)
        unregister = staticmethod(lambda *a, **k: None)
import subprocess
import socket
import tempfile
import threading
import numpy as np
import pytesseract
import re


TESTSRC_BASEDIR = os.environ.get("TESTSRC_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ocrlogdir = os.path.join(TESTSRC_BASEDIR, "compile_logs")
# OCR output without a run_id lands here
OCR_ADHOC_SUBDIR = "_adhoc"


def ocr_log_dir():
    """Per-run OCR/screenshot dir compile_logs/<run_id>/, created on demand (swept by reporthelper._move_assets, removed after the report is written)."""
    run_id = ""
    try:
        if APPBASE_DIR not in sys.path:
            sys.path.insert(0, APPBASE_DIR)
        from appstate import progress_state
        run_id = getattr(progress_state, "run_id", "") or ""
    except Exception:
        pass
    d = os.path.join(ocrlogdir, run_id or OCR_ADHOC_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d

# prefer bundled tessdata over apt's low-accuracy default
_TESSDATA_DIR = "/usr/local/share/tessdata"
TESSERACT_TESSDATA_ARGS = (
    f"--tessdata-dir {_TESSDATA_DIR}"
    if os.path.isfile(os.path.join(_TESSDATA_DIR, "eng.traineddata")) else ""
)

# macOS launchd PATH lacks Homebrew: fall back to Homebrew tesseract locations
TESSERACT_BIN = shutil.which("tesseract") or next(
    (p for p in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract")
     if os.path.isfile(p)),
    "tesseract")
pytesseract.pytesseract.tesseract_cmd = TESSERACT_BIN

# limit tesseract OpenMP threads (spin-wait starves the emulator); inherited by all tesseract calls
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


_SERIAL_BACKEND_PREFIXES = ("tcp:", "unix:", "socket:", "file:", "pipe:", "vc")
_SERIAL_BACKEND_EXACT = ("pty", "stdio", "null", "none")


def _serial_backend_arg(path):
    """Value for -serial: bare path -> file:<path> (one-way capture); a string naming a chardev backend (tcp:, unix:, pty, ...) passes through, e.g. "tcp:127.0.0.1:5556,server,nowait"."""
    if path in _SERIAL_BACKEND_EXACT or path.startswith(_SERIAL_BACKEND_PREFIXES):
        return path
    return f"file:{path}"


def _as_port(value, default=None):
    """Port -> int, else `default` ("" / None / junk)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# savevm/loadvm/delvm wait for the monitor prompt this long
SNAPSHOT_WAIT_S = 120

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def hmp_error(reply):
    """The "Error: ..." line in an HMP reply, or "" (the reply never starts with "Error": the command echo comes first)."""
    for line in _ANSI_RE.sub("", reply or "").replace("\r", "").splitlines():
        line = line.strip()
        if line.lower().startswith(("error", "invalid", "unknown command")):
            return line
    return ""


class QemuInstance:
    # serializes monitor socket traffic across threads
    lock = threading.RLock()
    def __init__(self, name, cpuarch, monitor_port, hdd1imagepath=None, hdd2imagepath=None, hdd3imagepath=None, hdd4imagepath=None, floppy_path=None, floppy2_path=None, cdrom_path=None, cdrom2_path=None, memory="4M", vnc_port=None,
                 sound_device=None, audio_backend="wav", audio_out_path=None, serial_path=None,
                 pa_out_name=None, pa_in_name=None, pa_server=None,
                 machine=None, cpu=None, vga=None, net_device=None, mac_address=None, hostfwd=None,
                 bios_path=None, boot_order=None, prom_env=None, extra_args=None,
                 extra_disks=None, screenshot_scale=None, qemu_binary=None, loadvm=None,
                 kernel_path=None, dtb_path=None, kernel_append=None, window_slot=None):
        self.name = name
        # tile slot (dispatch_functions._next_window_slot / windowlayout.tile_position)
        self.window_slot = window_slot
        # overrides the "qemu-system-<arch>" binary
        self.qemu_binary = qemu_binary or None
        # screenshot scale (CONFIG["ocr_scale"]); 1x default because gui_move_to() uses raw screenshot pixel coords
        try:
            self.screenshot_scale = float(screenshot_scale) if screenshot_scale not in (None, "") else 1.0
        except (TypeError, ValueError):
            self.screenshot_scale = 1.0
        # UTM config.plist fields; all optional
        self.machine = machine or None
        self.cpu = cpu or None
        self.vga = vga or None
        self.net_device = net_device or None
        self.mac_address = mac_address or None
        # e.g. "tcp::2222-:22": host port -> guest port in user-mode NAT
        self.hostfwd = hostfwd or None
        # machine ROM; else QEMU boots OpenBIOS
        self.bios_path = bios_path or None
        self.boot_order = boot_order or None
        # -prom-env settings (ppc, sparc)
        self.prom_env = list(prom_env or [])
        # extra flags appended last
        self.extra_args = list(extra_args or [])
        # internal-snapshot tag for -loadvm
        self.loadvm = loadvm or None
        # disks beyond hdd1..hdd4 (non-IDE buses)
        self.extra_disks = list(extra_disks or [])
        self.cpuarch = cpuarch
        self.monitor_port = _as_port(monitor_port, 55555)
        # first serial line -> host file
        self.serial_path = serial_path
        # sound opt-in via sound_device
        self.sound_device = sound_device
        self.audio_backend = (audio_backend or "none").strip().lower()
        self.audio_out_path = audio_out_path
        # named PulseAudio/PipeWire sink/source (echo tests)
        self.pa_out_name = pa_out_name or None
        self.pa_in_name = pa_in_name or None
        self.pa_server = pa_server or None
        # second QMP monitor for outside tools
        self.qmp_port = self.monitor_port + 1000
        self.memory = memory
        self.process = None
        self.sock = None
        self.stdout_lines = []
        self.stdout_thread = None
        self.screenshot_count = 0
        self.hdd1imagepath = hdd1imagepath
        self.hdd2imagepath = hdd2imagepath
        self.hdd3imagepath = hdd3imagepath
        self.hdd4imagepath = hdd4imagepath
        self.cdrom_path = cdrom_path
        # vnc_port is the TCP port; -vnc takes the display number (vnc_port - 5900)
        self.vnc_port = _as_port(vnc_port)
        self.vnc_client = None
        # floppy_path = A:, floppy2_path = B:, relative to TESTSRC_BASEDIR
        if floppy_path is not None:
            self.floppy_path = os.path.join(TESTSRC_BASEDIR, floppy_path)
        else:
            self.floppy_path = None
        if floppy2_path is not None:
            self.floppy2_path = os.path.join(TESTSRC_BASEDIR, floppy2_path)
        else:
            self.floppy2_path = None
        self.cdrom2_path = cdrom2_path
        # raspi: kernel+dtb handed to QEMU directly (no firmware boot stage)
        self.kernel_path = kernel_path or None
        self.dtb_path = dtb_path or None
        self.kernel_append = kernel_append or None


    def wait_for_ready(self, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            if self.process.poll() is not None:
                self.stdout_lines.append(f"QEMU process exited unexpectedly with code {self.process.returncode}")
                return False
            try:
                sock = socket.create_connection(("127.0.0.1", self.monitor_port), timeout=1)
                sock.close()
                return True
            except (ConnectionRefusedError, socket.timeout):
                time.sleep(0.3)
        self.stdout_lines.append(f"Timeout waiting for QEMU monitor on port {self.monitor_port}")
        return False


    def is_alive(self):
        """False once the QEMU process has exited. Also reaps the child via poll()."""
        return self.process is not None and self.process.poll() is None


    def _read_stdout(self):
        if self.process and self.process.stdout:
            for line in self.process.stdout:
                self.stdout_lines.append(line.strip())


    # PC IDE bus: 4 slots (0..3), allocated explicitly
    IDE_SLOTS = 4
    # CD-ROMs prefer the secondary channel; hard disks fill from the primary
    _CD_PREFERRED = (2, 3)

    def _allocate_ide(self):
        """IDE slots for the configured disks and CD-ROMs -> (assignments [(index, path, media)], error); error if more than IDE_SLOTS."""
        hdds = [p for p in (self.hdd1imagepath, self.hdd2imagepath,
                            self.hdd3imagepath, self.hdd4imagepath) if p]
        cds = [p for p in (self.cdrom_path, self.cdrom2_path) if p]
        total = len(hdds) + len(cds)
        if total > self.IDE_SLOTS:
            return None, (
                f"too many IDE devices for {self.name}: {len(hdds)} hard disk(s) + "
                f"{len(cds)} cdrom(s) = {total}, but the PC IDE bus has only "
                f"{self.IDE_SLOTS} slots (2 channels x 2 units). Drop one, or move a "
                f"disk to the floppy drives (A:/B:).")

        cd_slots = list(self._CD_PREFERRED[:len(cds)])
        free = [i for i in range(self.IDE_SLOTS) if i not in cd_slots]
        assignments = [(slot, path, "cdrom") for slot, path in zip(cd_slots, cds)]
        assignments += [(slot, path, "disk") for slot, path in zip(free, hdds)]
        return sorted(assignments), None

    @staticmethod
    def _drive_opt(path):
        """Escape a path for a -drive option (commas doubled)."""
        return str(path).replace(",", ",,")

    def build_args(self):
        """QEMU argument list for this instance (shared by start() and write_launch_script()); None (error in stdout_lines) if cpuarch is unsupported."""
        if self.cpuarch == "i386":
            args = [
                "qemu-system-i386",
                "-m", str(self.memory),
                "-monitor", f"tcp:127.0.0.1:{self.monitor_port},server,nowait",
                "-qmp", f"tcp:127.0.0.1:{self.qmp_port},server,nowait",
                "-vga", self._vga_name() or "std"
            ]
            # unset: QEMU defaults (-M pc, default CPU)
            if self.machine:
                args.extend(["-M", self.machine])
            if self.cpu:
                args.extend(["-cpu", self.cpu])
            if self.floppy_path:
                args.extend(["-fda", self.floppy_path])
            if self.floppy2_path:
                args.extend(["-fdb", self.floppy2_path])
            # HDDs and CD-ROMs share the 4-slot IDE bus
            ide, ide_err = self._allocate_ide()
            if ide_err:
                self.stdout_lines.append(ide_err)
                return None
            for index, path, media in ide:
                args.extend(["-drive",
                             f"file={self._drive_opt(path)},if=ide,"
                             f"index={index},media={media}"])
            args.extend(self.build_audio_args())
            args.extend(self.build_net_args())
            if self.boot_order:
                args.extend(["-boot", self.boot_order])


        elif self.cpuarch == "m68k":
            # q800 ASC/EASC sound is on-board: audiodev= in the machine string
            machine = "q800"
            audio_args = self.build_audio_args()
            if audio_args:
                backend_spec = audio_args[audio_args.index("-audiodev") + 1]
                aid = next(tok.split("=", 1)[1] for tok in backend_spec.split(",")
                          if tok.startswith("id="))
                machine = f"q800,audiodev={aid}"
            args = [
                "qemu-system-m68k",
                "-L", "pc-bios",
                "-M", machine,
                "-m", "64",
                # snapshot=on: read-only image, writes to a temp file (shared write lock allows only one q800)
                "-drive", f"id=hd0,file={TESTSRC_BASEDIR}/m68k/pramdisk.img,format=raw,if=none,snapshot=on",
                "-device", "scsi-hd,scsi-id=0,drive=hd0",
                # boot disk: hdd1_qcow_path (COW overlay, see _prepare_hdd_overlay) else legacy writable maindisk.img
                "-drive", (f"id=hd1,file={self.hdd1imagepath},format=qcow2,if=none"
                           if self.hdd1imagepath else
                           f"id=hd1,file={TESTSRC_BASEDIR}/m68k/maindisk.img,format=raw,if=none"),
                "-device", "scsi-hd,scsi-id=1,drive=hd1",
                "-bios", f"{TESTSRC_BASEDIR}/m68k/Quadra-650.ROM",
                "-boot", "d",
                "-monitor", f"tcp:127.0.0.1:{self.monitor_port},server,nowait",
                "-qmp", f"tcp:127.0.0.1:{self.qmp_port},server,nowait"
            ]
            # CD-ROM at scsi-id 3 (highest id boots): cdrom_path unset = MacOS761.iso, "none" = no CD (hard disk scsi-id 1 boots), <file> = that image
            cd = self.cdrom_path
            if cd is None:
                cd = f"{TESTSRC_BASEDIR}/m68k/MacOS761.iso"
            if str(cd).lower() != "none":
                args.extend(["-drive", f"id=cd0,file={cd},media=cdrom,if=none",
                             "-device", "scsi-cd,scsi-id=3,drive=cd0"])
            # only one of -audio none / -audiodev
            if audio_args:
                args.extend(audio_args)
            else:
                args.extend(["-audio", "none"])
            # optional SCSI disk (scsi-id 2): Retro68 HFS .dsk
            if self.hdd2imagepath is not None:
                args.extend([
                    "-drive", f"id=hd2,file={self.hdd2imagepath},format=raw,if=none",
                    "-device", "scsi-hd,scsi-id=2,drive=hd2",
                ])
            # optional serial port -> host file (console-less q800)
            if self.serial_path is not None:
                args.extend(["-serial", _serial_backend_arg(self.serial_path)])
            if self.vnc_port is not None:
                vnc_display = self.vnc_port - 5900
                args.extend(["-vnc", f":{vnc_display}"])



        elif self.cpuarch == "ppc":
            # mac99 has no floppy controller: warn; use hdd2_path (IDE)
            if self.floppy_path or self.floppy2_path:
                self.stdout_lines.append(
                    "WARNING: floppy_path/floppy2_path given but cpuarch=ppc/mac99 has "
                    "no floppy controller here -- ignored. Use hdd2_path instead.")
            # PowerMac screamer sound: standalone -audiodev
            machine = self.machine or "mac99"
            audio_args = self.build_audio_args()
            args = [
                "qemu-system-ppc",
                "-M", machine,
                "-m", str(self.memory),
                "-monitor", f"tcp:127.0.0.1:{self.monitor_port},server,nowait",
                "-qmp", f"tcp:127.0.0.1:{self.qmp_port},server,nowait",
            ]
            if self.cpu:
                args.extend(["-cpu", self.cpu])
            # mac99 on-board framebuffer; -vga only when named
            if self._vga_name():
                args.extend(["-vga", self._vga_name()])
            if self.bios_path:
                args.extend(["-bios", self.bios_path])
            args.extend(audio_args)
            # Mac OS 9 on the IDE bus (4 slots)
            ide, ide_err = self._allocate_ide()
            if ide_err:
                self.stdout_lines.append(ide_err)
                return None
            for index, path, media in ide:
                args.extend(["-drive",
                             f"file={self._drive_opt(path)},if=ide,"
                             f"index={index},media={media}"])
            args.extend(self.build_net_args())
            args.extend(self._prom_env_args())
            if self.boot_order:
                args.extend(["-boot", self.boot_order])
            if self.serial_path is not None:
                args.extend(["-serial", _serial_backend_arg(self.serial_path)])
            if self.vnc_port is not None:
                args.extend(["-vnc", f":{self.vnc_port - 5900}"])

        elif self.cpuarch == "sparc":
            # SPARCstation: SCSI chain, CD-ROM at unit 6
            args = [
                "qemu-system-sparc",
                "-M", self.machine or "SS-5",
                "-m", str(self.memory),
                "-monitor", f"tcp:127.0.0.1:{self.monitor_port},server,nowait",
                "-qmp", f"tcp:127.0.0.1:{self.qmp_port},server,nowait",
            ]
            if self.cpu:
                args.extend(["-cpu", self.cpu])
            # without a Sun PROM image QEMU boots OpenBIOS (some Solaris releases refuse)
            if self.bios_path:
                args.extend(["-bios", self.bios_path])
            unit = 0
            for path in [p for p in (self.hdd1imagepath, self.hdd2imagepath,
                                     self.hdd3imagepath, self.hdd4imagepath)
                         if p] + self.extra_disks:
                if unit == 6:
                    unit += 1
                args.extend(["-drive",
                             f"file={self._drive_opt(path)},if=scsi,bus=0,"
                             f"unit={unit},media=disk"])
                unit += 1
            if self.cdrom_path:
                args.extend(["-drive",
                             f"file={self._drive_opt(self.cdrom_path)},if=scsi,"
                             f"bus=0,unit=6,media=cdrom"])
            args.extend(self.build_audio_args())
            args.extend(self.build_net_args())
            args.extend(self._prom_env_args())
            if self.boot_order:
                args.extend(["-boot", self.boot_order])
            if self.serial_path is not None:
                args.extend(["-serial", _serial_backend_arg(self.serial_path)])
            if self.vnc_port is not None:
                args.extend(["-vnc", f":{self.vnc_port - 5900}"])

        elif self.cpuarch in ("arm", "aarch64"):
            # raspi: kernel+dtb extracted ahead of time, passed via -kernel/-dtb (see raspberry_pi/NOTES.txt)
            binary = "qemu-system-arm" if self.cpuarch == "arm" else "qemu-system-aarch64"
            if not self.kernel_path:
                self.stdout_lines = [
                    f"cpuarch={self.cpuarch} needs kernel_path (e.g. kernel.img/kernel8.img "
                    "extracted from the boot partition of the Raspbian image) -- QEMU's raspi "
                    "machines have no firmware stage to load one off the SD card themselves."]
                return None
            args = [
                binary,
                "-M", self.machine or ("raspi0" if self.cpuarch == "arm" else "raspi3ap"),
                "-kernel", self.kernel_path,
                "-monitor", f"tcp:127.0.0.1:{self.monitor_port},server,nowait",
                "-qmp", f"tcp:127.0.0.1:{self.qmp_port},server,nowait",
                "-no-reboot",
            ]
            if self.cpu:
                args.extend(["-cpu", self.cpu])
            if self.dtb_path:
                args.extend(["-dtb", self.dtb_path])
            if self.kernel_append:
                args.extend(["-append", self.kernel_append])
            # SD card = hdd1 (qcow2 overlay on the Raspbian template)
            if self.hdd1imagepath:
                args.extend(["-drive",
                             f"file={self._drive_opt(self.hdd1imagepath)},format=qcow2,if=sd"])
            # raspi has no PS/2: always attach USB controller + keyboard so sendkey works
            args.extend(["-usb", "-device", "usb-kbd"])
            # Pi Zero/Zero 2: NIC is usb-net on the same -usb
            if self.net_device:
                netdev = "user,id=net0"
                if self.hostfwd:
                    netdev += f",hostfwd={self.hostfwd}"
                args.extend(["-netdev", netdev,
                             "-device", f"{self.net_device},netdev=net0"])
            if self.serial_path is not None:
                args.extend(["-serial", _serial_backend_arg(self.serial_path)])
            else:
                # no serial configured: default to stdio
                args.extend(["-serial", "stdio"])
            if self.vnc_port is not None:
                args.extend(["-vnc", f":{self.vnc_port - 5900}"])

        else:
            self.stdout_lines = [f"Unsupported CPU architecture: {self.cpuarch}"]
            return None

        if self.qemu_binary:
            args[0] = self.qemu_binary

        if self.loadvm:
            args.extend(["-loadvm", self.loadvm])

        args.extend(self.extra_args)
        return args

    # UTM device name -> QEMU -vga type; unmapped pass through
    _VGA_ALIASES = {
        "cirrus-vga": "cirrus",
        "isa-vga": "std",
        "vga": "std",
        "vmware-svga": "vmware",
        "virtio-vga": "virtio",
        "qxl-vga": "qxl",
    }

    def _vga_name(self):
        if not self.vga:
            return None
        return self._VGA_ALIASES.get(str(self.vga).strip().lower(), self.vga)

    @staticmethod
    def _audiodev_id(audio_args):
        """id= of the -audiodev in `audio_args` (for machines enabling the sound chip by machine property)."""
        backend_spec = audio_args[audio_args.index("-audiodev") + 1]
        return next(tok.split("=", 1)[1] for tok in backend_spec.split(",")
                    if tok.startswith("id="))

    def build_net_args(self):
        """QEMU flags for the NIC, or [] (always user-mode networking)."""
        if not self.net_device:
            return []
        nic = f"{self.net_device},netdev=net0"
        if self.mac_address:
            nic += f",mac={self.mac_address}"
        netdev = "user,id=net0"
        if self.hostfwd:
            netdev += f",hostfwd={self.hostfwd}"
        return ["-netdev", netdev, "-device", nic]

    def _prom_env_args(self):
        out = []
        for setting in self.prom_env:
            out.extend(["-prom-env", setting])
        return out

    def build_audio_args(self):
        """QEMU flags for the sound card, or []. Backends:
          wav   mixed output to audio_out_path (default for automated runs)
          pa    PipeWire/pulse socket for a listener; pa_out_name/pa_in_name pick sink/source, pa_server the server
          none  card present, output discarded
        Recording needs an ADC-capable card/backend (e.g. ES1370, "pa"); SB16 is output-only.
        """
        if not self.sound_device or self.audio_backend in ("", "none"):
            return []

        aid = "snd0"
        if self.audio_backend == "wav":
            if not self.audio_out_path:
                self.stdout_lines.append(
                    "audio_backend='wav' needs audio_out_path; starting with no sound card")
                return []
            backend = f"wav,id={aid},path={self.audio_out_path}"
        elif self.audio_backend == "pa":
            # named PulseAudio sink/source (echo tests)
            backend = f"pa,id={aid}"
            if self.pa_out_name:
                backend += f",out.name={self.pa_out_name}"
            if self.pa_in_name:
                backend += f",in.name={self.pa_in_name}"
            if self.pa_server:
                backend += f",server={self.pa_server}"
        else:
            self.stdout_lines.append(
                f"Unknown audio_backend {self.audio_backend!r}; starting with no sound card")
            return []

        if self.cpuarch in ("m68k", "ppc"):
            # on-board sound: -audiodev id only, via the machine string
            return ["-audiodev", backend]
        return ["-audiodev", backend, "-device", f"{self.sound_device},audiodev={aid}"]

    def _prepare_audio(self, env):
        """Side effects the audio backend needs at launch (kept out of build_args())."""
        if not self.sound_device or self.audio_backend in ("", "none"):
            return

        if self.audio_backend == "wav" and self.audio_out_path:
            os.makedirs(os.path.dirname(self.audio_out_path) or ".", exist_ok=True)
            # clear stale capture
            try:
                os.remove(self.audio_out_path)
            except FileNotFoundError:
                pass
            except OSError as e:
                self.stdout_lines.append(f"[audio] could not clear {self.audio_out_path}: {e}")

        elif self.audio_backend == "pa":
            # fill PipeWire env vars the flask runner lacks
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            env.setdefault("PULSE_SERVER",
                           f"unix:{env['XDG_RUNTIME_DIR']}/pulse/native")

    def referenced_disk_images(self, args=None):
        """Disk/floppy/CD-ROM/BIOS paths the QEMU args point at; existing only, command-line order, de-duplicated."""
        if args is None:
            args = self.build_args() or []
        # flags followed by a bare path (e.g. -hda /path)
        path_flags = {"-hda", "-hdb", "-hdc", "-hdd", "-fda", "-fdb",
                      "-bios", "-cdrom", "-kernel", "-dtb"}
        found, prev = [], None
        for a in args:
            if prev in path_flags:
                found.append(a)
            # -drive/-device: path in file=...
            for tok in a.split(","):
                if tok.startswith("file="):
                    found.append(tok[len("file="):])
            prev = a
        out, seen = [], set()
        for f in found:
            if f and f not in seen and os.path.isfile(f):
                seen.add(f)
                out.append(f)
        return out

    def write_launch_script(self, path=None):
        """Write an executable shell script that launches QEMU with these parameters -> (success, path_or_error)."""
        import shlex
        args = self.build_args()
        if args is None:
            return False, f"Cannot build launch script: unsupported cpuarch {self.cpuarch!r}"
        if path is None:
            reports_dir = os.path.join(APPBASE_DIR, "reports")
            os.makedirs(reports_dir, exist_ok=True)
            path = os.path.join(reports_dir, f"start-qemu-{self.name}.sh")
        cmdline = " \\\n    ".join(shlex.quote(a) for a in args)
        script = (
            "#!/usr/bin/env bash\n"
            f"# Auto-generated for the '{self.name}' ({self.cpuarch}) VM.\n"
            "set -euo pipefail\n\n"
            f"exec {cmdline}\n"
        )
        try:
            with open(path, "w") as f:
                f.write(script)
            os.chmod(path, 0o755)
        except OSError as e:
            return False, f"Failed to write launch script to {path}: {e}"
        return True, path

    def start(self):
        env = os.environ.copy()
        args = self.build_args()
        if args is None:
            return False
        self._prepare_audio(env)

        try:
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env
            )
        except OSError as e:
            self.stdout_lines = [f"Failed to start QEMU: {e}"]
            return False

        processreg.register(self.name, self.process, source="qemu")

        time.sleep(1)
        retcode = self.process.poll()

        if retcode is not None:
            output, _ = self.process.communicate(timeout=1)
            self.stdout_lines = [output] if output else ["[no output captured]"]
            return False

        self.stdout_thread = threading.Thread(target=self._read_stdout, name="QEMU-stdout-reader")
        self.stdout_thread.daemon = True
        self.stdout_thread.start()

        start_time = time.time()
        while time.time() - start_time < 5:
            if self.stdout_lines:
                break
            time.sleep(0.1)

        if not self._wait_for_monitor():
            return False

        try:
            self.sock = socket.create_connection(("127.0.0.1", self.monitor_port), timeout=2)
        except Exception as e:
            self.stdout_lines.append(f"[monitor socket error] {e}")
            return False

        self._reposition_window_to_origin()

        return True

    def _reposition_window_to_origin(self, attempts=15, delay=0.3):
        """Move the QEMU GTK window onto its round-robin tile (windowlayout.tile_position) right after launch, clamped by its own size against the session; polls for the window to appear."""
        display = os.environ.get("DISPLAY")
        for _ in range(attempts):
            wid, w, h = self._gui_find_window()
            if wid:
                x, y = windowlayout.tile_position(self.window_slot, (w, h), display)
                self._xdo("windowmove", wid, str(x), str(y))
                return
            time.sleep(delay)

    def take_screenshot(self, test_step=None, filename=None):
        _helperdir = "/testrunnersrc/pyhelpers"
        if _helperdir not in sys.path:
            sys.path.insert(0, _helperdir)
        from appstate import progress_state, current_reports_dir
        stepnum = progress_state.step
        # idle instance ("Idle", no digits) would raise AttributeError
        _m = re.match(r'\d+', stepnum or "")
        stepnum = _m.group(0) if _m else "live"
        if not test_step:
            if not filename:
                test_step = stepnum


        reports_dir = current_reports_dir()

        if filename:
            png_path_abs = os.path.abspath(filename)
        else:
            step_str = f"-{os.path.basename(str(test_step))}" if test_step else ""
            name = f"screenshot-{self.name}{step_str}-{self.screenshot_count}.png"
            png_path_abs = os.path.join(reports_dir, name)

        ppm_path_abs = png_path_abs.replace(".png", ".ppm")

        if not self.sock:
            return False, "Monitor socket not connected"

        try:
            with self.lock:
                self.sock.sendall(f"screendump {ppm_path_abs}\n".encode("utf-8"))

                buf = b""
                while b"(qemu)" not in buf:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        return False, "Monitor disconnected"
                    buf += chunk

                # wait for the PPM size to stabilize
                start = time.time()
                last_size = -1
                stable = 0
                while stable < 2:
                    if os.path.exists(ppm_path_abs):
                        size = os.path.getsize(ppm_path_abs)
                        stable = stable + 1 if size > 0 and size == last_size else 0
                        last_size = size
                    if time.time() - start > 5:
                        return False, f"Timed out waiting for screendump {ppm_path_abs}"
                    time.sleep(0.05)

            img = Image.open(ppm_path_abs)
            if self.screenshot_scale != 1.0:
                img = img.resize(
                    (max(1, round(img.width * self.screenshot_scale)),
                     max(1, round(img.height * self.screenshot_scale))),
                    Image.LANCZOS
                )
            img.save(png_path_abs)

            self.screenshot_count += 1
        except Exception as e:
            return False, f"Failed to take screenshot or convert image: {e}"

        return True, png_path_abs


    def _wait_for_monitor(self, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            try:
                sock = socket.create_connection(("127.0.0.1", self.monitor_port), timeout=1)
                sock.close()
                print(f"[{self.name}] Monitor connected on port {self.monitor_port}")
                return True
            except (ConnectionRefusedError, socket.timeout) as e:
                print(f"[{self.name}] Monitor connect attempt failed: {e}")
                time.sleep(0.3)
        self.stdout_lines.append(f"QEMU monitor timeout on port {self.monitor_port}")
        print(f"[{self.name}] Monitor timeout on port {self.monitor_port}")
        return False


    def send_key(self, keyname, ctrl=False, alt=False, shift=False, delay=0.1):
        if not self.sock:
            print(f"[{self.name}] No monitor socket connected")
            return

        mods = []
        if ctrl: mods.append("ctrl")
        if alt: mods.append("alt")
        if shift: mods.append("shift")
        combo = "-".join(mods + [keyname]) if mods else keyname
        try:
            with self.lock:
                self.sock.sendall(f"sendkey {combo}\n".encode("utf-8"))
        except Exception as e:
            print(f"[{self.name}] Failed to send key: {e}")
        time.sleep(delay)


    def send_keyboardstring(self, text, delay=0.05):
        keymap = {
            'a': 'a', 'b': 'b', 'c': 'c', 'd': 'd', 'e': 'e', 'f': 'f',
            'g': 'g', 'h': 'h', 'i': 'i', 'j': 'j', 'k': 'k', 'l': 'l',
            'm': 'm', 'n': 'n', 'o': 'o', 'p': 'p', 'q': 'q', 'r': 'r',
            's': 's', 't': 't', 'u': 'u', 'v': 'v', 'w': 'w', 'x': 'x',
            'y': 'y', 'z': 'z', '0': '0', '1': '1', '2': '2', '3': '3',
            '4': '4', '5': '5', '6': '6', '7': '7', '8': '8', '9': '9',
            ' ': 'spc', '.': 'dot', ',': 'comma', '-': 'minus',
            '/': 'slash', '\\': 'backslash', ':': 'semicolon',
            ';': 'semicolon', '\n': 'ret', '\r': 'ret',
            '*': '8'
        }

        shift_required = {
            ':', '_', '+', '{', '}', '|', '<', '>', '"', '?',
            '~', '!', '@', '#', '$', '%', '^', '&', '*', '(', ')'
        }

        for ch in text:
            key_char = ch.lower()
            if key_char in keymap:
                key = keymap[key_char]
                shift = ch.isupper() or ch in shift_required
                self.send_key(key, shift=shift, delay=delay)
            else:
                print(f"[{self.name}] Unsupported char for sendkey: {repr(ch)}")


    def send_specialkeys(self, keystr, ctrl=False, alt=False, shift=False, delay=0.1):
        key = keystr.lower()

        if key == 'enter':
            key = 'ret'
        elif key.startswith('f') and key[1:].isdigit():
            key = key
        elif key == 'return':
            key = 'ret'

        self.send_key(keystr.lower(), ctrl=ctrl, alt=alt, shift=shift, delay=delay)


    def send_command(self, cmd, wait_prompt=None):
        """Send one HMP command -> printed reply. A 2s output lull ends the read; wait_prompt=N reads until the next "(qemu)" prompt or N seconds (savevm/loadvm)."""
        with self.lock:
            try:
                self.sock.settimeout(0.2)
                while True:
                    if not self.sock.recv(4096):
                        break
            except socket.timeout:
                pass
            except Exception:
                pass

            self.sock.sendall((cmd + "\n").encode("utf-8"))
            self.sock.settimeout(2.0)

            data = ""
            start = time.time()
            prompt_count = 0

            while True:
                try:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        break
                    decoded = chunk.decode("utf-8", errors="replace")
                    data += decoded
                    prompt_count += decoded.count("(qemu)")
                    if prompt_count >= 1 and data.strip().endswith("(qemu)"):
                        break
                except socket.timeout:
                    if wait_prompt and time.time() - start < wait_prompt:
                        continue
                    break
                if time.time() - start > (wait_prompt or 5):
                    break

            return data.strip()


    def pause_vm(self):
        """HMP 'stop': quiesce the guest (before savevm)."""
        return self.send_command("stop")

    def resume_vm(self):
        """Restarts the guest CPU (HMP 'cont') after pause_vm/save_snapshot."""
        return self.send_command("cont")

    def save_snapshot(self, tag="quicksave"):
        """Pause, write an internal qcow2 snapshot `tag` (HMP 'savevm'), resume. Lives in hdd1's qcow2; `loadvm=` boots into it. Every writable disk must be qcow2 and one must hold the VM state. Check the reply with hmp_error()."""
        self.pause_vm()
        reply = self.send_command(f"savevm {tag}", wait_prompt=SNAPSHOT_WAIT_S)
        self.resume_vm()
        return reply

    def load_snapshot(self, tag="quicksave"):
        """Restore a snapshot into a running instance (HMP 'loadvm'); for a fresh process pass `loadvm=tag` to __init__/test_startqemu. Check the reply with hmp_error()."""
        return self.send_command(f"loadvm {tag}", wait_prompt=SNAPSHOT_WAIT_S)

    def delete_snapshot(self, tag):
        """Drops an internal snapshot (HMP 'delvm') from every disk holding it."""
        return self.send_command(f"delvm {tag}", wait_prompt=SNAPSHOT_WAIT_S)

    def send_mouse_pos(self, x, y, dz=None):
        if not self.sock:
            return False, "Monitor socket not connected"
        try:
            if dz is not None:
                cmd = f"mouse_move {x} {y} {dz}\n"
            else:
                cmd = f"mouse_move {x} {y}\n"
            with self.lock:
                self.sock.sendall(cmd.encode("utf-8"))
            return True, f"Mouse moved to ({x}, {y})"
        except Exception as e:
            return False, f"Failed to send mouse_move command: {e}"


    def _monitor_send(self, cmd, drain_timeout=2.0):
        """Send one HMP command and drain to the next (qemu) prompt."""
        if not self.sock:
            return False, "Monitor socket not connected"
        try:
            with self.lock:
                self.sock.sendall((cmd + "\n").encode("utf-8"))
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
            return True, buf.decode("utf-8", errors="replace")
        except Exception as e:
            return False, f"Monitor command failed ({cmd!r}): {e}"

    def mouse_click(self, button=1, hold=0.15):
        """Press then release an ADB mouse button via HMP; mouse_button bitmask: 1=L, 2=R, 4=M."""
        ok, _ = self._monitor_send(f"mouse_button {button}")
        if not ok:
            return False, f"Failed to press mouse button {button}"
        time.sleep(hold)
        self._monitor_send("mouse_button 0")
        return True, f"Mouse button {button} clicked"

    def mouse_move_to(self, x, y, settle=0.4):
        """Place the relative ADB mouse at absolute (x, y): peg top-left, then move by (x, y); not pixel-exact (acceleration)."""
        for _ in range(3):
            self._monitor_send("mouse_move -4000 -4000")
            time.sleep(0.1)
        time.sleep(settle)
        ok, _ = self._monitor_send(f"mouse_move {int(x)} {int(y)}")
        time.sleep(settle)
        return ok, f"Mouse positioned near ({x}, {y}) via corner-reset"


    # GUI mouse via QEMU GTK window + xdotool, in a screendump-feedback loop (relative ADB mouse can't be positioned via monitor/VNC)

    @staticmethod
    def _xdo(*a):
        return subprocess.run(["xdotool", *a], capture_output=True, text=True).stdout.strip()

    def _gui_screencap(self):
        """Monitor screendump -> numpy int16 RGB array for cursor tracking."""
        if not self.sock:
            return None
        path = f"/tmp/_guicap_{self.name}_{self.monitor_port}.ppm"
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            with self.lock:
                self.sock.sendall(f"screendump {path}\n".encode("utf-8"))
                prev = self.sock.gettimeout()
                self.sock.settimeout(3)
                buf = b""
                try:
                    while b"(qemu)" not in buf:
                        d = self.sock.recv(4096)
                        if not d:
                            break
                        buf += d
                except socket.timeout:
                    pass
                finally:
                    self.sock.settimeout(prev)
            for _ in range(80):
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    break
                time.sleep(0.05)
            return np.asarray(Image.open(path).convert("RGB")).astype(np.int16)
        except Exception:
            return None

    def _gui_find_window(self):
        """Find this instance's QEMU GTK window (by process pid, else by name)."""
        ids = []
        if getattr(self, "process", None):
            ids = self._xdo("search", "--pid", str(self.process.pid)).split()
        if not ids:
            ids = [w for w in self._xdo("search", "--name", "").split()
                   if "qemu" in (self._xdo("getwindowname", w) or "").lower()]
        for w in ids:
            g = dict(l.split("=", 1) for l in
                     self._xdo("getwindowgeometry", "--shell", w).splitlines() if "=" in l)
            if int(g.get("WIDTH", 0)) >= 600:
                return w, int(g["WIDTH"]), int(g["HEIGHT"])
        return None, None, None

    def gui_move_to(self, gx, gy, do_click=True, button=1,
                    tol=3, gain=0.5, cap=28, max_iter=90, clicks=1,
                    drag_to=None, hold_s=0.0, dwell_s=1.0):
        """Position the guest mouse at framebuffer pixel (gx, gy) and optionally click, via the GTK window with xdotool in a screendump-feedback loop -> (success, log).
        clicks>1: multi-click without moving (opens a Finder icon).
        drag_to=(x2, y2): press-drag-release; the route is driven first with the button UP recording every relative move, then replayed with the button DOWN. hold_s>0 pauses after the press and before the release.
        """
        log = []
        wid, W, H = self._gui_find_window()
        if not wid:
            return False, "GUI window not found (needs QEMU -display gtk on an X display)"

        # grab the pointer: click once in the window
        self._xdo("windowactivate", wid); time.sleep(0.3)
        self._xdo("mousemove", "--window", wid, str(W // 2), str(H // 2)); time.sleep(0.2)
        self._xdo("click", "1"); time.sleep(0.5)

        def peg(dx, dy, n=25):
            args = []
            for _ in range(n):
                args += ["mousemove_relative", "--", str(dx), str(dy)]
            self._xdo(*args); time.sleep(0.25)

        def rel(dx, dy):
            self._xdo("mousemove_relative", "--", str(int(dx)), str(int(dy)))

        peg(45, 45)
        ref = self._gui_screencap()
        if ref is None:
            return False, "screencap failed (monitor screendump)"
        gh, gw = ref.shape[:2]

        def detect():
            """Arrow tip = top-left pixel of the arrow-sized cluster of changed pixels (menu-bar clock area masked; text caret ignored)."""
            cur = self._gui_screencap()
            if cur is None:
                return None
            m = np.abs(cur - ref).sum(axis=2) > 60
            m[:24, 470:] = False
            m[gh - 80:, gw - 55:] = False
            if not m.any():
                return None
            bw, bh = 14, 20
            ii = np.pad(m.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
            ys, xs = np.where(m)
            y1 = np.minimum(ys + bh, gh); x1 = np.minimum(xs + bw, gw)
            cnt = ii[y1, x1] - ii[ys, x1] - ii[y1, xs] + ii[ys, xs]
            ok = cnt >= 30
            if not ok.any():
                return None
            i = np.lexsort((xs[ok], ys[ok]))[0]
            return (int(xs[ok][i]), int(ys[ok][i]))

        def track(tx, ty, trace=None):
            """Closed-loop move to (tx, ty); returns (last_seen, residual)."""
            last = None
            for _ in range(max_iter):
                c = detect()
                if c is None:
                    rel(6, 6)
                    if trace is not None:
                        trace.append((6, 6, 0.0))
                    continue
                last = c
                ex, ey = tx - c[0], ty - c[1]
                if abs(ex) <= tol and abs(ey) <= tol:
                    break
                dx = max(-cap, min(cap, ex * gain))
                dy = max(-cap, min(cap, ey * gain))
                rel(dx, dy)
                if trace is not None:
                    trace.append((int(dx), int(dy), 0.1))
                time.sleep(0.1)
            return last, (None if last is None else (tx - last[0], ty - last[1]))

        peg(-45, -45)
        last, err = track(gx, gy)
        log.append(f"positioned cursor at {last} (target {gx},{gy}, residual {err})")
        if drag_to is not None:
            # target (x, y) or waypoints [(x, y), ...]: button stays down, dwell_s at each
            wps = [tuple(drag_to)] if isinstance(drag_to[0], int) else [tuple(w) for w in drag_to]
            segs = []
            for x2, y2 in wps:
                trace = []
                last2, err2 = track(x2, y2, trace)
                log.append(f"drag route learned: {len(trace)} moves to {last2} "
                           f"(target {x2},{y2}, residual {err2})")
                if last2 is None:
                    return False, "\n".join(log) + "\ndrag: cursor lost on the route"
                segs.append(trace)
            back, errb = track(gx, gy)
            log.append(f"returned to {back} (residual {errb})")
            time.sleep(0.3)
            self._xdo("mousedown", str(button))
            time.sleep(0.12 + hold_s)
            for i, trace in enumerate(segs):
                for dx, dy, dt in trace:
                    rel(dx, dy)
                    time.sleep(dt)
                if i + 1 < len(segs):
                    time.sleep(dwell_s)
            time.sleep(0.3 + hold_s)
            self._xdo("mouseup", str(button))
            log.append(f"dragged ({gx},{gy}) -> {wps} replaying {sum(map(len, segs))} moves")
            return True, "\n".join(log)
        if do_click:
            time.sleep(0.3)
            for i in range(max(1, int(clicks))):
                self._xdo("mousedown", str(button))
                time.sleep(0.12)
                self._xdo("mouseup", str(button))
                if i + 1 < clicks:
                    time.sleep(0.08)
            log.append(f"clicked button {button} x{max(1, int(clicks))} at ({gx},{gy})")
        return True, "\n".join(log)


    def vnc_connect(self):
        if self.vnc_port is None:
            return False, "No vnc_port configured for this instance"
        try:
            from vncdotool import api
            self.vnc_client = api.connect(f"127.0.0.1::{self.vnc_port}")
            return True, f"Connected to VNC on port {self.vnc_port}"
        except Exception as e:
            return False, f"Failed to connect to VNC: {e}"

    def vnc_mouse_move(self, x, y):
        if not self.vnc_client:
            return False, "VNC client not connected"
        try:
            self.vnc_client.mouseMove(x, y)
            return True, f"VNC mouse moved to ({x}, {y})"
        except Exception as e:
            return False, f"Failed to move VNC mouse: {e}"

    def vnc_mouse_click(self, x, y, button=1):
        if not self.vnc_client:
            return False, "VNC client not connected"
        try:
            # settle the pointer before pressing (vncdotool is async)
            self.vnc_client.mouseMove(x, y)
            time.sleep(0.3)
            self.vnc_client.mousePress(button)
            return True, f"VNC mouse clicked at ({x}, {y})"
        except Exception as e:
            return False, f"Failed to click VNC mouse: {e}"

    def vnc_disconnect(self):
        if self.vnc_client:
            try:
                self.vnc_client.disconnect()
            except Exception:
                pass
            self.vnc_client = None


    def collect_qemu_logs(self, save_path=None):
        logs = "\n".join(self.stdout_lines)
        if save_path:
            save_path = os.path.join("/testrunnerapp", save_path)
            try:
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(logs)
            except Exception as e:
                return False, f"Failed to write QEMU logs to {save_path}: {e}"
        return True, logs


    def take_screenshots_to_gif(self, interval, count, gif_name="screencap.gif", base_name="frame"):
        framerate = 10
        start_time = time.time()

        try:
            temp_dir = os.path.abspath(f"screens_tmp_{self.name}")
            os.makedirs(temp_dir, exist_ok=True)
            frames = []

            for i in range(count):
                name = f"{base_name}_{i}"
                ppm_path_abs = os.path.join(temp_dir, name + ".ppm")
                png_path = os.path.join(temp_dir, name + ".png")

                self.send_command(f"screendump {ppm_path_abs}")
                time.sleep(0.5)

                wait_start = time.time()
                while not os.path.exists(ppm_path_abs):
                    if time.time() - wait_start > 5:
                        return False, f"[{self.name}] Timed out waiting for screendump {ppm_path_abs}"
                    time.sleep(0.1)

                img = Image.open(ppm_path_abs)
                img.save(png_path)
                frames.append(img.convert("RGB"))

                time.sleep(interval)

            gif_path = os.path.abspath(os.path.join("reports", gif_name))
            frame_duration = max(1, int(1000.0 / framerate))

            frames[0].save(
                gif_path,
                save_all=True,
                append_images=frames[1:],
                duration=frame_duration,
                loop=0
            )

            duration = time.time() - start_time
            gif_playback_time = count / framerate
            speedup = duration / gif_playback_time if gif_playback_time > 0 else 0

            info = (f"[{self.name}] Captured {len(frames)} screenshots in {duration:.2f}s. "
                    f"GIF plays at {framerate} fps for {gif_playback_time:.2f}s "
                    f"(~{speedup:.1f}x speedup). Saved to {gif_path}")

            return True, info

        except Exception as e:
            return False, f"[{self.name}] GIF screenshot capture failed: {e}"


    def attach_floppy(self, path):
        if not os.path.exists(path):
            return False, f"[{self.name}] Floppy image not found: {path}"

        cmd = f"change floppy0 {path}"
        response = self.send_command(cmd)

        if "could not" in response.lower() or "error" in response.lower():
            return False, f"[{self.name}] Failed to attach floppy: {response.strip()}"
        return True, f"[{self.name}] Floppy attached successfully."


    def detatch_floppy(self):
        response = self.send_command("eject floppy0")

        if "ejected" in response.lower() or "floppy0" in response.lower():
            return True, f"[{self.name}] Floppy ejected successfully."
        elif "not found" in response.lower() or "error" in response.lower():
            return False, f"[{self.name}] Failed to eject floppy: {response.strip()}"
        return True, f"[{self.name}] Eject command sent."


    def stop(self):
        self.vnc_disconnect()
        processreg.unregister(self.name)
        if self.process:
            self.process.terminate()
            self.process.wait()
            self.sock.close()
            self.process = None
            self.sock = None


class MouseAction:
    @staticmethod
    def closedialogbutton(instance, test_step):
        button_path = os.path.join(TESTSRC_BASEDIR, "buttontest", "finder_window_closebutton.png")
        offset_x=-150
        offset_y=-190
        if not instance:
            return False, "No instance provided"

        log = []

        ok, screenshot_path = instance.take_screenshot(test_step)
        if not ok:
            log.append(f"[{instance.name}] Screenshot failed: {screenshot_path}")
            return False, "\n".join(log)

        log.append(f"[{instance.name}] Screenshot taken: {screenshot_path}")

        success, pos = find_button_in_screenshot(button_path, screenshot_path)
        if success:
            x, y = pos
            log.append(f"Button found at {pos}")
            ok, msg = instance.gui_move_to(x, y, do_click=True)
            log.append(msg)
            ok, screenshot_path = instance.take_screenshot(test_step)
        else:
            log.append("Button not found")

        return success, "\n".join(log)
    

    def findandclicktitlebar(instance, test_step):
        
        button_path = os.path.join(TESTSRC_BASEDIR, "buttontest", "inactive_titlebar_system7.png")
        offset_x=-150
        offset_y=-190
        if not instance:
            return False, "No instance provided"

        log = []

        ok, screenshot_path = instance.take_screenshot(test_step)
        if not ok:
            log.append(f"[{instance.name}] Screenshot failed: {screenshot_path}")
            return False, "\n".join(log)

        log.append(f"[{instance.name}] Screenshot taken: {screenshot_path}")

        success, pos = find_button_in_screenshot(button_path, screenshot_path)
        if success:
            x, y = pos
            log.append(f"Button found at {pos}")

            ok, msg = instance.gui_move_to(x, y, do_click=True)
            log.append(msg)
            ok, screenshot_path = instance.take_screenshot(test_step)
        else:
            log.append("Button not found")

        return success, "\n".join(log)

    @staticmethod
    def click_screen_center(instance, test_step):
        """Click the center of the guest framebuffer via gui_move_to (no image match)."""
        if not instance:
            return False, "No instance provided"

        log = []
        w = getattr(instance, "screen_width", None) or 1024
        h = getattr(instance, "screen_height", None) or 768
        gx, gy = w // 2, h // 2

        ok, msg = instance.gui_move_to(gx, gy, do_click=True)
        log.append(f"Clicked screen center ({gx}, {gy}): {msg}")
        return ok, "\n".join(log)

    @staticmethod
    def move_screen_center(instance, test_step):
        """Move (no click) to the center of the guest framebuffer, to park the cursor away from corner content before an OCR check."""
        if not instance:
            return False, "No instance provided"

        log = []
        w = getattr(instance, "screen_width", None) or 1024
        h = getattr(instance, "screen_height", None) or 768
        gx, gy = w // 2, h // 2

        ok, msg = instance.gui_move_to(gx, gy, do_click=False)
        log.append(f"Moved to screen center ({gx}, {gy}): {msg}")
        return ok, "\n".join(log)


def _text_area(img, scale_factor=1.0):
    """(x0, y0, x1, y1) pixels of the emulator output part of a screenshot. 86Box/DOSBox captures include menu/toolbar/status bars (flat neutral light grey, Qt 216..253); bars are trimmed from each edge while at least half their pixels are flat chrome. QEMU/VNC captures come back whole, as does anything leaving under 40%."""
    step = max(1, int(round(scale_factor)))
    rgb = np.asarray(img.convert("RGB"))[step // 2::step, step // 2::step].astype(np.int16)
    h, w = rgb.shape[:2]
    lum = rgb.sum(axis=2) // 3
    grey = (rgb.max(axis=2) - rgb.min(axis=2) <= 6) & (lum >= 205) & (lum <= 252)
    flat = grey.copy()
    flat[1:-1, 1:-1] = grey[1:-1, 1:-1]
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            flat[1:-1, 1:-1] &= grey[1 + dy:h - 1 + dy, 1 + dx:w - 1 + dx]
    flat[0, :] = flat[-1, :] = flat[:, 0] = flat[:, -1] = False

    def trim(flags):
        """(leading, trailing) counts of chrome entries; gaps of <= 3 (icon rows) are bridged."""
        idx = np.flatnonzero(flags)
        if len(idx) == 0:
            return 0, 0
        n = len(flags)
        lead = trail = 0
        if idx[0] <= 2:
            end = idx[0]
            for i in idx[1:]:
                if i - end > 4:
                    break
                end = i
            lead = end + 1
        if idx[-1] >= n - 3:
            start = idx[-1]
            for i in idx[::-1][1:]:
                if start - i > 4:
                    break
                start = i
            trail = n - start
        return lead, trail

    # skip the blurred chrome edge (3 native px) wherever a bar was cut
    def ringing(n, k):
        return n + k if n else 0

    top, bottom = trim(flat.mean(axis=1) >= 0.5)
    top, bottom = ringing(top, 3), ringing(bottom, 2)
    if bottom + top >= h:
        return 0, 0, img.width, img.height
    left, right = trim(flat[top:h - bottom].mean(axis=0) >= 0.5)
    left, right = ringing(left, 3), ringing(right, 2)
    y0, y1 = top * step, img.height - bottom * step
    x0, x1 = left * step, img.width - right * step
    if (y1 - y0) < img.height * 0.4 or (x1 - x0) < img.width * 0.5:
        return 0, 0, img.width, img.height
    return int(x0), int(y0), int(x1), int(y1)


def _text_grid(native_w, native_h, row_colors=None):
    """(cols, rows) for a text area of native_w x native_h pixels (before OCR upscale). Cell height 16, 14 or 8 px: the first whose row count tiles native_h within 2.5 px (400 = 25x16; 392 = 28x14; 344 = 43x8). `row_colors` (native_h x 3) decides 400 (16 vs 8): background changes at odd multiples of 8 mean 8-px cells. Columns 80 (or 40) if native_w is that many 8/9-px cells, else width/8."""
    fits = [c for c in (16, 14, 8) if abs(native_h - round(native_h / c) * c) <= 2.5]
    cell = fits[0] if fits else 16
    if 16 in fits and 8 in fits and row_colors is not None and len(row_colors) > 24:
        jump = np.abs(np.diff(row_colors.astype(float), axis=0)).sum(axis=1)
        ys = np.flatnonzero(jump >= 30) + 1
        on16 = sum(1 for y in ys if min(y % 16, 16 - y % 16) <= 1)
        on8 = sum(1 for y in ys if abs(y % 16 - 8) <= 1)
        if on8 > on16:
            cell = 8
    rows = max(1, int(round(native_h / cell)))
    for c in (80, 40):
        if min(abs(native_w - c * 8), abs(native_w - c * 9)) <= 1.5:
            return c, rows
    return (80 if 560 <= native_w <= 760 else max(1, int(round(native_w / 8)))), rows


def _normalize_polarity(img, scale_factor=1.0, min_scale=3, border=20):
    """Dark-on-white grayscale copy of a screen for tesseract's second pass: each text cell is flipped when most of it is darker than the midpoint of its min/max, contrast-stretched to 0..255, uniform cells white; grayscale kept.
    The cell grid is laid on the detected text area only (_text_area, _text_grid); whole image if none. Vectorised. Also Lanczos-upscaled to at least `min_scale`x native with a white border.
    """
    x0, y0, x1, y1 = _text_area(img, scale_factor)
    area = img.crop((x0, y0, x1, y1))
    lum = np.asarray(area.convert("L"), dtype=np.int32)
    h, w = lum.shape
    step = max(1, int(round(scale_factor)))
    native = np.asarray(area.convert("RGB"))[step // 2::step, step // 2::step]
    cols, rows = _text_grid(w / scale_factor, h / scale_factor, native.mean(axis=1))
    ys = np.linspace(0, h, rows + 1).round().astype(int)
    xs = np.linspace(0, w, cols + 1).round().astype(int)
    lo = np.minimum.reduceat(np.minimum.reduceat(lum, ys[:-1], axis=0), xs[:-1], axis=1)
    hi = np.maximum.reduceat(np.maximum.reduceat(lum, ys[:-1], axis=0), xs[:-1], axis=1)
    rh, cw = np.diff(ys), np.diff(xs)

    def expand(a):
        return np.repeat(np.repeat(a, rh, axis=0), cw, axis=1)
    mid = (lo + hi) // 2
    dark = (lum < expand(mid)).astype(np.int32)
    frac = np.add.reduceat(np.add.reduceat(dark, ys[:-1], axis=0), xs[:-1], axis=1) / np.outer(rh, cw)
    lo_p, hi_p = expand(lo), expand(hi)
    flip = expand(frac > 0.5)
    flat = expand((hi - lo) < 40)
    cell = np.where(flip, hi_p + lo_p - lum, lum)
    span = np.maximum(hi_p - lo_p, 1)
    out = np.where(flat, 255, (cell - lo_p) * 255 // span)
    norm = Image.fromarray(out.astype(np.uint8))
    if scale_factor < min_scale:
        f = math.ceil(min_scale / scale_factor)
        norm = norm.resize((w * f, h * f), Image.LANCZOS)
    return ImageOps.expand(norm, border=border, fill=255)


_NORM_POOL = None
_NORM_POOL_LOCK = threading.Lock()


def _ocr_parallel():
    """Run the normalized OCR pass alongside the raw one? Only with >= 8 cores; OCR_SERIAL=1 forces sequential."""
    return (os.cpu_count() or 1) >= 8 and not os.environ.get("OCR_SERIAL")


def _norm_pool():
    global _NORM_POOL
    with _NORM_POOL_LOCK:
        if _NORM_POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _NORM_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ocr-norm")
        return _NORM_POOL


def _normalized_ocr(img, scale_factor, ocr_config, save_path):
    """Polarity-normalize `img`, keep the image for the report, OCR it. -> (text, seconds)"""
    t0 = time.time()
    norm = _normalize_polarity(img, scale_factor)
    norm.save(save_path)
    return pytesseract.image_to_string(norm, config=ocr_config), time.time() - t0


def phrase_list(value):
    """Phrase param (string or list; None / "" / [] = none) -> list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)] if str(value) != "" else []


def _ocr_scratch_dir():
    """Host-local per-run dir for OCR screenshots/text: nothing written here is re-read through the shared mount."""
    run_id = ""
    try:
        if APPBASE_DIR not in sys.path:
            sys.path.insert(0, APPBASE_DIR)
        from appstate import progress_state
        run_id = getattr(progress_state, "run_id", "") or ""
    except Exception:
        pass
    d = os.path.join(tempfile.gettempdir(), "ocr_scratch", run_id or OCR_ADHOC_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


MIRROR_TIMEOUT = 20   # seconds to wait on the shared mount before giving up on the report copies


def _mirror_ocr_files(src_dir, names):
    """Copy this OCR wait's files to compile_logs/<run_id>/ for the report. Best effort on a daemon thread: a hung or erroring shared mount must not stall or fail the test."""
    import threading

    def work():
        try:
            dst = ocr_log_dir()
            for n in names:
                p = os.path.join(src_dir, n)
                if os.path.isfile(p):
                    shutil.copy2(p, os.path.join(dst, n))
        except Exception as e:
            print(f"OCR report copy to shared mount failed: {e}")

    t = threading.Thread(target=work, daemon=True, name="ocr-mirror")
    t.start()
    t.join(MIRROR_TIMEOUT)


def ocr_word_find(instance, successphrase, *args, **kwargs):
    """ocr_word_find_local() with its screenshots/text kept on a host-local dir, then copied to compile_logs/ for the report."""
    scratch = _ocr_scratch_dir()
    started = time.time() - 1
    try:
        return _ocr_word_find_local(instance, successphrase, scratch, *args, **kwargs)
    finally:
        _mirror_ocr_files(scratch, [n for n in os.listdir(scratch)
                                    if os.path.getmtime(os.path.join(scratch, n)) >= started])


def _ocr_word_find_local(instance, successphrase, ocr_dir, numberofattempts=5, attemptdelay=2, startx=None, starty=None, stopx=None, stopy=None, errorphrase=None, whilephrase=None, notphrase=None, should_stop=None):
    """Poll the screen until EVERY successphrase is on it and NO notphrase is. successphrase / notphrase: string or list. whilephrase = old name of notphrase. errorphrase hard-fails at once."""
    log = []

    start_time = time.time()
    # no successphrase: one OCR pass, capture screen text
    success_phrases = phrase_list(successphrase)
    phrases_lower = [p.lower() for p in success_phrases]
    error_lower = errorphrase.lower() if errorphrase else None
    # notphrase: blocks success while on screen (keeps polling)
    not_phrases = phrase_list(notphrase) + phrase_list(whilephrase)
    nots_lower = [p.lower() for p in not_phrases]
    attempts = 0
    text = ""

    # crop coords are native-resolution: scale to the screenshot
    scale_factor = float(getattr(instance, "screenshot_scale", 1.0) or 1.0)

    # end the wait if the emulator exits (is_alive(): DOSBox-X/86Box)
    alive = getattr(instance, "is_alive", None)
    inst_name = getattr(instance, "name", "instance")
    # reuse the last screenshot
    last_screenshot_path = None

    for i in range(numberofattempts):
        # should_stop: the run's Pause/Stop/Abandon (context["abort"])
        if should_stop is not None and should_stop():
            log.append("aborted (stop requested) — abandoning OCR wait")
            return False, text, attempts, log, last_screenshot_path
        if alive is not None and not alive():
            returncode = getattr(getattr(instance, "process", None), "returncode", None)
            log.append(f"{inst_name} exited (code {returncode}) — abandoning OCR wait")
            # let the stdout reader drain after poll()
            time.sleep(0.2)
            tail = getattr(instance, "stdout_lines", None)
            if tail:
                log.append(f"{inst_name} last output before exit:\n" + "\n".join(tail[-20:]))
            return False, text, attempts, log, last_screenshot_path

        attempts += 1
        iter_start = time.time()

        elapsed = int(iter_start - start_time)
        safe_phrase = (success_phrases[0] if success_phrases else "screen").replace(" ", "_")[:60]
        # PID-tagged: concurrent tests share the ocr dir
        pid = getattr(getattr(instance, "process", None), "pid", None)
        run_tag = f"{getattr(instance, 'name', 'inst')}-{pid if pid is not None else id(instance)}"
        filename_base = f"{run_tag}_{safe_phrase}_{elapsed}"
        screenshot_path = os.path.join(ocr_dir, filename_base)
        png_path = screenshot_path + ".png"
        txt_path = screenshot_path + ".txt"

        ok, msg = instance.take_screenshot(filename=png_path)
        if not ok:
            log.append(f"Screenshot failed: {msg}")
            time.sleep(2)
            continue

        last_screenshot_path = png_path

        try:
            crop_start = time.time()
            # OCR the file just written, not instance.last_image: another thread's grab (GIF recorder) may have replaced it, and the report shows png_path
            img = Image.open(png_path)

            if all(v is not None and str(v).isdigit() for v in (startx, starty, stopx, stopy)):
                box = (int(startx), int(starty), int(stopx), int(stopy))
                if scale_factor != 1.0:
                    box = tuple(round(v * scale_factor) for v in box)
                img = img.crop(box)

            img.load()
            crop_duration = time.time() - crop_start
            log.append(f"Crop completed in {float(crop_duration):.2f} seconds")

            ocr_start = time.time()
            # --psm 6 for VGA text; feed color, not grayscale (as liveview._ocr_png)
            ocr_config = f"--psm 6 {TESSERACT_TESSDATA_ARGS}".strip()
            # spare cores: start the polarity-normalized pass now (see _ocr_parallel)
            norm_future = None
            if phrases_lower and _ocr_parallel():
                norm_future = _norm_pool().submit(_normalized_ocr, img, scale_factor, ocr_config,
                                                  screenshot_path + "_norm.png")
            text = pytesseract.image_to_string(img, config=ocr_config)
            ocr_duration = time.time() - ocr_start
            log.append(f"OCR completed in {float(ocr_duration):.2f} seconds")

            # second pass only on a miss: inverted / low-contrast text (see _normalize_polarity)
            low = text.lower()
            if (phrases_lower and not all(p in low for p in phrases_lower)
                    and not (error_lower and error_lower in low)):
                if norm_future is not None:
                    alt, norm_secs = norm_future.result()
                    how = "in parallel"
                else:
                    alt, norm_secs = _normalized_ocr(img, scale_factor, ocr_config,
                                                     screenshot_path + "_norm.png")
                    how = "after the raw pass"
                text = f"{text}\n--- polarity-normalized pass ---\n{alt}"
                log.append(f"Normalized OCR pass completed in {float(norm_secs):.2f} seconds ({how})")

        except Exception as e:
            log.append(f"OCR failed on {png_path}: {str(e)}")
            text = ""

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)

        iter_total = time.time() - iter_start
        log.append(f"Total time this pass: {float(iter_total):.2f} seconds\n")

        text_lower = text.lower()

        if error_lower and error_lower in text_lower:
            log.append(f"Aborted early: '{errorphrase}' found.")
            return False, text, attempts, log, last_screenshot_path

        blocking = [p for p, pl in zip(not_phrases, nots_lower) if pl in text_lower]
        if blocking:
            log.append(f"NOT phrase(s) {blocking} still on screen — retrying")
        else:
            if not phrases_lower:
                return True, text, attempts, log, last_screenshot_path

            missing = [p for p, pl in zip(success_phrases, phrases_lower) if pl not in text_lower]
            if not missing:
                return True, text, attempts, log, last_screenshot_path
            if len(success_phrases) > 1:
                log.append(f"still waiting for {missing}")

        # check liveness each second
        deadline = time.time() + float(attemptdelay)
        while time.time() < deadline:
            if alive is not None and not alive():
                break
            if should_stop is not None and should_stop():
                break
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    return False, text, attempts, log, last_screenshot_path


def make_disk_image(floppypath, size):
    if not os.path.exists(floppypath):
        subprocess.check_call([
            "qemu-img",
            "create",
            floppypath,
            size
        ])
        return True, f"Created new floppy image of size {size}"
    else:
        return True, "Floppy image already exists with correct size"


DOS_BOOT_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "disk_artifacts", "dos_boot_assets")

# standard floppy sizes (KB) mformat knows
_STD_FLOPPY_KB = {160, 180, 320, 360, 720, 1200, 1440, 2880}
# FAT12 max 4084 clusters; FAT16 max 65524
_FAT12_MAX_CLUSTERS = 4084
_FAT16_MAX_CLUSTERS = 65524


def _pick_cluster_size(total_bytes, fat_bits):
    """Cluster size (512-byte sectors, power of 2) putting the cluster count in the FAT width's range; None lets mformat decide."""
    if fat_bits not in (12, 16):
        return None
    data_sectors = max(1, int(total_bytes) // 512)
    for cs in (1, 2, 4, 8, 16, 32, 64, 128):
        clusters = data_sectors // cs
        if fat_bits == 12 and clusters <= _FAT12_MAX_CLUSTERS:
            return cs
        if fat_bits == 16 and clusters <= _FAT16_MAX_CLUSTERS:
            return cs
    return 128


def _mtools_run(cmd, env=None, **kw):
    """subprocess.run for an mtools command (start_new_session=True: mtools prompts via /dev/tty)."""
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.STDOUT)
    return subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
                          start_new_session=True, **kw)


def _read_fat_type(path, offset=0):
    """Return '12'/'16'/'32' by parsing minfo's `disk type=` line, or None."""
    cfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
    cfg.write(f'drive z: file="{path}" offset={offset}\n')
    cfg.close()
    try:
        r = _mtools_run(["minfo", "z:"], env={**os.environ, "MTOOLSRC": cfg.name})
        m = re.search(r'disk type\s*=\s*"?FAT(\d+)', r.stdout.decode("utf-8", "replace"))
        return m.group(1) if m else None
    finally:
        os.unlink(cfg.name)


def create_disk_image(path, size_mb=None, size_kb=None, fat_bits=None, media="hdd",
                      bootable=False, label="", make_src_dir=False, overwrite=False):
    """Create a new FAT disk image from scratch -> (success, message).
      media     "hdd" (partitioned, offset 32256) | "floppy" (unpartitioned, offset 0)
      size_mb / size_kb   one of them (floppies size_kb, e.g. 1440)
      fat_bits  12 | 16 | None (mformat chooses)
      bootable  hdd only; delegates to create_fat_disk_image
    """
    media = str(media).strip().lower()
    if media not in ("hdd", "floppy"):
        return False, f"media must be 'hdd' or 'floppy', got {media!r}"
    try:
        fat_bits = int(fat_bits) if fat_bits not in (None, "", "auto") else None
    except (TypeError, ValueError):
        return False, f"invalid fat_bits: {fat_bits!r}"
    if fat_bits not in (None, 12, 16):
        return False, "fat_bits must be 12, 16, or None/auto"

    if size_kb:
        total_bytes = int(size_kb) * 1024
        trunc = f"{int(size_kb)}K"
    elif size_mb:
        total_bytes = int(size_mb) * 1024 * 1024
        trunc = f"{int(size_mb)}M"
    else:
        return False, "need size_mb or size_kb"
    if total_bytes < 8 * 1024:
        return False, "size too small"

    path = os.path.abspath(str(path))
    if os.path.exists(path) and not overwrite:
        return False, f"image already exists (pass overwrite=True to replace): {path}"

    # bootable HDD: DOS-stamping path (DOS picks fat_bits)
    if bootable:
        if media != "hdd":
            return False, "bootable is only supported for hdd media"
        if not size_mb:
            return False, "bootable hdd needs size_mb"
        return create_fat_disk_image(path, size_mb, label=label,
                                     make_src_dir=make_src_dir, overwrite=overwrite)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        # empty first: truncate -s keeps old bytes (stale files would match test_filecontains)
        open(path, "wb").close()
        subprocess.check_call(["truncate", "-s", trunc, path])
    except (subprocess.CalledProcessError, OSError) as e:
        return False, f"truncate failed: {e}"

    cluster = _pick_cluster_size(total_bytes, fat_bits)

    if media == "floppy":
        size_k = total_bytes // 1024
        cfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
        cfg.write(f'drive a: file="{path}" offset=0\n')
        cfg.close()
        env = {**os.environ, "MTOOLSRC": cfg.name}
        try:
            cmd = ["mformat"]
            if size_k in _STD_FLOPPY_KB:
                cmd += ["-f", str(size_k)]
            else:
                return False, (f"non-standard floppy size {size_k}K; use one of "
                               f"{sorted(_STD_FLOPPY_KB)} KB")
            if cluster:
                cmd += ["-c", str(cluster)]
            if label:
                cmd += ["-v", label]
            cmd += ["a:"]
            r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if r.returncode != 0:
                return False, "mformat failed:\n" + r.stdout.decode("utf-8", "replace")
            if make_src_dir:
                _mtools_run(["mmd", "a:/src"], env=env)
            actual = _read_fat_type(path, 0)
        finally:
            os.unlink(cfg.name)
        return True, _fmt_disk_msg("floppy", size_k, "K", fat_bits, actual, path)

    # non-bootable hdd: partitioned blank FAT
    ptype = "1" if fat_bits == 12 else ("6" if fat_bits == 16 else "6")
    cfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
    cfg.write(f'drive p: file="{path}" partition=1\n')
    cfg.close()
    env = {**os.environ, "MTOOLSRC": cfg.name}
    log = []
    try:
        steps = [
            ["mpartition", "-I", "p:"],
            ["mpartition", "-c", "-b", "63", "-T", ptype, "p:"],
            ["mformat"] + (["-c", str(cluster)] if cluster else [])
                        + (["-v", label] if label else []) + ["p:"],
            ["mpartition", "-a", "p:"],
        ]
        for cmd in steps:
            r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = r.stdout.decode("utf-8", "replace").strip()
            if out:
                log.append(out)
            if r.returncode != 0:
                return False, f"{' '.join(cmd)} failed:\n" + "\n".join(log)
        if make_src_dir:
            _mtools_run(["mmd", "p:/src"], env=env)
        actual = _read_fat_type(path, 32256)
    finally:
        os.unlink(cfg.name)
    size_m = total_bytes // (1024 * 1024)
    return True, _fmt_disk_msg("hdd", size_m, "MB", fat_bits, actual, path)


def _fmt_disk_msg(media, size, unit, requested, actual, path):
    note = ""
    if requested and actual and str(requested) != actual:
        note = f" (requested FAT{requested}, disk is too {'small' if requested==16 else 'large'} for it)"
    fat = f"FAT{actual}" if actual else "FAT"
    return f"Created {size}{unit} {fat} {media} image at {path}{note}"


def create_fat_disk_image(path, size_mb, label="", make_src_dir=True, overwrite=False,
                          system=True, fat_bits=None):
    """MS-DOS FAT disk image of size_mb MB from scratch -> (success, message).
    fat_bits=None: mformat chooses (FAT12 below ~16MB); 12/16 forces it (cluster size picked, partition type 01 FAT12 | 04 FAT16 <32MB | 06 FAT16 >=32MB).
    system=False: data-only disk (no MBR/IO.SYS/MSDOS.SYS/COMMAND.COM), for a D: source drive; otherwise boot code and system files from DOS_BOOT_ASSETS_DIR. Creates an empty /src by default.
    """
    try:
        size_mb = int(size_mb)
    except (TypeError, ValueError):
        return False, f"invalid size_mb: {size_mb!r}"
    if size_mb < 1:
        return False, "size_mb must be >= 1"

    path = os.path.abspath(str(path))
    if os.path.exists(path) and not overwrite:
        return False, f"image already exists (pass overwrite=True to replace): {path}"

    mbr_bin = os.path.join(DOS_BOOT_ASSETS_DIR, "mbr.bin")
    bootsec_bin = os.path.join(DOS_BOOT_ASSETS_DIR, "bootsec.bin")
    ptype_txt = os.path.join(DOS_BOOT_ASSETS_DIR, "ptype.txt")
    sysfiles_dir = os.path.join(DOS_BOOT_ASSETS_DIR, "sysfiles")
    for required in (mbr_bin, bootsec_bin, ptype_txt, sysfiles_dir):
        if not os.path.exists(required):
            return False, f"missing bundled DOS boot asset: {required}"
    with open(ptype_txt) as f:
        ptype = f.read().strip()
    cluster = None
    if fat_bits is not None:
        if int(fat_bits) not in (12, 16):
            return False, f"fat_bits must be 12 or 16, got {fat_bits!r}"
        fat_bits = int(fat_bits)
        cluster = _pick_cluster_size(size_mb * 1024 * 1024, fat_bits)
        ptype = "1" if fat_bits == 12 else ("4" if size_mb < 32 else "6")

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    OFF = 32256

    try:
        subprocess.check_call(["truncate", "-s", f"{size_mb}M", path])
    except (subprocess.CalledProcessError, OSError) as e:
        return False, f"truncate failed: {e}"

    pcfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
    pcfg.write(f'drive p: file="{path}" partition=1\n')
    pcfg.close()
    penv = {**os.environ, "MTOOLSRC": pcfg.name}
    log = []
    try:
        steps = [
            ["mpartition", "-I", "p:"],
            ["mpartition", "-c", "-b", "63", "-T", ptype, "p:"],
            ["mformat", "-B", bootsec_bin] + (["-c", str(cluster)] if cluster else [])
            + (["-v", label] if label else []) + ["p:"],
        ]
        for cmd in steps:
            r = _mtools_run(cmd, env=penv)
            out = r.stdout.decode("utf-8", "replace").strip()
            if out:
                log.append(out)
            if r.returncode != 0:
                return False, f"{' '.join(cmd)} failed:\n" + "\n".join(log)

        if system:
            subprocess.run(["dd", f"if={mbr_bin}", f"of={path}", "bs=446",
                            "count=1", "conv=notrunc", "status=none"], check=True)
            _mtools_run(["mpartition", "-a", "p:"], env=penv)

        # BPB hidden-sectors = partition LBA (63)
        with open(path, "r+b") as f:
            f.seek(OFF + 0x1C)
            f.write((63).to_bytes(4, "little"))

        # DOS system files: IO.SYS/MSDOS.SYS first (cluster 2)
        dcfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
        dcfg.write(f'drive d: file="{path}" offset={OFF}\n')
        dcfg.close()
        denv = {**os.environ, "MTOOLSRC": dcfg.name}
        try:
            if system:
                io_sys = os.path.join(sysfiles_dir, "IO.SYS")
                msdos_sys = os.path.join(sysfiles_dir, "MSDOS.SYS")
                command_com = os.path.join(sysfiles_dir, "COMMAND.COM")
                _mtools_run(["mcopy", "-n", "-o", io_sys, "d:/IO.SYS"], env=denv)
                _mtools_run(["mcopy", "-n", "-o", msdos_sys, "d:/MSDOS.SYS"], env=denv)
                _mtools_run(["mattrib", "+s", "+h", "+r", "d:/IO.SYS", "d:/MSDOS.SYS"], env=denv)
                _mtools_run(["mcopy", "-n", "-o", command_com, "d:/COMMAND.COM"], env=denv)
            if make_src_dir:
                _mtools_run(["mmd", "d:/src"], env=denv)
        finally:
            os.unlink(dcfg.name)

        kind = "bootable" if system else "data-only"
        return True, f"Created {kind} {size_mb}MB DOS disk image at {path}"
    finally:
        os.unlink(pcfg.name)


def clone_disk_image(src_path, dst_path, size_mb=None, overwrite=False):
    """Clone a disk image to dst_path -> (success, message). size_mb None or equal: exact byte copy; larger: new blank FAT image with files copied (not bootable); smaller: refused."""
    src_path = os.path.abspath(str(src_path))
    dst_path = os.path.abspath(str(dst_path))
    if not os.path.isfile(src_path):
        return False, f"source image not found: {src_path}"
    if os.path.exists(dst_path) and not overwrite:
        return False, f"destination exists (pass overwrite=True to replace): {dst_path}"
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)

    src_mb = int(round(os.path.getsize(src_path) / (1024 * 1024)))

    if size_mb is None or int(size_mb) == src_mb:
        shutil.copyfile(src_path, dst_path)
        return True, f"Cloned exact copy ({src_mb}MB) -> {dst_path}"

    size_mb = int(size_mb)
    if size_mb < src_mb:
        return False, f"target {size_mb}MB < source {src_mb}MB (shrink not supported)"

    # bootable source: rebuild larger, stay bootable (fatresize can't grow FAT12)
    if _has_dos_system(src_path):
        return build_bootable_dos_image(src_path, dst_path, size_mb, overwrite=True)

    # data disk: fresh blank FAT + file copy
    ok, msg = create_fat_disk_image(dst_path, size_mb, make_src_dir=False, overwrite=True)
    if not ok:
        return False, f"could not create target image: {msg}"

    def _cfg(path, drive):
        f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
        f.write(f'drive {drive}: file="{path}" offset=32256\n')
        f.close()
        return f.name

    scfg, dcfg = _cfg(src_path, "s"), _cfg(dst_path, "d")
    try:
        # merge drive configs so mcopy sees s: and d:
        merged = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
        merged.write(open(scfg).read())
        merged.write(open(dcfg).read())
        merged.close()
        env = {**os.environ, "MTOOLSRC": merged.name}
        r = subprocess.run(
            "mcopy -n -o -s s:/* d:/", shell=True, executable="/bin/bash", env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode("utf-8", "replace").strip()
        return True, (f"Resize-cloned {src_mb}MB -> {size_mb}MB (files copied; "
                      f"NOT a bootable copy) -> {dst_path}\n{out}")
    finally:
        for f in (scfg, dcfg, merged.name):
            try:
                os.unlink(f)
            except OSError:
                pass


def _has_dos_system(img_path, offset=32256):
    """True if the image's FAT partition (at byte offset) contains IO.SYS."""
    cfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
    cfg.write(f'drive s: file="{img_path}" offset={offset}\n')
    cfg.close()
    try:
        r = _mtools_run(["mdir", "-a", "s:/IO.SYS"],
                        env={**os.environ, "MTOOLSRC": cfg.name},
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    finally:
        os.unlink(cfg.name)


def build_bootable_dos_image(template_src, dst_path, size_mb, overwrite=False):
    """Bootable MS-DOS hard-disk image of size_mb MB from an existing bootable DOS template (boot sector, MBR, active partition, IO.SYS/MSDOS.SYS first, other files copied) -> (success, message)."""
    template_src = os.path.abspath(str(template_src))
    dst_path = os.path.abspath(str(dst_path))
    OFF = 32256
    if not os.path.isfile(template_src):
        return False, f"template not found: {template_src}"
    if os.path.exists(dst_path) and not overwrite:
        return False, f"destination exists (pass overwrite=True to replace): {dst_path}"
    try:
        size_mb = int(size_mb)
    except (TypeError, ValueError):
        return False, f"invalid size_mb: {size_mb!r}"
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)

    bootsec = dst_path + ".bootsec.tmp"
    subprocess.run(["dd", f"if={template_src}", f"of={bootsec}", "bs=512",
                    "skip=63", "count=1", "status=none"], check=True)
    with open(template_src, "rb") as f:
        f.seek(450)
        ptype = f.read(1)[0]

    subprocess.check_call(["truncate", "-s", f"{size_mb}M", dst_path])
    pcfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
    pcfg.write(f'drive p: file="{dst_path}" partition=1\n')
    pcfg.close()
    penv = {**os.environ, "MTOOLSRC": pcfg.name}
    try:
        _mtools_run(["mpartition", "-I", "p:"], env=penv, check=True)
        _mtools_run(["mpartition", "-c", "-b", "63", "-T", str(ptype), "p:"], env=penv, check=True)
        _mtools_run(["mformat", "-B", bootsec, "p:"], env=penv, check=True)
        subprocess.run(["dd", f"if={template_src}", f"of={dst_path}", "bs=446",
                        "count=1", "conv=notrunc", "status=none"], check=True)
        _mtools_run(["mpartition", "-a", "p:"], env=penv)
        # BPB hidden-sectors = partition LBA (63)
        with open(dst_path, "r+b") as f:
            f.seek(OFF + 0x1C)
            f.write((63).to_bytes(4, "little"))

        scfg = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
        scfg.write(f'drive s: file="{template_src}" offset={OFF}\n')
        scfg.write(f'drive d: file="{dst_path}" offset={OFF}\n')
        scfg.close()
        senv = {**os.environ, "MTOOLSRC": scfg.name}
        try:
            # IO.SYS + MSDOS.SYS first (cluster 2), sys attrs
            _mtools_run(["mcopy", "-n", "s:/IO.SYS", "d:/IO.SYS"], env=senv)
            _mtools_run(["mcopy", "-n", "s:/MSDOS.SYS", "d:/MSDOS.SYS"], env=senv)
            _mtools_run(["mattrib", "+s", "+h", "+r", "d:/IO.SYS", "d:/MSDOS.SYS"], env=senv)
            # other top-level entries
            listing = _mtools_run(["mdir", "-a", "-b", "s:/"], env=senv)
            for line in listing.stdout.decode("utf-8", "replace").splitlines():
                base = os.path.basename(line.strip().rstrip("/"))
                if not base or base.upper() in ("IO.SYS", "MSDOS.SYS"):
                    continue
                _mtools_run(["mcopy", "-n", "-s", f"s:/{base}", "d:/"], env=senv,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            os.unlink(scfg.name)
    finally:
        for f in (bootsec, pcfg.name):
            try:
                os.unlink(f)
            except OSError:
                pass

    src_mb = int(round(os.path.getsize(template_src) / (1024 * 1024)))
    return True, (f"Built bootable {size_mb}MB DOS image from "
                  f"{os.path.basename(template_src)} ({src_mb}MB) -> {dst_path}")


def overlay_backing_file(overlay_path):
    """Backing (template) file recorded in a QCOW2 overlay, or None."""
    try:
        result = subprocess.run(
            ["qemu-img", "info", "--output=json", str(overlay_path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        info = json.loads(result.stdout.decode("utf-8", "replace"))
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None
    backing = info.get("full-backing-filename") or info.get("backing-filename")
    return os.path.abspath(backing) if backing else None


def image_format(image_path):
    """Disk format qemu-img detects ("raw", "qcow2", ...), or None."""
    try:
        result = subprocess.run(
            ["qemu-img", "info", "--output=json", str(image_path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return json.loads(result.stdout.decode("utf-8", errors="replace")).get("format")
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def create_overlay_image(template_path, overlay_path, overwrite=True):
    """QCOW2 copy-on-write overlay backed by a read-only raw template (/testsrc/templates, chmod 444), recreated each run -> (success, msg)."""
    template_path = os.path.abspath(str(template_path))
    overlay_path = os.path.abspath(str(overlay_path))

    if not os.path.isfile(template_path):
        return False, f"[error] Template image not found: {template_path}"
    if os.path.exists(overlay_path):
        if not overwrite:
            return False, f"overlay already exists (pass overwrite=True): {overlay_path}"
        try:
            os.unlink(overlay_path)
        except OSError as e:
            return False, f"could not remove stale overlay {overlay_path}: {e}"

    os.makedirs(os.path.dirname(overlay_path) or ".", exist_ok=True)
    # -F states the template's real format (qcow2 vs raw)
    backing_fmt = image_format(template_path) or "raw"
    try:
        result = subprocess.run(
            ["qemu-img", "create", "-f", "qcow2",
             "-b", template_path, "-F", backing_fmt, overlay_path],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        return True, f"Overlay {overlay_path} backed by {template_path}\n{output}"
    except subprocess.CalledProcessError as e:
        output = e.stdout.decode("utf-8", errors="replace") if e.stdout else ''
        return False, output


def convert_raw_to_qcow2(hdd_img_path, qcow2_output):
    if not os.path.isfile(hdd_img_path):
        return False, f"[error] Raw image not found: {hdd_img_path}"

    if qcow2_output is None:
        qcow2_output = os.path.splitext(qcow2_output)[0] + ".qcow2"

    try:
        result = subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", hdd_img_path, qcow2_output],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        return True, output
    except subprocess.CalledProcessError as e:
        output = e.stdout.decode("utf-8", errors="replace") if e.stdout else ''
        return False, output


def convert_qcow2_to_raw(qcow2_path, raw_output):
    """Flatten a QCOW2 overlay and its backing chain into one raw image (for DOSBox-X, 86Box) -> (success, msg)."""
    if not os.path.isfile(qcow2_path):
        return False, f"[error] QCOW2 image not found: {qcow2_path}"

    os.makedirs(os.path.dirname(os.path.abspath(raw_output)) or ".", exist_ok=True)
    # remove, not overwrite: no stale tail
    if os.path.exists(raw_output):
        try:
            os.unlink(raw_output)
        except OSError as e:
            return False, f"could not remove stale raw image {raw_output}: {e}"

    try:
        result = subprocess.run(
            ["qemu-img", "convert", "-O", "raw", qcow2_path, raw_output],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        return True, f"Flattened {qcow2_path} -> {raw_output}\n{output}".rstrip()
    except subprocess.CalledProcessError as e:
        output = e.stdout.decode("utf-8", errors="replace") if e.stdout else ''
        return False, f"qemu-img convert failed: {output}"


def copy_to_fat_image(src_dir, hdd_img_path, dest_dir="src", offset=32256):
    """offset: 32256 for a partitioned HDD image, 0 for an unpartitioned floppy."""
    log = []
    dest_dir = str(dest_dir).strip("/")
    mtools_config = f'drive h: file="{hdd_img_path}" offset={int(offset)}\n'
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(mtools_config)
        config_path = tmp.name

    try:
        # start_new_session: mmd gets no controlling terminal
        t0 = time.time()
        try:
            r = subprocess.run(
                f'MTOOLSRC={config_path} mmd h:/{dest_dir}',
                shell=True,
                executable="/bin/bash",
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            note = " (already exists — expected on a reused image)" if r.returncode else ""
            log.append(f"mmd h:/{dest_dir}: rc={r.returncode} in {time.time() - t0:.2f}s{note}")
        except subprocess.TimeoutExpired:
            log.append(f"mmd h:/{dest_dir} TIMED OUT after {time.time() - t0:.2f}s "
                       f"(30s backstop) — continuing; mcopy -o does not need it")
        t1 = time.time()
        try:
            result = subprocess.run(
                f'MTOOLSRC={config_path} mcopy -n -o -s {src_dir}/* h:/{dest_dir}/',
                shell=True,
                check=True,
                executable="/bin/bash",
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
            )
            output = result.stdout.decode('utf-8', errors='replace')
            log.append(f"mcopy {src_dir}/* -> h:/{dest_dir}/ in {time.time() - t1:.2f}s")
            if output.strip():
                log.append(output)
            return True, "\n".join(log)
        except subprocess.CalledProcessError as e:
            output = e.stdout.decode('utf-8', errors='replace') if e.stdout else ''
            log.append(f"mcopy FAILED (rc={e.returncode}) after {time.time() - t1:.2f}s")
            log.append(output)
            return False, "\n".join(log)
        except subprocess.TimeoutExpired:
            log.append(f"mcopy into h:/{dest_dir} timed out after 300s")
            return False, "\n".join(log)
    finally:
        os.unlink(config_path)


def copy_from_fat_image(src_dos_dir, dst_dir, image_path):
    log = []
    os.makedirs(dst_dir, exist_ok=True)
    mtools_config = f'drive h: file="{image_path}" offset=32256\n'
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(mtools_config)
        config_path = tmp.name

    try:
        try:
            result = subprocess.run(
                f'MTOOLSRC={config_path} mcopy -n -o -s h:/{src_dos_dir} {dst_dir}/',
                shell=True,
                check=True,
                executable="/bin/bash",
                stdin=subprocess.DEVNULL,
                # avoid an overwrite prompt hang
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
            )
            output = result.stdout.decode('utf-8', errors='replace')
            log.append(output)           
            return True, output
        except subprocess.CalledProcessError as e:
            output = e.stdout.decode('utf-8', errors='replace') if e.stdout else ''
            log.append(output)
            return False, output
        except subprocess.TimeoutExpired:
            msg = f"mcopy out of h:/{src_dos_dir} timed out after 300s"
            log.append(msg)
            return False, msg
    finally:
        os.unlink(config_path)


def find_button_in_screenshot(button_path, screenshot_path):
    full_img = Image.open(screenshot_path).convert("L")
    button_img = Image.open(button_path).convert("L")

    full_converted_path = os.path.splitext(screenshot_path)[0] + "_converted.png"
    button_converted_path = os.path.splitext(button_path)[0] + "_converted.png"
    full_img.save(full_converted_path)
    button_img.save(button_converted_path)

    full_arr = np.array(full_img, dtype=np.uint8)
    button_arr = np.array(button_img, dtype=np.uint8)

    fh, fw = full_arr.shape
    bh, bw = button_arr.shape

    for y in range(fh - bh + 1):
        for x in range(fw - bw + 1):
            patch = full_arr[y:y+bh, x:x+bw]
            if (patch == button_arr).all():
                # centre of the match = clickable hotspot
                return True, (x + bw // 2, y + bh // 2)

    return False, "Button not found"


def find_icon_in_screenshot(icon_path, screenshot_path, tolerance=0.12,
                            pixel_delta=48, mask_path=None, region=None):
    """Tolerant template match for Finder icons (dithered backgrounds). Accepts the best position when the fraction of template pixels differing by more than `pixel_delta` is <= `tolerance`.
      tolerance   max fraction of (unmasked) pixels allowed to mismatch
      pixel_delta per-pixel grayscale difference that counts as "different"
      mask_path   optional PNG; black (<128) pixels ignored
      region      optional (x0, y0, x1, y1) search box in frame pixels
    Returns (True, (cx, cy)) at the glyph centre, or (False, reason).
    """
    from numpy.lib.stride_tricks import sliding_window_view

    full = np.array(Image.open(screenshot_path).convert("L"), dtype=np.int16)
    tmpl = np.array(Image.open(icon_path).convert("L"), dtype=np.int16)
    fh, fw = full.shape
    bh, bw = tmpl.shape

    if mask_path:
        m = np.array(Image.open(mask_path).convert("L"))
        mask = (m >= 128)
    else:
        mask = np.ones((bh, bw), dtype=bool)
    n_cmp = int(mask.sum())
    if n_cmp == 0:
        return False, "icon mask is empty (nothing to compare)"

    # clamp search to an ROI
    x0, y0, x1, y1 = (0, 0, fw, fh) if region is None else region
    x0 = max(0, int(x0)); y0 = max(0, int(y0))
    x1 = min(fw, int(x1)); y1 = min(fh, int(y1))
    roi = full[y0:y1, x0:x1]
    if roi.shape[0] < bh or roi.shape[1] < bw:
        return False, "search region smaller than icon template"

    windows = sliding_window_view(roi, (bh, bw))
    diff = np.abs(windows - tmpl[None, None, :, :])
    bad = (diff > pixel_delta) & mask[None, None, :, :]
    frac = bad.reshape(bad.shape[0], bad.shape[1], -1).sum(axis=2) / n_cmp

    idx = int(np.argmin(frac))
    yy, xx = np.unravel_index(idx, frac.shape)
    best = float(frac[yy, xx])
    if best <= tolerance:
        cx = x0 + int(xx) + bw // 2
        cy = y0 + int(yy) + bh // 2
        return True, (cx, cy)
    return False, f"icon not found (best mismatch {best:.2f} > tol {tolerance})"


def ppdcompile(sock):
    log_dir = "./compile_logs"
    os.makedirs(log_dir, exist_ok=True)

    start_time = time.time()
    send_monitor_key(sock, "f3")
    time.sleep(1)
    send_monitor_key(sock, "ret")
    time.sleep(1)
    send_monitor_key(sock, "F")
    time.sleep(1)
    send_monitor_key(sock, "ret")
    time.sleep(1)
    send_monitor_key(sock, "ret")
    time.sleep(1)
    send_monitor_key(sock, "ret")
    time.sleep(1)

    return False

# live QEMU control
"""Live QEMU control — see what's running and swap removable media on it.

Finds running qemu-system-* processes by scanning /proc, and drives their
HMP/QMP monitor to attach/detach media while the guest keeps running.

Scope is removable media only (floppy + cdrom): a hot-added hard disk isn't
noticed by a DOS/Win9x guest until reboot, so it isn't offered.

Two HMP quirks this depends on:
  - the monitor echoes each keystroke with no newline until the command runs,
    so everything before the first newline of a reply is echo, not output.
  - `change` splits args on whitespace, so a path must be double-quoted.
"""
_TESTSRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCEDIR = os.path.join(_TESTSRC_ROOT, "sourcedir")
IMAGES_ROOT = os.path.join(_TESTSRC_ROOT, "images")

# media only from the project tree or media library
ALLOWED_ROOTS = (SOURCEDIR, IMAGES_ROOT)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\[K|\r")
_MONITOR_RE = re.compile(r"tcp:(?:127\.0\.0\.1|localhost):(\d+)")

# 2.88MB largest standard floppy (as mediahelpers)
_MAX_FLOPPY_BYTES = 2949120
_ISO_EXTS = (".iso", ".cdr")


def media_kind_for_path(path):
    """Classify a media file as "iso", "floppy", or "hdd", by extension and size."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext in _ISO_EXTS:
        return "iso"
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return "floppy" if size and size <= _MAX_FLOPPY_BYTES else "hdd"


def _slot_kind(device):
    """Classify a drive slot by device name: "floppy" | "cdrom" | "disk" | "other"."""
    d = device.lower()
    if "floppy" in d or re.search(r"(^|[^a-z])fd\d?", d):
        return "floppy"
    if "cd" in d or "optical" in d or re.search(r"(^|[^a-z])sr\d", d):
        return "cdrom"
    if "hd" in d or "sd" in d or "virtio" in d or "scsi" in d:
        return "disk"
    return "other"


# media kinds per slot type
_SLOT_ACCEPTS = {
    "floppy": ["floppy"],
    "cdrom":  ["iso"],
    "disk":   [],
    "other":  ["floppy", "iso"],
}


def _drive_label(device, slot_kind):
    """A human label for a slot (e.g. "A:" instead of "floppy0"), or "" if none applies."""
    m = re.search(r"(\d+)$", device)
    n = int(m.group(1)) if m else 0
    if slot_kind == "floppy":
        return "A:" if n == 0 else ("B:" if n == 1 else "")
    if slot_kind == "cdrom":
        return f"CD {n + 1}"
    return ""


# QMP: HMP monitor is owned by the runner's QemuInstance

class _QMP:
    def __init__(self, port, timeout=3.0):
        self.port = int(port)
        self.timeout = timeout
        self.sock = None
        self.fh = None

    def __enter__(self):
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.fh = self.sock.makefile("rwb")
        greeting = self._read()
        if not greeting or "QMP" not in greeting:
            raise OSError(f"port {self.port} is not a QMP monitor")
        # capabilities negotiation (required first)
        r = self.execute("qmp_capabilities")
        if "error" in r:
            raise OSError(f"qmp_capabilities failed: {r['error'].get('desc')}")
        return self

    def __exit__(self, *exc):
        for c in (self.fh, self.sock):
            try:
                if c:
                    c.close()
            except OSError:
                pass
        return False

    def _read(self):
        line = self.fh.readline()
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return None

    def execute(self, cmd, arguments=None):
        msg = {"execute": cmd}
        if arguments:
            msg["arguments"] = arguments
        self.fh.write((json.dumps(msg) + "\n").encode())
        self.fh.flush()
        # skip async events until this command's return/error
        deadline = time.time() + self.timeout + 2
        while time.time() < deadline:
            m = self._read()
            if m is None:
                break
            if "event" in m:
                continue
            if "return" in m or "error" in m:
                return m
        return {"error": {"desc": f"no reply to '{cmd}'"}}
# parses: "floppy0 (#block186): /path/img (raw)" | "ide1-cd0: [not inserted]"
_BLOCK_RE = re.compile(r"^(\S+?)(?: \(#block\d+\))?: (.*)$")


def _read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _proc_start_epoch(pid):
    """Process start time (wall clock) from /proc/<pid>/stat field 22 and /proc/uptime; None if unreadable."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # index past comm's closing paren (may contain spaces/parens)
        fields = data[data.rindex(")") + 2:].split()
        starttime_ticks = int(fields[19])
        hz = os.sysconf("SC_CLK_TCK") or 100
        with open("/proc/uptime") as f:
            sys_uptime = float(f.read().split()[0])
        return time.time() - (sys_uptime - starttime_ticks / hz)
    except (OSError, ValueError, IndexError):
        return None


def list_instances():
    """Every running qemu-system-* process, with its monitor port. Returns a list of dicts."""
    out = []
    for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):
        argv = _read_cmdline(pid)
        if not argv:
            continue
        binary = os.path.basename(argv[0].decode("utf-8", "replace"))
        if not binary.startswith("qemu-system"):
            continue
        args = [a.decode("utf-8", "replace") for a in argv]
        joined = " ".join(args)

        def _port_after(flag):
            if flag not in args:
                return None
            i = args.index(flag) + 1
            m = _MONITOR_RE.search(args[i]) if i < len(args) else None
            return int(m.group(1)) if m else None

        port = _port_after("-monitor")
        qmp_port = _port_after("-qmp")
        vnc = None
        if "-vnc" in args:
            v = args[args.index("-vnc") + 1] if args.index("-vnc") + 1 < len(args) else ""
            mm = re.match(r":(\d+)", v)
            if mm:
                vnc = 5900 + int(mm.group(1))

        # label by project, else arch
        label = None
        for a in args:
            m = re.search(re.escape(SOURCEDIR) + r"/([^/]+)/", a)
            if m:
                label = m.group(1)
                break
        started = _proc_start_epoch(pid)
        out.append({
            "pid": int(pid),
            "binary": binary,
            "arch": binary.replace("qemu-system-", ""),
            "monitor_port": port,
            "qmp_port": qmp_port,
            "vnc_port": vnc,
            "label": label or binary.replace("qemu-system-", ""),
            "started": started,
            "uptime_s": int(time.time() - started) if started else None,
            # HMP-only VM is controllable only if its monitor connection is free
            "channel": "qmp" if qmp_port else ("hmp" if port else None),
            "controllable": bool(qmp_port or port),
            "cmdline": joined,
        })
    return out


def _find(pid):
    for i in list_instances():
        if i["pid"] == int(pid):
            return i
    return None


def _hmp_is_error(text):
    """True if an HMP reply is a failure (reported as text, not a status code)."""
    t = (text or "").strip().lower()
    return t.startswith("error") or t.startswith("invalid") or "not found" in t


def _hmp_via_qmp(qmp_port, cmd):
    """HMP command via QMP `human-monitor-command` (for runner-launched VMs whose HMP socket the runner holds) -> (ok, text)."""
    try:
        with _QMP(qmp_port) as q:
            r = q.execute("human-monitor-command", {"command-line": cmd})
    except OSError as e:
        return False, f"cannot reach QMP on port {qmp_port}: {e}"
    if "error" in r:
        return False, r["error"].get("desc", "human-monitor-command failed")
    text = (r.get("return") or "").strip()
    return (not _hmp_is_error(text)), text


def _hmp(port, cmd, timeout=3.0):
    """One HMP command directly on the monitor socket -> (ok, reply). Drops the keystroke echo; requires QEMU's greeting banner first (single client). Prefer _hmp_via_qmp where a QMP port exists."""
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
    except (OSError, ValueError) as e:
        return False, f"cannot reach QEMU monitor on port {port}: {e}"
    try:
        s.settimeout(1.2)
        time.sleep(0.25)
        banner = b""
        try:
            banner = s.recv(65536)
        except socket.timeout:
            pass
        if not banner:
            return False, (f"QEMU monitor on port {port} accepted the connection but "
                           f"never answered — its single monitor client slot is already "
                           f"taken (the test runner holds it while a VM it started is "
                           f"running). Use the QMP channel instead.")
        s.sendall((cmd + "\n").encode())
        time.sleep(0.7)
        buf = b""
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                buf += d
        except socket.timeout:
            pass
    finally:
        try:
            s.close()
        except OSError:
            pass

    text = _ANSI.sub("", buf.decode("utf-8", "replace"))
    text = text.split("\n", 1)[1] if "\n" in text else ""
    lines = [l for l in text.splitlines()
             if l.strip() and not l.strip().startswith("(qemu)")]
    reply = "\n".join(lines).strip()
    if reply.startswith("Error:") or reply.startswith("Error "):
        return False, reply
    return True, reply


def _qmp_blocks(qmp_port):
    """Drive slots via QMP query-block — structured, so no text parsing."""
    try:
        with _QMP(qmp_port) as q:
            r = q.execute("query-block")
    except OSError as e:
        return False, f"cannot reach QMP on port {qmp_port}: {e}"
    if "error" in r:
        return False, r["error"].get("desc", "query-block failed")
    devices = []
    for d in r.get("return", []):
        ins = d.get("inserted") or {}
        dev = d.get("device") or d.get("qdev") or ""
        sk = _slot_kind(dev)
        devices.append({
            "device": dev,
            "inserted": bool(ins),
            "path": ins.get("file"),
            "format": ins.get("drv"),
            "readonly": bool(ins.get("ro")),
            "removable": bool(d.get("removable")),
            "slot_kind": sk,
            "accepts": _SLOT_ACCEPTS.get(sk, []),
            "drive_label": _drive_label(dev, sk),
        })
    return True, devices


def list_blocks(pid=None, port=None, qmp_port=None):
    """Drive slots of a running guest (QMP preferred); a pid or explicit ports; removable slots marked -> (ok, list|msg)."""
    if pid is not None:
        inst = _find(pid)
        if not inst:
            return False, f"no running QEMU with pid {pid}"
        qmp_port, port = inst["qmp_port"], inst["monitor_port"]
    if qmp_port:
        return _qmp_blocks(qmp_port)
    if not port:
        return False, "instance has no monitor to talk to"
    ok, out = _hmp(port, "info block")
    if not ok:
        return False, out
    devices, cur = [], None
    for line in out.splitlines():
        if not line.startswith(" ") and ":" in line:
            m = _BLOCK_RE.match(line.strip())
            if not m:
                continue
            dev, rest = m.group(1), m.group(2).strip()
            inserted = not rest.startswith("[not inserted]")
            path, fmt = None, None
            if inserted:
                fm = re.match(r"^(.*?)\s+\(([^)]*)\)$", rest)
                if fm:
                    path, fmt = fm.group(1), fm.group(2)
                else:
                    path = rest
            sk = _slot_kind(dev)
            cur = {"device": dev, "inserted": inserted, "path": path, "format": fmt,
                   "readonly": bool(fmt and "read-only" in fmt), "removable": False,
                   "slot_kind": sk, "accepts": _SLOT_ACCEPTS.get(sk, []),
                   "drive_label": _drive_label(dev, sk)}
            devices.append(cur)
        elif cur is not None and line.strip().startswith("Removable device:"):
            cur["removable"] = True
    return True, devices


def _check_path(path):
    """Media must live in the project tree or the media library."""
    ap = os.path.abspath(str(path))
    if not any(ap == r or ap.startswith(r + "/") for r in ALLOWED_ROOTS):
        return None, f"path must be under {SOURCEDIR}/ or {IMAGES_ROOT}/"
    if not os.path.isfile(ap):
        return None, f"file not found: {ap}"
    return ap, None


def _media_image_format(path):
    """QCOW2 images must be declared as such; everything else here is raw."""
    try:
        with open(path, "rb") as f:
            return "qcow2" if f.read(4) == b"QFI\xfb" else "raw"
    except OSError:
        return "raw"


def attach_media(pid, device, path, read_only=None):
    """Insert an image into a running guest's removable drive -> (ok, message). read_only: None auto-detects from permissions; True/False forces (False fails on an unwritable file). QMP blockdev-change-medium, else HMP `change`."""
    ap, err = _check_path(path)
    if err:
        return False, err
    device = str(device).strip()
    if not device or '"' in device:
        return False, "invalid device name"
    inst = _find(pid)
    if not inst:
        return False, f"no running QEMU with pid {pid}"

    ok, blocks = list_blocks(pid=pid)
    if not ok:
        return False, blocks
    match = next((b for b in blocks if b["device"] == device), None)
    if not match:
        return False, (f"no drive '{device}' on this instance "
                       f"(have: {', '.join(b['device'] for b in blocks) or 'none'})")
    if not match["removable"]:
        return False, (f"'{device}' is not removable — a guest won't see a hot-swapped "
                       f"hard disk without a reboot. Use a floppy or cdrom slot.")

    # iso -> cdrom, floppy .img -> floppy drive
    mk = media_kind_for_path(ap)
    accepts = match.get("accepts") or ["floppy", "iso"]
    if mk not in accepts:
        want = " or ".join(accepts) if accepts else "no removable media"
        slot = match.get("slot_kind", "this")
        return False, (f"{os.path.basename(ap)} is {mk} media but the {slot} slot "
                       f"'{device}' takes {want}. Put an ISO in a cdrom and a floppy "
                       f"image in a floppy drive.")

    # unwritable floppy image: mount read-only
    writable = os.access(ap, os.W_OK)
    if read_only is None:
        read_only = not writable
    elif read_only is False and not writable:
        return False, (
            f"{os.path.basename(ap)} can't be mounted read-write: it isn't writable "
            f"by the runner (owned by another user, mode "
            f"{oct(os.stat(ap).st_mode)[-3:]}). Make it writable on the host — "
            f"chown it to uid {os.getuid()} or chmod a+w — then attach again.")
    fmt = _media_image_format(ap)
    # state read-only-mode explicitly (default "retain" sticks)
    mode = "read-only" if read_only else "read-write"
    if inst["qmp_port"]:
        try:
            with _QMP(inst["qmp_port"]) as q:
                r = q.execute("blockdev-change-medium",
                              {"device": device, "filename": ap, "format": fmt,
                               "read-only-mode": mode})
        except OSError as e:
            return False, f"cannot reach QMP: {e}"
        if "error" in r:
            return False, f"attach failed: {r['error'].get('desc')}"
    else:
        ok, reply = _hmp(inst["monitor_port"], f'change {device} "{ap}" {fmt} {mode}')
        if not ok:
            return False, f"attach failed: {reply}"
    note = " (read-only — not writable by qemu)" if read_only and match["slot_kind"] != "cdrom" else ""
    return True, f"Attached {os.path.basename(ap)} → {device}{note}"


def detach_media(pid, device, force=False):
    """Eject a removable drive (not forced by default) -> (ok, message)."""
    device = str(device).strip()
    if not device or '"' in device:
        return False, "invalid device name"
    inst = _find(pid)
    if not inst:
        return False, f"no running QEMU with pid {pid}"

    if inst["qmp_port"]:
        try:
            with _QMP(inst["qmp_port"]) as q:
                r = q.execute("eject", {"device": device, "force": bool(force)})
        except OSError as e:
            return False, f"cannot reach QMP: {e}"
        if "error" in r:
            desc = r["error"].get("desc", "")
            hint = ("" if force or "locked" not in desc.lower() else
                    " — the guest has the tray locked (it may be mid-write); "
                    "retry with force to override")
            return False, f"detach failed: {desc}{hint}"
    else:
        ok, reply = _hmp(inst["monitor_port"], f"eject {'-f ' if force else ''}{device}")
        if not ok:
            hint = ("" if force or "locked" not in reply.lower() else
                    " — the guest has the tray locked (it may be mid-write); "
                    "retry with force to override")
            return False, f"detach failed: {reply}{hint}"
    return True, f"Ejected {device}"


# boot_set is HMP-only
_BOOT_LETTER = {"floppy": "a", "disk": "c", "cdrom": "d"}


def set_boot_priority(pid, prefer_floppy):
    """Reorder the BIOS boot device list of a running instance: prefer_floppy=True floppy first, False hard-disk first. Takes effect on next boot/reset (reset_instance) -> (ok, message)."""
    inst = _find(pid)
    if not inst:
        return False, f"no running QEMU with pid {pid}"
    if not (inst.get("qmp_port") or inst.get("monitor_port")):
        return False, "instance has no monitor — boot_set needs one (relaunch to get it)"
    ok, blocks = list_blocks(pid=pid)
    if not ok:
        return False, blocks
    present = {b["slot_kind"] for b in blocks if b["slot_kind"] in _BOOT_LETTER}
    if not present:
        return False, "this instance has no floppy/disk/cdrom slots to order"
    order = (["floppy", "disk", "cdrom"] if prefer_floppy else ["disk", "floppy", "cdrom"])
    letters = "".join(_BOOT_LETTER[k] for k in order if k in present)

    # prefer QMP (HMP socket owned by the runner)
    if inst.get("qmp_port"):
        ok, reply = _hmp_via_qmp(inst["qmp_port"], f"boot_set {letters}")
    else:
        ok, reply = _hmp(inst["monitor_port"], f"boot_set {letters}")
    if not ok:
        return False, f"boot_set failed: {reply}"
    return True, (f"Boot order set to {letters} "
                  f"({'floppy first' if prefer_floppy else 'hard disk first'}) — "
                  f"takes effect on the next reset.")


def reset_instance(pid):
    """Reboot a running guest (destructive): QMP `system_reset`, else HMP -> (ok, message)."""
    inst = _find(pid)
    if not inst:
        return False, f"no running QEMU with pid {pid}"
    if inst["qmp_port"]:
        try:
            with _QMP(inst["qmp_port"]) as q:
                r = q.execute("system_reset")
        except OSError as e:
            return False, f"cannot reach QMP: {e}"
        if "error" in r:
            return False, f"reset failed: {r['error'].get('desc')}"
    elif inst["monitor_port"]:
        ok, reply = _hmp(inst["monitor_port"], "system_reset")
        if not ok:
            return False, f"reset failed: {reply}"
    else:
        return False, "instance has no monitor to reset it through"
    return True, "Guest reset."
