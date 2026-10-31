#!/usr/bin/env python3
"""Driving and watching a Verilator VGA testbench:
  - VerilatorLiveInstance
  - VgaFbLive: reader of the testbench live view sink. Little-endian uint32 header, then payload:
        0   MAGIC    "VLFB" = 0x42464C56
        1   VERSION  1
        2   FORMAT   1 = FMT_RGB1, 1 byte/pixel: bit2=R, bit1=G, bit0=B
                 2 = FMT_RGB565, 2 bytes/pixel little-endian: R[15:11] G[10:5] B[4:0]
        3   WIDTH    pixels
        4   HEIGHT   pixels
        5   STRIDE   bytes per row (== WIDTH for FMT_RGB1)
        6   BYTES    payload bytes in use
        7   SEQ      seqlock, odd while publish() is mid-write
        8   FRAME    frames presented
        9-15         reserved, zero
  - Monitor / main(): pygame window on the sink, beside a testbench started with --live:
        python3 /testsrc/pyhelpers/verilatorvgahelpers.py --live /dev/shm/vgafb
    Keys:  ESC / Q  quit        1-8  integer zoom        F  fit to window
  - VerilatorFbMonitorProcess: runs main() as a child process.
"""

import argparse
import mmap
import os
import struct
import subprocess
import sys
import time

HELPERDIR = os.path.dirname(os.path.abspath(__file__))
MONITOR_PY = os.path.abspath(__file__)

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

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


class VerilatorLiveInstance(object):
    def __init__(self, name, binary=None, live_path=None, cwd=None,
                 seconds=0, stats_path=None, extra_args=None):
        self.name = name
        self.binary = binary
        # --live sink: testbench publishes a frame to it per completed frame
        self.live_path = live_path
        self.cwd = cwd
        # 0 = run until stop()
        self.seconds = seconds
        # --stats sink: testbench (vstats.hpp) publishes a JSON snapshot there; see verilatorhelpers.StatsReader
        self.stats_path = stats_path
        self.extra_args = list(extra_args or [])
        self.process = None
        self.pid = None

    def _argv(self):
        argv = [self.binary]
        if self.live_path:
            argv += ["--live", self.live_path]
        if self.seconds:
            argv += ["--seconds", str(self.seconds)]
        if self.stats_path:
            argv += ["--stats", self.stats_path]
        return argv + self.extra_args

    def stats(self):
        """Latest stats snapshot dict, or None (no --stats sink, or not written yet)."""
        if not self.stats_path:
            return None
        from verilatorhelpers import StatsReader
        return StatsReader(self.stats_path).read()

    def start(self):
        if not self.binary or not os.path.exists(self.binary):
            return False, f"verilator binary not found: {self.binary}"
        try:
            self.process = subprocess.Popen(
                self._argv(),
                cwd=self.cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except Exception as exc:
            return False, f"Could not launch {self.binary}: {exc}"
        self.pid = self.process.pid
        processreg.register(self.name, self.process, source="verilator_live")
        time.sleep(0.5)
        if self.process.poll() is not None:
            err = ""
            try:
                err = (self.process.stderr.read() or b"").decode(errors="replace")
            except Exception:
                pass
            return False, f"{self.binary} exited immediately\n{err.strip()[-600:]}"

        return True, f"{self.name} (pid {self.pid}) running {self.binary} --live {self.live_path}"

    def is_alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        processreg.unregister(self.name)
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        self.process = None


MAGIC = 0x42464C56
VERSION = 1
HDR_BYTES = 64
HDR_WORDS = 16
MAX_PAYLOAD = 1024 * 1024 * 4
FILE_BYTES = HDR_BYTES + MAX_PAYLOAD

FMT_RGB1 = 1
FMT_RGB565 = 2

DEFAULT_LIVE_PATH = "/dev/shm/vgafb"

(H_MAGIC, H_VERSION, H_FORMAT, H_WIDTH, H_HEIGHT,
 H_STRIDE, H_BYTES, H_SEQ, H_FRAME) = range(9)

_HDR = struct.Struct("<%dI" % HDR_WORDS)


class Frame(object):
    __slots__ = ("width", "height", "stride", "fmt", "frame", "pixels")

    def __init__(self, width, height, stride, fmt, frame, pixels):
        self.width = width
        self.height = height
        self.stride = stride
        self.fmt = fmt
        self.frame = frame
        self.pixels = pixels

    @property
    def size(self):
        return (self.width, self.height)

    def __repr__(self):
        return "<Frame %d %dx%d fmt=%d>" % (
            self.frame, self.width, self.height, self.fmt)


class VgaFbError(Exception):
    pass


class VgaFbLive(object):
    """mmap reader for one live_sink.hpp sink (same shape as NovaFbLive, novafbhelpers.py)."""

    def __init__(self, path=DEFAULT_LIVE_PATH):
        self.path = path
        self._fd = None
        self._map = None
        self._last_seq = None
        self._last_frame = None
        self._last_geom = None

    def open(self):
        if self._map is not None:
            return self
        if not os.path.exists(self.path):
            raise VgaFbError("no live sink at %s -- was the testbench run "
                             "with --live %s?" % (self.path, self.path))
        size = os.path.getsize(self.path)
        if size < HDR_BYTES:
            raise VgaFbError("%s is too small to be a live sink (%d bytes)"
                             % (self.path, size))
        fd = os.open(self.path, os.O_RDONLY)
        try:
            mp = mmap.mmap(fd, min(size, FILE_BYTES), prot=mmap.PROT_READ)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._map = mp
        hdr = self._header()
        if hdr[H_MAGIC] != MAGIC:
            self.close()
            raise VgaFbError("%s is not a verilator_vga live sink (magic %#x)"
                             % (self.path, hdr[H_MAGIC]))
        if hdr[H_VERSION] != VERSION:
            self.close()
            raise VgaFbError("%s speaks live protocol v%d, this reader "
                             "speaks v%d" % (self.path, hdr[H_VERSION], VERSION))
        return self

    def wait_open(self, timeout=30.0, poll=0.1):
        deadline = time.time() + float(timeout)
        last = None
        while time.time() < deadline:
            try:
                return self.open()
            except VgaFbError as exc:
                last = exc
                time.sleep(poll)
        raise VgaFbError("timed out after %gs waiting for %s (%s)"
                         % (timeout, self.path, last))

    def close(self):
        if self._map is not None:
            self._map.close()
            self._map = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _header(self):
        return _HDR.unpack(self._map[:HDR_BYTES])

    def read(self, retries=8, only_new=True):
        if self._map is None:
            raise VgaFbError("read() before open()")

        for _ in range(max(1, int(retries))):
            hdr = self._header()
            seq = hdr[H_SEQ]
            if seq & 1:
                continue
            fmt = hdr[H_FORMAT]
            w, h = hdr[H_WIDTH], hdr[H_HEIGHT]
            stride, nbytes = hdr[H_STRIDE], hdr[H_BYTES]
            frame = hdr[H_FRAME]

            if (only_new and seq == self._last_seq and frame == self._last_frame
                    and (w, h, fmt) == self._last_geom):
                return None
            if not (0 < w <= 1024 and 0 < h <= 1024):
                continue
            if not (0 < nbytes <= MAX_PAYLOAD):
                continue

            pixels = self._map[HDR_BYTES:HDR_BYTES + nbytes]

            if self._header()[H_SEQ] != seq:
                continue

            self._last_seq = seq
            self._last_frame = frame
            self._last_geom = (w, h, fmt)
            return Frame(w, h, stride, fmt, frame, pixels)
        return None

    def geometry(self):
        hdr = self._header()
        return (hdr[H_WIDTH], hdr[H_HEIGHT])

    def frame_number(self):
        return self._header()[H_FRAME]


def rgb1_to_rgb(frame):
    """FMT_RGB1 Frame (1 byte/pixel, bit2=R bit1=G bit0=B) -> packed RGB888 bytes."""
    if frame.fmt != FMT_RGB1:
        raise VgaFbError("rgb1_to_rgb: frame format %d is not RGB1" % frame.fmt)

    w, h, stride = frame.width, frame.height, frame.stride
    try:
        import numpy as np
        raw = np.frombuffer(frame.pixels[:stride * h], dtype=np.uint8)
        raw = raw.reshape(h, stride)[:, :w]
        r = ((raw >> 2) & 1).astype(np.uint8) * 255
        g = ((raw >> 1) & 1).astype(np.uint8) * 255
        b = (raw & 1).astype(np.uint8) * 255
        return np.stack([r, g, b], axis=-1).tobytes()
    except ImportError:
        lut = [bytes(((v >> 2 & 1) * 255, (v >> 1 & 1) * 255, (v & 1) * 255))
              for v in range(256)]
        out = bytearray()
        for row in range(h):
            base = row * stride
            line = frame.pixels[base:base + w]
            for byte in line:
                out += lut[byte]
        return bytes(out)


def rgb565_to_rgb(frame):
    """FMT_RGB565 Frame (little-endian 16-bit pixels) -> packed RGB888 bytes."""
    w, h, stride = frame.width, frame.height, frame.stride
    try:
        import numpy as np
        raw = np.frombuffer(frame.pixels[:stride * h], dtype="<u2")
        raw = raw.reshape(h, stride // 2)[:, :w].astype(np.uint16)
        r = ((raw >> 11) & 31).astype(np.uint8)
        g = ((raw >> 5) & 63).astype(np.uint8)
        b = (raw & 31).astype(np.uint8)
        return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)],
                        axis=-1).tobytes()
    except ImportError:
        out = bytearray()
        for row in range(h):
            base = row * stride
            for i in range(w):
                p = frame.pixels[base + 2 * i] | (frame.pixels[base + 2 * i + 1] << 8)
                r, g, b = (p >> 11) & 31, (p >> 5) & 63, p & 31
                out += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
        return bytes(out)


def frame_to_rgb(frame):
    if frame.fmt == FMT_RGB1:
        return rgb1_to_rgb(frame)
    if frame.fmt == FMT_RGB565:
        return rgb565_to_rgb(frame)
    raise VgaFbError(
        "unknown live frame format %d -- this reader understands RGB1 and RGB565"
        % frame.fmt)


DEFAULT_WINDOW = (640, 480)
MAX_AUTO_SCALE = 4


def _resolve_display(explicit=None):
    try:
        from displayhelpers import resolve_display
        return resolve_display(explicit)
    except Exception:
        return explicit or os.environ.get("DISPLAY") or ":20"


class Monitor(object):
    def __init__(self, live_path=DEFAULT_LIVE_PATH, scale=0, fps=60,
                 title=None, wait=30.0, exit_when_gone=True,
                 window=None):
        self.live = VgaFbLive(live_path)
        self.scale = max(0, int(scale))
        self.fps = max(1, int(fps))
        self.title = title or "Verilator VGA - %s" % os.path.basename(live_path)
        self.wait = float(wait)
        self.exit_when_gone = exit_when_gone
        self.window = window
        self.screen = None
        self.surface = None
        self.mode = (0, 0)
        self.frames_shown = 0

    def _auto_scale(self, pygame, size):
        w, h = size
        if w <= 0 or h <= 0:
            return 1
        try:
            dw, dh = pygame.display.get_desktop_sizes()[0]
            dw, dh = int(dw * 0.9), int(dh * 0.9)
        except Exception:
            info = pygame.display.Info()
            dw, dh = int(info.current_w * 0.9), int(info.current_h * 0.9)
        return max(1, min(MAX_AUTO_SCALE, dw // w, dh // h))

    def _window_for(self, pygame, size):
        scale = self.scale or self._auto_scale(pygame, size)
        return (size[0] * scale, size[1] * scale)

    def _ensure_window(self, pygame, size):
        want = self._window_for(pygame, size)
        if self.screen is not None and want == self.window:
            return
        self.window = want
        self.screen = pygame.display.set_mode(want)
        scale = self.scale or self._auto_scale(pygame, size)
        pygame.display.set_caption("%s  %dx%d  %dx"
                                   % (self.title, size[0], size[1], scale))

    def _present(self, pygame):
        if self.screen is None or self.surface is None:
            return
        surf = self.surface
        if self.window != self.mode:
            surf = pygame.transform.scale(surf, self.window)
        self.screen.blit(surf, (0, 0))
        pygame.display.flip()

    def _blit(self, pygame, frame):
        self.surface = pygame.image.frombytes(frame_to_rgb(frame), frame.size,
                                              "RGB")
        if frame.size != self.mode:
            self.mode = frame.size
            self._ensure_window(pygame, self.mode)
        self.frames_shown += 1

    def _rescale(self, pygame, scale):
        self.scale = max(0, int(scale))
        if self.mode != (0, 0):
            self._ensure_window(pygame, self.mode)
            self._present(pygame)

    def run(self):
        os.environ["DISPLAY"] = _resolve_display(os.environ.get("DISPLAY"))
        # pygame.init() minus the mixer; avoids the SDL audio probe delay
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
        import pygame

        self.live.wait_open(timeout=self.wait)

        pygame.display.init()
        try:
            self.mode = self.live.geometry() or DEFAULT_WINDOW
            self._ensure_window(pygame, self.mode)
            clock = pygame.time.Clock()
            running = True
            last_idle_present = 0.0
            IDLE_PRESENT_HZ = 4
            while running:
                for ev in pygame.event.get():
                    if ev.type == pygame.QUIT:
                        running = False
                    elif ev.type == pygame.KEYDOWN:
                        if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                            running = False
                        elif pygame.K_1 <= ev.key <= pygame.K_8:
                            self._rescale(pygame, ev.key - pygame.K_0)
                        elif ev.key == pygame.K_f:
                            self._rescale(pygame, 0)

                try:
                    frame = self.live.read()
                except VgaFbError:
                    break
                if frame is not None:
                    self._blit(pygame, frame)
                    self._present(pygame)
                    last_idle_present = time.time()
                elif self.exit_when_gone and not os.path.exists(self.live.path):
                    running = False
                elif time.time() - last_idle_present >= 1.0 / IDLE_PRESENT_HZ:
                    self._present(pygame)
                    last_idle_present = time.time()

                clock.tick(self.fps)
        finally:
            pygame.quit()
            self.live.close()
        return self.frames_shown


def main(argv=None):
    ap = argparse.ArgumentParser(description="A monitor for a Verilator VGA "
                                  "testbench: a pygame window fed by the "
                                  "live view sink.")
    ap.add_argument("--live", default=DEFAULT_LIVE_PATH,
                    help="live sink path, matching the testbench's --live "
                         "(default: %(default)s)")
    ap.add_argument("--scale", type=int, default=0,
                    help="integer zoom; 0 (default) fits the display")
    ap.add_argument("--fps", type=int, default=60,
                    help="redraw poll rate (default: %(default)s)")
    ap.add_argument("--title", default=None, help="window caption")
    ap.add_argument("--wait", type=float, default=30.0,
                    help="seconds to wait for the sink to appear")
    ap.add_argument("--stay", action="store_true",
                    help="keep the window up after the testbench exits")
    ap.add_argument("--display", default=None,
                    help="X display (default: DISPLAY, else the xrdp session)")
    args = ap.parse_args(argv)

    if args.display:
        os.environ["DISPLAY"] = args.display

    mon = Monitor(live_path=args.live, scale=args.scale, fps=args.fps,
                  title=args.title, wait=args.wait,
                  exit_when_gone=not args.stay)
    try:
        shown = mon.run()
    except VgaFbError as exc:
        sys.stderr.write("verilatorfbmonitor: %s\n" % exc)
        return 2
    except KeyboardInterrupt:
        return 0
    sys.stderr.write("verilatorfbmonitor: %d frames shown\n" % shown)
    return 0


class VerilatorFbMonitorProcess(object):
    def __init__(self, name="verilatorfb_monitor", live_path=DEFAULT_LIVE_PATH,
                 scale=0, fps=60, title=None, wait=30.0, python=None,
                 keep_open=False):
        self.name = name
        self.live_path = live_path
        self.scale = scale
        self.fps = fps
        self.title = title
        self.wait = wait
        self.keep_open = keep_open
        self.display = None
        self.python = python or sys.executable or "python3"
        self.process = None
        self.pid = None

    def _argv(self):
        argv = [self.python, MONITOR_PY,
                "--live", self.live_path,
                "--scale", str(self.scale),
                "--fps", str(self.fps),
                "--wait", str(self.wait)]
        if self.display:
            argv += ["--display", self.display]
        if self.title:
            argv += ["--title", self.title]
        if self.keep_open:
            argv += ["--stay"]
        return argv

    def _resolve_display(self):
        try:
            sys.path.insert(0, HELPERDIR)
            from displayhelpers import resolve_display
            return resolve_display(os.environ.get("DISPLAY"))
        except Exception:
            return os.environ.get("DISPLAY") or "(unset)"

    def start(self):
        if not os.path.exists(MONITOR_PY):
            return False, f"verilatorvgahelpers.py missing at {MONITOR_PY}"
        self.display = self._resolve_display()
        try:
            self.process = subprocess.Popen(
                self._argv(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except Exception as exc:
            return False, f"Could not launch verilator_vga monitor: {exc}"
        self.pid = self.process.pid
        processreg.register(self.name, self.process, source="verilatorfb_monitor")

        time.sleep(1.0)
        if self.process.poll() is not None:
            err = ""
            try:
                err = (self.process.stderr.read() or b"").decode(errors="replace")
            except Exception:
                pass
            hint = ""
            if "No module named 'pygame'" in err:
                hint = (" -- pygame is in requirements.txt; a container built "
                        "before it was added needs "
                        "'/opt/venv/bin/pip install pygame'")
            return False, (f"verilator_vga monitor exited immediately"
                           f"{hint}\n{err.strip()[-600:]}")

        return True, (f"verilator_vga monitor {self.name} (pid {self.pid}) "
                      f"watching {self.live_path} at "
                      f"{'fit' if not self.scale else str(self.scale) + 'x'}"
                      f" on DISPLAY {self.display}"
                      + (" (kept open after the run)" if self.keep_open else ""))

    def is_alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.process is None:
            return
        processreg.unregister(self.name)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None


if __name__ == "__main__":
    sys.exit(main())
