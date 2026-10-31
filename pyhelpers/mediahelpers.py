"""Media, capture and artifact helpers (merged from gifhelpers, medialibhelpers, audiohelpers, artifacthelpers): GIF screen recording, media library (images/), tone analysis of captured audio, disk artifact repository (injectable DOS directory trees)."""

import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import threading
import time

from PIL import Image


# GIF screen recording
"""
gifhelpers.py
-------------
Background screen recorder: grabs a frame every `interval` seconds on its own
thread and writes an animated GIF into the runner's reports dir.

Why a background thread rather than a capture-N-frames step (which is what
QemuInstance.take_screenshots_to_gif does): the interesting motion is what the
NEXT step causes 

It stops on the first of:
  * an explicit stop() -- test_stop_gif_capture, test_terminate_all, or the
    runner's end-of-run hook
  * max_seconds / max_frames
  * the testlist ending                 (progress_state.step -> "Idle")
  * the instance dying (consecutive capture failures)

a capture runs until something explicitly ends it with
test_stop_gif_capture (or max_seconds/max_frames)

The GIF is named screenshot-<instance>-<stepnum>.gif with the step number of
the step that STOPPED it (test_stop_gif_capture, or whatever teardown step
finally called stop()) 
"""
REPORTS_DIR = "/testrunnerapp/reports"

# consecutive failed grabs before the recorder gives up
_MAX_CONSECUTIVE_FAILURES = 3

_ACTIVE = []
_ACTIVE_LOCK = threading.Lock()


def _progress_step():
    """The runner's current step token ("3/15", "Idle"), or "" if unreadable."""
    try:
        from appstate import progress_state
        return str(progress_state.step)
    except Exception:
        return ""


def _reports_dir():
    """Per-run reports directory (see appstate.current_reports_dir)"""
    try:
        from appstate import current_reports_dir
        return current_reports_dir()
    except Exception:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        return REPORTS_DIR


def stepnum_of(token=None):
    """Leading integer of a step token -- the number report assets are keyed by."""
    m = re.match(r"\d+", str(token if token is not None else _progress_step()))
    return m.group(0) if m else "0"


class GifRecorder:
    """One recording in flight. Create, start(), and it stops itself."""

    def __init__(self, instance, interval=0.5, max_seconds=120, max_frames=240,
                 scale=1.0, playback_speed=1.0, gif_path=None):
        self.instance   = instance
        self.name       = getattr(instance, "name", "instance")
        self.interval   = max(0.05, float(interval))
        self.max_seconds = float(max_seconds) if max_seconds else 0.0
        self.max_frames = int(max_frames) if max_frames else 0
        self.scale      = float(scale)
        self.playback_speed = max(0.05, float(playback_speed))

        self.start_token = _progress_step()
        self.stepnum     = stepnum_of(self.start_token)
        # provisional name from the start step; renamed to the stop step in _run() unless a path was pinned
        self._gif_path_pinned = gif_path is not None
        self.gif_path    = gif_path or os.path.join(
            _reports_dir(), f"screenshot-{self.name}-{self.stepnum}.gif")

        self._frames   = []
        self._stop_evt = threading.Event()
        # set while the run is SUSPENDED: take one frame, then wait; held time not counted toward max_seconds
        self._hold_evt = threading.Event()
        self._held_total = 0.0
        self._thread   = None
        self._done     = threading.Event()
        # tempfile, not a literal /tmp (Windows)
        self._tmp_png  = os.path.join(tempfile.gettempdir(),
                                      f"_gifcap_{self.name}_{self.stepnum}.png")
        self.last_error = ""
        self.stop_reason = None
        self.result = None


    def start(self):
        os.makedirs(os.path.dirname(self.gif_path) or REPORTS_DIR, exist_ok=True)
        self._thread = threading.Thread(target=self._run,
                                        name=f"gifcap-{self.name}-{self.stepnum}",
                                        daemon=True)
        self._thread.start()
        with _ACTIVE_LOCK:
            _ACTIVE.append(self)
        return self

    def stop(self, reason="stopped", timeout=20):
        """Ask the thread to finish and wait for the GIF to be written."""
        if self.stop_reason is None:
            self.stop_reason = reason
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._done.wait(timeout=2)
        return self.result or (False, f"[{self.name}] recorder produced no GIF")

    def hold(self):
        """Take at most one more frame, then stop capturing until release()."""
        self._hold_evt.set()

    def release(self):
        self._hold_evt.clear()

    @property
    def held(self):
        return self._hold_evt.is_set()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _grab(self):
        """One frame as a palettised PIL image, or None (via the instance's take_screenshot; palettised to save memory)."""
        try:
            result = self.instance.take_screenshot(filename=self._tmp_png)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None
        ok = result[0] if isinstance(result, tuple) else bool(result)
        if not ok or not os.path.exists(self._tmp_png):
            self.last_error = str(result[1]) if isinstance(result, tuple) and len(result) > 1 else "screenshot failed"
            return None
        try:
            with Image.open(self._tmp_png) as img:
                img = img.convert("RGB")
                if self.scale and self.scale != 1.0:
                    img = img.resize((max(1, int(img.width * self.scale)),
                                      max(1, int(img.height * self.scale))),
                                     Image.NEAREST)
                return img.convert("P", palette=Image.ADAPTIVE, colors=256)
        except Exception:
            return None

    def _should_stop(self, started_at):
        if self._stop_evt.is_set():
            return self.stop_reason or "stopped"
        if self.max_frames and len(self._frames) >= self.max_frames:
            return f"max_frames={self.max_frames}"
        if self.max_seconds and (time.time() - started_at - self._held_total) >= self.max_seconds:
            return f"max_seconds={self.max_seconds:g}"
        # safety net: stop when the run ends; "" = progress_state unreadable, keep recording
        if _progress_step() == "Idle":
            return "testlist ended"
        return None

    def _finalize_gif_path(self):
        """Rename the pending GIF to the STOP step's number (failed/aborted tests don't orphan captures)."""
        if self._gif_path_pinned:
            return
        stop_stepnum = stepnum_of()
        if stop_stepnum == "0":
            return
        self.gif_path = os.path.join(
            os.path.dirname(self.gif_path) or _reports_dir(),
            f"screenshot-{self.name}-{stop_stepnum}.gif")

    def _run(self):
        started_at = time.time()
        failures = 0
        try:
            while True:
                frame = self._grab()
                if frame is None:
                    failures += 1
                    if failures >= _MAX_CONSECUTIVE_FAILURES:
                        self.stop_reason = (self.stop_reason
                                            or "capture failed (instance gone?)"
                                            + (f": {self.last_error}" if self.last_error else ""))
                        break
                else:
                    failures = 0
                    self._frames.append(frame)

                reason = self._should_stop(started_at)
                if reason:
                    self.stop_reason = reason
                    break
                if self._hold_evt.is_set():
                    held_at = time.time()
                    while self._hold_evt.is_set() and not self._stop_evt.wait(0.25):
                        pass
                    self._held_total += time.time() - held_at
                    if self._stop_evt.is_set():
                        self.stop_reason = self.stop_reason or "stopped"
                        break
                    continue
                # event-based sleep so stop() is not delayed
                if self._stop_evt.wait(self.interval):
                    self.stop_reason = self.stop_reason or "stopped"
                    break

            self._finalize_gif_path()
            self.result = self._write(time.time() - started_at)
        except Exception as e:
            self.result = (False, f"[{self.name}] gif recorder crashed: "
                                  f"{type(e).__name__}: {e}")
        finally:
            try:
                os.remove(self._tmp_png)
            except OSError:
                pass
            with _ACTIVE_LOCK:
                if self in _ACTIVE:
                    _ACTIVE.remove(self)
            self._done.set()

    def _letterbox(self, frame, target_size):
        """Center `frame` on a black `target_size` canvas if smaller (mode changes mid-recording)."""
        if frame.size == target_size:
            return frame
        canvas = Image.new("RGB", target_size, (0, 0, 0))
        rgb = frame.convert("RGB")
        x = (target_size[0] - rgb.width) // 2
        y = (target_size[1] - rgb.height) // 2
        canvas.paste(rgb, (x, y))
        return canvas.convert("P", palette=Image.ADAPTIVE, colors=256)

    def _write(self, elapsed):
        if not self._frames:
            return False, (f"[{self.name}] no frames captured "
                           f"({self.stop_reason}) — nothing written")
        target_size = (max(f.width for f in self._frames),
                       max(f.height for f in self._frames))
        if any(f.size != target_size for f in self._frames):
            self._frames = [self._letterbox(f, target_size) for f in self._frames]
        duration_ms = max(20, int(self.interval * 1000 / self.playback_speed))
        try:
            self._frames[0].save(
                self.gif_path, save_all=True, append_images=self._frames[1:],
                duration=duration_ms, loop=0, optimize=False)
        except Exception as e:
            return False, f"[{self.name}] gif write failed: {e}"
        size_kb = os.path.getsize(self.gif_path) / 1024.0
        msg = (f"[{self.name}] recorded {len(self._frames)} frame(s) over "
               f"{elapsed:.1f}s (every {self.interval:g}s) -> {self.gif_path} "
               f"({size_kb:.0f} KB, {duration_ms}ms/frame). "
               f"Stopped: {self.stop_reason}.")
        # drop frame references once the file exists
        self._frames = []
        return True, msg


def active_recorders():
    with _ACTIVE_LOCK:
        return list(_ACTIVE)


def stop_all(reason="stopped", timeout=20):
    """Stop every recorder still running. Returns a list of (ok, message)."""
    return [r.stop(reason=reason, timeout=timeout) for r in active_recorders()]


"""Media library — the repository side of the disk builder for whole media files.

this module deals in whole media the user
drops into `/testsrc/images`. floppy images, ISOs, prebuilt hard-disk
images. this module scans over them and
keeps a small SQLite index alongside so each one can carry annotation
a shortname/nickname and a long description of what the thing
actually is.

Layout is the user's: subdirs are fine and are part of an item's identity
(`win3_1/win31.iso`). 

Using an item differs by kind, and deliberately so:
  - floppy  → exported (copied) into a project dir, since a test boots/writes it
              and each project wants its own mutable copy.
  - iso/hdd → left where they are and referenced by absolute path; ISOs are large
              and read-only, so copying per project wastes GBs.
"""
_TESTSRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_ROOT = os.path.join(_TESTSRC_ROOT, "images")

DB_PATH = os.path.join(_TESTSRC_ROOT, "media_library.sqlite")
_ISO_EXTS    = (".iso", ".cdr")
_DISK_EXTS   = (".img", ".ima", ".vfd", ".flp", ".dsk", ".qcow2", ".qcow")
_MEDIA_EXTS  = _ISO_EXTS + _DISK_EXTS
_MAX_FLOPPY_BYTES = 2949120

KINDS = ("floppy", "iso", "hdd")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_item (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            rel_path      TEXT UNIQUE NOT NULL,
            shortname     TEXT,
            description   TEXT,
            kind          TEXT,
            kind_override TEXT,
            size_bytes    INTEGER,
            mtime         REAL,
            first_seen    TEXT,
            last_scanned  TEXT,
            missing       INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def _classify(path, size):
    """Media kind from extension + size; .img/.dsk split floppy vs hdd at the 2.88MB ceiling."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _ISO_EXTS:
        return "iso"
    if ext in (".qcow2", ".qcow"):
        return "hdd"
    if ext in (".vfd", ".flp", ".ima"):
        return "floppy"
    return "floppy" if size and size <= _MAX_FLOPPY_BYTES else "hdd"


def _row_to_dict(r):
    d = dict(r)
    d["kind"] = d.get("kind_override") or d.get("kind")
    d["abs_path"] = os.path.join(IMAGES_ROOT, d["rel_path"])
    d["filename"] = os.path.basename(d["rel_path"])
    d["subdir"] = os.path.dirname(d["rel_path"])
    d["missing"] = not os.path.isfile(d["abs_path"])
    # floppies are copied per project; others are referenced in place
    d["exportable"] = d["kind"] == "floppy"
    return d


def scan_media():
    """Walk IMAGES_ROOT and reconcile the index with disk; new files get a derived kind and no annotation -> (ok, message)."""
    os.makedirs(IMAGES_ROOT, exist_ok=True)
    init_db()
    conn = _connect()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    seen = set()
    added = updated = 0

    for root, dirs, files in os.walk(IMAGES_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in sorted(files):
            if fn.startswith(".") or not fn.lower().endswith(_MEDIA_EXTS):
                continue
            ap = os.path.join(root, fn)
            rel = os.path.relpath(ap, IMAGES_ROOT)
            try:
                st = os.stat(ap)
            except OSError:
                continue
            seen.add(rel)
            kind = _classify(ap, st.st_size)
            cur = conn.execute("SELECT id, size_bytes, mtime, missing FROM media_item "
                               "WHERE rel_path = ?", (rel,)).fetchone()
            if cur is None:
                conn.execute(
                    "INSERT INTO media_item (rel_path, shortname, description, kind, "
                    "size_bytes, mtime, first_seen, last_scanned, missing) "
                    "VALUES (?, '', '', ?, ?, ?, ?, ?, 0)",
                    (rel, kind, st.st_size, st.st_mtime, now, now))
                added += 1
            else:
                changed = (cur["size_bytes"] != st.st_size or cur["mtime"] != st.st_mtime
                           or cur["missing"])
                conn.execute(
                    "UPDATE media_item SET kind = ?, size_bytes = ?, mtime = ?, "
                    "last_scanned = ?, missing = 0 WHERE id = ?",
                    (kind, st.st_size, st.st_mtime, now, cur["id"]))
                if changed:
                    updated += 1

    placeholders = ",".join("?" * len(seen))
    if seen:
        gone = conn.execute(
            f"UPDATE media_item SET missing = 1 WHERE rel_path NOT IN ({placeholders}) "
            "AND missing = 0", tuple(seen)).rowcount
    else:
        gone = conn.execute("UPDATE media_item SET missing = 1 WHERE missing = 0").rowcount
    conn.commit()
    conn.close()
    return True, (f"Scanned {IMAGES_ROOT}: {len(seen)} media file(s) — "
                  f"{added} new, {updated} changed, {gone} now missing")


def list_media(kind=None, include_missing=True):
    """All indexed items, optionally one kind; missing items included by default."""
    init_db()
    conn = _connect()
    rows = conn.execute("SELECT * FROM media_item ORDER BY rel_path").fetchall()
    conn.close()
    out = [_row_to_dict(r) for r in rows]
    if kind:
        out = [d for d in out if d["kind"] == kind]
    if not include_missing:
        out = [d for d in out if not d["missing"]]
    return out


def get_media(item_id):
    init_db()
    conn = _connect()
    r = conn.execute("SELECT * FROM media_item WHERE id = ?", (int(item_id),)).fetchone()
    conn.close()
    return _row_to_dict(r) if r else None


def update_media(item_id, shortname=None, description=None, kind=None):
    """Annotate an indexed item; only passed fields change. `kind` sets the override; "" restores the derived kind."""
    init_db()
    if not get_media(item_id):
        return False, f"media item {item_id} not found"
    sets, vals = [], []
    if shortname is not None:
        sets.append("shortname = ?")
        vals.append(str(shortname).strip()[:64])
    if description is not None:
        sets.append("description = ?")
        vals.append(str(description).strip())
    if kind is not None:
        k = str(kind).strip().lower()
        if k and k not in KINDS:
            return False, f"kind must be one of {', '.join(KINDS)} (or empty to auto-detect)"
        sets.append("kind_override = ?")
        vals.append(k or None)
    if not sets:
        return False, "nothing to update"
    conn = _connect()
    conn.execute(f"UPDATE media_item SET {', '.join(sets)} WHERE id = ?", vals + [int(item_id)])
    conn.commit()
    conn.close()
    return True, "Saved."


def forget_media(item_id):
    """Drop an index row and its annotation (for items whose file is gone) -> (ok, message)."""
    item = get_media(item_id)
    if not item:
        return False, f"media item {item_id} not found"
    if os.path.isfile(item["abs_path"]):
        return False, (f"'{item['rel_path']}' is still on disk — the next scan would "
                       f"re-index it. Delete it from the library dir first if you "
                       f"really want it gone.")
    conn = _connect()
    conn.execute("DELETE FROM media_item WHERE id = ?", (int(item_id),))
    conn.commit()
    conn.close()
    return True, f"Forgot '{item['shortname'] or item['rel_path']}' and its notes."


def export_media(item_id, dest_dir, dest_name=None, overwrite=False):
    """Copy a floppy image from the library into a project dir (testlist floppy1_img). Refuses to overwrite unless overwrite is set -> (ok, message)."""
    item = get_media(item_id)
    if not item:
        return False, f"media item {item_id} not found"
    if item["missing"] or not os.path.isfile(item["abs_path"]):
        return False, f"media file is missing from disk: {item['rel_path']}"
    if item["kind"] != "floppy":
        return False, (f"'{item['rel_path']}' is {item['kind']} media — not exportable. "
                       f"Reference it in place at {item['abs_path']}")

    dest_dir = os.path.abspath(str(dest_dir))
    if not os.path.isdir(dest_dir):
        return False, f"destination dir does not exist: {dest_dir}"
    name = os.path.basename(str(dest_name or "").strip()) or item["filename"]
    if name in (".", ".."):
        return False, "invalid destination name"
    dest = os.path.join(dest_dir, name)
    if os.path.exists(dest) and not overwrite:
        return False, (f"{name} already exists in {os.path.basename(dest_dir)} — "
                       f"not overwriting (tick overwrite to replace it)")
    try:
        shutil.copy2(item["abs_path"], dest)
    except OSError as e:
        return False, f"copy failed: {e}"
    mb = round(item["size_bytes"] / (1024 * 1024), 2)
    return True, f"Exported {item['rel_path']} → {dest} ({mb} MB)"


def config_path(item_id):
    """Absolute path a CONFIG should use for an in-place read-only item (ISOs, prebuilt hard-disk images; not for hdd use) -> (ok, path|message)."""
    item = get_media(item_id)
    if not item:
        return False, f"media item {item_id} not found"
    if item["missing"] or not os.path.isfile(item["abs_path"]):
        return False, f"media file is missing from disk: {item['rel_path']}"
    return True, item["abs_path"]


# Audio tone analysis
"""Tone analysis for audio captured out of a QEMU guest.

The pipeline this supports: QEMU's `wav` audiodev writes the emulated sound
card's mixed output straight to a .wav file on the host, and these helpers then
answer "did the guest actually play the tone it was supposed to?".
"""
def read_wav(path):
    """PCM .wav -> (samples, rate); samples mono floats in -1..1."""
    if not os.path.isfile(path):
        return None, f"No audio capture at {path} (did the guest never open the sound card?)"

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return None, f"Cannot read {path}: {e}"

    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None, f"{path} is not a RIFF/WAVE file"

    fmt = None
    pcm = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        (csize,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = pos + 8

        if cid == b"fmt ":
            if body + 16 > len(data):
                return None, f"{path}: truncated fmt chunk"
            fmt = struct.unpack("<HHIIHH", data[body:body + 16])
        elif cid == b"data":
            # unfinalized header: trust the file, not the size field
            avail = len(data) - body
            if csize == 0 or csize > avail:
                csize = avail
            pcm = data[body:body + csize]
            break

        pos = body + csize + (csize & 1)

    if fmt is None:
        return None, f"{path}: no fmt chunk"
    if not pcm:
        return None, (f"{path}: no audio data — QEMU created the capture but the "
                      f"guest never wrote samples to it")

    audio_fmt, channels, rate, _byte_rate, _align, bits = fmt
    if audio_fmt != 1:
        return None, f"{path}: not uncompressed PCM (format tag {audio_fmt})"
    if channels < 1:
        return None, f"{path}: bogus channel count {channels}"

    if bits == 8:
        # 8-bit wav is unsigned, wider is signed
        frames = [(b - 128) / 128.0 for b in pcm]
    elif bits == 16:
        count = len(pcm) // 2
        frames = [s / 32768.0 for s in struct.unpack("<%dh" % count, pcm[:count * 2])]
    else:
        return None, f"{path}: unsupported sample width {bits} bits"

    if channels > 1:
        # downmix to mono
        usable = len(frames) - (len(frames) % channels)
        frames = [sum(frames[i:i + channels]) / channels
                  for i in range(0, usable, channels)]

    if not frames:
        return None, f"{path}: contains a header but zero samples"

    return (frames, rate), None


def goertzel_power(samples, rate, freq):
    """Normalized generalized-Goertzel power of `freq` within `samples`."""
    n = len(samples)
    if n == 0:
        return 0.0
    w = 2.0 * math.pi * freq / rate
    coeff = 2.0 * math.cos(w)
    s1 = 0.0
    s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2 = s1
        s1 = s0
    power = (s1 * s1) + (s2 * s2) - (coeff * s1 * s2)
    return power / (n * n)


def rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(x * x for x in samples) / len(samples))


def loudest_window(samples, rate, seconds=0.5):
    """The `seconds`-long slice with the most energy."""
    width = int(rate * seconds)
    if width <= 0 or len(samples) <= width:
        return samples, 0.0

    # coarse hop for scanning
    hop = max(1, width // 8)
    best_start = 0
    best_energy = -1.0
    for start in range(0, len(samples) - width + 1, hop):
        window = samples[start:start + width]
        energy = sum(x * x for x in window)
        if energy > best_energy:
            best_energy = energy
            best_start = start
    return samples[best_start:best_start + width], best_start / float(rate)


def find_peak_freq(samples, rate, lo_hz=100, hi_hz=8000, step_hz=None):
    """Coarse Goertzel sweep over a band -> (peak_hz, peak_power)."""
    hi_hz = min(hi_hz, rate / 2.0)
    if step_hz is None:
        bin_hz = rate / max(1, len(samples))
        step_hz = max(1.0, bin_hz / 3.0)
    best_hz = 0.0
    best_power = -1.0
    freq = lo_hz
    while freq <= hi_hz:
        power = goertzel_power(samples, rate, freq)
        if power > best_power:
            best_power = power
            best_hz = freq
        freq += step_hz
    return best_hz, best_power


def detect_tone(path, expect_hz, tolerance_hz=60.0, min_snr_db=10.0,
                window_seconds=0.5, silence_rms=0.005):
    """Check `path` contains a tone at `expect_hz`: not silence, strongest frequency within `tolerance_hz`, and `min_snr_db` above the rest of the band -> (ok, message, details_dict)."""
    expect_hz = float(expect_hz)
    tolerance_hz = float(tolerance_hz)
    min_snr_db = float(min_snr_db)

    parsed, err = read_wav(path)
    if err:
        return False, err, {}
    samples, rate = parsed

    duration = len(samples) / float(rate)
    window, offset = loudest_window(samples, rate, window_seconds)
    level = rms(window)

    details = {
        "path": path,
        "rate": rate,
        "duration_s": round(duration, 3),
        "window_offset_s": round(offset, 3),
        "rms": round(level, 5),
    }

    if level < silence_rms:
        return False, (f"Capture is silent (peak RMS {level:.5f} over {duration:.2f}s "
                       f"< {silence_rms}). The sound card was wired up but the guest "
                       f"never played anything."), details

    peak_hz, peak_power = find_peak_freq(window, rate)
    # x2: a real signal splits tone energy between the +f and -f bins
    want_power = 2.0 * goertzel_power(window, rate, expect_hz)

    total_power = level * level
    residual = max(total_power - want_power, 1e-12)
    snr_db = 10.0 * math.log10(max(want_power, 1e-12) / residual)

    details.update({
        "expect_hz": expect_hz,
        "peak_hz": round(peak_hz, 1),
        "snr_db": round(snr_db, 2),
    })

    drift = abs(peak_hz - expect_hz)
    if drift > tolerance_hz:
        return False, (f"Wrong tone: expected {expect_hz:.0f} Hz, strongest component "
                       f"is {peak_hz:.0f} Hz (off by {drift:.0f} Hz, tolerance "
                       f"{tolerance_hz:.0f} Hz). RMS {level:.4f} over {duration:.2f}s."), details

    if snr_db < min_snr_db:
        return False, (f"Tone at {expect_hz:.0f} Hz is too weak to trust: SNR {snr_db:.1f} dB "
                       f"< {min_snr_db:.1f} dB. Something is making noise, but it is not a "
                       f"clean tone."), details

    return True, (f"Tone verified: {peak_hz:.0f} Hz (expected {expect_hz:.0f} Hz), "
                  f"SNR {snr_db:.1f} dB, RMS {level:.4f}, {duration:.2f}s captured "
                  f"at {rate} Hz."), details


def detect_tone_sequence(path, expect_hz_list, tolerance_hz=60.0, min_snr_db=10.0,
                         window_seconds=0.4, hop_seconds=0.2, silence_rms=0.005):
    """Check `path` contains ALL of expect_hz_list as tones (best-SNR window per frequency) -> (ok, message, details_dict); details_dict["tones"] = {expected Hz: best-window result}."""
    expect_hz_list = [float(h) for h in expect_hz_list]
    tolerance_hz = float(tolerance_hz)
    min_snr_db = float(min_snr_db)

    parsed, err = read_wav(path)
    if err:
        return False, err, {}
    samples, rate = parsed
    duration = len(samples) / float(rate)

    width = int(rate * window_seconds)
    hop = max(1, int(rate * hop_seconds))
    if width <= 0 or len(samples) <= width:
        return False, (f"Capture too short ({duration:.2f}s) for a "
                       f"{window_seconds:.2f}s analysis window"), {}

    per_tone = {hz: {"snr_db": -1e9, "peak_hz": 0.0, "offset_s": 0.0, "rms": 0.0}
               for hz in expect_hz_list}

    for start in range(0, len(samples) - width + 1, hop):
        window = samples[start:start + width]
        level = rms(window)
        if level < silence_rms:
            continue
        total_power = level * level

        powers = {hz: 2.0 * goertzel_power(window, rate, hz) for hz in expect_hz_list}
        for hz in expect_hz_list:
            want_power = powers[hz]
            # residual against the other candidates only (see docstring)
            other_power = sum(p for h, p in powers.items() if h != hz)
            residual = max(other_power, total_power - want_power, 1e-12)
            snr_db = 10.0 * math.log10(max(want_power, 1e-12) / residual)
            if snr_db > per_tone[hz]["snr_db"]:
                peak_hz, _ = find_peak_freq(window, rate,
                                            lo_hz=max(100, hz - 500), hi_hz=hz + 500)
                per_tone[hz] = {"snr_db": round(snr_db, 2), "peak_hz": round(peak_hz, 1),
                                "offset_s": round(start / float(rate), 3),
                                "rms": round(level, 5)}

    details = {"path": path, "rate": rate, "duration_s": round(duration, 3), "tones": per_tone}

    missing = []
    for hz in expect_hz_list:
        info = per_tone[hz]
        drift = abs(info["peak_hz"] - hz)
        if info["snr_db"] < min_snr_db or drift > tolerance_hz:
            missing.append(
                f"{hz:.0f} Hz: best window found {info['peak_hz']:.0f} Hz "
                f"(off by {drift:.0f} Hz) at SNR {info['snr_db']:.1f} dB "
                f"(need <= {tolerance_hz:.0f} Hz drift, >= {min_snr_db:.1f} dB SNR)")

    if missing:
        return False, ("Tone sequence incomplete over " + f"{duration:.2f}s capture:\n  "
                       + "\n  ".join(missing)), details

    summary = "; ".join(f"{hz:.0f}Hz@{per_tone[hz]['offset_s']:.2f}s "
                        f"(SNR {per_tone[hz]['snr_db']:.1f}dB)" for hz in expect_hz_list)
    return True, (f"All {len(expect_hz_list)} tones verified over {duration:.2f}s: "
                  f"{summary}"), details


"""Disk-artifact repository.

An "artifact" is a frozen, reusable DOS directory tree , used for capturing compiler output. 
Each artifact is stored as a compressed tarball of
the tree's *contents* plus a small JSON manifest describing where on a disk it
belongs (its DOS dest dir) and how it was captured.

This generalizes the single bundled `dos_boot_assets/` (the C:\\ DOS system) to
any number of named, injectable trees — so a built C:\\WATCOM can be stuffed
into a fresh or existing disk image the same way C:\\DOS is, at disk-creation or
disk-edit time, without re-running the installer.

All disk I/O goes through mtools. The build disk is a
partitioned FAT with the partition at byte offset 32256 (sector 63), matching
qemuhelpers.copy_to_fat_image.
"""
# captured artifacts live under the test root (/testsrc), not pyhelpers/
ARTIFACT_DIR = os.path.join(_TESTSRC_ROOT, "disk_artifacts")
DEFAULT_OFFSET = 32256

# v2 tarball layout: captured tree under tree/, root DOS boot config files under boot/
_TREE_SUBDIR = "tree"
_BOOT_SUBDIR = "boot"
_ROOT_BOOTFILES = ("CONFIG.SYS", "AUTOEXEC.BAT")


def _safe_name(name):
    """Artifact name → filesystem/DOS-safe slug."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(name or "")).strip("-_")[:32]


def _manifest_path(name):
    return os.path.join(ARTIFACT_DIR, name + ".json")


def _tarball_path(name):
    return os.path.join(ARTIFACT_DIR, name + ".tar.gz")


def _detect_offset(img_path):
    """Byte offset of the FAT filesystem in a disk image: first partition start (LBA*512) if MBR, else 0 (unpartitioned floppy)."""
    try:
        with open(img_path, "rb") as f:
            f.seek(0x1FE)
            if f.read(2) != b"\x55\xAA":
                return 0
            f.seek(0x1C2)
            ptype = f.read(1)[0]
            f.seek(0x1C6)
            start_lba = int.from_bytes(f.read(4), "little")
            if ptype != 0 and start_lba != 0:
                return start_lba * 512
    except (OSError, IndexError):
        pass
    return 0


def _mtoolsrc(img_path, drive="h", offset=DEFAULT_OFFSET):
    f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".mtoolsrc")
    f.write(f'drive {drive}: file="{img_path}" offset={offset}\n')
    f.close()
    return f.name


def _run(cmd, env=None):
    """Run a shell command (bash; stdin=DEVNULL + start_new_session) -> (returncode, combined_output)."""
    r = subprocess.run(cmd, shell=True, executable="/bin/bash", env=env,
                       stdin=subprocess.DEVNULL, start_new_session=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def _is_qcow2(path):
    """True if the file is a QCOW2 image (mtools reads raw only)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"QFI\xfb"
    except OSError:
        return False


def _to_raw(img_path):
    """QCOW2 -> temp raw image next to the source -> (raw_path, True); else (img_path, False). Callers unlink the raw when True; for writes call _commit_raw first."""
    if not _is_qcow2(img_path):
        return img_path, False
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".raw.tmp",
        dir=os.path.dirname(os.path.abspath(img_path)))
    tmp.close()
    r = subprocess.run(["qemu-img", "convert", "-O", "raw", img_path, tmp.name],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise RuntimeError("qemu-img convert (qcow2→raw) failed: "
                           + r.stdout.decode("utf-8", "replace"))
    return tmp.name, True


def _commit_raw(raw_path, qcow2_path):
    """Fold a modified raw image back into the QCOW2 it came from."""
    r = subprocess.run(["qemu-img", "convert", "-O", "qcow2", raw_path, qcow2_path],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise RuntimeError("qemu-img convert (raw→qcow2) failed: "
                           + r.stdout.decode("utf-8", "replace"))


def _dir_tarball_size(name):
    tp = _tarball_path(name)
    return os.path.getsize(tp) if os.path.isfile(tp) else 0


def list_artifacts():
    """Every artifact's manifest plus `_tarball_exists` and `_tarball_size` (bytes), sorted by name."""
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    out = []
    for fn in sorted(os.listdir(ARTIFACT_DIR)):
        if not fn.endswith(".json"):
            continue
        name = fn[:-5]
        try:
            with open(_manifest_path(name)) as f:
                man = json.load(f)
        except (OSError, ValueError):
            continue
        man["_tarball_exists"] = os.path.isfile(_tarball_path(name))
        man["_tarball_size"] = _dir_tarball_size(name)
        out.append(man)
    return out


def get_artifact(name):
    name = _safe_name(name)
    if not os.path.isfile(_manifest_path(name)):
        return None
    with open(_manifest_path(name)) as f:
        man = json.load(f)
    man["_tarball_exists"] = os.path.isfile(_tarball_path(name))
    man["_tarball_size"] = _dir_tarball_size(name)
    return man


def capture_artifact(name, source_img, dos_dir, dest=None, description="",
                     offset=None, overwrite=False):
    """Freeze a directory tree from a DOS disk image into an artifact (<name>.tar.gz + <name>.json manifest) -> (success, message).
      name        artifact id (slugified)
      source_img  disk image to read
      dos_dir     directory on the image, relative to the C: root (e.g. "WATCOM")
      dest        DOS dir to inject into later (default dos_dir)
    """
    name = _safe_name(name)
    if not name:
        return False, "invalid/empty artifact name"
    source_img = os.path.abspath(str(source_img))
    dos_dir = str(dos_dir).strip("/").strip("\\").replace("\\", "/")
    dest = (str(dest).strip("/").strip("\\").replace("\\", "/")) if dest else dos_dir
    if not dos_dir:
        return False, "dos_dir is required (the C:\\<dir> to capture)"
    if not os.path.isfile(source_img):
        return False, f"source image not found: {source_img}"
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    if os.path.isfile(_tarball_path(name)) and not overwrite:
        return False, f"artifact '{name}' already exists (pass overwrite=True to replace)"

    try:
        raw, is_qcow = _to_raw(source_img)
    except RuntimeError as e:
        return False, str(e)
    if offset is None:
        offset = _detect_offset(raw)
    cfg = _mtoolsrc(raw, "h", offset)
    env = {**os.environ, "MTOOLSRC": cfg}
    stage = tempfile.mkdtemp(prefix=f"artifact_{name}_")
    tree_stage = os.path.join(stage, _TREE_SUBDIR)
    boot_stage = os.path.join(stage, _BOOT_SUBDIR)
    os.makedirs(tree_stage)
    os.makedirs(boot_stage)
    try:
        # copy the tree's contents into tree/ (mcopy -s -n -o)
        rc, out = _run(f'mcopy -n -o -s h:/{dos_dir}/* "{tree_stage}"/', env=env)
        # mcopy fails on an empty dir: error only if the source dir is missing
        if not os.listdir(tree_stage):
            rc_check, _ = _run(f'mdir h:/{dos_dir} >/dev/null 2>&1', env=env)
            if rc_check != 0:
                return False, f"C:\\{dos_dir} not found on {os.path.basename(source_img)}:\n{out}"

        # root DOS boot config files
        bootfiles = []
        for bf in _ROOT_BOOTFILES:
            _run(f'mcopy -n -o "h:/{bf}" "{boot_stage}"/', env=env)
            # normalize 8.3 names to upper
            for got in os.listdir(boot_stage):
                if got.upper() == bf and got != bf:
                    os.rename(os.path.join(boot_stage, got), os.path.join(boot_stage, bf))
            if os.path.isfile(os.path.join(boot_stage, bf)):
                bootfiles.append(bf)

        tarball = _tarball_path(name)
        with tarfile.open(tarball, "w:gz") as tf:
            tf.add(tree_stage, arcname=_TREE_SUBDIR)
            if bootfiles:
                tf.add(boot_stage, arcname=_BOOT_SUBDIR)

        file_count = sum(len(files) for _, _, files in os.walk(tree_stage))
        manifest = {
            "name": name,
            "dest": dest,
            "description": description or "",
            "layout": "v2",
            "boot": False,
            "bootfiles": bootfiles,
            "captured_from": os.path.basename(source_img),
            "captured_dos_dir": dos_dir,
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "file_count": file_count,
        }
        with open(_manifest_path(name), "w") as f:
            json.dump(manifest, f, indent=2)

        bf_note = f", +{'/'.join(bootfiles)}" if bootfiles else ""
        return True, (f"Captured artifact '{name}' (C:\\{dos_dir} → dest C:\\{dest}, "
                      f"{file_count} files{bf_note}, {_dir_tarball_size(name)} bytes tarball)")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            os.unlink(cfg)
        except OSError:
            pass
        if is_qcow:
            try:
                os.unlink(raw)
            except OSError:
                pass


def inject_artifact(name, hdd_img_path, dest=None, offset=None, apply_bootfiles=False):
    """Extract an artifact's tarball and mcopy it into a disk image at its DOS dest dir (fresh or existing disk). apply_bootfiles: also write the root boot config (CONFIG.SYS/AUTOEXEC.BAT), overwriting."""
    name = _safe_name(name)
    man = get_artifact(name)
    if not man:
        return False, f"artifact '{name}' not found"
    if not man.get("_tarball_exists"):
        return False, f"artifact '{name}' has no tarball on disk"
    hdd_img_path = os.path.abspath(str(hdd_img_path))
    if not os.path.isfile(hdd_img_path):
        return False, f"disk image not found: {hdd_img_path}"
    dest = (str(dest).strip("/").strip("\\").replace("\\", "/")) if dest else man["dest"]
    dest = dest.strip("/")
    if not dest:
        return False, "no dest dir (artifact manifest has no dest)"

    try:
        raw, is_qcow = _to_raw(hdd_img_path)
    except RuntimeError as e:
        return False, str(e)
    if offset is None:
        offset = _detect_offset(raw)
    cfg = _mtoolsrc(raw, "h", offset)
    env = {**os.environ, "MTOOLSRC": cfg}
    stage = tempfile.mkdtemp(prefix=f"inject_{name}_")
    try:
        with tarfile.open(_tarball_path(name), "r:gz") as tf:
            tf.extractall(stage)
        # v2: tree/ + boot/; v1 (no "layout"): tree at top level, no boot files
        if man.get("layout") == "v2":
            tree_dir = os.path.join(stage, _TREE_SUBDIR)
            boot_dir = os.path.join(stage, _BOOT_SUBDIR)
        else:
            tree_dir, boot_dir = stage, None
        if not os.path.isdir(tree_dir) or not os.listdir(tree_dir):
            return False, f"artifact '{name}' tarball is empty"
        # create dest dir (ignore "already exists"), then copy contents in
        _run(f'mmd h:/{dest}', env=env)
        rc, out = _run(f'mcopy -n -o -s "{tree_dir}"/* h:/{dest}/', env=env)
        if rc != 0:
            hint = ""
            if "full" in out.lower():
                need = sum(os.path.getsize(os.path.join(r, fn))
                           for r, _, fs in os.walk(tree_dir) for fn in fs)
                hint = (f"\nartifact needs ~{need // (1024*1024)}MB — the target disk "
                        f"is too small (partially-copied files may remain in C:\\{dest}).")
            return False, f"inject of '{name}' → C:\\{dest} failed:\n{out}{hint}"

        applied = []
        if apply_bootfiles and boot_dir and os.path.isdir(boot_dir):
            for bf in sorted(os.listdir(boot_dir)):
                r2, o2 = _run(f'mcopy -n -o "{os.path.join(boot_dir, bf)}" h:/', env=env)
                if r2 != 0:
                    return False, f"applying {bf} to C:\\ failed:\n{o2}"
                applied.append(bf)

        if is_qcow:
            _commit_raw(raw, hdd_img_path)
        extra = f" (applied C:\\{', C:\\'.join(applied)})" if applied else ""
        return True, f"Injected artifact '{name}' → C:\\{dest} on {os.path.basename(hdd_img_path)}{extra}"
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            os.unlink(cfg)
        except OSError:
            pass
        if is_qcow:
            try:
                os.unlink(raw)
            except OSError:
                pass


def remove_dir_from_disk(dest, hdd_img_path, offset=None):
    """Delete a directory tree (e.g. an injected artifact's dest) off a disk image -> (success, message)."""
    hdd_img_path = os.path.abspath(str(hdd_img_path))
    if not os.path.isfile(hdd_img_path):
        return False, f"disk image not found: {hdd_img_path}"
    dest = str(dest).strip("/").strip("\\").replace("\\", "/").strip("/")
    if not dest:
        return False, "no dest dir to remove"
    try:
        raw, is_qcow = _to_raw(hdd_img_path)
    except RuntimeError as e:
        return False, str(e)
    if offset is None:
        offset = _detect_offset(raw)
    cfg = _mtoolsrc(raw, "h", offset)
    env = {**os.environ, "MTOOLSRC": cfg}
    try:
        rc, out = _run(f'mdeltree h:/{dest}', env=env)
        if rc != 0:
            return False, f"remove of C:\\{dest} failed (maybe not present):\n{out}"
        if is_qcow:
            _commit_raw(raw, hdd_img_path)
        return True, f"Removed C:\\{dest} from {os.path.basename(hdd_img_path)}"
    finally:
        try:
            os.unlink(cfg)
        except OSError:
            pass
        if is_qcow:
            try:
                os.unlink(raw)
            except OSError:
                pass


def list_disk_toplevel(hdd_img_path, offset=None):
    """Top-level directory names on a disk image's FAT partition -> (ok, list|msg)."""
    hdd_img_path = os.path.abspath(str(hdd_img_path))
    if not os.path.isfile(hdd_img_path):
        return False, f"disk image not found: {hdd_img_path}"
    try:
        raw, is_qcow = _to_raw(hdd_img_path)
    except RuntimeError as e:
        return False, str(e)
    if offset is None:
        offset = _detect_offset(raw)
    cfg = _mtoolsrc(raw, "h", offset)
    env = {**os.environ, "MTOOLSRC": cfg}
    try:
        rc, out = _run("mdir -a h:/", env=env)
        if rc != 0:
            return False, out
        dirs = []
        for line in out.splitlines():
            # mtools dir line: "NAME         <DIR>     date time"
            m = re.match(r"^(\S.*?)\s+<DIR>", line)
            if m:
                nm = m.group(1).strip()
                if nm not in (".", ".."):
                    dirs.append(nm)
        return True, dirs
    finally:
        try:
            os.unlink(cfg)
        except OSError:
            pass
        if is_qcow:
            try:
                os.unlink(raw)
            except OSError:
                pass
