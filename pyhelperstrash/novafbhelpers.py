#!/usr/bin/env python3
"""nova_fb live view sink: reader, viewer, and viewer subprocess wrapper (merged from novafbhelpers, novafbmonitor, novafbmonitor_proc).
  - NovaFbLive / Frame / frame_to_rgb: reader for `SET FB LIVE=<path>`; present() copies the front buffer into a shared-memory file that reader processes mmap.
    Little-endian uint32 header, then payload:
        0   MAGIC    "NVFB" = 0x4246564E
        1   VERSION  1
        2   FORMAT   1 = FMT_MONO1, packed 1bpp
        3   WIDTH    pixels
        4   HEIGHT   pixels
        5   STRIDE   bytes per row
        6   BYTES    payload bytes in use
        7   SEQ      seqlock, odd while present() is mid-write
        8   FRAME    frames presented (FB FRAME register)
        9-15         reserved, zero
    File is a fixed 64 + 4MiB regardless of mode.
  - Monitor / main(): pygame window on the sink (read-only), beside a simulator whose SIMH script has `set fb live=<path>`:
        python3 /testsrc/pyhelpers/novafbhelpers.py --live /dev/shm/novafb
    --scale N multiplies the zoom (256x256 at 3x = 768x768); 0 (default) = auto.
    Keys:  ESC / Q  quit        1-8  integer zoom        F  fit to window
  - NovaFbMonitorProcess: runs main() as a child process a testlist can hold.
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


MAGIC = 0x4246564E
VERSION = 1
HDR_BYTES = 64
HDR_WORDS = 16
MAX_PAYLOAD = 1024 * 1024 * 4
FILE_BYTES = HDR_BYTES + MAX_PAYLOAD

FMT_MONO1 = 1

DEFAULT_LIVE_PATH = "/dev/shm/novafb"

# Header word indices; match #defines in nova_fb.c
(H_MAGIC, H_VERSION, H_FORMAT, H_WIDTH, H_HEIGHT,
 H_STRIDE, H_BYTES, H_SEQ, H_FRAME) = range(9)

_HDR = struct.Struct("<%dI" % HDR_WORDS)


class Frame(object):
    """One whole, untorn frame plus the geometry it was presented at."""

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


class NovaFbError(Exception):
    pass


class NovaFbLive(object):
    """mmap reader for one `SET FB LIVE=` sink; open, then read()."""

    def __init__(self, path=DEFAULT_LIVE_PATH):
        self.path = path
        self._fd = None
        self._map = None
        self._last_seq = None
        self._last_frame = None
        self._last_geom = None

    def open(self):
        """Map the sink. Raises NovaFbError if it is missing or not ours."""
        if self._map is not None:
            return self
        if not os.path.exists(self.path):
            raise NovaFbError("no live sink at %s -- is 'set fb live=%s' in "
                              "the SIMH script?" % (self.path, self.path))
        size = os.path.getsize(self.path)
        if size < HDR_BYTES:
            raise NovaFbError("%s is too small to be a live sink (%d bytes)"
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
            raise NovaFbError("%s is not a nova_fb live sink (magic %#x)"
                              % (self.path, hdr[H_MAGIC]))
        if hdr[H_VERSION] != VERSION:
            self.close()
            raise NovaFbError("%s speaks live protocol v%d, this reader "
                              "speaks v%d" % (self.path, hdr[H_VERSION], VERSION))
        return self

    def wait_open(self, timeout=30.0, poll=0.1):
        """Wait for the simulator to create the sink file, then open it."""
        deadline = time.time() + float(timeout)
        last = None
        while time.time() < deadline:
            try:
                return self.open()
            except NovaFbError as exc:
                last = exc
                time.sleep(poll)
        raise NovaFbError("timed out after %gs waiting for %s (%s)"
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
        """Current Frame, or None. only_new=True: None if the frame counter has not advanced. None also if the counter can't be read."""
        if self._map is None:
            raise NovaFbError("read() before open()")

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
        """(width, height) currently published, without reading pixels."""
        hdr = self._header()
        return (hdr[H_WIDTH], hdr[H_HEIGHT])

    def frame_number(self):
        return self._header()[H_FRAME]


_LUT_RGB = None


def _lut_rgb():
    """256 -> 24 bytes: one byte's 8 pixels as black/white RGB triples."""
    global _LUT_RGB
    if _LUT_RGB is None:
        _LUT_RGB = [
            b"".join((b"\xff\xff\xff" if (b >> (7 - i)) & 1 else b"\x00\x00\x00")
                     for i in range(8))
            for b in range(256)
        ]
    return _LUT_RGB


def mono_to_rgb(frame):
    """FMT_MONO1 Frame -> packed RGB888 bytes, white on black."""
    if frame.fmt != FMT_MONO1:
        raise NovaFbError("mono_to_rgb: frame format %d is not mono" % frame.fmt)

    w, h, stride = frame.width, frame.height, frame.stride
    try:
        import numpy as np
    except ImportError:
        lut = _lut_rgb()
        out = bytearray()
        for row in range(h):
            base = row * stride
            line = frame.pixels[base:base + stride]
            out += b"".join([lut[b] for b in line])[:w * 3]
        return bytes(out)

    raw = np.frombuffer(frame.pixels[:stride * h], dtype=np.uint8)
    bits = np.unpackbits(raw.reshape(h, stride), axis=1)[:, :w]
    return np.repeat(bits * 255, 3, axis=1).astype(np.uint8).tobytes()


def frame_to_rgb(frame):
    """Any published frame format -> packed RGB888."""
    if frame.fmt == FMT_MONO1:
        return mono_to_rgb(frame)
    raise NovaFbError(
        "unknown live frame format %d -- this reader understands mono only "
        "(is nova_fb.c newer than pyhelpers?)" % frame.fmt)


DEFAULT_WINDOW = (640, 480)
MAX_AUTO_SCALE = 4


def _resolve_display(explicit=None):
    """X display emulator windows open on."""
    try:
        from displayhelpers import resolve_display
        return resolve_display(explicit)
    except Exception:
        return explicit or os.environ.get("DISPLAY") or ":20"


class Monitor(object):
    def __init__(self, live_path=DEFAULT_LIVE_PATH, scale=0, fps=60,
                 title=None, wait=30.0, exit_when_gone=True,
                 window=None):
        self.live = NovaFbLive(live_path)
        self.scale = max(0, int(scale))
        self.fps = max(1, int(fps))
        self.title = title or "Nova FB - %s" % os.path.basename(live_path)
        self.wait = float(wait)
        self.exit_when_gone = exit_when_gone
        self.window = window
        self.screen = None
        self.surface = None
        self.mode = (0, 0)
        self.frames_shown = 0


    def _auto_scale(self, pygame, size):
        """Largest integer zoom that fits the X display (>= 1); used when --scale is 0."""
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
        """The window size for a frame: the frame, times the zoom. Nothing else."""
        scale = self.scale or self._auto_scale(pygame, size)
        return (size[0] * scale, size[1] * scale)

    def _ensure_window(self, pygame, size):
        """Size the window to the card's mode; on mode or zoom change only (set_mode replaces the X window)."""
        want = self._window_for(pygame, size)
        if self.screen is not None and want == self.window:
            return
        self.window = want
        self.screen = pygame.display.set_mode(want)
        scale = self.scale or self._auto_scale(pygame, size)
        pygame.display.set_caption("%s  %dx%d  %dx"
                                   % (self.title, size[0], size[1], scale))

    def _present(self, pygame):
        """Paint the frame at window size. The two are equal by construction."""
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
        """Change the zoom, which changes the window; 0 means auto."""
        self.scale = max(0, int(scale))
        if self.mode != (0, 0):
            self._ensure_window(pygame, self.mode)
            self._present(pygame)


    def run(self):
        os.environ["DISPLAY"] = _resolve_display(os.environ.get("DISPLAY"))
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
                except NovaFbError:
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
    ap = argparse.ArgumentParser(description="A monitor for the nova_fb "
                                  "card: a pygame window fed by the live "
                                  "view sink.")
    ap.add_argument("--live", default=DEFAULT_LIVE_PATH,
                    help="live sink path, matching 'set fb live=' "
                         "(default: %(default)s)")
    ap.add_argument("--scale", type=int, default=0,
                    help="integer zoom; the window is the card's mode times "
                         "this. 0 (default) picks the largest zoom that fits "
                         "the display")
    ap.add_argument("--fps", type=int, default=60,
                    help="redraw poll rate (default: %(default)s)")
    ap.add_argument("--title", default=None, help="window caption")
    ap.add_argument("--wait", type=float, default=30.0,
                    help="seconds to wait for the sink to appear")
    ap.add_argument("--stay", action="store_true",
                    help="keep the window up after the simulator exits")
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
    except NovaFbError as exc:
        sys.stderr.write("novafbmonitor: %s\n" % exc)
        return 2
    except KeyboardInterrupt:
        return 0
    sys.stderr.write("novafbmonitor: %d frames shown\n" % shown)
    return 0


class NovaFbMonitorProcess(object):
    def __init__(self, name="novafb_monitor", live_path=DEFAULT_LIVE_PATH,
                 scale=0, fps=60, title=None, wait=30.0, python=None,
                 keep_open=False):
        self.name = name
        self.live_path = live_path
        self.scale = scale
        self.fps = fps
        self.title = title
        self.wait = wait
        # keep the window up after test_terminate_all
        self.keep_open = keep_open
        self.display = None
        # runs under its own venv
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
        """The display the window will open on -- the suite's usual rule."""
        try:
            sys.path.insert(0, HELPERDIR)
            from displayhelpers import resolve_display
            return resolve_display(os.environ.get("DISPLAY"))
        except Exception:
            return os.environ.get("DISPLAY") or "(unset)"

    def start(self):
        """Launch the viewer -> (ok, message); reports import-time failures."""
        if not os.path.exists(MONITOR_PY):
            return False, f"novafbhelpers.py missing at {MONITOR_PY}"
        self.display = self._resolve_display()
        try:
            self.process = subprocess.Popen(
                self._argv(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except Exception as exc:
            return False, f"Could not launch nova_fb monitor: {exc}"
        self.pid = self.process.pid
        processreg.register(self.name, self.process, source="novafb_monitor")

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
            return False, (f"nova_fb monitor exited immediately"
                           f"{hint}\n{err.strip()[-600:]}")

        return True, (f"nova_fb monitor {self.name} (pid {self.pid}) watching "
                      f"{self.live_path} at "
                      f"{'fit' if not self.scale else str(self.scale) + 'x'}"
                      f" on DISPLAY "
                      f"{self.display}"
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
