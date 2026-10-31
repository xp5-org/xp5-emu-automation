import os
import math
import shutil
import sys
import time
import re
import shlex
import socket
import subprocess
import tempfile
import signal
import numpy as np
import pexpect
from PIL import Image

IS_WINDOWS = sys.platform.startswith("win")
# test_hostbuild `command` is POSIX shell; run via MSYS2 bash.exe on Windows (cmd.exe cannot parse it)
WINDOWS_BASH_BIN = r"C:\msys64\usr\bin\bash.exe"

TESTSRC_BASEDIR = os.environ.get("TESTSRC_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTSRC_HELPERDIR = os.path.join(TESTSRC_BASEDIR, "pyhelpers")
if TESTSRC_HELPERDIR not in sys.path:
    sys.path.insert(0, TESTSRC_HELPERDIR)

from qemuhelpers import (
    copy_to_fat_image,
    copy_from_fat_image,
    ocr_word_find,
    phrase_list,
    ocr_log_dir,
    ppdcompile,
    convert_raw_to_qcow2,
    create_overlay_image,
    overlay_backing_file,
    create_fat_disk_image,
    create_disk_image,
    convert_qcow2_to_raw,
    make_disk_image,
    find_button_in_screenshot,
    find_icon_in_screenshot,
)
from mediahelpers import detect_tone, detect_tone_sequence
TEMPLATES_DIR = os.path.join(TESTSRC_BASEDIR, "templates")
from qemuhelpers import QemuInstance
from qemuhelpers import hmp_error
import snaptimeline
import guiclickhelpers
from qemuhelpers import MouseAction
from simhhelpers import NovaSimhInstance, NovaTermProcess, compile_via_nova_llvm
from novafbhelpers import DEFAULT_LIVE_PATH
from novafbhelpers import NovaFbMonitorProcess
from verilatorvgahelpers import (VerilatorLiveInstance,
                                 DEFAULT_LIVE_PATH as VERILATOR_DEFAULT_LIVE_PATH,
                                 VerilatorFbMonitorProcess)
from basiliskhelpers import BasiliskInstance
from dosboxhelpers import DosboxInstance, DosboxConf
from box86helpers import Box86Instance
from pcsx2helpers import Pcsx2Instance
from xemuhelpers import XemuInstance
from apphelpers import init_test_env
from mediahelpers import GifRecorder, active_recorders, stop_all as stop_all_gif_recorders


GUI_INSTANCE_TYPES = (DosboxInstance, Box86Instance, Pcsx2Instance, XemuInstance)
OCR_INSTANCE_TYPES = (QemuInstance, BasiliskInstance) + GUI_INSTANCE_TYPES


# the runner re-executes this file on a dispatch reload, but plain imports would hand back the cached backend
# modules; reload them (common first) so edits to dispatch_*.py take effect the same way
import importlib
for _modname in ("dispatch_common", "dispatch_cc65", "dispatch_vice", "dispatch_atari", "dispatch_apple"):
    if _modname in sys.modules:
        importlib.reload(sys.modules[_modname])

# shared primitives live in dispatch_common so the dispatch_*.py backend modules can use them too
from dispatch_common import dispatchtest_step, build_vars, resolve_path, EXTRA_TERMINABLE
# per-backend step modules (CC65 builds, VICE, Hatari, Apple II): their @dispatchtest_step functions are re-exported
# here because the runner loads only this file's namespace
from dispatch_cc65 import *
from dispatch_vice import *
from dispatch_atari import *
from dispatch_apple import *

def _next_window_slot(context):
    """Round-robin tile slot for this run's GUI windows, counted in the shared `context` (windowlayout.tile_position)."""
    slot = context.get("_window_slot_count", 0) if context is not None else 0
    if context is not None:
        context["_window_slot_count"] = slot + 1
    return slot


@dispatchtest_step
def test_sendkeyboardinput(inputstring, name="qemu1", takeascreenshot=True, **kwargs):
    log = []
    context = kwargs.get("context")
    instance_name = name or "qemu1"
    instance = context.get(instance_name)
    if not instance:
        return False, f"No QEMU instance '{instance_name}' available in context"

    instance.send_keyboardstring(inputstring)
    log.append(f"{instance_name} Sent string:\n{inputstring}")
    instance.send_keyboardstring("\n")
    time.sleep(0.5)

    _maybe_screenshot(instance, takeascreenshot, log, label=instance_name)

    return True, "\n".join(log)


def _as_bool(v):
    """catch python true/false as strings and return bool type, used with JS & Python exchange"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _as_list(v):
    """Config value -> list of non-empty strings (list-valued params prom_env, extra_args, extra_disks travel to/from the testbuilder as one comma-joined string)."""
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [part.strip() for part in str(v).split(",") if part.strip()]


def _log_ocr_text(instance, log, label=None):
    """OCR the current screen of `instance` into the step log (every screenshot-taking step). Instances without read_screen() (QEMU, Basilisk) are skipped; they get OCR through test_ocrwordsearch."""
    if not hasattr(instance, "read_screen"):
        return
    name = label or getattr(instance, "name", "instance")
    ok, text = instance.read_screen()
    log.append(f"{name} OCR:\n{text}" if ok else f"{name} OCR failed: {text}")


def _screenshot_and_ocr(instance, log, label=None):
    """Screenshot one instance for the current step number, OCR it, append both to `log` (test_sendcommand / test_86box_monitor)."""
    name = label or getattr(instance, "name", "instance")
    ok, shot = instance.take_screenshot(test_step=_current_stepnum())
    log.append(f"[{name}] screenshot: {shot}" if ok
               else f"[{name}] screenshot failed: {shot}")
    if ok:
        _log_ocr_text(instance, log, name)
    return ok


def _maybe_screenshot(instance, flag, log, label=None):
    """Per-step screenshot, driven by the step's `takeascreenshot` checkbox."""
    if not _as_bool(flag):
        return True
    name = label or getattr(instance, "name", "instance")
    if not hasattr(instance, "take_screenshot"):
        log.append(f"{name}: no screenshot capability, skipped")
        return True
    result = instance.take_screenshot()
    ok = result[0] if isinstance(result, tuple) else bool(result)
    log.append(f"Screenshot for {name} taken" if ok
               else f"Screenshot for {name} failed")
    if ok:
        _log_ocr_text(instance, log, name)
    return bool(ok)


def _reuse_or_take_screenshot(instance, ocr_screenshot_path, flag, log, label=None):
    """Report picture for an OCR step."""
    if not _as_bool(flag):
        return True
    name = label or getattr(instance, "name", "instance")
    if not ocr_screenshot_path or not os.path.exists(ocr_screenshot_path):
        return _maybe_screenshot(instance, flag, log, label=label)

    helperdir = "/testrunnerapp"
    if helperdir not in sys.path:
        sys.path.insert(0, helperdir)
    from appstate import progress_state, current_reports_dir
    m = re.match(r'\d+', progress_state.step or "")
    stepnum = m.group(0) if m else "live"

    reports_dir = current_reports_dir()
    dest_name = f"screenshot-{name}-{stepnum}-{instance.screenshot_count}.png"
    dest_path = os.path.join(reports_dir, dest_name)
    try:
        shutil.copy2(ocr_screenshot_path, dest_path)
    except OSError as e:
        log.append(f"{name}: failed to reuse OCR screenshot: {e}")
        return True
    instance.screenshot_count += 1
    log.append(f"Screenshot for {name} reused from OCR capture")
    return True


@dispatchtest_step
def test_sendspecialkey(key=None, name="qemu1", alt=False, ctrl=False, shift=False,
                        delay=0.1, takeascreenshot=False, **kwargs):
    """Send one special/function key (f1..f12, ret/enter, esc, ...) with optional modifiers via the monitor."""
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    if not key:
        return False, "no key specified"

    instance.send_specialkeys(key, ctrl=_as_bool(ctrl), alt=_as_bool(alt),
                              shift=_as_bool(shift), delay=float(delay))
    mods = "".join(m for m, f in (("ctrl-", ctrl), ("alt-", alt), ("shift-", shift)) if _as_bool(f))
    log = [f"{name} sent key: {mods}{key}"]
    _maybe_screenshot(instance, takeascreenshot, log, label=name)
    return True, "\n".join(log)


@dispatchtest_step
def test_sendspecialkeys(keys=None, name="qemu1", delay=0.5, takeascreenshot=True, **kwargs):
    """Send a sequence of special/function keys; `keys` = list of tokens, each followed by `delay` s. Token may carry modifiers in QEMU sendkey form ("f3", "ret", "alt-f", "ctrl-alt-del"); a comma/space separated string is accepted ("f3, ret, f, ret")."""
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    if not keys:
        return False, "no keys specified"
    if isinstance(keys, str):
        keys = [k for k in re.split(r"[,\s]+", keys.strip()) if k]

    delay = float(delay)
    for k in keys:
        instance.send_specialkeys(str(k), delay=delay)
    log = [f"{name} sent key sequence: {' '.join(str(k) for k in keys)}"]
    _maybe_screenshot(instance, takeascreenshot, log, label=name)
    return True, "\n".join(log)


@dispatchtest_step
def test_dirandfilesto_hddimg(sourcecode_dir=None, hdd_img_path=None, dest_dir="src", **kwargs):
    log = []
    context = kwargs.get("context")
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    if sourcecode_dir is None:
        sourcecode_dir = "{projbasedir}{projdir}/{src_dir}"
    if hdd_img_path is None:
        hdd_img_path = "{projbasedir}{projdir}/{hdd1_img}"

    sourcecode_dir = resolve_path(sourcecode_dir, vars_map)
    hdd_img_path = resolve_path(hdd_img_path, vars_map)
    dest_dir = resolve_path(dest_dir, vars_map)

    success, output = copy_to_fat_image(sourcecode_dir, hdd_img_path, dest_dir=dest_dir)
    log.append(output)

    if not success:
        context["abort"] = True
        return False, "\n".join(log)

    return success, "\n".join(log)


@dispatchtest_step
def test_rebuild_srcdisk(srcdisk_path=None, srcdisk_size_mb=None, sourcecode_dir=None, **kwargs):
    """Recreate D: from scratch and repopulate it from sourcecode_dir (as _prepare_srcdisk for hdd2_prepare)."""
    log = []
    context = kwargs.get("context")
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    srcdisk_path = resolve_path(srcdisk_path, vars_map)
    sourcecode_dir = resolve_path(
        sourcecode_dir or "{projbasedir}{projdir}/{src_dir}", vars_map)
    srcdisk_size_mb = resolve_path(
        srcdisk_size_mb or config.get("srcdisk_size_mb") or 512, vars_map)

    if not srcdisk_path:
        return False, "test_rebuild_srcdisk: srcdisk_path is required"

    if not _prepare_srcdisk(srcdisk_path, srcdisk_size_mb, sourcecode_dir, log):
        context["abort"] = True
        return False, "\n".join(log)

    return True, "\n".join(log)


def _rebuild_floppy_windows(floppy_path, sourcecode_dir, size_kb, label, dest_dir, context):
    """test_rebuild_floppy on Windows: runs truncate/mformat/mcopy through MSYS2's bash (mtools -i flag). Needs `pacman -S mingw-w64-ucrt-x86_64-mtools`."""
    def msys(p):
        return os.path.abspath(p).replace("\\", "/")

    if not (sourcecode_dir and os.path.isdir(sourcecode_dir)):
        context["abort"] = True
        return False, f"no sourcecode_dir to copy (looked for {sourcecode_dir!r})"
    img = shlex.quote(msys(floppy_path))
    src = shlex.quote(msys(sourcecode_dir))
    dest = str(dest_dir or "").strip("/")
    fmt = f"mformat -i {img} -f {int(size_kb)}" + (f" -v {shlex.quote(label)}" if label else "") + " ::"
    mkd = f"mmd -i {img} ::/{shlex.quote(dest)} && " if dest else ""
    command = (f"set -e; umask 000; command -v mformat >/dev/null || "
               f"{{ echo 'mtools missing -- run: pacman -S mingw-w64-ucrt-x86_64-mtools'; exit 127; }}; "
               f"mkdir -p \"$(dirname {img})\"; rm -f {img}; truncate -s {int(size_kb)}K {img}; "
               f"{fmt}; {mkd}mcopy -i {img} -s -o -n {src}/* ::/{shlex.quote(dest)}; mdir -i {img} ::/{shlex.quote(dest)}")
    env = os.environ.copy()
    env.setdefault("MSYSTEM", "UCRT64")
    r = subprocess.run([WINDOWS_BASH_BIN, "-lc", command], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=300)
    out = f"$ {command}\n" + r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        context["abort"] = True
        return False, out + f"\nexit {r.returncode}"
    return True, out


@dispatchtest_step
def test_rebuild_floppy(floppy_path=None, sourcecode_dir=None, size_kb=1440, label="",
                        dest_dir="", **kwargs):
    """Recreate a blank FAT12 floppy and copy sourcecode_dir into it (floppy counterpart of test_rebuild_srcdisk, payload on B:). Files go to the floppy root unless dest_dir names a subdirectory."""
    log = []
    context = kwargs.get("context")
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    floppy_path = resolve_path(floppy_path, vars_map)
    sourcecode_dir = resolve_path(sourcecode_dir, vars_map)
    size_kb = resolve_path(size_kb, vars_map)
    label = resolve_path(label, vars_map)
    dest_dir = resolve_path(dest_dir, vars_map)

    if not floppy_path:
        return False, "test_rebuild_floppy: floppy_path is required"

    if IS_WINDOWS:
        return _rebuild_floppy_windows(floppy_path, sourcecode_dir, size_kb, label,
                                       dest_dir, context)

    success, output = create_disk_image(floppy_path, size_kb=size_kb, media="floppy",
                                        label=label, overwrite=True)
    log.append(output)
    if not success:
        context["abort"] = True
        return False, "\n".join(log)

    if not (sourcecode_dir and os.path.isdir(sourcecode_dir)):
        log.append(f"no sourcecode_dir to copy (looked for {sourcecode_dir!r})")
        context["abort"] = True
        return False, "\n".join(log)

    success, output = copy_to_fat_image(sourcecode_dir, floppy_path, dest_dir=dest_dir, offset=0)
    log.append(output)
    if not success:
        context["abort"] = True
    return success, "\n".join(log)


@dispatchtest_step
def test_extract_from_hddimg(hdd_img_path=None, src_dos_dir="src", dest_dir=None, **kwargs):
    """Pull build output out of a disk image the emulator wrote (e.g. D: from test_dirandfilesto_hddimg / _prepare_srcdisk) into a host directory; each file logged as ARTIFACT: for the Build Artifacts table."""
    log = []
    context = kwargs.get("context")
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    if hdd_img_path is None:
        hdd_img_path = "{projbasedir}{projdir}/{srcdisk_img}"
    if dest_dir is None:
        dest_dir = "{projbasedir}{projdir}/{src_dir}"

    hdd_img_path = resolve_path(hdd_img_path, vars_map)
    src_dos_dir = resolve_path(src_dos_dir, vars_map)
    dest_dir = resolve_path(dest_dir, vars_map)

    if not hdd_img_path or not os.path.isfile(hdd_img_path):
        return False, f"test_extract_from_hddimg: image not found: {hdd_img_path!r}"

    def _snapshot():
        snap = {}
        if os.path.isdir(dest_dir):
            for root, _dirs, files in os.walk(dest_dir):
                for fn in files:
                    p = os.path.join(root, fn)
                    snap[p] = os.path.getmtime(p)
        return snap

    before = _snapshot()

    success, output = copy_from_fat_image(src_dos_dir, dest_dir, hdd_img_path)
    log.append(output)
    if not success:
        context["abort"] = True
        return False, "\n".join(log)

    after = _snapshot()

    new_or_changed = sorted(p for p, mtime in after.items()
                             if p not in before or mtime != before[p])
    if not new_or_changed:
        log.append(f"no new/changed files found under {dest_dir}")
    for p in new_or_changed:
        log.append(f"ARTIFACT: {p}")

    return True, "\n".join(log)


def _prepare_hdd_overlay(hdd1_template, hdd1_overlay_path, log, persist=False):
    """C: as a COW overlay on a shared read-only template -> True/False; detail appended to `log`.
    Default: rebuilt each run (pristine template). persist=True (config "hdd1_persist"): existing overlay writes retained; its recorded backing file is checked first, and a mismatch (hdd1_template changed) rebuilds.
    hdd1_template: bare filename under /testsrc/templates.
    """
    if os.sep not in str(hdd1_template):
        hdd1_template = os.path.join(TEMPLATES_DIR, hdd1_template)
    hdd1_template = os.path.abspath(hdd1_template)

    if persist and os.path.isfile(hdd1_overlay_path):
        backing = overlay_backing_file(hdd1_overlay_path)
        if backing == hdd1_template:
            log.append(f"C: keeping existing overlay (hdd1_persist): "
                       f"{hdd1_overlay_path} on {os.path.basename(hdd1_template)} "
                       f"— writes from previous runs are preserved")
            return True
        log.append(f"C: overlay {hdd1_overlay_path} is backed by {backing!r}, not the "
                   f"requested {hdd1_template!r} — rebuilding it despite hdd1_persist")

    success, output = create_overlay_image(hdd1_template, hdd1_overlay_path, overwrite=True)
    log.append(output)
    return success


def _prepare_srcdisk(srcdisk_path, srcdisk_size_mb, sourcecode_dir, log,
                     srcdisk_label="SRC"):
    """D: as a raw FAT16 data disk (source in, build output out), recreated on every start; raw so mtools and the host read/write it directly -> True/False; detail appended to `log`."""
    success, output = create_fat_disk_image(
        srcdisk_path, srcdisk_size_mb, label=srcdisk_label,
        make_src_dir=True, overwrite=True, system=False,
    )
    log.append(output)
    if not success:
        return False

    if sourcecode_dir and os.path.isdir(sourcecode_dir):
        success, output = copy_to_fat_image(sourcecode_dir, srcdisk_path, dest_dir="src")
        log.append(output)
        return success

    log.append(f"no sourcecode_dir to copy (looked for {sourcecode_dir!r})")
    return True


@dispatchtest_step
def test_convert_hddimg_to_hddqcow(hdd_img_path=None, hdd_qcow_path=None, **kwargs):
    log = []
    context = kwargs.get("context")
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    hdd_img_path = resolve_path(hdd_img_path, vars_map)
    hdd_qcow_path = resolve_path(hdd_qcow_path, vars_map)

    success, output = convert_raw_to_qcow2(hdd_img_path, hdd_qcow_path)
    log.append(output)

    if not success:
        context["abort"] = True
        return False, "\n".join(log)

    return success, "\n".join(log)


def _ocr_instances(context, name=None):
    """Target instance names for OCR (qemuinstance, dosbox instance) -> (list, error)."""
    if name:
        inst = context.get(name)
        if not isinstance(inst, OCR_INSTANCE_TYPES):
            return None, f"No OCR-capable instance '{name}' available in context"
        return [(name, inst)], None

    found = []
    for key, inst in context.items():
        if isinstance(inst, OCR_INSTANCE_TYPES) and not any(inst is f for _, f in found):
            found.append((key, inst))
    return found, None


def _ocr_schedule(numberofattempts, attemptdelay, timeout, poll):
    """OCR schedule: number of attempts, delay between attempts, total timeout for the step."""
    if timeout not in (None, ""):
        delay = float(poll) if poll not in (None, "") else 2.0
        delay = max(delay, 0.1)
        attempts = int(math.ceil(float(timeout) / delay))
        return max(1, attempts), delay

    attempts = int(numberofattempts) if numberofattempts not in (None, "") else 1
    delay = float(attemptdelay) if attemptdelay not in (None, "") else 3.0
    return max(1, attempts), delay


def _resolve_phrases(value, vars_map):
    """phrase_list() + {token} resolution on each entry."""
    return [p for p in (resolve_path(v, vars_map) for v in phrase_list(value)) if p]


@dispatchtest_step
def test_ocrwordsearch(successphrase=None, failphrase=None, whilephrase=None,
                       notphrase=None,
                       numberofattempts=None, attemptdelay=None,
                       timeout=None, poll=None, name=None,
                       startx=None, starty=None, stopx=None, stopy=None,
                       require_success=False, takeascreenshot=True,
                       screenshot_all=False, **kwargs):
    """Poll an instance's screen with OCR until successphrase appears (the screen-wait for every backend with a screen).
      successphrase   string or list ("+ and"): ALL must be on screen at once
      notphrase       string or list ("+ not"): while ANY is on screen the step does not succeed (never fails by itself; keeps polling); whilephrase = old name
      failphrase      seen on screen: always fails
      name            targets one instance
      startx/starty/stopx/stopy   crop before OCR
      require_success a missing successphrase FAILs and aborts the run
      CONFIG["ocr_scale"]   upscales every screenshot 2 or 4
      screenshot_all  with `name`: also screenshot+OCR-log every OTHER OCR-capable instance for the report (no gating); off by default
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    require_success = _as_bool(require_success)
    attempts_n, delay = _ocr_schedule(numberofattempts, attemptdelay, timeout, poll)

    # resolve {token} in the phrases like the other phrase-matching steps
    successphrase = _resolve_phrases(successphrase, vars_map)
    failphrase = resolve_path(failphrase, vars_map)
    notphrase = _resolve_phrases(notphrase, vars_map) + _resolve_phrases(whilephrase, vars_map)
    name = resolve_path(name, vars_map) or None

    targets, err = _ocr_instances(context, name)
    if err:
        return False, err

    log = []
    abort = False
    ocr_ok = True
    saw_failphrase = False

    for label, instance in targets:
        success, screentext, attempts, ocrlog, ocr_screenshot_path = ocr_word_find(
            instance,
            successphrase,
            numberofattempts=attempts_n,
            attemptdelay=delay,
            startx=startx,
            starty=starty,
            stopx=stopx,
            stopy=stopy,
            errorphrase=failphrase,
            notphrase=notphrase,
            should_stop=lambda: context.get("abort") is True,
        )
        ocr_ok = ocr_ok and bool(success)
        if failphrase and failphrase.lower() in (screentext or "").lower():
            saw_failphrase = True

        log.append(f"{label} screentext:\n{screentext}")
        log.append(f"{label} ocrlog:\n{ocrlog}")

        if not _reuse_or_take_screenshot(instance, ocr_screenshot_path, takeascreenshot, log, label=label) and not success:
            abort = True

    if _as_bool(screenshot_all) and _as_bool(takeascreenshot):
        covered = [inst for _, inst in targets]
        for key, inst in context.items():
            if isinstance(inst, OCR_INSTANCE_TYPES) and not any(inst is c for c in covered):
                covered.append(inst)
                _screenshot_and_ocr(inst, log, key)

    if abort:
        context["abort"] = True
        for label, instance in targets:
            log.append(f"Stopping {label}")
            instance.stop()
        return False, "\n".join(log)

    if saw_failphrase:
        context["abort"] = True
        log.append(f"fail phrase '{failphrase}' is on screen — failing step")
        return False, "\n".join(log)

    if not targets:
        log.append("No OCR-capable instances found in context")
        if require_success:
            context["abort"] = True
            return False, "\n".join(log)
        return True, "\n".join(log)

    if require_success and not ocr_ok:
        context["abort"] = True
        log.append(f"OCR did not find success phrase(s) {successphrase} "
                   f"(require_success) — failing step")
        return False, "\n".join(log)

    return True, "\n".join(log)


def ocr_preview(context, config, successphrase=None, failphrase=None,
                whilephrase=None, notphrase=None, name=None, startx=None, starty=None,
                stopx=None, stopy=None, **_ignored):
    """One OCR pass exactly as test_ocrwordsearch would make it, no waiting. Not a step: the live editor previews an OCR step against the running instance while a run is suspended.
    Scheduling params are ignored (single attempt).
    Returns (ok, list_or_error): per target instance {label, text, found, failphrase_seen, whilephrase_seen, verdict, screenshot, scale, log}.
    """
    vars_map = build_vars(config or {})
    successphrase = _resolve_phrases(successphrase, vars_map)
    failphrase = resolve_path(failphrase, vars_map) or None
    notphrase = _resolve_phrases(notphrase, vars_map) + _resolve_phrases(whilephrase, vars_map)
    name = resolve_path(name, vars_map) or None

    targets, err = _ocr_instances(context or {}, name)
    if err:
        return False, err
    if not targets:
        return False, "No OCR-capable instance in the run's context"

    out = []
    for label, instance in targets:
        success, text, _attempts, log, shot = ocr_word_find(
            instance, successphrase, numberofattempts=1, attemptdelay=0,
            startx=startx, starty=starty, stopx=stopx, stopy=stopy,
            errorphrase=failphrase, notphrase=notphrase)
        low = (text or "").lower()
        fail_seen = bool(failphrase and failphrase.lower() in low)
        while_seen = any(p.lower() in low for p in notphrase)
        if fail_seen:
            verdict = "FAIL (failphrase on screen)"
        elif success:
            verdict = "PASS" if successphrase else "PASS (no successphrase: capture only)"
        elif while_seen:
            verdict = "WAIT (a NOT phrase is still on screen -- the step would keep polling)"
        else:
            verdict = "NOT YET (successphrase not on screen -- the step would keep polling)"
        out.append({
            "label": label, "text": text or "",
            "found": bool(success) and not fail_seen,
            "failphrase_seen": fail_seen, "whilephrase_seen": while_seen,
            "verdict": verdict, "screenshot": shot,
            "scale": float(getattr(instance, "screenshot_scale", 1.0) or 1.0),
            "resolved": {"successphrase": successphrase, "failphrase": failphrase,
                         "notphrase": notphrase, "whilephrase": notphrase,
                         "name": name},
            "log": log,
        })
    return True, out


@dispatchtest_step
def test_startqemu(name=None, cpuarch=None, port=55555, floppy1_path=None,
                   floppy1_size=None, floppy2_path=None, floppy2_size=None,
                   hdd1_qcow_path=None, hdd2_path=None,
                   cdrom_path=None, cdrom2_path=None, hdd1_template=None,
                   hdd1_persist=None, srcdisk_size_mb=None,
                   sourcecode_dir=None, memory="4M", vnc_port=None,
                   sound_device=None, audio_backend="wav", audio_out_path=None,
                   pa_out_name=None, pa_in_name=None, pa_server=None,
                   serial_path=None, hdd2_prepare=True,
                   machine=None, cpu=None, vga=None, net_device=None,
                   mac_address=None, hostfwd=None, bios_path=None, boot_order=None,
                   prom_env=None, extra_args=None, extra_disks=None,
                   qemu_binary=None, takeascreenshot=False, loadvm=None,
                   kernel_path=None, dtb_path=None, kernel_append=None, **kwargs):
    log = []
    context = kwargs.get("context")
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    sound_device = resolve_path(sound_device or config.get("sound_device"), vars_map) or None
    audio_backend = resolve_path(audio_backend or config.get("audio_backend") or "wav", vars_map)
    audio_out_path = resolve_path(audio_out_path or config.get("audio_out_path"), vars_map) or None
    pa_out_name = resolve_path(pa_out_name or config.get("pa_out_name"), vars_map) or None
    pa_in_name = resolve_path(pa_in_name or config.get("pa_in_name"), vars_map) or None
    pa_server = resolve_path(pa_server or config.get("pa_server"), vars_map) or None
    serial_path = resolve_path(serial_path or config.get("serial_path"), vars_map) or None
    floppy1_size = resolve_path(floppy1_size or config.get("floppy1_size"), vars_map)
    floppy1_path = resolve_path(floppy1_path or vars_map.get("floppy1_path", ""), vars_map)
    hdd1_qcow_path = resolve_path(hdd1_qcow_path, vars_map)
    hdd2_path = resolve_path(hdd2_path, vars_map) or None
    cdrom_path = resolve_path(cdrom_path, vars_map) or None
    floppy2_path = resolve_path(floppy2_path or config.get("floppy2_path"), vars_map) or None
    floppy2_size = resolve_path(floppy2_size or config.get("floppy2_size"), vars_map)
    cdrom2_path = resolve_path(cdrom2_path or config.get("cdrom2_path"), vars_map) or None
    hdd1_template = resolve_path(hdd1_template or config.get("hdd1_template"), vars_map)
    hdd1_persist = resolve_path(hdd1_persist if hdd1_persist is not None
                                else config.get("hdd1_persist", False), vars_map)
    hdd1_persist = _as_bool(hdd1_persist)
    if hdd1_template and hdd1_qcow_path:
        if not _prepare_hdd_overlay(hdd1_template, hdd1_qcow_path, log,
                                    persist=hdd1_persist):
            context["abort"] = True
            return False, "\n".join(log)

    # hdd2: prepare=True formats a FAT "D:" source/output disk and copies the project source in; prepare=False attaches AS-IS (pre-built template)
    hdd2_prepare = _as_bool(hdd2_prepare)
    if hdd2_path and hdd2_prepare:
        srcdisk_size_mb = resolve_path(
            srcdisk_size_mb or config.get("srcdisk_size_mb") or 512, vars_map)
        sourcecode_dir = resolve_path(
            sourcecode_dir or "{projbasedir}{projdir}/{src_dir}", vars_map)
        if not _prepare_srcdisk(hdd2_path, srcdisk_size_mb, sourcecode_dir, log):
            context["abort"] = True
            return False, "\n".join(log)
    elif hdd2_path:
        log.append(f"  hdb attached as-is (no format): {hdd2_path}")

    # floppy is optional
    if floppy1_path:
        make_disk_image(floppy1_path, floppy1_size)
    else:
        floppy1_path = None
    if floppy2_path and floppy2_size:
        make_disk_image(floppy2_path, floppy2_size)

    memory = resolve_path(memory or config.get("memory") or "4M", vars_map)
    machine = resolve_path(machine or config.get("machine"), vars_map) or None
    cpu = resolve_path(cpu or config.get("cpu"), vars_map) or None
    vga = resolve_path(vga or config.get("vga"), vars_map) or None
    net_device = resolve_path(net_device or config.get("net_device"), vars_map) or None
    mac_address = resolve_path(mac_address or config.get("mac_address"), vars_map) or None
    hostfwd = resolve_path(hostfwd or config.get("hostfwd"), vars_map) or None
    bios_path = resolve_path(bios_path or config.get("bios_path"), vars_map) or None
    boot_order = resolve_path(boot_order or config.get("boot_order"), vars_map) or None

    qemu_binary = resolve_path(qemu_binary or config.get("qemu_binary"), vars_map) or None
    loadvm = resolve_path(loadvm or config.get("loadvm"), vars_map) or None
    kernel_path = resolve_path(kernel_path or config.get("kernel_path"), vars_map) or None
    dtb_path = resolve_path(dtb_path or config.get("dtb_path"), vars_map) or None
    kernel_append = resolve_path(kernel_append or config.get("kernel_append"), vars_map) or None
    prom_env = [resolve_path(v, vars_map)
                for v in _as_list(prom_env if prom_env is not None
                                  else config.get("prom_env"))]
    extra_args = [resolve_path(v, vars_map)
                  for v in _as_list(extra_args if extra_args is not None
                                    else config.get("extra_args"))]
    extra_disks = [resolve_path(v, vars_map)
                   for v in _as_list(extra_disks if extra_disks is not None
                                     else config.get("extra_disks"))]
    
    log.append(f"Starting {name} on port {port} with image={hdd1_qcow_path}")
    if machine or cpu:
        log.append(f"  machine  = {machine or '(qemu default)'}"
                   f" cpu={cpu or '(qemu default)'} vga={vga or '(qemu default)'}")
    if bios_path:
        log.append(f"  bios     = {bios_path}")
    if kernel_path:
        log.append(f"  kernel   = {kernel_path}")
    if dtb_path:
        log.append(f"  dtb      = {dtb_path}")
    if kernel_append:
        log.append(f"  append   = {kernel_append}")
    if qemu_binary:
        log.append(f"  binary   = {qemu_binary}")
    if loadvm:
        log.append(f"  loadvm   = {loadvm} (skipping boot, resuming snapshot)")
    for extra in extra_disks:
        log.append(f"  disk     = {extra}")
    if hdd2_path:
        log.append(f"  hdb (D:) = {hdd2_path}")
    if floppy1_path:
        log.append(f"  fda (A:) = {floppy1_path}")
    if floppy2_path:
        log.append(f"  fdb (B:) = {floppy2_path}")
    if cdrom_path:
        log.append(f"  cdrom    = {cdrom_path}")
    if cdrom2_path:
        log.append(f"  cdrom2   = {cdrom2_path}")
    if hostfwd:
        log.append(f"  hostfwd  = {hostfwd} (user-mode/SLIRP NAT, no host net privileges needed)")
    if serial_path:
        _serial_note = "raw chardev backend" if any(
            serial_path.startswith(p) for p in ("tcp:", "unix:", "socket:", "pipe:")
        ) or serial_path in ("pty", "stdio", "null", "none") else "guest stdout via -serial file:"
        log.append(f"  serial   = {serial_path} ({_serial_note})")
    if sound_device:
        log.append(f"  sound    = {sound_device} via -audiodev {audio_backend}"
                   + (f" -> {audio_out_path}" if audio_backend == "wav" else "")
                   + (f" (out={pa_out_name or 'default'}, in={pa_in_name or 'default'})"
                      if audio_backend == "pa" else ""))
    instance = QemuInstance(name, cpuarch, port, hdd1imagepath=hdd1_qcow_path,
                            hdd2imagepath=hdd2_path, cdrom_path=cdrom_path,
                            cdrom2_path=cdrom2_path,
                            floppy_path=floppy1_path, floppy2_path=floppy2_path,
                            memory=memory, vnc_port=vnc_port,
                            sound_device=sound_device, audio_backend=audio_backend,
                            audio_out_path=audio_out_path,
                            pa_out_name=pa_out_name, pa_in_name=pa_in_name,
                            pa_server=pa_server, serial_path=serial_path,
                            machine=machine, cpu=cpu, vga=vga,
                            net_device=net_device, mac_address=mac_address, hostfwd=hostfwd,
                            bios_path=bios_path, boot_order=boot_order,
                            prom_env=prom_env, extra_args=extra_args,
                            extra_disks=extra_disks,
                            screenshot_scale=config.get("ocr_scale"),
                            qemu_binary=qemu_binary, loadvm=loadvm,
                            kernel_path=kernel_path, dtb_path=dtb_path,
                            kernel_append=kernel_append,
                            window_slot=_next_window_slot(context))

    # write a clickable launch script and register it
    projbasedir = vars_map.get("projbasedir", "") or config.get("projbasedir", "")
    projdir = vars_map.get("projdir", "") or config.get("projdir", "")
    project_dir = None
    if projbasedir and projdir:
        project_dir = os.path.join(projbasedir, projdir)
    elif hdd1_qcow_path:
        project_dir = os.path.dirname(hdd1_qcow_path)
    elif floppy1_path:
        project_dir = os.path.dirname(floppy1_path)
    script_target = (os.path.join(project_dir, f"start-qemu-{name}.sh")
                     if project_dir and os.path.isdir(project_dir) else None)
    script_ok, script_path = instance.write_launch_script(path=script_target)
    if script_ok:
        log.append(f"Wrote QEMU launch script: {script_path}")
        log.append(f"ARTIFACT: {script_path}")
        for img in instance.referenced_disk_images():
            log.append(f"ARTIFACT: {img}")
    else:
        log.append(script_path)

    time.sleep(3)

    # collect_qemu_logs joins a relative save_path onto /testrunnerapp
    helperdir = "/testrunnerapp"
    if helperdir not in sys.path:
        sys.path.insert(0, helperdir)
    from appstate import current_reports_dir
    qemu_log_path = os.path.join(current_reports_dir(), f"qemu_stdout_{name}.log")

    if not instance.start():
        success, logs_or_msg = instance.collect_qemu_logs(qemu_log_path)
        log.append("Failed to start QEMU.")
        log.append(logs_or_msg)
        context["abort"] = True
        return False, "\n".join(log)

    success, logs_or_msg = instance.collect_qemu_logs(qemu_log_path)
    log.append(logs_or_msg)

    if not instance.wait_for_ready():
        log.append(f"{name} did not become ready.")
        log.append(logs_or_msg)
        return False, "\n".join(log)

    context[name] = instance
    # first-started instance is the default "qemu1" target
    context.setdefault("qemu1", instance)
    log.append(f"{name} is ready.")
    log.append(logs_or_msg)

    if vnc_port:
        vnc_ok, vnc_msg = instance.vnc_connect()
        log.append(vnc_msg)
        if not vnc_ok:
            context["abort"] = True
            return False, "\n".join(log)

    _maybe_screenshot(instance, takeascreenshot, log, label=name)

    return True, "\n".join(log)


@dispatchtest_step
def test_qemu_pause(name="qemu1", **kwargs):
    """HMP 'stop' without saving (before manual inspection or a hand-saved snapshot)."""
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    reply = instance.pause_vm()
    return True, f"{name} paused\n{reply}"


@dispatchtest_step
def test_qemu_resume(name="qemu1", **kwargs):
    """Restarts the guest CPU (HMP 'cont') after test_qemu_pause."""
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    reply = instance.resume_vm()
    return True, f"{name} resumed\n{reply}"


@dispatchtest_step
def test_qemu_wait_exit(name="qemu1", attemptdelay=2, numberofattempts=15, **kwargs):
    """Wait for the guest to power itself off: passes once the QEMU process has exited (e.g. classic Mac OS Special > Shut Down on q800), fails if still running after numberofattempts checks attemptdelay s apart. Use instead of a fixed wait so the disk cache is flushed."""
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    for attempt in range(1, int(numberofattempts) + 1):
        if not instance.is_alive():
            return True, f"{name}: QEMU exited (check {attempt})"
        time.sleep(float(attemptdelay))
    context["abort"] = True
    return False, f"{name}: QEMU still running after {numberofattempts} checks"


@dispatchtest_step
def test_qemu_savestate(name="qemu1", tag="quicksave", **kwargs):
    """Pause, write an internal qcow2 snapshot `tag`, resume. A later instance boots into it via test_startqemu's `loadvm`."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    name = resolve_path(name, vars_map) or "qemu1"
    tag = resolve_path(tag, vars_map) or "quicksave"
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    reply = instance.save_snapshot(tag)
    if hmp_error(reply):
        return False, f"{name} savevm {tag!r} failed: {hmp_error(reply)}\n{reply}"
    return True, f"{name} saved snapshot {tag!r}\n{reply}"


@dispatchtest_step
def test_qemu_loadstate(name="qemu1", tag="quicksave", **kwargs):
    """Restore a snapshot into a running instance (HMP 'loadvm'); for a fresh process prefer test_startqemu's `loadvm`."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    name = resolve_path(name, vars_map) or "qemu1"
    tag = resolve_path(tag, vars_map) or "quicksave"
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    reply = instance.load_snapshot(tag)
    if hmp_error(reply):
        return False, f"{name} loadvm {tag!r} failed: {hmp_error(reply)}\n{reply}"
    return True, f"{name} loaded snapshot {tag!r}\n{reply}"


@dispatchtest_step
def test_qemu_snap(label="", name=None, takeascreenshot=False, **kwargs):
    """Timeline snapshot: savevm every running QEMU instance (or 86Box with monitor_port; or just `name`), put on the run's snapshot timeline under `label` (testbuilder timeline strip, test_qemu_rollback).
    No-op unless CONFIG["snapshots"] = True. For a snap after an ordinary step use that step's "snap" checkbox. QEMU: writable disks must be qcow2 (snaptimeline.py); 86Box: raw is fine, snapshots live in the process. Unlike test_qemu_savestate (one fixed tag for `loadvm`; ignores the switch).
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    label = resolve_path(label, vars_map) or ""
    name = resolve_path(name, vars_map) or None
    if not snaptimeline.enabled(config):
        return True, "snapshots are off for this testlist (CONFIG snapshots) -- snap point skipped"
    entry, msg = snaptimeline.take(context, label=label, source="step", name=name)
    if entry is None:
        return False, msg
    context["_snap_taken_by_step"] = entry["id"]
    log = [msg]
    for key in entry["instances"]:
        _maybe_screenshot(context.get(key), takeascreenshot, log, label=key)
    return True, "\n".join(log)


@dispatchtest_step
def test_qemu_rollback(to="", name=None, takeascreenshot=True, **kwargs):
    """Roll every instance a timeline snapshot was taken on back to it (HMP loadvm; 86Box remote monitor loadvm). `to`: label (test_qemu_snap), title of the step whose "snap" took it, tag, or timeline id; empty = most recent.
    Guest only; the testlist continues from the NEXT step. With CONFIG["snapshots"] off: passes with a warning, changes nothing.
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    to = resolve_path(to, vars_map) or ""
    if not snaptimeline.enabled(config):
        return True, ("TESTRUNNER_WARN: snapshots are off for this testlist (CONFIG snapshots) "
                      f"-- rollback to {to or 'latest'!r} skipped, guest NOT rolled back")
    tl = context.get(snaptimeline.KEY) or []
    entry = snaptimeline.find(context, to) if to else (tl[-1] if tl else None)
    if entry is None:
        have = ", ".join(repr(s["label"]) for s in tl) or "none taken yet"
        return False, f"no snapshot {to!r} on the timeline (have: {have})"
    if name and name not in entry["instances"]:
        return False, f"snapshot {entry['label']!r} wasn't taken on {name!r}"
    ok, msg = snaptimeline.restore(context, entry)
    context["_snap_restored_by_step"] = entry["id"]
    time.sleep(0.5)
    log = [msg]
    for key in entry["instances"]:
        _maybe_screenshot(context.get(key), takeascreenshot and ok, log, label=key)
    return ok, "\n".join(log)


@dispatchtest_step
def test_qemu_changemedia(name="qemu1", device="ide1-cd0", path=None, **kwargs):
    """Hot-swap removable media (HMP 'eject' then 'change') on a running instance; generates an ATAPI media-changed event so the guest drops its cache (needed after loadvm; `diskutil eject` in the guest does not work).
    `device` default "ide1-cd0" (legacy `-drive if=ide,index=2,media=cdrom`).
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    name = resolve_path(name, vars_map) or "qemu1"
    device = resolve_path(device, vars_map) or "ide1-cd0"
    path = resolve_path(path, vars_map)
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    if not path:
        return False, "test_qemu_changemedia: path is required"
    eject_reply = instance.send_command(f"eject -f {device}")
    change_reply = instance.send_command(f'change {device} "{path}"')
    log = f"eject -f {device}: {eject_reply}\nchange {device} {path!r}: {change_reply}"
    if any("error" in (r or "").lower() or "could not" in (r or "").lower()
           for r in (eject_reply, change_reply)):
        context["abort"] = True
        return False, log
    return True, log


@dispatchtest_step
def test_start_basilisk(name="basilisk1", rom=None, boot_disk=None,
                        extra_disk=None, serial_path=None, ramsize_mb=64,
                        modelid=14, cpu=4, fpu="true", takeascreenshot=False,
                        **kwargs):
    """Launch a Basilisk II classic-Mac emulator (alternative to the QEMU q800); mounts RAW HFS images. extra_disk = raw .dsk from a host build."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    rom = resolve_path(rom or config.get("basilisk_rom"), vars_map) or None
    boot_disk = resolve_path(boot_disk or config.get("basilisk_boot_disk"), vars_map) or None
    extra_disk = resolve_path(extra_disk, vars_map) or None
    serial_path = resolve_path(serial_path or config.get("serial_path"), vars_map) or None

    kw = {}
    if rom: kw["rom"] = rom
    if boot_disk: kw["boot_disk"] = boot_disk
    inst = BasiliskInstance(
        name, extra_disks=[extra_disk] if extra_disk else None,
        serial_path=serial_path, ramsize_mb=int(ramsize_mb),
        modelid=int(modelid), cpu=int(cpu), fpu=_as_bool(fpu),
        screenshot_scale=config.get("ocr_scale"), **kw)

    log = [f"Starting Basilisk '{name}'"]
    if extra_disk:
        log.append(f"  app disk (raw HFS) = {extra_disk}")
    if serial_path:
        log.append(f"  serial -> {serial_path} (Mac serial A via pty)")
    if not inst.start():
        context["abort"] = True
        return False, "\n".join(log + inst.stdout_lines)

    context[name] = inst
    context.setdefault("qemu1", inst)
    log.append(f"{name} launched (pid {inst.process.pid})")
    _maybe_screenshot(inst, takeascreenshot, log, label=name)
    return True, "\n".join(log)


class HostBuildInstance:
    """Live-view handle for a running test_hostbuild subprocess (log_path + process), matching liveview.py's _HostBuildAdapter; published into context[name] for the step, popped when it finishes ('tty' view of stdout-so-far)."""
    def __init__(self, name, process, log_path):
        self.name = name
        self.process = process
        self.log_path = log_path


@dispatchtest_step
def test_hostbuild(command=None, cwd=None, timeout=600, name=None,
                   attach_glob=None, attach_name="img", attach_scale=1, **kwargs):
    """Host-side build command (retro68k etc.; the 'compile on the Linux host' half of m68k).
    attach_glob: path or glob of image(s) the command produced, attached to THIS step's report entry; a miss is logged but does not fail the step (exit code decides).
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    command = resolve_path(command, vars_map)
    cwd = resolve_path(cwd, vars_map) or None
    attach_glob = resolve_path(attach_glob, vars_map) or None
    if not command:
        return False, "test_hostbuild: no command given"

    name = name or config.get("function") or config.get("projdir") or "hostbuild"

    log = [f"$ {command}" + (f"   (cwd={cwd})" if cwd else "")]
    timeout = int(timeout)
    fd, out_path = tempfile.mkstemp(prefix="test_hostbuild_", suffix=".log")
    os.close(fd)
    start = time.time()
    try:
        with open(out_path, "w") as out_f:
            if IS_WINDOWS:
                # bash -lc with MSYSTEM=UCRT64: puts /usr/bin and the ucrt64 toolchain (cmake/ninja/gcc) on PATH
                win_env = os.environ.copy()
                win_env.setdefault("MSYSTEM", "UCRT64")
                # umask 000: NFS files stay readable by this runner
                proc = subprocess.Popen([WINDOWS_BASH_BIN, "-lc", "umask 000; " + command], cwd=cwd,
                                        stdout=out_f, stderr=subprocess.STDOUT,
                                        env=win_env)
            else:
                proc = subprocess.Popen(command, shell=True, cwd=cwd,
                                        stdout=out_f, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        # visible on /instances (liveview _HostBuildAdapter): live stdout tail
        context[name] = HostBuildInstance(name, proc, out_path)
        timed_out = False
        aborted = False
        while True:
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if context.get("abort"):
                    aborted = True
                elif time.time() - start > timeout:
                    timed_out = True
                else:
                    continue
                if IS_WINDOWS:
                    # os.killpg is POSIX-only; on MSYS2 a grandchild may outlive the kill
                    proc.kill()
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait()
                break

        with open(out_path) as f:
            output = f.read().rstrip()
    finally:
        context.pop(name, None)
        try:
            os.remove(out_path)
        except OSError:
            pass

    if output:
        log.append(output)

    if aborted:
        return False, "\n".join(log + ["aborted (stop requested)"])
    if timed_out:
        context["abort"] = True
        return False, "\n".join(log + [f"timed out after {timeout}s"])
    if proc.returncode != 0:
        context["abort"] = True
        log.append(f"exit {proc.returncode} — build FAILED")
        return False, "\n".join(log)
    log.append("build OK")

    if attach_glob:
        _ok, attach_log = _attach_report_images(attach_glob, name=attach_name, scale=attach_scale)
        log.extend(attach_log)

    return True, "\n".join(log)


@dispatchtest_step
def test_hostbashexec(command=None, cwd=None, timeout=600, name=None, **kwargs):
    """One arbitrary host shell command, pass/fail on its exit code; delegates to test_hostbuild (same shell/timeout/abort handling), without attach_glob."""
    return test_hostbuild(command=command, cwd=cwd, timeout=timeout, name=name, **kwargs)


@dispatchtest_step
def test_sshcommand(host="127.0.0.1", port=22, username=None, password=None,
                    command=None, hostkeyalgorithms="+ssh-rsa", timeout=120,
                    numberofattempts=1, attemptdelay=3, **kwargs):
    """One command on a guest over SSH; pass/fail follows its exit code. Password auth with hostkeyalgorithms="+ssh-rsa" (old sshd, e.g. Tiger's OpenSSH 5.1).
    command runs via the login shell (one shlex.quote of the whole string); chain with &&, e.g. "cd ~/Proj && xcodebuild -project Proj.xcodeproj build".
    numberofattempts/attemptdelay retry the CONNECTION only (as test_ocrwordsearch); a command that connects and exits non-zero is never retried.
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    host = resolve_path(host, vars_map) or "127.0.0.1"
    port = resolve_path(port, vars_map) or 22
    username = resolve_path(username or config.get("ssh_username"), vars_map)
    password = resolve_path(password or config.get("ssh_password"), vars_map)
    command = resolve_path(command, vars_map)
    hostkeyalgorithms = resolve_path(hostkeyalgorithms, vars_map)
    timeout = int(timeout)
    numberofattempts = max(1, int(numberofattempts))
    attemptdelay = float(attemptdelay)

    if not command:
        return False, "test_sshcommand: no command given"
    if not username:
        return False, "test_sshcommand: no username given"

    ssh_opts = (
        f"-p {port} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o ConnectTimeout=25 -o PreferredAuthentications=password "
        f"-o PubkeyAuthentication=no"
    )
    if hostkeyalgorithms:
        ssh_opts += f" -o HostKeyAlgorithms={hostkeyalgorithms}"
    ssh_cmd = f"ssh {ssh_opts} {username}@{host} {shlex.quote(command)}"
    log = [f"$ ssh {username}@{host}:{port} {command}"]

    output = ""
    rc = None
    for attempt in range(1, numberofattempts + 1):
        # errors="replace": guest output may be non-UTF8
        child = pexpect.spawn(ssh_cmd, timeout=timeout, encoding="utf-8", codec_errors="replace")
        try:
            i = child.expect(["password:", pexpect.EOF, pexpect.TIMEOUT])
            if i == 0:
                if not password:
                    return False, "\n".join(log + ["guest asked for a password but none was given"])
                child.sendline(password)
                child.expect(pexpect.EOF)
                output = (child.before or "").strip()
                child.close()
                rc = child.exitstatus
                break
            if i == 2:
                if attempt == numberofattempts:
                    context["abort"] = True
                    return False, "\n".join(log + [f"timed out after {timeout}s connecting/authenticating ({numberofattempts} attempt(s))"])
                log.append(f"attempt {attempt}/{numberofattempts}: timed out waiting for a password prompt — sshd may still be starting, retrying in {attemptdelay:g}s")
            else:
                output = (child.before or "").strip()
                child.close()
                rc = child.exitstatus
                if attempt == numberofattempts:
                    break
                log.append(f"attempt {attempt}/{numberofattempts}: {output or 'connection closed before authentication'} — retrying in {attemptdelay:g}s")
        finally:
            if child.isalive():
                child.close(force=True)
        time.sleep(attemptdelay)

    if output:
        log.append(output)
    if rc != 0:
        log.append(f"exit {rc} — ssh command FAILED")
        return False, "\n".join(log)
    log.append("ssh command OK")
    return True, "\n".join(log)


@dispatchtest_step
def test_filecontains(file_path=None, successphrase=None, failphrase=None,
                      timeout=20, takeascreenshot=False, name=None, **kwargs):
    """Assert a host file contains successphrase and not failphrase (e.g. guest writes a token to serial, QEMU -serial file:); polls up to `timeout` s.
    takeascreenshot: required in audio tests (this poll has no other visual record); off by default; with `name` that instance, else every GUI instance in context; fires on every exit path.
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    file_path = resolve_path(file_path, vars_map)
    if not file_path:
        return False, "test_filecontains: no file_path given"
    successphrase = resolve_path(successphrase, vars_map)
    failphrase = resolve_path(failphrase, vars_map)

    def _shoot(log):
        if not _as_bool(takeascreenshot):
            return
        if name:
            instance, err = _gui_instance(context, name)
            if err:
                log.append(err)
                return
            _screenshot_and_ocr(instance, log, instance.name)
        else:
            targets, err = _gui_instances(context)
            if err:
                log.append(err)
                return
            for label, instance in targets:
                _screenshot_and_ocr(instance, log, label)

    log = [f"checking {file_path} for '{successphrase}'"]
    deadline = time.time() + int(timeout)
    content = ""
    while time.time() < deadline:
        # Stop/Abandon set context["abort"]: break out of long polls
        if context.get("abort"):
            log.append("aborted: stop requested while waiting")
            return False, "\n".join(log)
        try:
            with open(file_path, "r", errors="replace") as f:
                content = f.read()
        except FileNotFoundError:
            content = ""
        if failphrase and failphrase in content:
            context["abort"] = True
            log.append(f"FAIL phrase '{failphrase}' present")
            # include tail-of-file context, as the timeout branch does
            log.append(f"--- file tail ---\n{content[-1000:]}")
            _shoot(log)
            return False, "\n".join(log)
        if successphrase and successphrase in content:
            log.append(f"found '{successphrase}'")
            _shoot(log)
            return True, "\n".join(log)
        time.sleep(1)

    context["abort"] = True
    log.append(f"'{successphrase}' not found within {timeout}s")
    log.append(f"--- file tail ---\n{content[-1000:]}")
    _shoot(log)
    return False, "\n".join(log)


def _attach_report_images(image_path, name="img", scale=1):
    """Copy host-side image(s) into the current step's report assets (shared by test_attach_screenshot and test_hostbuild's attach_glob).
    image_path: one file or a glob ("output/cursor*.png"), attached in sorted order.
    Assets are matched by filename screenshot-<name>-<stepnum>[-<n>].(png|gif) in the reports dir; <name> must contain no '-'; reporthelper._move_assets sweeps them into the report subdir.
    scale>1: nearest-neighbour upscale (small/1-bit frames). Returns (bool, log_lines list).
    """
    import glob as _glob
    import shutil as _shutil

    if "-" in str(name):
        return False, [f"attach: name {name!r} must not contain '-'"]

    matches = sorted(_glob.glob(image_path)) if any(c in image_path for c in "*?[") \
        else ([image_path] if os.path.exists(image_path) else [])
    log = [f"attaching {len(matches)} image(s) from {image_path}"]
    if not matches:
        return False, log + ["no image matched — nothing to attach"]

    # step number for the filename (as qemuhelpers.take_screenshot)
    _helperdir = "/testrunnersrc/pyhelpers"
    if _helperdir not in sys.path:
        sys.path.insert(0, _helperdir)
    try:
        from appstate import progress_state, current_reports_dir
        stepnum = re.match(r"\d+", progress_state.step).group(0)
    except Exception as e:
        return False, log + [f"could not determine step number: {e}"]

    try:
        reports_dir = current_reports_dir()
    except OSError as e:
        return False, log + [f"reports dir unavailable: {e}"]

    scale = int(scale)
    for i, src in enumerate(matches):
        suffix = f"-{i}" if len(matches) > 1 else ""
        dest = os.path.join(reports_dir, f"screenshot-{name}-{stepnum}{suffix}.png")
        if scale > 1:
            img = Image.open(src)
            img = img.convert("L").resize((img.width * scale, img.height * scale),
                                          Image.NEAREST)
            img.save(dest)
            log.append(f"  {os.path.basename(src)} -> {os.path.basename(dest)} "
                       f"({img.width}x{img.height}, {scale}x)")
        else:
            _shutil.copyfile(src, dest)
            log.append(f"  {os.path.basename(src)} -> {os.path.basename(dest)}")
    return True, log


@dispatchtest_step
def test_attach_screenshot(image_path=None, name="img", scale=1, **kwargs):
    """Attach host-side image(s) to this step of the report like a QEMU screendump (for platforms whose "screen" is a file, e.g. nova_fb PNGs); see _attach_report_images."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    image_path = resolve_path(image_path, vars_map)
    if not image_path:
        return False, "test_attach_screenshot: no image_path given"

    ok, log = _attach_report_images(image_path, name=name, scale=scale)
    if not ok:
        context["abort"] = True
    return ok, "\n".join(log)


@dispatchtest_step
def test_close_instance_window(name=None, timeout=10, **kwargs):
    """Ask an instance to quit gracefully (not SIGTERM via test_terminate_all): Box86Instance.request_graceful_shutdown() sends "shutdownnoprompt" over 86Box's VM-manager socket (do_stop() + MainWindow::close(): the real quit path, so pc_close()/sound_close()/al_capture_close() run).
    86Box ignores SIGTERM/SIGINT; xdotool windowclose needs a WM (none on Xvfb); the exit accelerator is unbound. Other instance types do nothing.
    Waits up to `timeout` s for exit. Still run test_terminate_all afterwards.
    """
    context = kwargs.get("context", {}) or {}
    inst = context.get(name)
    if inst is None:
        return False, f"test_close_instance_window: no instance named {name!r}"
    if not hasattr(inst, "request_graceful_shutdown"):
        return False, f"test_close_instance_window: {name!r} has no graceful shutdown method"

    if inst.request_graceful_shutdown(timeout=timeout):
        return True, f"{name} exited via graceful shutdown request"
    return False, f"{name} still running {timeout}s after graceful shutdown request"


@dispatchtest_step
def test_dump_instance_log(name=None, dest_path=None, **kwargs):
    """Write an emulator instance's captured console output (instance.stdout_lines: stdout+stderr merged) to a host file, for diagnostics that only reach the emulator's stderr (e.g. 86Box al_capture SNDIN_BUFTEST) so test_filecontains can check them. Safe after test_terminate_all."""
    context = kwargs.get("context", {}) or {}
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    dest_path = resolve_path(dest_path, vars_map)
    if not dest_path:
        return False, "test_dump_instance_log: dest_path is required"

    inst = context.get(name)
    if inst is None or not hasattr(inst, "stdout_lines"):
        return False, f"test_dump_instance_log: no instance named {name!r} with captured output"

    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "w") as f:
            f.write("\n".join(inst.stdout_lines))
            f.write("\n")
    except OSError as e:
        return False, f"test_dump_instance_log: {e}"

    return True, f"wrote {len(inst.stdout_lines)} line(s) to {dest_path}"


@dispatchtest_step
def test_start_gif_capture(name="qemu1", interval=0.5, max_seconds=120,
                           max_frames=240, scale=1, playback_speed=1, **kwargs):
    """Start recording the screen to an animated GIF and RETURN IMMEDIATELY (background thread, a frame every `interval` s).
    Stops on: test_stop_gif_capture, test_terminate_all, max_seconds, max_frames, testlist end, or instance death. The GIF attaches to the step that STOPS it.
      interval        seconds between frames (0.5 = 2 fps)
      max_seconds     cap on wall-clock recording time (0 = none)
      max_frames      cap on frames kept in memory (0 = none)
      scale           <1 shrinks the frames
      playback_speed  >1 plays faster than real time
    """
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No instance '{name}' available in context"
    if not hasattr(instance, "take_screenshot"):
        return False, f"Instance '{name}' ({type(instance).__name__}) cannot screenshot"
    if "-" in str(getattr(instance, "name", "")):
        # instance name must not contain '-' (report assets match screenshot-[^-]+-(\d+))
        return False, f"instance name {instance.name!r} must not contain '-'"

    # close out any earlier recorder on this instance first
    log = []
    for rec in active_recorders():
        if rec.instance is instance:
            log.append(rec.stop(reason="superseded by a new capture step")[1])

    recorder = GifRecorder(
        instance,
        interval=float(interval),
        max_seconds=float(max_seconds),
        max_frames=int(max_frames),
        scale=float(scale),
        playback_speed=float(playback_speed),
    ).start()

    context.setdefault("_gif_recorders", []).append(recorder)

    log.append(f"[{instance.name}] recording started — a frame "
               f"every {recorder.interval:g}s, until test_stop_gif_capture"
               f" (caps: {recorder.max_seconds:g}s / {recorder.max_frames} frames)."
               f" GIF will attach to whichever step stops it, not this one.")
    return True, "\n".join(log)


@dispatchtest_step
def test_stop_gif_capture(name=None, **kwargs):
    """Stop a recording started by test_start_gif_capture and write its GIF (pair each start with a stop)."""
    context = kwargs.get("context", {})
    recorders = [r for r in active_recorders()
                 if not name or getattr(r.instance, "name", None) == name]
    if not recorders:
        # recorder already ended on its own: nothing to stop
        done = [r for r in (context.get("_gif_recorders") or [])
                if getattr(r, "result", None)
                and (not name or getattr(r.instance, "name", None) == name)]
        if done:
            ok_all = all(r.result[0] for r in done)
            return ok_all, "\n".join(
                "recording had already ended before this step: " + r.result[1]
                for r in done)
        return True, ("no GIF recording in progress"
                      + (f" for '{name}'" if name else ""))

    log = []
    ok_all = True
    for rec in recorders:
        ok, msg = rec.stop(reason="test_stop_gif_capture")
        ok_all = ok_all and ok
        log.append(msg)
    return ok_all, "\n".join(log)


class SerialBridgeInstance:
    """Handle for a running two-box COM1 cable, registered in context so test_terminate_all tears it down.
    POSIX: `process` is serial_bridge.py (finds each box's PTY via /proc or lsof). Windows: relay in-process (box86helpers.win_start_stdio_bridge); `process` is None.
    """
    def __init__(self, name, vm_a, vm_b, process=None, log_path=None):
        self.name = name
        self.vm_a = vm_a
        self.vm_b = vm_b
        self.process = process
        self.log_path = log_path
        self._win_active = process is None

    def is_alive(self):
        if self.process is None:
            return self._win_active
        return self.process.poll() is None

    def stop(self):
        if self.process is None:
            if self._win_active:
                from box86helpers import win_stop_stdio_bridge
                win_stop_stdio_bridge(self.vm_a, self.vm_b)
                self._win_active = False
            return
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _read_tail(path, limit=4000):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()[-limit:]
    except OSError:
        return ""


@dispatchtest_step
def test_start_serial_bridge(vm_a=None, vm_b=None, name="serial_bridge", timeout=40,
                             script=None, log_path=None, **kwargs):
    """Wire two running 86Box instances' COM1 "Virtual Console (COM)" (stdio) devices to each other (null-modem). vm_a/vm_b = the vm_path values of the two test_start_86box steps.
    Linux/macOS: launches serial_bridge.py (`script`, default {projbasedir}{projdir}/serial_bridge.py) and waits for its ready file; Windows: cross-wires the piped stdin/stdout in-process. Up until test_stop_serial_bridge or test_terminate_all.
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    vm_a = resolve_path(vm_a, vars_map)
    vm_b = resolve_path(vm_b, vars_map)
    if not vm_a or not vm_b:
        return False, "test_start_serial_bridge: vm_a and vm_b are required"
    timeout = float(timeout)
    old = context.get(name)
    if isinstance(old, SerialBridgeInstance):
        old.stop()

    if IS_WINDOWS:
        from box86helpers import win_start_stdio_bridge
        deadline = time.time() + timeout
        ok, msg = win_start_stdio_bridge(vm_a, vm_b)
        while not ok and time.time() < deadline:
            time.sleep(0.5)
            ok, msg = win_start_stdio_bridge(vm_a, vm_b)
        if not ok:
            context["abort"] = True
            return False, f"serial bridge not started: {msg}"
        context[name] = SerialBridgeInstance(name, vm_a, vm_b)
        return True, f"in-process stdio bridge: {msg}"

    script = resolve_path(script, vars_map) or os.path.join(
        vars_map.get("projbasedir", ""), vars_map.get("projdir", ""), "serial_bridge.py")
    log_path = resolve_path(log_path, vars_map) or os.path.join(
        os.path.dirname(script), f"{name}.log")
    ready = os.path.join(tempfile.gettempdir(), f"{name}_{os.getpid()}.ready")
    if os.path.exists(ready):
        os.remove(ready)
    cmd = [sys.executable, script, "--vm-a", vm_a, "--vm-b", vm_b,
           "--find-timeout", str(int(timeout)), "--ready", ready]
    with open(log_path, "w") as log_f:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log_f,
                                stderr=subprocess.STDOUT, start_new_session=True)
    inst = SerialBridgeInstance(name, vm_a, vm_b, process=proc, log_path=log_path)
    context[name] = inst

    deadline = time.time() + timeout + 5
    while time.time() < deadline:
        if os.path.exists(ready):
            return True, f"serial_bridge.py running as pid {proc.pid} (log: {log_path})"
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    inst.stop()
    context["abort"] = True
    return False, (f"serial_bridge.py {'exited' if proc.poll() is not None else 'never became ready'}"
                   f" -- its log:\n{_read_tail(log_path)}")


@dispatchtest_step
def test_stop_serial_bridge(name="serial_bridge", **kwargs):
    """Tear down the cable test_start_serial_bridge put up."""
    context = kwargs.get("context", {})
    inst = context.pop(name, None)
    if not isinstance(inst, SerialBridgeInstance):
        return True, f"no serial bridge '{name}' in context"
    alive = inst.is_alive()
    inst.stop()
    msg = f"serial bridge '{name}' stopped" + ("" if alive else " (had already exited)")
    if inst.log_path:
        msg += f"\n{_read_tail(inst.log_path)}"
    return True, msg


@dispatchtest_step
def test_check_serial_bridge(name="serial_bridge", **kwargs):
    """Fail (and abort) if the cable is no longer up."""
    context = kwargs.get("context", {})
    inst = context.get(name)
    if not isinstance(inst, SerialBridgeInstance):
        context["abort"] = True
        return False, f"no serial bridge '{name}' in context"
    if inst.is_alive():
        return True, f"serial bridge '{name}' still up"
    context["abort"] = True
    return False, (f"serial bridge '{name}' is no longer running"
                   + (f" -- its log:\n{_read_tail(inst.log_path)}" if inst.log_path else ""))


class HostServiceInstance:
    """Handle for a long-running host process (SIP server, mock daemon...) registered in context so test_terminate_all tears it down. Own session/process group (stop() takes down children); must run in the FOREGROUND."""
    def __init__(self, name, process, log_path=None, command=None,
                 stop_timeout=5.0):
        self.name = name
        self.process = process
        self.log_path = log_path
        self.command = command
        self.stop_timeout = stop_timeout

    def is_alive(self):
        return self.process.poll() is None

    def _signal_group(self, sig):
        if IS_WINDOWS:
            (self.process.terminate if sig == signal.SIGTERM
             else self.process.kill)()
            return
        try:
            os.killpg(self.process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self):
        if self.process.poll() is None:
            self._signal_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL if not IS_WINDOWS
                                   else signal.SIGTERM)
                try:
                    self.process.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    pass
        elif not IS_WINDOWS:
            # leader gone: still sweep its group
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def _sip_options_ok(hostport, timeout=1.0):
    """True if a SIP UA at host:port answers a UDP OPTIONS probe."""
    import socket
    host, _, port = hostport.rpartition(":")
    host = host or "127.0.0.1"
    msg = (f"OPTIONS sip:probe@{host}:{port} SIP/2.0\r\n"
           f"Via: SIP/2.0/UDP 127.0.0.1:0;branch=z9hG4bKprobe{os.getpid()}\r\n"
           "Max-Forwards: 70\r\nFrom: <sip:probe@127.0.0.1>;tag=probe\r\n"
           f"To: <sip:probe@{host}:{port}>\r\nCall-ID: probe-{os.getpid()}\r\n"
           "CSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(msg, (host, int(port)))
        return s.recv(4096).startswith(b"SIP/2.0")
    except (OSError, ValueError):
        return False
    finally:
        s.close()


def _tcp_port_open(host, port, timeout=0.5):
    import socket
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout):
            return True
    except (OSError, ValueError):
        return False


@dispatchtest_step
def test_start_hostservice(name="hostservice", command=None, cwd=None, env=None,
                           json_config=None, log_path=None,
                           ready_regex=None, ready_port=None,
                           ready_host="127.0.0.1", ready_file=None,
                           ready_sip=None, timeout=30, stop_timeout=5, **kwargs):
    """Start a long-running FOREGROUND host process for the following steps, until test_stop_hostservice or test_terminate_all. A service that exits or never gets ready fails the step and aborts the run.
      command       list (or shlex-split string); {tokens} resolve
      env           {var: value} merged over the runner's environment
      json_config   dict handed to the service as <name>.json next to the log (tokens resolved); path in {hostservice_config} for command/env and $HOSTSERVICE_CONFIG
      log_path      stdout+stderr (default <cwd or tmp>/<name>.log)
      ready_*       all given must hold: ready_regex (in the log), ready_port (+ready_host, TCP), ready_file, ready_sip ("host:port", UDP OPTIONS answered); none = process alive after 1s
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    if isinstance(command, str):
        command = shlex.split(command, posix=not IS_WINDOWS)
    if not command:
        return False, "test_start_hostservice: command is required"
    cwd = resolve_path(cwd, vars_map) or None
    if cwd and not os.path.isdir(cwd):
        context["abort"] = True
        return False, f"test_start_hostservice: cwd {cwd} is not a directory"
    log_path = resolve_path(log_path, vars_map) or os.path.join(
        cwd or tempfile.gettempdir(), f"{name}.log")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    except OSError as e:
        context["abort"] = True
        return False, f"test_start_hostservice: cannot create log directory for {log_path}: {e}"
    config_path = None
    if json_config is not None:
        import json

        def _resolve_tree(v):
            if isinstance(v, str):
                return resolve_path(v, vars_map)
            if isinstance(v, list):
                return [_resolve_tree(x) for x in v]
            if isinstance(v, dict):
                return {k: _resolve_tree(x) for k, x in v.items()}
            return v
        config_path = os.path.splitext(log_path)[0] + ".json"
        try:
            with open(config_path, "w") as f:
                json.dump(_resolve_tree(json_config), f, indent=2)
        except OSError as e:
            context["abort"] = True
            return False, f"test_start_hostservice: cannot write {config_path}: {e}"
        vars_map = dict(vars_map, hostservice_config=config_path)
    cmd = [str(resolve_path(c, vars_map)) for c in command]
    ready_regex = resolve_path(ready_regex, vars_map)
    ready_file = resolve_path(ready_file, vars_map)
    ready_sip = resolve_path(ready_sip, vars_map)
    ready_host = resolve_path(ready_host, vars_map)
    ready_port = resolve_path(ready_port, vars_map)
    proc_env = dict(os.environ)
    if config_path:
        proc_env["HOSTSERVICE_CONFIG"] = config_path
    for k, v in (env or {}).items():
        proc_env[str(k)] = str(resolve_path(v, vars_map))

    old = context.pop(name, None)
    if isinstance(old, HostServiceInstance):
        old.stop()
    if ready_file and os.path.exists(ready_file):
        os.remove(ready_file)

    popen_kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if IS_WINDOWS else {"start_new_session": True})
    try:
        with open(log_path, "w") as log_f:
            proc = subprocess.Popen(cmd, cwd=cwd, env=proc_env,
                                    stdin=subprocess.DEVNULL, stdout=log_f,
                                    stderr=subprocess.STDOUT, **popen_kw)
    except OSError as e:
        context["abort"] = True
        return False, f"test_start_hostservice: cannot start {cmd[0]}: {e}"
    inst = HostServiceInstance(name, proc, log_path=log_path, command=cmd,
                               stop_timeout=float(stop_timeout))
    context[name] = inst

    checks = []
    if ready_regex:
        rx = re.compile(ready_regex)
        checks.append(("log regex", lambda: bool(rx.search(_read_tail(log_path, 1 << 20)))))
    if ready_port:
        checks.append((f"tcp {ready_host}:{ready_port}",
                       lambda: _tcp_port_open(ready_host, ready_port)))
    if ready_file:
        checks.append(("ready file", lambda: os.path.exists(ready_file)))
    if ready_sip:
        checks.append((f"sip {ready_sip}", lambda: _sip_options_ok(str(ready_sip))))

    deadline = time.time() + float(timeout)
    if not checks:
        time.sleep(1.0)
        checks_done = True
    else:
        checks_done = False
        while time.time() < deadline and proc.poll() is None:
            if all(fn() for _, fn in checks):
                checks_done = True
                break
            time.sleep(0.25)

    if checks_done and proc.poll() is None:
        return True, (f"'{name}' running as pid {proc.pid} (log: {log_path})"
                      + (f"; ready: {', '.join(d for d, _ in checks)}" if checks else ""))
    why = ("exited with code %s" % proc.returncode if proc.poll() is not None
           else "never became ready (%s)" % ", ".join(d for d, _ in checks))
    inst.stop()
    context.pop(name, None)
    context["abort"] = True
    return False, f"'{name}' {why} -- its log:\n{_read_tail(log_path)}"


@dispatchtest_step
def test_stop_hostservice(name="hostservice", **kwargs):
    """Stop a service test_start_hostservice put up and show its log tail."""
    context = kwargs.get("context", {})
    inst = context.pop(name, None)
    if not isinstance(inst, HostServiceInstance):
        return True, f"no host service '{name}' in context"
    alive = inst.is_alive()
    inst.stop()
    msg = f"host service '{name}' stopped" + ("" if alive else " (had already exited)")
    if inst.log_path:
        msg += f"\n{_read_tail(inst.log_path)}"
    return True, msg


@dispatchtest_step
def test_check_hostservice(name="hostservice", **kwargs):
    """Fail (and abort) if the service is no longer up."""
    context = kwargs.get("context", {})
    inst = context.get(name)
    if not isinstance(inst, HostServiceInstance):
        context["abort"] = True
        return False, f"no host service '{name}' in context"
    if inst.is_alive():
        return True, f"host service '{name}' still up"
    context["abort"] = True
    return False, (f"host service '{name}' is no longer running (exit "
                   f"{inst.process.returncode}) -- its log:\n{_read_tail(inst.log_path)}")


@dispatchtest_step
def test_terminate_all(**kwargs):
    """Stop every emulator instance registered in context (QEMU, SIMH, Basilisk, DOSBox-X, 86Box, plus any type a dispatch_*.py backend registered, e.g. VICE)."""
    context = kwargs.get("context", {}) or {}
    log = []

    for ok, msg in stop_all_gif_recorders(reason="test_terminate_all"):
        log.append(msg)

    time.sleep(3)

    seen, uniq = set(), []
    for inst in context.values():
        if isinstance(inst, (QemuInstance, NovaSimhInstance, BasiliskInstance,
                             NovaFbMonitorProcess, NovaTermProcess,
                             VerilatorLiveInstance, VerilatorFbMonitorProcess,
                             SerialBridgeInstance, HostServiceInstance)
                      + GUI_INSTANCE_TYPES + tuple(EXTRA_TERMINABLE)) and id(inst) not in seen:
            seen.add(id(inst))
            uniq.append(inst)

    if not uniq:
        log.append("No QEMU/SIMH instances found to stop.")
        return True, "\n".join(log)

    for inst in uniq:
        if isinstance(inst, (NovaFbMonitorProcess, NovaTermProcess,
                            VerilatorFbMonitorProcess)) and inst.keep_open:
            # testlist asked to stay active: "PAUSE ON"
            log.append(f"Leaving {inst.name} open (keep_open); close it with Q")
            continue
        iname = getattr(inst, "name", type(inst).__name__)
        log.append(f"Stopping {iname}")
        inst.stop()
        log.append(f"{iname} has exited.")
    return True, "\n".join(log)


@dispatchtest_step
def test_assert_screen_size(expected_width=None, expected_height=None,
                            name="qemu1", tolerance=0, require_success=True,
                            **kwargs):
    """Assert the guest framebuffer resolution: fresh `screendump` (native size) vs expected_width x expected_height."""
    context = kwargs.get("context", {}) or {}
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    if expected_width is None or expected_height is None:
        return False, "expected_width and expected_height are required"

    ew, eh, tol = int(expected_width), int(expected_height), int(tolerance)

    ok, path = instance.take_screenshot()
    if not ok:
        return False, f"screendump failed: {path}"

    try:
        w, h = Image.open(path).size
    except Exception as e:
        return False, f"could not read screendump {path}: {e}"

    match = abs(w - ew) <= tol and abs(h - eh) <= tol
    msg = f"screen {w}x{h} vs expected {ew}x{eh} (tol {tol}) -> {'MATCH' if match else 'MISMATCH'}"
    if match:
        return True, msg
    return (not _as_bool(require_success)), msg


@dispatchtest_step
def test_audio_tonecheck(wav_path=None, expect_hz=1000, tolerance_hz=60,
                         min_snr_db=10, instance_name="qemu1",
                         require_success=True, **kwargs):
    """Verify the guest played a tone at expect_hz: Goertzel on the .wav from QEMU's wav audiodev. Run AFTER test_terminate_all (QEMU fills the RIFF header on exit)."""
    log = []
    context = kwargs.get("context", {}) or {}
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    require_success = str(require_success).strip().lower() in ("1", "true", "yes", "on")

    wav_path = resolve_path(wav_path, vars_map) or None
    try:
        expect_hz = float(resolve_path(expect_hz, vars_map))
        tolerance_hz = float(resolve_path(tolerance_hz, vars_map))
        min_snr_db = float(resolve_path(min_snr_db, vars_map))
    except (TypeError, ValueError) as e:
        msg = f"Bad tone-check parameter: {e}"
        log.append(msg)
        if require_success:
            context["abort"] = True
        return (not require_success), "\n".join(log)
    if not wav_path:

        instance = context.get(instance_name)
        wav_path = getattr(instance, "audio_out_path", None)
    if not wav_path:
        msg = (f"No wav_path given and instance {instance_name!r} has no audio_out_path "
               f"— did test_startqemu set sound_device/audio_backend=wav?")
        log.append(msg)
        if require_success:
            context["abort"] = True
        return (not require_success), "\n".join(log)

    ok, msg, details = detect_tone(wav_path, expect_hz=expect_hz,
                                   tolerance_hz=tolerance_hz,
                                   min_snr_db=min_snr_db)
    log.append(msg)
    if details:
        log.append(f"  measured: {details}")


    if os.path.isfile(wav_path):
        log.append(f"ARTIFACT: {wav_path}")

    if not ok and require_success:
        context["abort"] = True
        return False, "\n".join(log)

    return True, "\n".join(log)


@dispatchtest_step
def test_audio_tonesequence(wav_path=None, expect_hz_list=None, tolerance_hz=60,
                            min_snr_db=10, instance_name="qemu1",
                            require_success=True, **kwargs):
    """Verify several tones (mediahelpers.detect_tone_sequence: each expected frequency's own best window). expect_hz_list: comma-separated string ("440,660,880") or list. Run AFTER test_terminate_all."""
    log = []
    context = kwargs.get("context", {}) or {}
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    require_success = str(require_success).strip().lower() in ("1", "true", "yes", "on")

    wav_path = resolve_path(wav_path, vars_map) or None
    expect_hz_list = resolve_path(expect_hz_list, vars_map)
    try:
        if isinstance(expect_hz_list, str):
            expect_hz_list = [float(h) for h in re.split(r"[,\s]+", expect_hz_list.strip()) if h]
        else:
            expect_hz_list = [float(h) for h in (expect_hz_list or [])]
        tolerance_hz = float(resolve_path(tolerance_hz, vars_map))
        min_snr_db = float(resolve_path(min_snr_db, vars_map))
    except (TypeError, ValueError) as e:
        msg = f"Bad tone-sequence-check parameter: {e}"
        log.append(msg)
        if require_success:
            context["abort"] = True
        return (not require_success), "\n".join(log)
    if not expect_hz_list:
        msg = "test_audio_tonesequence: no expect_hz_list given"
        log.append(msg)
        if require_success:
            context["abort"] = True
        return (not require_success), "\n".join(log)
    if not wav_path:
        instance = context.get(instance_name)
        wav_path = getattr(instance, "audio_out_path", None)
    if not wav_path:
        msg = (f"No wav_path given and instance {instance_name!r} has no audio_out_path "
               f"— did test_startqemu set sound_device/audio_backend=wav?")
        log.append(msg)
        if require_success:
            context["abort"] = True
        return (not require_success), "\n".join(log)

    ok, msg, details = detect_tone_sequence(wav_path, expect_hz_list,
                                            tolerance_hz=tolerance_hz,
                                            min_snr_db=min_snr_db)
    log.append(msg)
    if details:
        log.append(f"  measured: {details}")

    if os.path.isfile(wav_path):
        log.append(f"ARTIFACT: {wav_path}")

    if not ok and require_success:
        context["abort"] = True
        return False, "\n".join(log)

    return True, "\n".join(log)


@dispatchtest_step
def test_startnovasimh(name="nova1", disk_image_path=None, boot_timeout=30, memory=None,
                       binary=None, script_path=None, cwd=None, boot_rdos=True,
                       live_path=None, **kwargs):
    """Power on a dgnova SIMH instance and drive RDOS cold boot to the 'R' prompt. Optional:
      binary      alternate simulator (sourcedir/nova_fb/bin/dgnova-fb has the framebuffer device on code 042)
      script_path SIMH command file instead of the generated boot script
      cwd         working directory for relative paths in that script
      boot_rdos   False for a script that runs a bare program and halts
      live_path   nova_fb live view sink ("set fb live="), opened by test_start_novafb_monitor
    """
    log = []
    context = kwargs.get("context")
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    disk_image_path = resolve_path(disk_image_path, vars_map) if disk_image_path else None
    binary = resolve_path(binary, vars_map) if binary else None
    script_path = resolve_path(script_path, vars_map) if script_path else None
    cwd = resolve_path(cwd, vars_map) if cwd else None
    live_path = resolve_path(live_path, vars_map) if live_path else None
    boot_timeout = int(boot_timeout)
    boot_rdos = _as_bool(boot_rdos)

    instance = NovaSimhInstance(name, disk_image=disk_image_path, memory=memory,
                                binary=binary, script_path=script_path, cwd=cwd,
                                live_path=live_path)
    if script_path:
        log.append(f"Starting {name} ({instance.binary}) with script={script_path}")
    else:
        log.append(f"Starting {name} ({instance.binary}) with disk={instance.disk_image}")

    if not instance.start():
        log.append("Failed to start dgnova.")
        log.append(instance.buf)
        context["abort"] = True
        return False, "\n".join(log)

    if boot_rdos and not instance.boot_to_rdos(timeout=boot_timeout):
        log.append("Timed out waiting for RDOS boot / 'R' prompt.")
        log.append(instance.buf[-1000:])
        instance.stop()
        context["abort"] = True
        return False, "\n".join(log)

    context[name] = instance
    # first-started instance is the default "nova1" target
    context.setdefault("nova1", instance)
    log.append(f"{name} booted to RDOS console." if boot_rdos
               else f"{name} started; running {script_path}.")
    log.append(instance.buf[-500:])
    return True, "\n".join(log)


@dispatchtest_step
def test_start_novafb_monitor(name="novafb_monitor", live_path=None, scale=0,
                              fps=60, title=None, wait=30, require_success=True,
                              keep_open=False, **kwargs):
    """Window showing what the nova_fb card presents (novafbhelpers.Monitor, pygame, fed by the shared-memory live sink); pair with live_path on test_startnovasimh.
    Window = card resolution (720x400 framebuffer = 720x400 window); size set only on mode/zoom change. keep_open leaves it up after test_terminate_all. The message names the DISPLAY used.
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    live_path = resolve_path(live_path, vars_map) if live_path else DEFAULT_LIVE_PATH

    monitor = NovaFbMonitorProcess(name, live_path=live_path, scale=int(scale),
                                   fps=int(fps), title=title, wait=float(wait),
                                   keep_open=_as_bool(keep_open))
    ok, msg = monitor.start()
    if not ok:
        # vga fb viewer, not the term monitor
        if _as_bool(require_success):
            context["abort"] = True
            return False, msg
        return True, f"{msg} (require_success=False, continuing headless)"

    context[name] = monitor
    return True, msg


@dispatchtest_step
def test_start_verilator_live(name="verilator1", binary=None, live_path=None,
                              cwd=None, seconds=0, stats_path=None, **kwargs):
    """Run a compiled Verilator testbench in --live mode: clock runs indefinitely, publishing a frame to `live_path` per frame. Pair with test_start_verilatorfb_monitor (viewer half of the verilator_vga backend). `stats_path` passes --stats so liveview reports the testbench's vstats.hpp counters (capability "stats")."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    binary = resolve_path(binary, vars_map)
    live_path = resolve_path(live_path, vars_map) if live_path else VERILATOR_DEFAULT_LIVE_PATH
    cwd = resolve_path(cwd, vars_map) if cwd else None

    stats_path = resolve_path(stats_path, vars_map) if stats_path else None

    inst = VerilatorLiveInstance(name, binary=binary, live_path=live_path,
                                 cwd=cwd, seconds=int(seconds), stats_path=stats_path)
    ok, msg = inst.start()
    if not ok:
        context["abort"] = True
        return False, msg

    context[name] = inst
    return True, msg


@dispatchtest_step
def test_start_verilatorfb_monitor(name="verilatorfb_monitor", live_path=None,
                                   scale=0, fps=60, title=None, wait=30,
                                   require_success=True, keep_open=False,
                                   **kwargs):
    """Window showing what a Verilator testbench presents (as test_start_novafb_monitor); require_success=False keeps a headless container going."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    live_path = resolve_path(live_path, vars_map) if live_path else VERILATOR_DEFAULT_LIVE_PATH

    monitor = VerilatorFbMonitorProcess(name, live_path=live_path, scale=int(scale),
                                        fps=int(fps), title=title, wait=float(wait),
                                        keep_open=_as_bool(keep_open))
    ok, msg = monitor.start()
    if not ok:
        if _as_bool(require_success):
            context["abort"] = True
            return False, msg
        return True, f"{msg} (require_success=False, continuing headless)"

    context[name] = monitor
    return True, msg


@dispatchtest_step
def test_open_nova_terminal(name="nova_terminal", instance="nova1",
                            sock_path=None, title=None, geometry="100x30",
                            font_size=14, wait=30, require_success=True,
                            keep_open=False, banner=None, **kwargs):
    """Terminal window on the user's desktop attached to a running Nova's SIMH console; require_success=False keeps a headless container going."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    inst = context.get(instance)
    if not inst:
        return False, f"No SIMH/Nova instance '{instance}' available in context"

    sock_path = (resolve_path(sock_path, vars_map) if sock_path
                 else f"/tmp/novaconsole-{instance}-{os.getpid()}.sock")
    try:
        inst.open_console_relay(sock_path)
    except Exception as exc:
        msg = f"Could not serve {instance}'s console on {sock_path}: {exc}"
        if _as_bool(require_success):
            context["abort"] = True
            return False, msg
        return True, f"{msg} (require_success=False, continuing)"

    term = NovaTermProcess(name, sock_path=sock_path, title=title,
                           geometry=geometry, font_size=int(font_size),
                           wait=float(wait), keep_open=_as_bool(keep_open),
                           banner=banner or "")
    ok, msg = term.start()
    if not ok:
        if _as_bool(require_success):
            context["abort"] = True
            return False, msg
        return True, f"{msg} (require_success=False, continuing headless)"

    context[name] = term
    return True, msg


@dispatchtest_step
def test_sendnovacommand(cmd_text="", name="nova1", delay=1.0, **kwargs):
    """Send a line of text to a dgnova SIMH instance's console."""
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No SIMH/Nova instance '{name}' available in context"

    instance.send_command(cmd_text)
    time.sleep(float(delay))
    log = [f"{name} sent command: {cmd_text!r}", instance.buf[-500:]]
    return True, "\n".join(log)


@dispatchtest_step
def test_novascreensearch(successphrase=None, failphrase=None, timeout=15,
                          require_success=False, name="nova1", **kwargs):
    """Search a Nova SIMH instance's captured console text for a phrase """
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No SIMH/Nova instance '{name}' available in context"

    timeout = int(timeout)
    require_success = _as_bool(require_success)
    log = []

    found = instance.wait_for(successphrase, timeout=timeout) if successphrase else True
    failed = bool(failphrase) and failphrase in instance.buf

    log.append(f"{name} console tail:\n{instance.buf[-800:]}")

    if failed:
        log.append(f"Fail phrase '{failphrase}' found on console")
        context["abort"] = True
        instance.stop()
        log.append(f"Stopped {name} (abort path — a later teardown step "
                   f"won't run since context['abort'] short-circuits it)")
        return False, "\n".join(log)

    if require_success and not found:
        log.append(f"Success phrase '{successphrase}' not found "
                   f"(require_success) — failing step")
        context["abort"] = True
        instance.stop()
        log.append(f"Stopped {name} (abort path — a later teardown step "
                   f"won't run since context['abort'] short-circuits it)")
        return False, "\n".join(log)

    log.append(f"success phrase found: {found}")
    return True, "\n".join(log)


@dispatchtest_step
def test_cully_llvm_compile(src_path=None, out_path=None, cpu="nova3",
                            header_lines=None, footer_lines=None,
                            cully_llvm_dir=None, timeout=120, **kwargs):
    """Compile a Nova C source via cully_llvm's nova-llvm-backend (nova-cc) into a SIMH deposit script (live counterpart of the pre-built gen/*.ini).
    header_lines/footer_lines wrap the deposits (e.g. nova_fb "set fb ...", "go 100"/"quit"). Toolchain paths and missing-toolchain fallback live in simhhelpers.compile_via_nova_llvm.
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    src_path = resolve_path(src_path, vars_map)
    out_path = resolve_path(out_path, vars_map)
    cully_llvm_dir = resolve_path(cully_llvm_dir, vars_map) or None

    success, msg = compile_via_nova_llvm(
        src_path, out_path, cpu=cpu, cully_llvm_dir=cully_llvm_dir,
        header_lines=header_lines, footer_lines=footer_lines, timeout=timeout,
    )
    if not success:
        # a failed build must not let test_startnovasimh boot a stale out_path
        context["abort"] = True
    return success, msg


@dispatchtest_step
def test_mouseaction(action=None, name="qemu1", test_step=0,
                     takeascreenshot=False, **kwargs):
    """Run a named MouseAction helper against a QEMU instance (image-match + click macros, Mac m68k tests)."""
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"

    fn = getattr(MouseAction, action, None) if action else None
    if not callable(fn):
        return False, f"Unknown MouseAction '{action}'"

    success, log = fn(instance, test_step=test_step)
    shot_log = []
    _maybe_screenshot(instance, takeascreenshot, shot_log, label=name)
    if shot_log:
        log = f"{log}\n" + "\n".join(shot_log)
    if not success:
        context["abort"] = True
    return success, log


@dispatchtest_step
def test_findbutton_click(button_path=None, name="qemu1", test_step=0, **kwargs):
    """Screenshot the instance, locate button_path, VNC-click its centre (absolute mouse move; VNC mac tests)."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"

    button_path = resolve_path(button_path, vars_map)
    log = []

    ok, shot = instance.take_screenshot(test_step=test_step)
    if not ok:
        return False, f"[{instance.name}] Screenshot failed: {shot}"
    log.append(f"[{instance.name}] Screenshot taken: {shot}")

    found, pos = find_button_in_screenshot(button_path, shot)
    if not found:
        context["abort"] = True
        log.append(f"Button not found: {button_path}")
        return False, "\n".join(log)

    x, y = pos
    log.append(f"Button found at {pos}")

    # q800 ADB mouse is relative: position + click via GTK-window closed loop (xdotool + screendump)
    ok, msg = instance.gui_move_to(x, y, do_click=True)
    log.append(msg)
    if not ok:
        context["abort"] = True
        return False, "\n".join(log)

    ok, shot = instance.take_screenshot(test_step=test_step)
    log.append(f"[{instance.name}] Post-click screenshot: {shot}")
    return True, "\n".join(log)


@dispatchtest_step
def test_find_and_open_icon(icon_path=None, name="qemu1", test_step=0,
                            clicks=2, tolerance=0.12, pixel_delta=48,
                            mask_path=None, region=None, exact_fallback=True,
                            **kwargs):
    """Locate a Finder icon, mounted disk or application in a window and OPEN it with a double-click (find_icon_in_screenshot, falls back to the exact matcher if asked; GTK/xdotool closed loop since the q800 ADB mouse is relative). Params:
      icon_path     template PNG (crop tight to the glyph)
      clicks        2 = double-click to open; 1 = just select
      tolerance     max fraction of pixels allowed to mismatch
      pixel_delta   grayscale delta that counts a pixel as different
      mask_path     optional PNG, black = ignore
      region        optional "x0,y0,x1,y1" search box (disks mount top-right)
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"

    icon_path = resolve_path(icon_path, vars_map)
    mask_path = resolve_path(mask_path, vars_map) or None
    log = []

    # region: "x0,y0,x1,y1" string or list
    roi = None
    if region:
        if isinstance(region, str):
            roi = tuple(int(v) for v in region.split(","))
        else:
            roi = tuple(int(v) for v in region)

    ok, shot = instance.take_screenshot(test_step=test_step)
    if not ok:
        return False, f"[{instance.name}] Screenshot failed: {shot}"
    log.append(f"[{instance.name}] Screenshot taken: {shot}")

    found, pos = find_icon_in_screenshot(
        icon_path, shot, tolerance=float(tolerance),
        pixel_delta=int(pixel_delta), mask_path=mask_path, region=roi)
    if not found:
        log.append(f"tolerant match: {pos}")
        if exact_fallback:
            found, pos = find_button_in_screenshot(icon_path, shot)
            log.append(f"exact-match fallback: {'hit '+str(pos) if found else pos}")
    if not found:
        context["abort"] = True
        log.append(f"Icon not found: {icon_path}")
        return False, "\n".join(log)

    x, y = pos
    log.append(f"Icon found at {pos}; opening with {int(clicks)} click(s)")
    ok, msg = instance.gui_move_to(x, y, do_click=True, clicks=int(clicks))
    log.append(msg)
    if not ok:
        context["abort"] = True
        return False, "\n".join(log)

    time.sleep(2)
    ok, shot = instance.take_screenshot(test_step=test_step)
    log.append(f"[{instance.name}] Post-open screenshot: {shot}")
    return True, "\n".join(log)


@dispatchtest_step
def test_mouse_drag(name="qemu1", test_step=0, from_xy=None, to_xy=None,
                    from_icon=None, to_icon=None, from_region=None,
                    to_region=None, hold=0, tolerance=0.12, pixel_delta=48,
                    takeascreenshot=True, dwell=1.0, **kwargs):
    """Press-drag-release on the q800's relative ADB mouse (QemuInstance.gui_move_to drag_to=). Each end: guest pixel "x,y" (from_xy / to_xy) or icon template PNG (from_icon / to_icon, optional from_region / to_region "x0,y0,x1,y1"; click point = template centre). hold = seconds with the button down after the press and before the release."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    log = []

    def box(v):
        if not v:
            return None
        return tuple(int(x) for x in (v.split(",") if isinstance(v, str) else v))

    def end(label, xy, icon, region):
        if xy:
            x, y = box(xy)
            return (x, y), None
        icon = resolve_path(icon, vars_map)
        ok, shot = instance.take_screenshot(test_step=test_step)
        if not ok:
            return None, f"[{instance.name}] Screenshot failed: {shot}"
        found, pos = find_icon_in_screenshot(
            icon, shot, tolerance=float(tolerance), pixel_delta=int(pixel_delta),
            region=box(region))
        if not found:
            found, pos = find_button_in_screenshot(icon, shot)
        if not found:
            return None, f"{label} icon not found: {icon} ({pos})"
        log.append(f"{label} icon at {pos}")
        return tuple(pos), None

    src, err = end("from", from_xy, from_icon, from_region)
    if err:
        context["abort"] = True
        return False, err
    if isinstance(to_xy, str) and ";" in to_xy:
        # waypoints "x,y;x,y;..." (e.g. Apple menu row, then submenu)
        dst = [tuple(box(w)) for w in to_xy.split(";")]
    else:
        dst, err = end("to", to_xy, to_icon, to_region)
        if err:
            context["abort"] = True
            return False, err

    ok, msg = instance.gui_move_to(src[0], src[1], do_click=False,
                                   drag_to=dst, hold_s=float(hold), dwell_s=float(dwell))
    log.append(msg)
    shot_log = []
    time.sleep(1)
    _maybe_screenshot(instance, takeascreenshot, shot_log, label=name)
    log.extend(shot_log)
    if not ok:
        context["abort"] = True
    return ok, "\n".join(log)


@dispatchtest_step
def test_closeallwindows(button_path=None, name="qemu1", max_windows=8, **kwargs):
    """Close every open window by repeatedly locating a close-box image and clicking it."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"

    button_path = resolve_path(
        button_path or os.path.join(TESTSRC_BASEDIR, "buttontest", "finder_window_closebutton.png"), vars_map)

    max_windows = int(max_windows)
    log = []
    frames = []
    tmp_shot = f"/tmp/_closewin_{instance.name}.png"

    def capture():
        """Grab the guest framebuffer -> temp png + PIL frame for the gif; returns the temp path (or None)."""
        arr = instance._gui_screencap()
        if arr is None:
            return None
        img = Image.fromarray(arr.astype("uint8"))
        img.save(tmp_shot)
        frames.append(img.copy())
        return tmp_shot

    closed = 0
    for i in range(max_windows):
        shot = capture()
        if shot is None:
            return False, "screencap failed (monitor screendump)"
        found, pos = find_button_in_screenshot(button_path, shot)
        if not found:
            log.append(f"no close box found — {closed} window(s) closed")
            break
        okm, msg = instance.gui_move_to(pos[0], pos[1], do_click=True)
        log.append(f"window {i}: close box {pos} -> {msg.splitlines()[-1] if msg else ''}")
        closed += 1
        time.sleep(1.2)
    else:
        log.append(f"hit max_windows={max_windows}; {closed} closed")

    capture()

    # frames -> animated gif screenshot-<name>-<stepnum>.gif
    if frames:
        try:
            from appstate import progress_state, current_reports_dir
            stepnum = re.match(r"\d+", str(progress_state.step)).group(0)
            reports_dir = current_reports_dir()
        except Exception:
            stepnum = "0"
            reports_dir = "/testrunnerapp/reports"
        gif_path = os.path.join(reports_dir, f"screenshot-{instance.name}-{stepnum}.gif")
        try:
            frames[0].save(gif_path, save_all=True, append_images=frames[1:],
                           duration=900, loop=0, optimize=False)
            log.append(f"animated gif ({len(frames)} frames): {gif_path}")
        except Exception as e:
            log.append(f"gif build failed: {e}")

    return True, "\n".join(log)


def _parse_region(region):
    """'x0,y0,x1,y1' (config string) or a 4-list -> tuple of ints, or None."""
    if region in (None, ""):
        return None
    if isinstance(region, str):
        region = region.split(",")
    return tuple(int(v) for v in region)


def _guest_frame(instance, path):
    """Screenshot `instance` to `path` as an RGB PIL image in guest pixels (undoes instance.screenshot_scale)."""
    ok, msg = instance.take_screenshot(filename=path)
    if not ok:
        return None, f"screenshot failed: {msg}"
    img = Image.open(path).convert("RGB")
    scale = float(getattr(instance, "screenshot_scale", 1.0) or 1.0)
    if scale != 1.0:
        img = img.resize((round(img.width / scale), round(img.height / scale)))
    return img, ""


@dispatchtest_step
def test_ocr_click(text=None, name="qemu1", region=None, occurrence=1, clicks=1,
                   button=1, offset_x=0, offset_y=0, numberofattempts=None,
                   attemptdelay=None, wait_after=1.5, park_after=True,
                   takeascreenshot=True, **kwargs):
    """Find `text` on a PC guest's screen with OCR, move the relative PS/2 mouse onto it and click (Windows 3.1 etc. under QEMU 'pc' machines; see guiclickhelpers.ocr_find, guiclickhelpers.RelativeMouse closed loop).
      text        phrase to click; case and '&' accelerators ignored
      region      "x0,y0,x1,y1" search box (guest pixels), default whole screen
      occurrence  1-based, reading order, when the phrase is on screen twice
      clicks      2 = double-click (open a program icon)
      offset_x/y  click this many pixels away from the phrase centre
      numberofattempts/attemptdelay  as test_ocrwordsearch: use "10" and "2"
      park_after  peg the pointer back to the top-left corner after the click
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    text = resolve_path(text, vars_map)
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"
    if not text:
        return False, "no text to click"
    region = _parse_region(region)
    attempts_n, delay = _ocr_schedule(numberofattempts, attemptdelay, None, None)

    shot_path = os.path.join(ocr_log_dir(), f"{name}-ocrclick-{os.getpid()}.png")
    log = []
    mouse = guiclickhelpers.RelativeMouse(instance, shot_path, log)

    found, box = False, None
    for attempt in range(attempts_n):
        try:
            # pegged first: pointer must not sit on the OCR'd words
            mouse.peg()
            img, err = _guest_frame(instance, shot_path)
        except RuntimeError as e:
            img, err = None, str(e)
        if img is None:
            log.append(err)
        else:
            found, box, words = guiclickhelpers.ocr_find(
                img, text, region=region, occurrence=int(occurrence))
            if found:
                break
            log.append(f"attempt {attempt + 1}/{attempts_n}: {box}; OCR saw: "
                       + " ".join(w["text"] for w in words)[:300])
        time.sleep(delay)

    if not found:
        context["abort"] = True
        _maybe_screenshot(instance, takeascreenshot, log, label=name)
        return False, "\n".join(log)

    x, y, w, h = box
    cx, cy = x + w / 2 + float(offset_x), y + h / 2 + float(offset_y)
    log.append(f"found {text!r} at box ({x:.0f},{y:.0f},{w:.0f},{h:.0f}); clicking ({cx:.0f},{cy:.0f})")
    try:
        ok, msg = mouse.goto(cx, cy, peg=False)
        if not ok:
            context["abort"] = True
            log.append(msg)
            return False, "\n".join(log)
        mouse.click(button=int(button), clicks=int(clicks))
        time.sleep(float(wait_after))
        if _as_bool(park_after):
            mouse.peg()
    except RuntimeError as e:
        context["abort"] = True
        log.append(str(e))
        return False, "\n".join(log)
    _maybe_screenshot(instance, takeascreenshot, log, label=name)
    return True, "\n".join(log)


@dispatchtest_step
def test_win31_close_groups(name="qemu1", max_groups=20, max_stuck=6, wait_after=1.5,
                            takeascreenshot=True, **kwargs):
    """Close every open group window in Windows 3.1's Program Manager: Alt+- opens the control menu, the mouse clicks 'Close' (OCR). A click that leaves the screen unchanged sends Ctrl+F6 (next child); `max_stuck` unchanged clicks in a row end the sweep, as does Alt+- no longer bringing up the menu (recognised by 'Maximize')."""
    context = kwargs.get("context", {})
    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"

    shot_path = os.path.join(ocr_log_dir(), f"{name}-closegroups-{os.getpid()}.png")
    log = []
    mouse = guiclickhelpers.RelativeMouse(instance, shot_path, log)
    closed = stuck = 0
    try:
        for _ in range(int(max_groups) + int(max_stuck) * 4):
            mouse.peg()
            before, err = _guest_frame(instance, shot_path)
            if before is None:
                context["abort"] = True
                return False, "\n".join(log + [err])
            instance.send_specialkeys("minus", alt=True, delay=0.3)
            time.sleep(1.2)
            img, err = _guest_frame(instance, shot_path)
            if img is None:
                context["abort"] = True
                return False, "\n".join(log + [err])
            menu, _, _ = guiclickhelpers.ocr_find(img, "Maximize")
            if not menu:
                break
            found, box, words = guiclickhelpers.ocr_find(img, "Close")
            if not found:
                context["abort"] = True
                log.append("group control menu is open but 'Close' was not found; OCR saw: "
                           + " ".join(w["text"] for w in words)[:300])
                _maybe_screenshot(instance, takeascreenshot, log, label=name)
                return False, "\n".join(log)
            ok, msg = mouse.goto(box[0] + box[2] / 2, box[1] + box[3] / 2, peg=False)
            if not ok:
                context["abort"] = True
                return False, "\n".join(log + [msg])
            mouse.click()
            time.sleep(float(wait_after))
            mouse.peg()
            after, err = _guest_frame(instance, shot_path)
            changed = int((np.asarray(before) != np.asarray(after)).any(axis=2).sum())
            if changed > 300:
                closed += 1
                stuck = 0
            else:
                stuck += 1
                log.append(f"Close changed nothing (icon was active); Ctrl+F6 to next child ({stuck}/{max_stuck})")
                if stuck >= int(max_stuck):
                    break
                instance.send_specialkeys("f6", ctrl=True, delay=0.5)
                time.sleep(0.5)
        else:
            context["abort"] = True
            log.append("group sweep did not settle")
            return False, "\n".join(log)
        # Alt+- with nothing to open can leave the menu bar armed
        instance.send_specialkeys("esc", delay=0.3)
        mouse.peg()
    except RuntimeError as e:
        context["abort"] = True
        return False, "\n".join(log + [str(e)])
    log.append(f"closed {closed} group window(s)")
    _maybe_screenshot(instance, takeascreenshot, log, label=name)
    return True, "\n".join(log)


@dispatchtest_step
def test_mouse_hang_probe(name="qemu1", rounds=24, moves_per_round=6, step_px=24,
                          settle=0.05, latency_ratio_threshold=4.0, freeze_rounds=4,
                          out_log=None, takeascreenshot=False, **kwargs):
    """Stress a PC-machine guest's PS/2 mouse over an extended period and watch for a progressive freeze. Fails (and aborts, so test_terminate_all still tears down) if either signal crosses its threshold in the back half of the run."""
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    out_log = resolve_path(out_log, vars_map) if out_log else None

    instance = context.get(name)
    if not instance:
        return False, f"No QEMU instance '{name}' available in context"

    rounds = int(rounds)
    moves_per_round = int(moves_per_round)
    step_px = int(step_px)
    settle = float(settle)
    latency_ratio_threshold = float(latency_ratio_threshold)
    freeze_rounds = int(freeze_rounds)

    pattern = [(step_px if i % 2 == 0 else -step_px, 0) for i in range(moves_per_round)]

    log_lines = []
    latencies = []
    diffs = []
    prev_frame = None

    for r in range(rounds):
        t0 = time.time()
        for dx, dy in pattern:
            ok, msg = instance.send_mouse_pos(dx, dy)
            if not ok:
                log_lines.append(f"round {r}: mouse_move failed: {msg}")
            time.sleep(settle)
        shot_ok, shot_path = instance.take_screenshot()
        latency = time.time() - t0
        latencies.append(latency)

        diff = None
        if shot_ok:
            try:
                frame = np.asarray(Image.open(shot_path).convert("RGB")).astype(np.int32)
                if prev_frame is not None and prev_frame.shape == frame.shape:
                    diff = int(np.abs(frame - prev_frame).sum())
                prev_frame = frame
            except Exception as e:
                log_lines.append(f"round {r}: frame load failed: {e}")
        else:
            log_lines.append(f"round {r}: screendump failed: {shot_path}")
        diffs.append(diff)

        log_lines.append(f"round {r}: latency={latency:.2f}s diff={diff}")

    baseline_n = max(1, rounds // 6)
    tail_n = max(1, min(freeze_rounds, rounds // 2))
    baseline_latency = sorted(latencies[:baseline_n])[len(latencies[:baseline_n]) // 2]
    tail_latencies = latencies[-tail_n:]
    tail_latency = sorted(tail_latencies)[len(tail_latencies) // 2]
    latency_ratio = (tail_latency / baseline_latency) if baseline_latency > 0 else float("inf")

    early_diffs = [d for d in diffs[baseline_n:baseline_n + tail_n] if d is not None]
    tail_diffs = [d for d in diffs[-tail_n:] if d is not None]
    early_moved = any(d and d > 0 for d in early_diffs)
    tail_frozen = len(tail_diffs) == tail_n and all((d or 0) == 0 for d in tail_diffs)

    verdict_lines = [
        f"baseline_latency={baseline_latency:.2f}s tail_latency={tail_latency:.2f}s "
        f"ratio={latency_ratio:.2f} (threshold {latency_ratio_threshold})",
        f"early_moved={early_moved} tail_frozen={tail_frozen} (last {tail_n} rounds)",
    ]

    hang_detected = (latency_ratio >= latency_ratio_threshold) or (early_moved and tail_frozen)
    if hang_detected:
        verdict = "MOUSE_HANG_DETECTED"
    else:
        verdict = "MOUSE_OK"
    verdict_lines.insert(0, verdict)

    full_log = "\n".join(verdict_lines + log_lines)
    if out_log:
        try:
            os.makedirs(os.path.dirname(out_log), exist_ok=True)
            with open(out_log, "w") as f:
                f.write(full_log + "\n")
        except Exception as e:
            full_log += f"\n(failed to write out_log {out_log}: {e})"

    _maybe_screenshot(instance, takeascreenshot, [], label=name)

    if hang_detected:
        context["abort"] = True
        return False, full_log
    return True, full_log


def _as_lines(v):
    """Step param -> list of lines (split on newlines only; commas are ordinary in DOS/emulator command lines, unlike _as_list)."""
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [ln for ln in str(v).splitlines() if ln.strip()]


def _resolve_tokens(text, vars_map, passes=5):
    """Resolve {tokens} in a file's CONTENT one token at a time (unlike resolve_path, a token without a CONFIG value does not abort the file). Unresolved tokens stay verbatim. Returns (text, sorted list of unresolved token names)."""
    atomic = {k: v for k, v in vars_map.items() if isinstance(v, (str, int))}
    pattern = re.compile(r"\{([a-zA-Z_]\w*)\}")

    def sub(m):
        key = m.group(1)
        return str(atomic[key]) if key in atomic else m.group(0)

    for _ in range(passes):
        new_text = pattern.sub(sub, text)
        if new_text == text:
            break
        text = new_text

    leftover = sorted({m.group(1) for m in pattern.finditer(text)})
    return text, leftover


def _current_stepnum(default="0"):
    """Number of the step now running (for report screenshot names, see test_attach_screenshot)."""
    _helperdir = "/testrunnersrc/pyhelpers"
    if _helperdir not in sys.path:
        sys.path.insert(0, _helperdir)
    try:
        from appstate import progress_state
        return re.match(r"\d+", str(progress_state.step)).group(0)
    except Exception:
        return default


def _gui_instance(context, name=None):
    """DOSBox-X / 86Box instance from context; with no name, the only such instance -> (instance, error_message)."""
    if name:
        inst = context.get(name)
        if not isinstance(inst, GUI_INSTANCE_TYPES):
            return None, f"No DOSBox-X/86Box instance '{name}' available in context"
        return inst, None

    found = []
    for inst in context.values():
        if isinstance(inst, GUI_INSTANCE_TYPES) and not any(inst is f for f in found):
            found.append(inst)
    if not found:
        return None, "No DOSBox-X/86Box instance available in context"
    if len(found) > 1:
        return None, ("Several DOSBox-X/86Box instances are running "
                      f"({', '.join(i.name for i in found)}) — pass name=")
    return found[0], None


def _gui_instances(context):
    """Every DOSBox-X / 86Box instance in context, keyed by context name (for steps that hit every emulator, e.g. test_sendcommand's screenshot-only call) -> (list, error)."""
    found = []
    for key, inst in context.items():
        if isinstance(inst, GUI_INSTANCE_TYPES) and not any(inst is f for _, f in found):
            found.append((key, inst))
    if not found:
        return None, "No DOSBox-X/86Box instance available in context"
    return found, None


def _dead_gui_instance(context):
    """Name of a DOSBox-X/86Box instance in context that has exited, else None (also None if no GUI instance is in context); lets a step waiting on an off-screen artifact give up when the window is closed."""
    for inst in context.values():
        if isinstance(inst, GUI_INSTANCE_TYPES):
            alive = getattr(inst, "is_alive", None)
            if alive is not None and not alive():
                return inst.name
    return None


@dispatchtest_step
def test_flatten_qcow_to_raw(qcow_path=None, raw_path=None, hdd1_template=None,
                             **kwargs):
    """Flatten a QCOW2 overlay (plus its backing template) into a raw image, for DOSBox-X / 86Box (which cannot read QCOW2); the template is never written.
    hdd1_template (template filename in /testsrc/templates or absolute path): optional, used only if qcow_path does not exist yet (overlay created from it first).
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    qcow_path = resolve_path(qcow_path, vars_map)
    raw_path = resolve_path(raw_path, vars_map)
    hdd1_template = resolve_path(hdd1_template or config.get("hdd1_template"), vars_map)

    if not qcow_path or not raw_path:
        return False, "test_flatten_qcow_to_raw: qcow_path and raw_path are required"

    if not os.path.isfile(qcow_path):
        if not hdd1_template:
            context["abort"] = True
            return False, (f"No QCOW2 at {qcow_path} and no hdd1_template to build "
                           f"one from")
        if not _prepare_hdd_overlay(hdd1_template, qcow_path, log, persist=False):
            context["abort"] = True
            return False, "\n".join(log)

    success, output = convert_qcow2_to_raw(qcow_path, raw_path)
    log.append(output)
    if not success:
        context["abort"] = True
    return success, "\n".join(log)


@dispatchtest_step
def test_copy_tree(src_dir=None, dest_dir=None, clean=True, **kwargs):
    """Copy a directory tree on the host, replacing the destination by default (throwaway copy of an emulator's VM dir, or of src/ for a build)."""
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    src_dir = resolve_path(src_dir, vars_map)
    dest_dir = resolve_path(dest_dir, vars_map)
    if not src_dir or not dest_dir:
        return False, "test_copy_tree: src_dir and dest_dir are required"
    if not os.path.isdir(src_dir):
        context["abort"] = True
        return False, f"source directory not found: {src_dir}"

    try:
        if _as_bool(clean) and os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir)
        os.makedirs(os.path.dirname(os.path.abspath(dest_dir)) or ".", exist_ok=True)
        shutil.copytree(src_dir, dest_dir, dirs_exist_ok=not _as_bool(clean))
    except OSError as e:
        context["abort"] = True
        return False, "\n".join(log + [f"copy failed: {e}"])

    log.append(f"Copied {src_dir} -> {dest_dir}")
    return True, "\n".join(log)


@dispatchtest_step
def test_render_file(template_path=None, out_path=None, **kwargs):
    """Write out a config file with its {tokens} resolved against CONFIG (for generated files with host paths, not hand-edited ones; a GUI-tuned emulator config stays in the project dir, e.g. __testlist__OWC_CLONETEST2_86box.py's 86box.cfg)."""
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    template_path = resolve_path(template_path, vars_map)
    out_path = resolve_path(out_path, vars_map)
    if not template_path or not out_path:
        return False, "test_render_file: template_path and out_path are required"
    if not os.path.isfile(template_path):
        context["abort"] = True
        return False, f"template not found: {template_path}"

    with open(template_path, "r") as f:
        rendered, leftover = _resolve_tokens(f.read(), vars_map)

    if leftover:
        context["abort"] = True
        return False, (f"{template_path}: no value in CONFIG for "
                       f"{', '.join('{'+t+'}' for t in leftover)}")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(rendered)
    log.append(f"Rendered {template_path} -> {out_path}")
    log.append(f"ARTIFACT: {out_path}")
    return True, "\n".join(log)


@dispatchtest_step
def test_dosbox_conf(config_path=None, template_path=None, autoexec=None,
                     machine=None, memsize=None, cpu_cycles=None, **kwargs):
    """Build the dosbox-x .conf a run boots from, written to config_path.
    template_path: full dosbox-x config; only [autoexec] lines are added. Without it a minimal config is generated from machine/memsize.
    `autoexec`: lines to run at boot. `cpu_cycles`, if given, is written as `[cpu]` then `cycles=<value>`.
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    config_path = resolve_path(config_path, vars_map)
    template_path = resolve_path(template_path or config.get("config_path"), vars_map)
    if not config_path:
        return False, "test_dosbox_conf: config_path is required"

    os.makedirs(os.path.dirname(os.path.abspath(config_path)) or ".", exist_ok=True)

    if template_path and os.path.isfile(template_path):
        shutil.copyfile(template_path, config_path)
        log.append(f"Based on template {template_path}")
    elif template_path:
        context["abort"] = True
        return False, f"dosbox template conf not found: {template_path}"
    else:
        machine = resolve_path(machine or config.get("machine") or "svga_s3", vars_map)
        memsize = resolve_path(memsize or config.get("memsize") or 16, vars_map)
        with open(config_path, "w") as f:
            f.write(f"[dosbox]\nmachine={machine}\nmemsize={memsize}\n\n[autoexec]\n")
        log.append(f"Generated a minimal config (machine={machine}, memsize={memsize})")

    conf = DosboxConf(config_path)
    conf.disable_quit_warning()

    cpu_cycles = resolve_path(cpu_cycles, vars_map) if cpu_cycles else None
    if cpu_cycles:
        conf.section_insert("[cpu]", f"cycles={cpu_cycles}")
        log.append(f"  cpu: cycles={cpu_cycles}")

    lines = [resolve_path(ln, vars_map) for ln in _as_lines(autoexec)]
    if lines:
        # autoexec_insert inserts after [autoexec]: insert in reverse to keep testlist order
        for line in reversed(lines):
            conf.autoexec_insert(line)
        for line in lines:
            log.append(f"  autoexec: {line}")
    conf.save(config_path)

    log.append(f"Wrote {config_path}")
    log.append(f"ARTIFACT: {config_path}")
    return True, "\n".join(log)


@dispatchtest_step
def test_start_dosbox(name="dosbox1", config_path=None, display=None,
                      timeout=20, takeascreenshot=False, **kwargs):
    """Launch dosbox-x on the config test_dosbox_conf wrote and wait for its window; display is unset by default."""
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    config_path = resolve_path(config_path, vars_map) or None
    display = resolve_path(display, vars_map) or None

    instance = DosboxInstance(name, config_path, display=display,
                              screenshot_scale=config.get("ocr_scale"),
                              window_slot=_next_window_slot(context))
    log.append(f"Starting DOSBox-X '{name}' with config={config_path}")
    if not instance.start():
        log.append("Failed to start DOSBox-X.")
        log.extend(instance.stdout_lines[:10])
        context["abort"] = True
        return False, "\n".join(log)

    if not instance.wait_for_ready(timeout=int(timeout)):
        log.append(f"Timeout ({timeout}s) waiting for the DOSBox-X window.")
        log.extend(instance.stdout_lines[:10])
        instance.stop()
        context["abort"] = True
        return False, "\n".join(log)

    context[name] = instance
    log.append(f"{name} is ready on display {instance.display}")
    _maybe_screenshot(instance, takeascreenshot, log, label=name)
    return True, "\n".join(log)


SAVED_CFG_NAME = "box_cfg_saved.json"
SAVED_CFG_IGNORE = {("General", "host_cpu"), ("General", "uuid")}


def _save_cfg_changes(vm_path, vm_seed, config, vars_map, log):
    """Settings changed in 86Box's own Settings dialog (CPU speed, RAM, ...) live in 86box.cfg, which test_86box_cfg rebuilds from CONFIG['box_cfg'] every run. Diff the 86box.cfg 86Box wrote against box_cfg and keep the differences in vm_seed/box_cfg_saved.json; test_86box_cfg merges them back over box_cfg. Appends what it did to `log`; False only on a write error."""
    import configparser
    import json
    cfg_path = os.path.join(vm_path, "86box.cfg")
    base = config.get("box_cfg") or {}
    if not os.path.isfile(cfg_path):
        log.append(f"no {cfg_path}: cfg settings not saved")
        return True
    cp = configparser.RawConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read(cfg_path, encoding="utf-8")
    except (OSError, configparser.Error) as e:
        log.append(f"cannot parse {cfg_path}: {type(e).__name__}: {e}: cfg settings not saved")
        return True
    diff = {}
    for sec in cp.sections():
        for key, val in cp.items(sec):
            if (sec, key) in SAVED_CFG_IGNORE:
                continue
            want = (base.get(sec) or {}).get(key)
            if want is not None:
                want = _resolve_tokens(str(want), vars_map)[0]
            # private_media rewrites image paths to media/<name>; those are not user settings
            if key in (base.get(sec) or {}) and "media/" in val:
                continue
            if want != val:
                diff.setdefault(sec, {})[key] = val
                log.append(f"  86box.cfg [{sec}] {key}: {want if want is not None else '(unset)'} -> {val}")
    out = os.path.join(vm_seed, SAVED_CFG_NAME)
    try:
        os.makedirs(vm_seed, exist_ok=True)
        if diff:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(diff, fh, indent=1, sort_keys=True)
            log.append(f"Saved {sum(len(v) for v in diff.values())} cfg setting(s) -> {out}")
        elif os.path.isfile(out):
            os.remove(out)
            log.append(f"86box.cfg matches box_cfg: removed old {out}")
        else:
            log.append("86box.cfg matches box_cfg: no cfg settings changed")
    except OSError as e:
        log.append(f"FAILED to write {out}: {type(e).__name__}: {e}")
        return False
    return True


@dispatchtest_step
def test_vm_save_seed(name="box86_1", vm_path=None, vm_seed=None, timeout=15, **kwargs):
    """Copy a VM config's VM dir nvr/ into its vm_seed (the cfg step copies vm_seed over the VM dir each run, so hand BIOS changes are lost otherwise). If instance `name` is still up it is asked to quit via the manager socket first (86Box writes nvr/ on a real quit; see test_close_instance_window); if the user closed it, it just copies.
    Teardown step of a VM config only (before test_terminate_all); consumers never get it. Empty or missing nvr/ leaves the seed as is (not a failure).
    """
    context = kwargs.get("context", {}) or {}
    vars_map = build_vars(kwargs.get("config", {}))
    vm_path = resolve_path(vm_path, vars_map) or None
    vm_seed = resolve_path(vm_seed, vars_map) or None
    if not vm_path or not vm_seed or "{" in vm_path or "{" in vm_seed:
        return False, f"vm_path and vm_seed must resolve (got {vm_path!r}, {vm_seed!r})"
    log = []

    inst = context.get(name)
    if inst is not None and hasattr(inst, "is_alive") and inst.is_alive():
        if hasattr(inst, "request_graceful_shutdown") and inst.request_graceful_shutdown(timeout=int(timeout)):
            log.append(f"{name} quit cleanly (nvr/ written)")
        else:
            log.append(f"{name} did not quit within {timeout}s: nvr/ may be stale. "
                       f"Close 86Box yourself to save the BIOS setup.")

    cfg_ok = _save_cfg_changes(vm_path, vm_seed, kwargs.get("config", {}), vars_map, log)

    src = os.path.join(vm_path, "nvr")
    files = sorted(os.listdir(src)) if os.path.isdir(src) else []
    if not files:
        log.append(f"no nvr files in {src}: seed left as it is")
        return cfg_ok, "\n".join(log)
    dst = os.path.join(vm_seed, "nvr")

    def _read(p):
        try:
            with open(p, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    # what changes vs the old seed, per file, so a save that changed nothing is visible
    changed = []
    for f in files:
        new, old = _read(os.path.join(src, f)), _read(os.path.join(dst, f))
        if old is None:
            log.append(f"  {f}: new in seed ({len(new or b'')} bytes)")
            changed.append(f)
        elif new == old:
            log.append(f"  {f}: unchanged ({len(new)} bytes)")
        else:
            ndiff = sum(a != b for a, b in zip(new, old)) + abs(len(new) - len(old))
            log.append(f"  {f}: CHANGED, {ndiff} byte(s) differ ({len(old)} -> {len(new)} bytes)")
            changed.append(f)
        mt = os.path.getmtime(os.path.join(src, f))
        if time.time() - mt > 6 * 3600:
            log.append(f"  {f}: WARNING last written {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mt))}, "
                       f"not by this run: 86Box did not write nvr/ at quit")

    try:
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        os.makedirs(vm_seed, exist_ok=True)
        shutil.copytree(src, dst)
    except OSError as e:
        log.append(f"FAILED to update seed {dst}: {type(e).__name__}: {e}")
        return False, "\n".join(log)

    # read back: the seed on disk must now equal the VM dir
    bad = [f for f in files if _read(os.path.join(src, f)) != _read(os.path.join(dst, f))]
    if bad:
        log.append(f"seed verify FAILED, {', '.join(bad)} differ from {src} after copy")
        return False, "\n".join(log)
    if not changed:
        log.append("seed identical to the previous one: no BIOS setup change was written by 86Box")
    log.append(f"Saved {', '.join(files)} -> {dst} (verified)")
    return cfg_ok, "\n".join(log)


@dispatchtest_step
def test_find_windows_com_port(usb_id=None, name_contains=None, section=None, key="path", cfg_key="box_cfg", **kwargs):
    """Find a USB serial adapter's COM port on this Windows host and put it in CONFIG[cfg_key][section][key] for the later cfg step.
    usb_id: substring of the PnP DeviceID (e.g. VID_067B).
    name_contains: substring of the device Name (e.g. FT232R), case-insensitive. At least one of usb_id / name_contains is required; when both are given a device must match both. Looked up among working (Status OK) Win32_PnPEntity Ports devices; (COMn) read off its Name. Written in place on the nested dict (steps get a shallow config copy). More than one match fails. Run before the boot unit.
    """
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    usb_id = resolve_path(usb_id, vars_map) if usb_id else ""
    name_contains = resolve_path(name_contains, vars_map) if name_contains else ""
    section = resolve_path(section, vars_map)
    if not (usb_id or name_contains) or not section:
        return False, "test_find_windows_com_port: usb_id or name_contains, and section, are required"
    want = " + ".join(x for x in (f"DeviceID~{usb_id}" if usb_id else "", f"Name~{name_contains}" if name_contains else "") if x)
    sections = config.get(cfg_key)
    if not isinstance(sections, dict) or not isinstance(sections.get(section), dict):
        return False, f"test_find_windows_com_port: CONFIG[{cfg_key!r}][{section!r}] is not a section dict"
    if not IS_WINDOWS:
        return False, "test_find_windows_com_port: Windows only"

    ps = ("Get-CimInstance Win32_PnPEntity -Filter \"PNPClass='Ports'\" | "
          "Select-Object Status,DeviceID,Name | ConvertTo-Csv -NoTypeInformation")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"test_find_windows_com_port: powershell failed: {type(e).__name__}: {e}"
    if proc.returncode != 0:
        return False, f"test_find_windows_com_port: powershell exit {proc.returncode}: {proc.stderr.strip()}"

    import csv
    import io
    all_rows = list(csv.DictReader(io.StringIO(proc.stdout)))
    rows = [r for r in all_rows
            if usb_id.lower() in (r.get("DeviceID") or "").lower()
            and name_contains.lower() in (r.get("Name") or "").lower()]
    log = [f"USB serial ports matching {want} (Status, DeviceID, Name):"]
    log += [f"  {r.get('Status')}, {r.get('DeviceID')}, {r.get('Name')}" for r in rows] or ["  (none)"]
    if not rows:
        log += ["All Ports devices Windows reported:"]
        log += [f"  {r.get('Status')}, {r.get('DeviceID')}, {r.get('Name')}" for r in all_rows] or ["  (none)"]
    found = []
    for r in rows:
        m = re.search(r"\((COM\d+)\)\s*$", r.get("Name") or "")
        if r.get("Status") == "OK" and m:
            found.append(m.group(1))
    if not found:
        return False, "\n".join(log + [f"no working ({want}) USB serial adapter present -- "
                                       "plugged in, driver OK in Device Manager?"])
    if len(found) > 1:
        return False, "\n".join(log + [f"{len(found)} adapters match {want} ({', '.join(found)}) -- "
                                       "unplug the extras"])
    sections[section][key] = found[0]
    return True, "\n".join(log + [f"[{section}] {key} = {found[0]}"])


@dispatchtest_step
def test_find_linux_tty_port(driver=None, section=None, key="path", sudo_password=None, cfg_key="box_cfg", **kwargs):
    """Find a USB serial adapter's /dev/ttyUSBn on this Linux host by its kernel driver and put it in CONFIG[cfg_key][section][key] for the later cfg step.
    driver: kernel driver the tty node is bound to (/sys/class/tty/ttyUSB*/device/driver), e.g. ftdi_sio, pl2303. The kernel numbers ttyUSB nodes in probe order, so the node is looked up, never assumed.
    sudo_password: when the node is not rw for the runner, chmod 666 it via sudo -S.
    More than one match, none, or a node already held open (fuser) fails. Run before the boot unit.
    """
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    driver = resolve_path(driver, vars_map) if driver else ""
    section = resolve_path(section, vars_map)
    sudo_password = resolve_path(sudo_password, vars_map) if sudo_password else ""
    if not driver or not section:
        return False, "test_find_linux_tty_port: driver and section are required"
    sections = config.get(cfg_key)
    if not isinstance(sections, dict) or not isinstance(sections.get(section), dict):
        return False, f"test_find_linux_tty_port: CONFIG[{cfg_key!r}][{section!r}] is not a section dict"
    if IS_WINDOWS:
        return False, "test_find_linux_tty_port: Linux only"

    import glob
    log = ["USB serial ttys -> driver:"]
    found = []
    for t in sorted(glob.glob("/sys/class/tty/ttyUSB*")):
        link = os.path.join(t, "device", "driver")
        if not os.path.exists(link):
            continue
        tty = "/dev/" + os.path.basename(t)
        drv = os.path.basename(os.path.realpath(link))
        log.append(f"  {tty} -> {drv}")
        if drv == driver:
            found.append(tty)
    if len(log) == 1:
        log.append("  (none)")
    if not found:
        return False, "\n".join(log + [f"no ttyUSB bound to the {driver} driver -- adapter plugged in? (lsusb; dmesg | grep -i {driver})"])
    if len(found) > 1:
        return False, "\n".join(log + [f"{len(found)} adapters on {driver} ({', '.join(found)}) -- unplug the extras"])
    port = found[0]
    if not (os.access(port, os.R_OK) and os.access(port, os.W_OK)) and sudo_password:
        subprocess.run(["sudo", "-S", "-p", "", "chmod", "666", port], input=sudo_password + "\n",
                       capture_output=True, text=True, timeout=15)
    if not (os.access(port, os.R_OK) and os.access(port, os.W_OK)):
        return False, "\n".join(log + [f"{port} is not read/write for the runner"])
    try:
        fu = subprocess.run(["fuser", port], capture_output=True, text=True, timeout=10)
        if fu.returncode == 0 and fu.stdout.strip():
            return False, "\n".join(log + [f"{port} is already open by pid(s) {fu.stdout.strip()}"])
    except (OSError, subprocess.TimeoutExpired):
        pass
    sections[section][key] = port
    return True, "\n".join(log + [f"[{section}] {key} = {port}"])


@dispatchtest_step
def test_find_macos_tty_port(tty_glob=None, section=None, key="path", cfg_key="box_cfg", **kwargs):
    """Find a USB serial adapter's /dev/cu.* node on this macOS host and put it in CONFIG[cfg_key][section][key] for the later cfg step.
    tty_glob: glob of the callout node the adapter's driver creates, e.g. /dev/cu.usbserial-* (FTDI), /dev/cu.PL2303* (Prolific). The suffix comes from the adapter's serial number / USB port, so it is looked up, never assumed.
    None, more than one match, or a node already held open (lsof) fails. Run before the boot unit.
    """
    config = kwargs.get("config", {})
    vars_map = build_vars(config)
    tty_glob = resolve_path(tty_glob, vars_map) if tty_glob else ""
    section = resolve_path(section, vars_map)
    if not tty_glob or not section:
        return False, "test_find_macos_tty_port: tty_glob and section are required"
    sections = config.get(cfg_key)
    if not isinstance(sections, dict) or not isinstance(sections.get(section), dict):
        return False, f"test_find_macos_tty_port: CONFIG[{cfg_key!r}][{section!r}] is not a section dict"
    if sys.platform != "darwin":
        return False, "test_find_macos_tty_port: macOS only"

    import glob
    found = sorted(glob.glob(tty_glob))
    log = [f"{tty_glob} matches:"] + [f"  {f}" for f in found] + ([] if found else ["  (none)"])
    if not found:
        log += ["all /dev/cu.* nodes:"] + [f"  {f}" for f in sorted(glob.glob("/dev/cu.*"))]
        return False, "\n".join(log + ["adapter plugged in and its driver loaded? (ioreg -p IOUSB; system_profiler SPUSBDataType)"])
    if len(found) > 1:
        return False, "\n".join(log + [f"{len(found)} adapters match -- unplug the extras"])
    port = found[0]
    if not (os.access(port, os.R_OK) and os.access(port, os.W_OK)):
        return False, "\n".join(log + [f"{port} is not read/write for the runner"])
    try:
        lo = subprocess.run(["lsof", "-t", port], capture_output=True, text=True, timeout=10)
        if lo.stdout.strip():
            return False, "\n".join(log + [f"{port} is already open by pid(s) {' '.join(lo.stdout.split())}"])
    except (OSError, subprocess.TimeoutExpired):
        pass
    sections[section][key] = port
    return True, "\n".join(log + [f"[{section}] {key} = {port}"])


@dispatchtest_step
def test_86box_cfg(vm_path=None, vm_template=None, cfg_key="box_cfg", box_cfg_over=None,
                   private_media=None, nvr_patch=None, **kwargs):
    """Write a VM dir's 86box.cfg from CONFIG[cfg_key], a {section: {key: value}} dict (so a cascade cell can swap single entries). Values may hold {tokens}; None is left out (86Box default), "" is an empty value.
      vm_template    copied to vm_path first (nvr/, BIOS flash), replacing the dir; empty template dir = fresh CMOS. Without it only 86box.cfg is rewritten.
      private_media  list of cfg keys (e.g. ["fdd_01_fn"]): copy each named image to vm_path/media/ and point the cfg at the copy (wp:// kept), since a shared image open in one 86Box is locked across NFS hosts. Small images only.
      box_cfg_over   {section: {key: value}} merged over CONFIG[cfg_key] for this write only (None removes a key); set by overriding this step of the {"use": "vm_boot"} template (vmconfig.py).
      nvr_patch      [{"set": {offset: byte}, "checksum": {"from", "to", "at", "order"}}] applied to the nvr/*.nvr just copied from vm_template (e.g. drive 0 type 2); checksum stores the 2-byte sum of bytes [from, to) at `at`, checked before patching (a seed with a different layout fails).
    """
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    vm_path = resolve_path(vm_path, vars_map)
    vm_template = resolve_path(vm_template, vars_map) or None
    sections = config.get(cfg_key)
    if not vm_path:
        return False, "test_86box_cfg: vm_path is required"
    if not isinstance(sections, dict) or not all(isinstance(v, dict) for v in sections.values()):
        context["abort"] = True
        return False, f"test_86box_cfg: CONFIG[{cfg_key!r}] must be a {{section: {{key: value}}}} dict"

    log = []
    if box_cfg_over:
        if not isinstance(box_cfg_over, dict) or not all(isinstance(v, dict) for v in box_cfg_over.values()):
            context["abort"] = True
            return False, "test_86box_cfg: box_cfg_over must be a {section: {key: value}} dict"
        sections = {sec: dict(ent) for sec, ent in sections.items()}
        for sec, ent in box_cfg_over.items():
            dst = sections.setdefault(sec, {})
            for key, value in ent.items():
                if value is None:
                    dst.pop(key, None)
                else:
                    dst[key] = value
                log.append(f"this machine only: [{sec}] {key} = {value}")
    if vm_template:
        if not os.path.isdir(vm_template):
            context["abort"] = True
            return False, f"VM template directory not found: {vm_template}"
        def _force_rm(func, path, exc):
            # read-only parent dir (NFS/86Box nvr/) or entry (Windows): make writable and retry once
            parent = os.path.dirname(path)
            os.chmod(parent, 0o777)
            try:
                os.chmod(path, 0o777)
            except OSError:
                pass
            func(path)

        try:
            if os.path.isdir(vm_path):
                shutil.rmtree(vm_path, onexc=_force_rm)
            shutil.copytree(vm_template, vm_path)
        except OSError as e:
            # half-cleared VM dir would boot the default machine: stop the run
            context["abort"] = True
            return False, (f"test_86box_cfg: cannot reset VM dir {vm_path} from {vm_template}: "
                           f"{type(e).__name__}: {e}. Check ownership/permissions of the dir "
                           f"(e.g. files left by an 86Box run as another user).")
        log.append(f"Copied VM template {vm_template} -> {vm_path}")
    saved = os.path.join(vm_path, SAVED_CFG_NAME)
    if vm_template and os.path.isfile(saved):
        import json
        try:
            with open(saved, encoding="utf-8") as fh:
                overlay = json.load(fh)
            sections = {sec: dict(ent) for sec, ent in sections.items()}
            for sec, ent in overlay.items():
                for key, value in ent.items():
                    log.append(f"saved with the VM config: [{sec}] {key} = {value}")
                    sections.setdefault(sec, {})[key] = value
            # the test's own per-machine overrides still win
            if box_cfg_over:
                for sec, ent in box_cfg_over.items():
                    for key, value in ent.items():
                        if value is None:
                            sections.get(sec, {}).pop(key, None)
                        else:
                            sections.setdefault(sec, {})[key] = value
        except (OSError, ValueError, AttributeError) as e:
            context["abort"] = True
            return False, f"test_86box_cfg: cannot read saved settings {saved}: {type(e).__name__}: {e}"
    if nvr_patch:
        import glob as _glob
        nvrs = sorted(_glob.glob(os.path.join(vm_path, "nvr", "*.nvr")))
        if len(nvrs) != 1:
            context["abort"] = True
            return False, f"test_86box_cfg: nvr_patch needs exactly one nvr/*.nvr in {vm_path} (found {len(nvrs)})"
        try:
            data = bytearray(open(nvrs[0], "rb").read())
            for patch in nvr_patch:
                cs = patch.get("checksum")
                if cs:
                    have = int.from_bytes(data[cs["at"]:cs["at"] + 2], cs["order"])
                    if sum(data[cs["from"]:cs["to"]]) & 0xFFFF != have:
                        context["abort"] = True
                        return False, (f"test_86box_cfg: {os.path.basename(nvrs[0])} checksum over "
                                       f"[{cs['from']:#x},{cs['to']:#x}) does not match {have:#06x} at {cs['at']:#x}: "
                                       f"this CMOS does not have the layout nvr_patch assumes")
                for off, val in patch["set"].items():
                    log.append(f"nvr {os.path.basename(nvrs[0])}[{int(off):#04x}] {data[int(off)]:#04x} -> {int(val):#04x}")
                    data[int(off)] = int(val)
                if cs:
                    data[cs["at"]:cs["at"] + 2] = (sum(data[cs["from"]:cs["to"]]) & 0xFFFF).to_bytes(2, cs["order"])
            open(nvrs[0], "wb").write(data)
        except (OSError, IndexError, KeyError, ValueError) as e:
            context["abort"] = True
            return False, f"test_86box_cfg: nvr_patch failed on {nvrs[0]}: {type(e).__name__}: {e}"
    try:
        os.makedirs(vm_path, exist_ok=True)
    except OSError as e:
        context["abort"] = True
        return False, f"test_86box_cfg: cannot create VM dir {vm_path}: {type(e).__name__}: {e}"

    if private_media:
        if isinstance(private_media, str):
            private_media = [private_media]
        sections = {sec: dict(ent) for sec, ent in sections.items()}
        media_dir = os.path.join(vm_path, "media")
        for sec, ent in sections.items():
            for key in private_media:
                value = ent.get(key)
                if value in (None, ""):
                    continue
                text, missing = _resolve_tokens(str(value), vars_map)
                if missing:
                    continue
                prefix = "wp://" if text.startswith("wp://") else ""
                src = text[len(prefix):]
                dst = os.path.join(media_dir, os.path.basename(src))
                try:
                    os.makedirs(media_dir, exist_ok=True)
                    shutil.copyfile(src, dst)
                except OSError as e:
                    context["abort"] = True
                    return False, (f"test_86box_cfg: cannot copy {key} image {src} -> {dst}: "
                                   f"{type(e).__name__}: {e}")
                ent[key] = prefix + dst
                log.append(f"private copy of [{sec}] {key}: {src} -> {dst}")

    lines, leftover = [], []
    for section, entries in sections.items():
        body = []
        for key, value in entries.items():
            if value is None:
                continue
            text, missing = _resolve_tokens(str(value), vars_map)
            leftover += missing
            body.append(f"{key} = {text}")
        if body:
            lines += [f"[{section}]"] + body + [""]
    if leftover:
        context["abort"] = True
        return False, (f"test_86box_cfg: no value in CONFIG for "
                       f"{', '.join(sorted(set('{' + t + '}' for t in leftover)))}")
    cfg_path = os.path.join(vm_path, "86box.cfg")
    try:
        with open(cfg_path, "w") as f:
            f.write("\n".join(lines))
    except OSError as e:
        context["abort"] = True
        return False, f"test_86box_cfg: cannot write {cfg_path}: {type(e).__name__}: {e}"
    log.append(f"Wrote {cfg_path}:")
    log += lines
    return True, "\n".join(log)


# 86Box floppy drive types -> (size label, accepted image byte lengths)
_FDD_TYPES = {
    "35_2hd": ("1.44M", {1474560}),
    "35_2dd": ("720K", {737280}),
    "525_2hd": ("1.2M", {1228800}),
    "525_2dd": ("360K", {368640}),
}
_FDD_SIZE_ALIASES = {"1.44": "1.44M", "1.44MB": "1.44M", "1440K": "1.44M", "1440KB": "1.44M",
                     "720": "720K", "720KB": "720K", "1.2": "1.2M", "1.2MB": "1.2M",
                     "360": "360K", "360KB": "360K"}


def _apply_86box_floppies(vm_path, config, vars_map, log):
    """Patch fdd_01_* / fdd_02_* in vm_path/86box.cfg from CONFIG floppyA_* / floppyB_* (type, image, size); only filled-in keys apply -> error string or None."""
    wanted = {}
    for letter, num in (("A", "01"), ("B", "02")):
        vals = {}
        for field in ("type", "image", "size"):
            v = config.get(f"floppy{letter}_{field}")
            v = resolve_path(str(v), vars_map).strip() if v not in (None, "") else ""
            vals[field] = v
        if not any(vals.values()):
            continue
        ftype, image, size = vals["type"], vals["image"], vals["size"].upper().replace(" ", "")
        size = _FDD_SIZE_ALIASES.get(size, size)
        if size and not ftype:
            matches = [t for t, (lbl, _) in _FDD_TYPES.items() if lbl == size]
            if not matches:
                return f"floppy{letter}_size {vals['size']!r} is not one of {sorted(l for l, _ in _FDD_TYPES.values())}"
            ftype = matches[0]
        if ftype and ftype not in _FDD_TYPES:
            return f"floppy{letter}_type {ftype!r} unknown; known: {sorted(_FDD_TYPES)}"
        if ftype and size and _FDD_TYPES[ftype][0] != size:
            return (f"floppy{letter}: size {size} does not match type {ftype} "
                    f"({_FDD_TYPES[ftype][0]})")
        if image:
            if not os.path.isfile(image):
                return f"floppy{letter}_image not found: {image}"
            n = os.path.getsize(image)
            if ftype and n not in _FDD_TYPES[ftype][1]:
                return (f"floppy{letter}_image {image} is {n} bytes, wrong for "
                        f"type {ftype} ({_FDD_TYPES[ftype][0]})")
        wanted[num] = {"type": ftype, "fn": image}

    if not wanted:
        return None
    cfg_path = os.path.join(vm_path, "86box.cfg")
    if not os.path.isfile(cfg_path):
        return f"floppy config set but {cfg_path} does not exist"
    with open(cfg_path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    sect = "[Floppy and CD-ROM drives]"
    if sect not in lines:
        lines += ["", sect]
    start = lines.index(sect) + 1
    end = start
    while end < len(lines) and not lines[end].startswith("["):
        end += 1
    body = lines[start:end]
    trailing = []
    while body and not body[-1].strip():
        trailing.append(body.pop())
    for num, vals in wanted.items():
        for field, val in vals.items():
            if not val:
                continue
            key = f"fdd_{num}_{field}"
            body = [l for l in body if l.split("=")[0].strip() != key]
            body.append(f"{key} = {val}")
            log.append(f"86box.cfg: {key} = {val}")
    body.sort(key=lambda l: l.split("=")[0].strip())
    lines[start:end] = body + trailing
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return None


@dispatchtest_step
def test_start_86box(name="box86_1", vm_path=None, vm_template=None,
                     takeascreenshot=False, monitor_port=None,
                     display=None, timeout=30, binary=None, logfile=None,
                     loadvm=None, **kwargs):
    """Launch 86Box on a VM directory (its -P root: 86box.cfg, nvr/, images).
      binary        custom 86Box executable; else CONFIG["box_binary"] (A/B override), else the /usr/local/bin/86box AppImage wrapper
      logfile       86Box's own log (-L), truncated at each start; lets test_filecontains gate on emulator output
      loadvm        snapshot file (remote monitor exportvm / snapshot.export) loaded and restored once up; needs monitor_port (RMM build), same 86Box build and VM config, and each hard disk the image the snapshot was based on (fresh copy of the exporting run's disk, [General] rmm_disk_track = 1); else the step fails
      monitor_port  remote monitor: "auto" = free port, integer = pinned, omitted = off. On: test_sendcommand / test_86box_monitor drive the guest over TCP (no X focus needed). Only remote-monitor-patch builds accept it (stock AppImage refuses to start).
      CONFIG floppyA_/floppyB_ type|image|size (A = fdd_01, B = fdd_02): patched into vm_path/86box.cfg before launch and authoritative where filled in. type = 86Box fdd type (35_2hd...), size = label (1.44M/720K/1.2M/360K) agreeing with the type; the image length must fit, or the step fails.
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    vm_path = resolve_path(vm_path, vars_map)
    vm_template = resolve_path(vm_template, vars_map) or None
    display = resolve_path(display, vars_map) or None
    # CONFIG["box_binary"] fallback (set by A/B runs, buildhelper.IMPLICIT_KEYS)
    binary = resolve_path(binary or config.get("box_binary"), vars_map) or None
    if not vm_path:
        return False, "test_start_86box: vm_path is required"

    if vm_template:
        if not os.path.isdir(vm_template):
            context["abort"] = True
            return False, f"VM template directory not found: {vm_template}"
        if os.path.isdir(vm_path):
            shutil.rmtree(vm_path)
        shutil.copytree(vm_template, vm_path)
        log.append(f"Copied VM template {vm_template} -> {vm_path}")
    elif not os.path.isdir(vm_path):
        context["abort"] = True
        return False, f"VM directory not found: {vm_path}"

    err = _apply_86box_floppies(vm_path, config, vars_map, log)
    if err:
        context["abort"] = True
        return False, "\n".join(log + [f"test_start_86box: {err}"])

    monitor_port = resolve_path(monitor_port, vars_map) or None
    logfile = resolve_path(logfile, vars_map) or None
    loadvm = resolve_path(loadvm, vars_map) or None
    if loadvm:
        loadvm = os.path.abspath(loadvm)
    if loadvm and not monitor_port:
        context["abort"] = True
        return False, "test_start_86box: loadvm needs monitor_port (the load goes through the remote monitor)"
    if loadvm and not os.path.isfile(loadvm):
        context["abort"] = True
        return False, f"test_start_86box: loadvm file not found: {loadvm}"
    instance = Box86Instance(name, vm_path, display=display, binary=binary,
                             monitor_port=monitor_port, logfile=logfile,
                             screenshot_scale=config.get("ocr_scale"),
                             window_slot=_next_window_slot(context))
    log.append(f"Starting 86Box '{name}' with -P {vm_path}")
    if logfile:
        log.append(f"Emulator log -> {logfile}")
    if instance.monitor_port:
        log.append(f"Remote monitor on 127.0.0.1:{instance.monitor_port}")
    if not instance.start():
        log.append("Failed to start 86Box.")
        log.extend(instance.stdout_lines[:10])
        context["abort"] = True
        return False, "\n".join(log)

    if not instance.wait_for_ready(timeout=int(timeout)):
        log.append(f"Timeout ({timeout}s) waiting for the 86Box window.")
        log.extend(instance.stdout_lines[:10])
        instance.stop()
        context["abort"] = True
        return False, "\n".join(log)

    if instance.monitor and not instance.monitor.wait_ready(timeout=int(timeout)):
        # non-fatal (send_command falls back to xdotool); usual cause: binary lacks the remote-monitor patch
        log.append(f"WARNING: no answer on monitor port {instance.monitor_port} "
                   f"({instance.monitor.last_error}); input will use xdotool")

    if loadvm:
        reply = instance.load_snapshot_file(loadvm)
        log.append(f"loadvm {loadvm}: {reply}")
        if not reply or not reply.startswith("OK"):
            instance.stop()
            context["abort"] = True
            return False, "\n".join(log)

    context[name] = instance
    log.append(f"{name} is ready on display {instance.display}")
    _maybe_screenshot(instance, takeascreenshot, log, label=name)
    return True, "\n".join(log)


@dispatchtest_step
def test_start_pcsx2(name="pcsx2_1", elf_path=None, iso_path=None, bios_file=None,
                     datapath=None, takeascreenshot=False, display=None,
                     timeout=30, binary=None, **kwargs):
    """Launch PCSX2 on a homebrew ELF (elf_path, e.g. test_hostbuild with the PS2SDK toolchain) or a disc image (iso_path); exactly one.
    bios_file: PS2 BIOS dump or a directory of them (e.g. ps2_roms/; every file is copied, the largest is the main BIOS). Supplied out of band (copyrighted); without one PCSX2 exits before creating a window.
    datapath: per-instance PCSX2 config/BIOS/savestate dir (-datapath); default under /tmp from `name`.
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    elf_path = resolve_path(elf_path, vars_map) or None
    iso_path = resolve_path(iso_path, vars_map) or None
    bios_file = resolve_path(bios_file, vars_map) or None
    datapath = resolve_path(datapath, vars_map) or None
    display = resolve_path(display, vars_map) or None
    binary = resolve_path(binary, vars_map) or None

    if not elf_path and not iso_path:
        return False, "test_start_pcsx2: one of elf_path/iso_path is required"

    instance = Pcsx2Instance(name, elf_path=elf_path, iso_path=iso_path,
                             bios_file=bios_file, datapath=datapath,
                             display=display, binary=binary,
                             screenshot_scale=config.get("ocr_scale"))
    log.append(f"Starting PCSX2 '{name}' with elf={elf_path} iso={iso_path}")
    if not instance.start():
        log.append("Failed to start PCSX2.")
        log.extend(instance.stdout_lines[:10])
        context["abort"] = True
        return False, "\n".join(log)

    if not instance.wait_for_ready(timeout=int(timeout)):
        log.append(f"Timeout ({timeout}s) waiting for the PCSX2 window.")
        log.extend(instance.stdout_lines[:10])
        instance.stop()
        context["abort"] = True
        return False, "\n".join(log)

    context[name] = instance
    log.append(f"{name} is ready on display {instance.display}")
    _maybe_screenshot(instance, takeascreenshot, log, label=name)
    return True, "\n".join(log)


@dispatchtest_step
def test_86box_monitor(commands=None, name=None, require_success=True,
                       delay=0, takeascreenshot=False, **kwargs):
    """Run raw 86Box remote-monitor commands against a running instance started with monitor_port (attach/detach media, read guest memory, ...).
    commands: one per list entry (or comma-separated string), e.g.
        ["cdload 0 {projbasedir}{projdir}/boot.iso", "media"]
        ["pause on", "memread 0x400 16", "pause off"]
    require_success: fail if any command answers ERR; turn off to just log replies.
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    instance, err = _gui_instance(context, name)
    if err:
        return False, err

    monitor = getattr(instance, "monitor", None)
    if not monitor:
        context["abort"] = True
        return False, (f"[{instance.name}] has no remote monitor; start it with "
                       f"monitor_port set on test_start_86box")

    cmds = [resolve_path(c, vars_map) for c in _as_list(commands)]
    if not cmds:
        return False, "test_86box_monitor: no commands given"

    if not monitor.connect():
        context["abort"] = True
        return False, f"[{instance.name}] monitor not reachable ({monitor.last_error})"

    failed = []
    for cmd in cmds:
        reply = monitor.cmd(cmd)
        if reply is None:
            context["abort"] = True
            log.append(f"[{instance.name}] {cmd} -> link lost ({monitor.last_error})")
            return False, "\n".join(log)
        log.append(f"[{instance.name}] {cmd} -> {reply}")
        if not reply.startswith("OK"):
            failed.append(cmd)

    if delay not in (None, "") and float(delay):
        time.sleep(float(delay))

    if _as_bool(takeascreenshot):
        _screenshot_and_ocr(instance, log, instance.name)

    if failed and _as_bool(require_success):
        context["abort"] = True
        log.append(f"{len(failed)} command(s) did not return OK: {', '.join(failed)}")
        return False, "\n".join(log)

    return True, "\n".join(log)


@dispatchtest_step
def test_start_xemu(name="xemu_1", dvd_path=None, hdd_path=None, bootrom_file=None,
                    flashrom_file=None, datadir=None, monitor_port=None,
                    takeascreenshot=False, display=None, timeout=30, binary=None,
                    **kwargs):
    """Launch xemu (Xbox emulator) on a bootable disc image (dvd_path, e.g. from test_hostbuild with nxdk) and/or a hard disk image (hdd_path).
    bootrom_file / flashrom_file: real Xbox MCPX boot ROM / BIOS dump files, supplied out of band (copyrighted); without a bootrom xemu sits paused ("Failed to load BIOS").
    datadir: per-instance xemu config/eeprom dir (XDG_DATA_HOME); default under /tmp from `name`.
    monitor_port: "auto" = free port, integer = pinned, omitted = off; HMP monitor only (status/quit/sendkey); screenshots go through xdotool/`import` (see xemuhelpers.XemuInstance).
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    dvd_path = resolve_path(dvd_path, vars_map) or None
    hdd_path = resolve_path(hdd_path, vars_map) or None
    bootrom_file = resolve_path(bootrom_file, vars_map) or None
    flashrom_file = resolve_path(flashrom_file, vars_map) or None
    datadir = resolve_path(datadir, vars_map) or None
    display = resolve_path(display, vars_map) or None
    binary = resolve_path(binary, vars_map) or None
    monitor_port = resolve_path(monitor_port, vars_map) or None

    if not dvd_path and not hdd_path:
        return False, "test_start_xemu: one of dvd_path/hdd_path is required"

    instance = XemuInstance(name, dvd_path=dvd_path, hdd_path=hdd_path,
                            bootrom_file=bootrom_file, flashrom_file=flashrom_file,
                            datadir=datadir, monitor_port=monitor_port,
                            display=display, binary=binary,
                            screenshot_scale=config.get("ocr_scale"))
    log.append(f"Starting xemu \'{name}\' with dvd={dvd_path} hdd={hdd_path}")
    if instance.monitor_port:
        log.append(f"Monitor on 127.0.0.1:{instance.monitor_port}")
    if not instance.start():
        log.append("Failed to start xemu.")
        log.extend(instance.stdout_lines[:10])
        context["abort"] = True
        return False, "\n".join(log)

    if not instance.wait_for_ready(timeout=int(timeout)):
        log.append(f"Timeout ({timeout}s) waiting for the xemu window.")
        log.extend(instance.stdout_lines[:10])
        instance.stop()
        context["abort"] = True
        return False, "\n".join(log)

    context[name] = instance
    log.append(f"{name} is ready on display {instance.display}")
    _maybe_screenshot(instance, takeascreenshot, log, label=name)
    return True, "\n".join(log)


@dispatchtest_step
def test_sendcommand(cmd_text=None, special_keys=None, name=None, key_delay=None,
                     delay=0, takeascreenshot=False, screenshot_all=False,
                     settle=None, **kwargs):
    """Type into a DOSBox-X / 86Box window.
      cmd_text         typed text; {tokens} resolve through resolve_path
      special_keys     special keys to send
      name             target instance (required with cmd_text/special_keys unless there is a single GUI instance)
      settle           (86Box only) seconds to wait after the focus click before the first key; ignored for DOSBox-X
      screenshot_all   with `name`: also screenshot+OCR-log every OTHER GUI_INSTANCE_TYPES instance for the report (no gating; as test_ocrwordsearch); off by default
    With no cmd_text/special_keys AND no name: screenshot(+OCR) every DOSBox-X/86Box instance in context (a checkpoint step).
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    cmd_text = resolve_path(cmd_text, vars_map)
    name = resolve_path(name, vars_map)
    keys = [resolve_path(k, vars_map) for k in _as_list(special_keys)]
    # may be {tokens}
    settle = resolve_path(settle, vars_map)
    key_delay = resolve_path(key_delay, vars_map)
    for pname, pval in (("settle", settle), ("key_delay", key_delay)):
        if pval not in (None, ""):
            try:
                float(pval)
            except (TypeError, ValueError):
                return False, (f"test_sendcommand: {pname} must be a number, got {pval!r} "
                               f"(an unresolved {{token}}? check CONFIG for that key)")

    if not name and not cmd_text and not keys:
        targets, err = _gui_instances(context)
        if err:
            return False, err
        if not _as_bool(takeascreenshot):
            return True, "no cmd_text/special_keys/name given and takeascreenshot is off — nothing to do"
        for label, instance in targets:
            _screenshot_and_ocr(instance, log, label)
        return True, "\n".join(log)

    instance, err = _gui_instance(context, name)
    if err:
        return False, err

    kw = {"special_keys": keys or None}
    if key_delay not in (None, ""):
        kw["key_delay"] = int(key_delay)
    if settle not in (None, "") and float(settle) and isinstance(instance, Box86Instance):
        kw["settle"] = float(settle)

    if not instance.send_command(cmd_text or None, **kw):
        context["abort"] = True
        if getattr(instance, "monitor", None):
            return False, (f"[{instance.name}] could not send input over the remote "
                           f"monitor, and the xdotool fallback also failed "
                           f"({instance.monitor.last_error})")
        # send_command returns False when the window never took focus
        return False, (f"[{instance.name}] could not send input — the window did not "
                       f"take focus (is another instance sharing display "
                       f"{instance.display}?)")

    if cmd_text:
        log.append(f"[{instance.name}] typed: {cmd_text}")
    if keys:
        log.append(f"[{instance.name}] sent keys: {' '.join(keys)}")

    if delay not in (None, "") and float(delay):
        time.sleep(float(delay))

    if _as_bool(takeascreenshot):
        _screenshot_and_ocr(instance, log, instance.name)

        if _as_bool(screenshot_all):
            for key, inst in context.items():
                if isinstance(inst, GUI_INSTANCE_TYPES) and inst is not instance:
                    _screenshot_and_ocr(inst, log, key)

    return True, "\n".join(log)


@dispatchtest_step
def test_fileexists(file_path=None, timeout=0, min_size=1, **kwargs):
    """Wait for a host file to exist and be at least min_size bytes."""
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    vars_map = build_vars(config)

    file_path = resolve_path(file_path, vars_map)
    if not file_path:
        return False, "test_fileexists: no file_path given"

    timeout, min_size = int(timeout), int(min_size)

    def size_of(path):
        """File size, or None if not a file; falls back to opening it when os.stat() raises WinError 33 (Windows runner, NFS file written by shell `>`)."""
        if os.path.isfile(path):
            return os.path.getsize(path)
        if not IS_WINDOWS:
            return None
        try:
            if os.path.basename(path) not in os.listdir(os.path.dirname(path) or "."):
                return None
            with open(path, "rb") as f:
                return f.seek(0, os.SEEK_END)
        except OSError:
            return None

    def big_enough():
        n = size_of(file_path)
        return n is not None and n >= min_size

    deadline = time.time() + timeout
    dead = None
    while not big_enough() and time.time() < deadline:
        dead = _dead_gui_instance(context)
        if dead:
            break
        time.sleep(2)

    if not big_enough():
        context["abort"] = True
        if dead:
            log.append(f"[{dead}] exited (window closed?) before {file_path} appeared")
        n = size_of(file_path)
        if n is not None:
            log.append(f"{file_path} is only {n} bytes "
                       f"(wanted >= {min_size})")
        else:
            log.append(f"{file_path} does not exist"
                       + (f" after {timeout}s" if timeout else ""))
        return False, "\n".join(log)

    log.append(f"{file_path} exists ({size_of(file_path)} bytes)")
    log.append(f"ARTIFACT: {file_path}")
    return True, "\n".join(log)

