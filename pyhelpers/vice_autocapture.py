"""
Checkpoint-driven scene capture for a running VICE instance.

Polling the monitor on a timer (or on a GUI button click) only ever catches
whatever frame happened to be on screen at that moment, so scene transitions
get missed. This instead sets binary-monitor watchpoints on the addresses a
scene change actually touches -- sprite pointers, VIC bank, screen/charset
select, sprite enable -- so VICE halts and tells us the instant one is
written. We read a snapshot while it's halted (guaranteeing a consistent
view) and then resume.

Used by both vice_pygame_tool.py (AutoCap button) and the
test_autocapture_start dispatch step, so a testlist can rip a demo with no
GUI involved.
"""

import hashlib
import json
import os
import queue
import threading

# Imported both as a bare module (pyhelpers on sys.path, e.g. from
# vice_pygame_tool.py) and as part of the testsrc.pyhelpers package (from
# dispatch_functions.py), so accept either import path.
try:
    from vice_binmon import (
        ViceBinMon, OP_STORE, RESP_CHECKPOINT_INFO, parse_checkpoint_info, BinMonError,
    )
except ImportError:
    from testsrc.pyhelpers.vice_binmon import (
        ViceBinMon, OP_STORE, RESP_CHECKPOINT_INFO, parse_checkpoint_info, BinMonError,
    )

DEFAULT_WATCH_RANGES = [
    (0x07F8, 0x07FF),  # sprite pointer table
    (0xD015, 0xD015),  # sprite enable
    (0xD018, 0xD018),  # screen/charset/bitmap select
    (0xDD00, 0xDD00),  # VIC bank (CIA2 port A)
]

# Errors that mean "the emulator went away / the link broke" rather than a bug
# on our side. Caught so one bad read can't kill the capture thread.
_LINK_ERRORS = (BinMonError, ConnectionError, OSError)


class SceneCapture:
    """
    Watches a VICE instance over the binary monitor and writes one directory
    per distinct scene into `capture_dir`.

    A "scene" is identified by its *signature*: the VIC bank, $D018, $D015 and
    the 8 sprite pointers. That's what structurally defines what the VIC is
    displaying, and it's cheap to read (11 bytes) while the emulator is
    halted. Only when the signature is one we haven't seen do we pay for the
    full dump. This matters because a demo may rewrite these registers every
    frame at 50Hz -- hashing the full screen/colour/sprite payload each time
    would bog the emulator down, and hashing volatile content (scrolling text,
    music registers) would make almost every frame look "new" and bury the
    handful of real scenes in thousands of directories.

    Cycling sprite pointers for animation produces a new signature per frame,
    which is intentional: that's how you get every animation frame of a sprite
    rather than just the first.
    """

    def __init__(self, host="127.0.0.1", port=6511, capture_dir=None,
                 watch_ranges=None, dump_ram=True, on_log=None):
        self.host = host
        self.port = port
        self.capture_dir = capture_dir or "/testsrc/captures"
        self.watch_ranges = list(watch_ranges or DEFAULT_WATCH_RANGES)
        self.dump_ram = dump_ram
        self._on_log = on_log

        self.mon = None
        self.checknums = []
        self.enabled = False
        self.scene_count = 0
        self.hit_count = 0
        self.log = []              # most recent first
        self._seen_signatures = set()
        self._hit_queue = None
        self._worker = None
        self._stopping = threading.Event()

    # -- logging ---------------------------------------------------------

    def _log(self, msg):
        print("[autocap]", msg, flush=True)
        self.log.insert(0, msg)
        del self.log[8:]
        if self._on_log:
            self._on_log(msg)

    # -- lifecycle -------------------------------------------------------

    def start(self):
        """Connect, arm the watchpoints and begin capturing. Raises on failure."""
        if self.mon is not None:
            return

        self._hit_queue = queue.Queue()
        mon = ViceBinMon(host=self.host, port=self.port, on_event=self._on_binmon_event)
        self.mon = mon

        for start, end in self.watch_ranges:
            self.checknums.append(
                mon.checkpoint_set(start, end, OP_STORE, stop_when_hit=True)
            )

        os.makedirs(self.capture_dir, exist_ok=True)

        self._worker = threading.Thread(
            target=self._capture_worker, name="autocap-worker", daemon=True
        )
        self._worker.start()

        self.enabled = True
        # Setting checkpoints can leave the emulator halted; make sure it runs.
        mon.exit_monitor()
        self._log(f"armed on port {self.port}, watching {len(self.watch_ranges)} ranges "
                  f"-> {self.capture_dir}")

    def stop(self):
        self.enabled = False
        self._stopping.set()
        if self._hit_queue is not None:
            self._hit_queue.put(None)
        if self.mon is not None:
            try:
                for checknum in self.checknums:
                    self.mon.checkpoint_delete(checknum)
                self.mon.exit_monitor()   # leave the emulator running
            except _LINK_ERRORS:
                pass
            self.mon.close()
            self.mon = None
        self.checknums = []
        self._log(f"stopped after {self.scene_count} scene(s), {self.hit_count} hit(s)")

    # -- event plumbing --------------------------------------------------

    def _on_binmon_event(self, resp_type, err, body):
        if resp_type != RESP_CHECKPOINT_INFO:
            return
        info = parse_checkpoint_info(body)
        if not info["currently_hit"]:
            return
        if self._hit_queue is not None:
            self._hit_queue.put(info)

    def _capture_worker(self):
        while not self._stopping.is_set():
            info = self._hit_queue.get()
            if info is None:
                return
            self.hit_count += 1
            try:
                if self.enabled:
                    self._handle_hit(info)
            except _LINK_ERRORS as e:
                self._log(f"capture error: {e}")
            finally:
                # Always resume, even if the dump failed -- otherwise the
                # emulator stays halted forever and the demo stops dead.
                try:
                    if self.mon is not None:
                        self.mon.exit_monitor()
                except _LINK_ERRORS:
                    pass

    # -- capture ---------------------------------------------------------

    def _read_signature(self, mon):
        dd00 = mon.memory_get(0xDD00, 0xDD00)[0]
        d015 = mon.memory_get(0xD015, 0xD015)[0]
        d018 = mon.memory_get(0xD018, 0xD018)[0]
        ptrs = mon.memory_get(0x07F8, 0x07FF)
        return (dd00 & 0x03, d015, d018, tuple(ptrs))

    def _handle_hit(self, info):
        mon = self.mon
        sig = self._read_signature(mon)
        if sig in self._seen_signatures:
            return
        self._seen_signatures.add(sig)

        snap = self._capture_snapshot(mon, sig)
        scene_dir = self._save_snapshot(snap, info["checknum"])
        self._log(f"saved {os.path.basename(scene_dir)} "
                  f"({len(snap['sprites'])} sprites)")

    def _capture_snapshot(self, mon, sig):
        vic_bank_sel, d015, d018, ptrs = sig
        vic_bank_base = [0x0000, 0x4000, 0x8000, 0xC000][(~vic_bank_sel) & 0x03]

        sprites = {}
        for i, ptr in enumerate(ptrs):
            if not ((d015 >> i) & 1):
                continue
            addr = vic_bank_base + (ptr << 6)
            sprites[i] = {"addr": addr, "bytes": mon.memory_get(addr, addr + 62)}

        screen_base = vic_bank_base + ((d018 >> 4) & 0x0F) * 0x0400
        charset_base = vic_bank_base + ((d018 >> 1) & 0x07) * 0x0800

        snap = {
            "vic_bank_sel": vic_bank_sel,
            "vic_bank_base": vic_bank_base,
            "d015": d015,
            "d018": d018,
            "sprite_ptrs": list(ptrs),
            "screen_base": screen_base,
            "charset_base": charset_base,
            "sprites": sprites,
            "sprite_colors": mon.memory_get(0xD027, 0xD02E),
            "shared_colors": mon.memory_get(0xD025, 0xD026),
            "border_bg": mon.memory_get(0xD020, 0xD024),
            "d011": mon.memory_get(0xD011, 0xD011)[0],
            "d016": mon.memory_get(0xD016, 0xD016)[0],
            "sid_regs": mon.memory_get(0xD400, 0xD41C),
            "screen_bytes": mon.memory_get(screen_base, screen_base + 0x03E7),
            "color_bytes": mon.memory_get(0xD800, 0xDBE7),
        }

        if self.dump_ram:
            # The SID registers alone are just one frame of a playing tune --
            # useless for reconstructing the music. The player routine and its
            # data live in RAM, so grab the lot; a handful of scenes at 64K is
            # cheap and lets you extract the tune offline.
            # Split because the protocol's length field is 16-bit, so a single
            # 0000-ffff request can't express 65536 bytes.
            snap["ram"] = (mon.memory_get(0x0000, 0x7FFF)
                           + mon.memory_get(0x8000, 0xFFFF))

        return snap

    def _save_snapshot(self, snap, checknum):
        self.scene_count += 1
        digest = hashlib.sha1(
            repr((snap["vic_bank_sel"], snap["d015"], snap["d018"],
                  snap["sprite_ptrs"])).encode()
        ).hexdigest()[:8]
        scene_dir = os.path.join(
            self.capture_dir, f"scene_{self.scene_count:04d}_{digest}"
        )
        os.makedirs(scene_dir, exist_ok=True)

        for i, s in snap["sprites"].items():
            with open(os.path.join(scene_dir, f"sprite_{i}.bin"), "wb") as f:
                f.write(bytes(s["bytes"]))

        for name, key in (("sid_regs.bin", "sid_regs"),
                          ("screen.bin", "screen_bytes"),
                          ("color.bin", "color_bytes")):
            with open(os.path.join(scene_dir, name), "wb") as f:
                f.write(bytes(snap[key]))

        if "ram" in snap:
            with open(os.path.join(scene_dir, "ram.bin"), "wb") as f:
                f.write(bytes(snap["ram"]))

        meta = {
            "trigger_checkpoint": checknum,
            "vic_bank_base": snap["vic_bank_base"],
            "d015": snap["d015"],
            "d018": snap["d018"],
            "d011": snap["d011"],
            "d016": snap["d016"],
            "screen_base": snap["screen_base"],
            "charset_base": snap["charset_base"],
            "sprite_ptrs": snap["sprite_ptrs"],
            "sprite_colors": snap["sprite_colors"],
            "shared_colors": snap["shared_colors"],
            "border_bg": snap["border_bg"],
            "sprite_addrs": {str(i): s["addr"] for i, s in snap["sprites"].items()},
        }
        with open(os.path.join(scene_dir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)

        return scene_dir
