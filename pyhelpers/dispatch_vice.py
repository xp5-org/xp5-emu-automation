"""VICE (C64/C128/PET/VIC20/...) run control: start, keyboard, screen checks, relay/fileserver, capture, audio.

Loaded through dispatch_functions (star import); shared primitives are in dispatch_common.
"""
import os
import time
import threading
from dispatch_common import dispatchtest_step, resolve_path as _common_resolve_path, as_bool, register_terminable
from vicehelpers import send_vice_command, ViceInstance, next_vice_instance, launch_vice_instance
from vicehelpers import send_single_command
from vicehelpers import create_blank_tap, tape_control, keyboard_buffer_for
import ip232relayserver
import ip232fileserver
from vice_autocapture import SceneCapture

register_terminable(ViceInstance)


def _resolve_path(path, config):
    """Same call shape as the old per-project resolver: '' for an empty path, CONFIG scalars as the token map."""
    if not path:
        return ""
    return _common_resolve_path(path, config)


@dispatchtest_step
def test_make_blank_tape(tape_path=None, pad_bytes=1024, **kwargs):
    """Create/overwrite a blank .tap image for the datasette.

    Run this before test_emulator_start so each run records onto a fresh tape
    instead of appending to whatever the previous run left behind.
    """
    config = kwargs.get("config", {})
    path = _resolve_path(tape_path or config.get("tape_file"), config)
    if not path:
        return False, "make_blank_tape: no tape_path given"

    try:
        pad_bytes = int(pad_bytes)
    except (TypeError, ValueError):
        return False, f"make_blank_tape: bad pad_bytes {pad_bytes!r}"

    success, out = create_blank_tap(path, pad_bytes=pad_bytes)
    return success, out


@dispatchtest_step
def test_tape_control(command=None, name="vice1", **kwargs):
    """Press a datasette button (stop/play/forward/rewind/record/reset).

    The KERNAL blocks on "PRESS RECORD & PLAY ON TAPE" until the buttons are
    down, and the datasette has no way to rewind itself, so read-back after a
    write needs stop -> rewind -> play from here.
    """
    context = kwargs.get("context", {})
    instance = context.get(name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{name}' found in context"

    if not command:
        return False, "tape_control: no command given"

    try:
        out = tape_control(context, name, command)
    except Exception as e:
        return False, f"tape_control {command!r} failed: {e}"

    return True, f"tape control '{command}' sent to {name}:\n{out}"


@dispatchtest_step
def test_wait30seconds(seconds=30, **kwargs):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return False, f"Invalid seconds value: {seconds}"

    context = kwargs.get("context")
    if not context:
        return False, "No emulator instances in context"

    if not isinstance(context, dict):
        try:
            context = dict(context)
        except Exception:
            return False, f"Context is not iterable: {type(context)}"

    for name, instance in context.items():
        if not hasattr(instance, "take_screenshot") or not callable(instance.take_screenshot):
            continue

        success, info = instance.take_screenshot()
        if not success:
            err_msg = getattr(instance, "startup_error", None)
            reason = info or err_msg or "unknown"
            return False, f"Screenshot failed for {name}. Reason: {reason}"

    time.sleep(seconds)

    for name, instance in context.items():
        if not hasattr(instance, "take_screenshot") or not callable(instance.take_screenshot):
            continue

        success, info = instance.take_screenshot()
        if not success:
            err_msg = getattr(instance, "startup_error", None)
            reason = info or err_msg or "unknown"
            return False, f"Screenshot failed for {name}. Reason: {reason}"

    return True


@dispatchtest_step
def test_emulator_start(name=None, port=6502, viceconf=None, disk8_path=None, disk9_path=None,
                        tape_path=None, autostart_path=None, rom_path=None, warpmode=True,
                        binmon_port=None, emubin=None, **kwargs):
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})

    disk8_path = _resolve_path(disk8_path or config.get("d64_drive8_file"), config)
    disk9_path = _resolve_path(disk9_path or config.get("d64_drive9_file"), config)
    tape_path = _resolve_path(tape_path or config.get("tape_file"), config)
    viceconf = _resolve_path(viceconf or config.get("viceconf"), config)
    autostart_path = _resolve_path(autostart_path or "", config)
    rom_path = _resolve_path(rom_path or "", config)

    # allocate a unique port using sequential counter
    auto_name, port = next_vice_instance(context)
    if name and str(name).strip() and str(name).strip().lower() not in ("none", "null"):
        name = str(name).strip()
    else:
        name = auto_name

    resolved_binmon_port = int(binmon_port) if binmon_port not in (None, "", "None") else None
    # "False" from a testlist is a non-empty string and would otherwise enable
    # warp; see as_bool. Autostart manages warp on its own during loading.
    warpmode = as_bool(warpmode, default=True)

    # Optional emulator-binary override; falls back to the archtype default.
    emubin = emubin or config.get("emubinary")

    instance = ViceInstance(
        name,
        port,
        config.get("archtype"),
        config_path=viceconf,
        disk8_path=disk8_path,
        disk9_path=disk9_path,
        tape_path=tape_path,
        warpmode=warpmode,
        autostart_path=autostart_path,
        rom_path=rom_path,
        binmon_port=resolved_binmon_port,
        emubin=emubin
    )

    # record start order so the relay can correlate its connections
    context.setdefault("_vice_start_order", []).append(name)
    relay_running = ip232relayserver.server_running.is_set()
    relay_before = ip232relayserver.client_count() if relay_running else 0

    started = instance.start()
    context[name] = instance

    time.sleep(0.5)

    stdout_lines = instance.get_output()
    if stdout_lines:
        log.append("VICE stdout:")
        log.append("".join(stdout_lines))

    instance.take_screenshot()
    log.append(f"Launching {name} on port {port} with disk8={disk8_path} disk9={disk9_path} tape={tape_path} config={viceconf}")
    log.append("Command: " + " ".join(instance.cmd))

    # If the relay is up, wait for instance to connect before returning to prevent getting logging out of order
    # might need a better way to correlate relay to vice instance someday
    if relay_running:
        deadline = time.time() + 20
        while time.time() < deadline:
            if ip232relayserver.client_count() > relay_before:
                log.append(f"{name} connected to relay (client #{relay_before})")
                break
            time.sleep(0.5)
        else:
            log.append(f"WARNING: {name} did not connect to relay within timeout; "
                       f"relay correlation by connect-order may be unreliable")

    return True, "\n".join(log)


@dispatchtest_step
def test_sendrun(name="vice1", **kwargs):
    context = kwargs.get("context", {})
    target_name = name
    instance = context.get(target_name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{target_name}' found in context"

    log = []
    try:
        success, output = send_vice_command(context, target_name, 'LOAD "*",8\n')
        time.sleep(3)
        success, output = send_vice_command(context, target_name, "RUN\n")
        
        instance.take_screenshot()
        
        log.append(f"Sent RUN to {target_name}:\n{output}")
        return True, "\n".join(log)
    except Exception as e:
        return False, f"Failed to send to {target_name}: {e}"


@dispatchtest_step
def test_basic_sendlistdisk(disk_idnum, name="vice1", **kwargs):
    context = kwargs.get("context", {})
    target_name = name
    instance = context.get(target_name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{target_name}' found in context"

    log = []
    try:
        success, output = send_vice_command(context, target_name, f'load "$",{disk_idnum}\n')
        time.sleep(3)
        success, output = send_vice_command(context, target_name, "list\n")
        
        instance.take_screenshot()
        
        log.append(f"Sent RUN to {target_name}:\n{output}")
        return True, "\n".join(log)
    except Exception as e:
        return False, f"Failed to send to {target_name}: {e}"


    

@dispatchtest_step
def test_sendastring(inputkeyboardstring=None, name="vice1", **kwargs):
    context = kwargs.get("context", {})
    target_name = name
    instance = context.get(target_name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{target_name}' found in context"

    log = []
    try:
        success, output = send_vice_command(context, target_name, inputkeyboardstring)
        instance.take_screenshot()

        log.append(f"Sent RUN to {target_name}:\n{output}")
        return True, "\n".join(log)
    except Exception as e:
        return False, f"Failed to send to {target_name}: {e}"


_KEY_PETSCII = {
    "up": 0x91, "down": 0x11, "left": 0x9D, "right": 0x1D,
    "return": 0x0D, "enter": 0x0D, "space": 0x20,
    "del": 0x14, "home": 0x13, "clear": 0x93,
    "f1": 0x85, "f3": 0x86, "f5": 0x87, "f7": 0x88,
}


# Codes for characters whose PETSCII value differs from ASCII on a real C64
# keyboard (only the ones our own testlists actually need to type go here).
# { and } aren't reachable via plain ASCII injection into the KEYD buffer --
# the real keys are SHIFT+= and SHIFT+-, which produce these PETSCII codes.
_CHAR_PETSCII_OVERRIDES = {
    "{": 0xDB,
    "}": 0xDD,
}


@dispatchtest_step
def test_sendslowstring(inputkeyboardstring=None, name="vice1", delay=0.08, **kwargs):
    """Type a string one keystroke at a time with a delay between each.

    Full-screen programs (editors, etc.) that poll their own keyboard buffer
    once per main-loop iteration can lose most of a string if it's stuffed
    into KEYD in the usual 10-byte chunks with no pacing -- especially under
    warp mode. Typing slowly, one character per KEYD/NDX write, avoids that.
    Also remaps { and } to their real C64 PETSCII codes (0xDB/0xDD): sending
    their raw ASCII codes (0x7B/0x7D) writes a *different* character on a
    C64, since the C64 charset has no ASCII curly braces at those codes.
    """
    context = kwargs.get("context", {})
    target_name = name
    instance = context.get(target_name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{target_name}' found in context"

    if inputkeyboardstring is None:
        return False, "No inputkeyboardstring specified"

    delay = float(delay)
    log = []
    try:
        for ch in inputkeyboardstring.replace("\n", "\r"):
            if ch in _CHAR_PETSCII_OVERRIDES:
                code = _CHAR_PETSCII_OVERRIDES[ch]
            elif 'a' <= ch <= 'z':
                code = ord(ch.upper())
            else:
                code = ord(ch)
            send_single_command(context, target_name, f"f 0277 0277 {code:02X}")
            send_single_command(context, target_name, "f 00c6 00c6 01")
            time.sleep(delay)

        instance.take_screenshot()
        log.append(f"Slow-typed {len(inputkeyboardstring)} chars to {target_name}")
        return True, "\n".join(log)
    except Exception as e:
        return False, f"Failed to send to {target_name}: {e}"


@dispatchtest_step
def test_sendkey(key=None, name="vice1", **kwargs):
    """Inject a single keypress into a VICE instance's keyboard buffer.

    `key` may be a name (up/down/left/right/return/space/f1...) or a raw
    PETSCII code ("0x11", "17"). Targets context[name] (default 'vice1')."""
    context = kwargs.get("context", {})
    target_name = name
    instance = context.get(target_name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{target_name}' found in context"

    if key is None or str(key).strip() == "":
        return False, "No key specified"

    k = str(key).strip().lower()
    if k in _KEY_PETSCII:
        raw = _KEY_PETSCII[k]
    else:
        try:
            # Numeric first, so a bare digit stays a PETSCII code ("5" -> $05)
            # rather than becoming the character '5'.
            raw = int(k, 0) & 0xFF
        except ValueError:
            if len(k) == 1 and k.isprintable():
                # Single character: PETSCII uppercases letters, same convention
                # as ascii_to_petscii_cmd in vicehelpers.
                raw = ord(k.upper()) & 0xFF
            else:
                return False, (f"Unknown key: {key!r} (use a name, a single "
                               f"character, or a PETSCII code like 0x11)")

    log = []
    try:
        # Place the PETSCII byte in KEYD and set NDX=1; both addresses depend
        # on the machine (the C128 and PET do not use the C64's $0277/$00C6).
        keyd_addr, ndx_addr = keyboard_buffer_for(context, target_name)
        log.append(send_single_command(context, target_name, f"f {keyd_addr:04X} {keyd_addr:04X} {raw:02X}"))
        log.append(send_single_command(context, target_name, f"f {ndx_addr:04X} {ndx_addr:04X} 01"))
        time.sleep(0.5)
        instance.take_screenshot()
        log.append(f"Sent key {key!r} (PETSCII ${raw:02X}) to {target_name}")
        return True, "\n".join(log)
    except Exception as e:
        return False, f"Failed to send key to {target_name}: {e}"


# C64 colour palette index by name (for $D020/$D021 register checks).
_C64_COLORS = {
    "black": 0, "white": 1, "red": 2, "cyan": 3, "purple": 4, "green": 5,
    "blue": 6, "yellow": 7, "orange": 8, "brown": 9, "lightred": 10,
    "darkgrey": 11, "darkgray": 11, "grey": 12, "gray": 12, "lightgreen": 13,
    "lightblue": 14, "lightgrey": 15, "lightgray": 15,
}


@dispatchtest_step
def test_checkbordercolor(expected=None, name="vice1", numberofattempts=10, attemptdelay=1, **kwargs):
    """Confirm a VICE instance's border colour ($D020) over the monitor.

    Reads the VIC-II border register directly (low nibble = colour 0-15;
    bits 4-7 read back as 1, so they are masked off). `expected` may be a
    colour name (blue/red/...) or a numeric index. Targets context[name]
    (default 'vice1'). Aborts the run on mismatch."""
    import re as _re

    context = kwargs.get("context", {})
    target_name = name
    instance = context.get(target_name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{target_name}' found in context"

    if expected is None or str(expected).strip() == "":
        return False, "No expected color specified"

    e = str(expected).strip().lower()
    if e in _C64_COLORS:
        want = _C64_COLORS[e]
    else:
        try:
            want = int(e, 0) & 0x0F
        except ValueError:
            return False, f"Unknown color: {expected!r}"

    numberofattempts = int(numberofattempts)
    attemptdelay = int(attemptdelay)

    # Memory-dump line for $D020 looks like ">C:d020  f6 ..": grab the first
    # 2-hex byte that follows the address (and is not part of a 4-hex address,
    # so the echoed 'm d020 d020' command can't be mis-parsed).
    pat = _re.compile(r"(?i)d020\s+([0-9a-f]{2})(?![0-9a-f])")

    log = []
    last = None
    for attempt in range(numberofattempts):
        resp = send_single_command(context, target_name, "m d020 d020")
        matches = pat.findall(resp or "")
        if matches:
            last = int(matches[-1], 16) & 0x0F
            log.append(f"attempt {attempt}: border=${last:02X} ({last}) want={want}")
            if last == want:
                instance.take_screenshot()
                log.append(f"{target_name} border color is {expected} ({want}) - OK")
                return True, "\n".join(log)
        else:
            log.append(f"attempt {attempt}: could not parse border from monitor response:\n{resp}")
        time.sleep(attemptdelay)

    instance.take_screenshot()
    log.append(f"{target_name} border never reached {expected} ({want}); last seen={last}")
    context["abort"] = True
    return False, "\n".join(log)


@dispatchtest_step
def test_autocapture_start(binmon_port=6511, capture_dir=None, dump_ram=True,
                           name="vice1", **kwargs):
    """Arm checkpoint-driven scene capture against a running VICE instance.

    Requires the instance to have been started with a `binmon_port` (see
    test_emulator_start). Capture runs on a background thread, so following
    steps (waits, keypresses) proceed normally while scenes are collected.
    Pair with test_autocapture_report to summarise what was caught."""
    context = kwargs.get("context", {})
    instance = context.get(name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{name}' found in context"

    port = getattr(instance, "binmon_port", None) or binmon_port
    if not port:
        return False, (f"{name} has no binary monitor port; start it with "
                       f"binmon_port set in test_emulator_start")

    if str(dump_ram).strip().lower() in ("0", "false", "no", "none", ""):
        dump_ram = False

    capture_dir = _resolve_path(capture_dir or "", kwargs.get("config", {})) or "/testsrc/captures"

    capture = SceneCapture(port=int(port), capture_dir=capture_dir, dump_ram=bool(dump_ram))
    try:
        capture.start()
    except Exception as e:
        return False, f"Failed to arm auto-capture on port {port}: {e}"

    context["_autocapture"] = capture
    return True, (f"Auto-capture armed on binary monitor port {port}; "
                  f"writing scenes to {capture_dir}")


@dispatchtest_step
def test_autocapture_report(stop=True, **kwargs):
    """Report what auto-capture collected, and by default stop it."""
    context = kwargs.get("context", {})
    capture = context.get("_autocapture")

    if capture is None:
        return False, "Auto-capture was never started (no _autocapture in context)"

    log = [f"checkpoint hits: {capture.hit_count}",
           f"scenes saved:    {capture.scene_count}",
           f"capture dir:     {capture.capture_dir}"]

    if capture.scene_count:
        for entry in sorted(os.listdir(capture.capture_dir)):
            log.append(f"  {entry}")

    if str(stop).strip().lower() not in ("0", "false", "no"):
        capture.stop()
        del context["_autocapture"]

    if capture.hit_count == 0:
        log.append("")
        log.append("No checkpoints fired at all -- the program under test never "
                   "wrote to the sprite pointers, $D015, $D018 or $DD00. If the "
                   "demo is sitting on a title screen waiting for input, it "
                   "hasn't started its scene cycle yet.")
        return False, "\n".join(log)

    return True, "\n".join(log)


@dispatchtest_step
def test_wordsearch(successphrase=None, failphrase=None, c128windowcolnum=None, numberofattempts=10, attemptdelay=3, **kwargs):
    # need to pass these args into function scanner later
    context = kwargs.get("context", {})

    # need to set things we expect to be numbers as ints
    numberofattempts = int(kwargs.get("numberofattempts", numberofattempts))
    attemptdelay = int(kwargs.get("attemptdelay", attemptdelay))
    # c128windowcolnum = "80col"
    # c128windowcolnum = "40col"
    log = []
    abort = False

    for name, instance in context.items():
        if not isinstance(instance, ViceInstance):
            continue

        attempt = 0
        screentext = ""
        found_status = False

        while attempt < numberofattempts:
            screentext = instance.screentextdump(context, window=c128windowcolnum)
            if screentext is None:
                log.append(f"{name} screentextdump returned None")
                return False, "\n".join(log)
            screentext = screentext.lower()


            if failphrase in screentext:
                log.append(f"{name} - failphrase {failphrase} found at attempt {attempt}.  ")
                abort = True
                found_status = True
                break

            if successphrase in screentext:
                log.append(f"{name} - successphrase {successphrase} found at attempt {attempt}.  ")
                found_status = True
                break

            time.sleep(attemptdelay)
            attempt += 1

        if not found_status:
            log.append(f"{name} did not report success or failure")
            abort = True

        log.append(f"{name} screentext:\n{screentext}")

        if instance.take_screenshot(window=c128windowcolnum):
            log.append(f"Screenshot for {name} taken")
        else:
            log.append(f"Screenshot for {name} failed")
            abort = True

    if not log:
        log.append("No ViceInstances found in context")

    if abort:
        context["abort"] = True
        # Leaving the emulator up is what you want when working by hand: the
        # failure is on screen and the machine is still there to poke at.
        # Off by default so batch runs still clean up after themselves.
        if context.get("keepopen"):
            log.append("keepopen set: leaving emulator(s) running for inspection")
        else:
            for name, instance in context.items():
                if isinstance(instance, ViceInstance):
                    log.append(f"Stopping {name} on port {instance.port}")
                    instance.stop()
        return False, "\n".join(log)

    return True, "\n".join(log)


@dispatchtest_step
def test_relay_start(port=6501, **kwargs):
    log = []
    context = kwargs.get("context", {})
    name = "relay_server"

    relay_info = context.get(name)
    if relay_info and relay_info.get("started"):
        log.append(f"{name} was already started")
        return True, "\n".join(log)

    server_thread = threading.Thread(target=ip232relayserver.start_server, daemon=True)
    server_thread.start()
    context[name] = {"thread": server_thread, "started": True}
    log.append(f"{name} started on port {port}")

    return True, "\n".join(log)


@dispatchtest_step
def test_relay_reset(**kwargs):
    """Clear the relay's per-client sent/received capture buffers and symbol
    counts"""
    ip232relayserver.reset_client_stats()
    return True, "relay client stats reset"


@dispatchtest_step
def test_relay_stop(**kwargs):
    log = []
    context = kwargs.get("context", {})
    name = "relay_server"

    relay_info = context.get(name)
    if not relay_info or not relay_info.get("started"):
        return False, "error: relay server was not running"

    # Pass the instance start order so the relay labels each connection with
    # its Python-side instance name (vice1_rx / vice2_tx) instead of the
    # anonymous connect-order name.
    start_order = context.get("_vice_start_order", [])
    thread = relay_info.get("thread")
    logs = ip232relayserver.stop_server(instance_names=start_order)
    if thread:
        thread.join(timeout=5)
    relay_info["started"] = False

    log.append("relay stopped")
    log.extend(logs)

    return True, "\n".join(log)


@dispatchtest_step
def test_fileserver_start(port=6510, **kwargs):
    log = []
    context = kwargs.get("context", {})
    name = "file_server"

    info = context.get(name)
    if info and info.get("started"):
        log.append(f"{name} was already started")
        return True, "\n".join(log)

    # A run that aborted before its stop step leaves the socket bound, and the
    # next run would fail to bind. The server is process-global, so reclaim it.
    if ip232relayserver.server_running.is_set():
        log.append("a previous server was still bound; stopping it first")
        ip232fileserver.stop_server()

    port = int(port)
    server_thread = threading.Thread(
        target=ip232fileserver.start_server, kwargs={"port": port}, daemon=True)
    server_thread.start()
    context[name] = {"thread": server_thread, "started": True}
    log.append(f"{name} started on port {port}")
    log.append(f"catalog: {', '.join(ip232fileserver.ITEMS)}")

    return True, "\n".join(log)


@dispatchtest_step
def test_keepopen(enabled=True, **kwargs):
    """Stop failing steps from shutting the emulator down.

    Steps that abort a run normally stop every ViceInstance on their way out,
    which is right for a batch run but not when you are working by hand: the
    machine disappears at exactly the moment you want to look at it. Put this
    first in a testlist and the abort still happens (later steps skip) but the
    emulator is left running. Pair with test_hold_open to also keep the run
    itself parked."""
    context = kwargs.get("context", {})
    on = str(enabled).strip().lower() not in ("0", "false", "no", "none", "")
    context["keepopen"] = on
    return True, f"keepopen = {on}"


@dispatchtest_step
def test_hold_open(name="vice1", seconds=3600, pollinterval=2, **kwargs):
    """Block while a VICE instance stays up, so the run does not finish.

    Leaving test_terminate_all out is not enough on its own: once the last
    step returns, the run completes and nothing is holding the emulator open
    any more. This step parks the run — keeping a live reference to the
    instance — so the machine stays usable for hands-on work (scrolling the
    menu during playback, listening to a clip through). It returns as soon as
    the emulator exits, or after `seconds` as a backstop so a forgotten run
    cannot pin the runner forever."""
    context = kwargs.get("context", {})
    instance = context.get(name)

    if not isinstance(instance, ViceInstance):
        return False, f"No ViceInstance named '{name}' found in context"

    seconds = int(seconds)
    pollinterval = int(pollinterval)
    waited = 0

    while waited < seconds:
        if instance.proc is None or instance.proc.poll() is not None:
            return True, f"{name} exited after {waited}s; releasing the run"
        time.sleep(pollinterval)
        waited += pollinterval

    return True, (f"held {name} open for {seconds}s (limit reached); "
                  f"emulator still running")


@dispatchtest_step
def test_fileserver_stop(**kwargs):
    log = []
    context = kwargs.get("context", {})
    name = "file_server"

    info = context.get(name)
    if not info or not info.get("started"):
        return False, "error: file server was not running"

    start_order = context.get("_vice_start_order", [])
    thread = info.get("thread")
    logs = ip232fileserver.stop_server(instance_names=start_order)
    if thread:
        thread.join(timeout=5)
    info["started"] = False

    log.append("file server stopped")
    log.extend(logs)

    return True, "\n".join(log)


def _phrase_in_screen(instance, context, phrase):
    """True if `phrase` (case-insensitive) appears in the instance's screen,
    read from screen RAM via the VICE monitor (screentextdump uses 'sc')."""
    try:
        screen = instance.screentextdump(context)
    except Exception as e:
        return False, f"screentextdump error: {e}"
    if screen is None:
        return False, "screentextdump returned None"
    return (phrase.lower() in screen.lower()), screen


@dispatchtest_step
def test_checkrelaytraffic(name="vice1", direction="recv", contains=None,
                           exact=False, check_screen=False,
                           numberofattempts=10, attemptdelay=1, **kwargs):
    """Validate what a named instance sent/received via the relay, and
    optionally cross-check the emulator screen.

    Correlates the relay connection to the instance by connect order, then:
      - direction "recv" (default) / "sent" selects which relay buffer to check
      - contains      — required phrase (case-insensitive)
      - exact=True    — the relay payload must equal `contains` exactly once
                        stripped (proves NO extra garbage chars on the wire)
      - check_screen  — also confirm the phrase is present on the instance's
                        screen (read from screen RAM via the remote monitor),
                        so relay and screen are validated to carry the same
                        basic phrase.
    Aborts on failure. Run while the relay is still up (before relay_stop)."""
    context = kwargs.get("context", {})
    start_order = context.get("_vice_start_order", [])
    direction = (direction or "recv").strip().lower()
    want = (contains or "")
    exact = str(exact).strip().lower() in ("1", "true", "yes")
    check_screen = str(check_screen).strip().lower() in ("1", "true", "yes")

    numberofattempts = int(numberofattempts)
    attemptdelay = int(attemptdelay)

    instance = context.get(name)

    log = []
    last = None
    for attempt in range(numberofattempts):
        sent, recv = ip232relayserver.get_client_traffic(start_order, name)
        if sent is None and recv is None:
            log.append(f"attempt {attempt}: no relay connection correlated to '{name}' yet")
            time.sleep(attemptdelay)
            continue

        hay = recv if direction == "recv" else sent
        last = hay
        log.append(f"attempt {attempt}: {name} relay {direction} chars = {hay!r}")

        if exact:
            relay_ok = (hay or "").strip().lower() == want.lower()
        else:
            relay_ok = (not want) or (want.lower() in (hay or "").lower())

        if not relay_ok:
            time.sleep(attemptdelay)
            continue

        # Relay side satisfied — optionally confirm the screen agrees.
        if check_screen and want:
            if not isinstance(instance, ViceInstance):
                log.append(f"check_screen requested but no ViceInstance '{name}' in context")
                context["abort"] = True
                return False, "\n".join(log)
            screen_ok, screen = _phrase_in_screen(instance, context, want)
            log.append(f"  screen contains {want!r}: {screen_ok}")
            if not screen_ok:
                log.append(f"  screen text:\n{screen}")
                time.sleep(attemptdelay)
                continue
            instance.take_screenshot()

        mode = "exact" if exact else "contains"
        log.append(f"{name} relay {direction} [{mode}] {want!r} OK"
                   + (" + screen match" if check_screen else ""))
        return True, "\n".join(log)

    log.append(f"{name} {direction} validation failed for {want!r} "
               f"(exact={exact}, check_screen={check_screen}); last relay={last!r}")
    context["abort"] = True
    return False, "\n".join(log)


# --------------------------------------------------------------------------- #
# Audio capture / verification steps                                          #
#                                                                             #
# Records the emulator's sound output so a build like the MIDI->SID player    #
# can be verified in software: capture the audio while the program runs, then #
# check the WAV actually contains signal (not silence).                       #
#                                                                             #
# Capture is done from a dedicated PulseAudio/PipeWire *null sink* rather     #
# than xrdp-sink.monitor: a null sink's monitor keeps running even when no    #
# RDP client is attached, so the test works headless. An optional loopback    #
# mirrors it back to the previous default sink so it stays audible over RDP.  #
# --------------------------------------------------------------------------- #
import subprocess as _sp
import wave as _wave
import math as _math
import struct as _struct

_PULSE_ENV_DEFAULTS = {
    "XDG_RUNTIME_DIR": "/run/user/1001",
    "PULSE_SERVER": "unix:/run/user/1001/pulse/native",
}


def _pulse_env():
    env = os.environ.copy()
    for k, v in _PULSE_ENV_DEFAULTS.items():
        env.setdefault(k, v)
    return env


def _pactl(env, *args):
    return _sp.run(["pactl", *args], env=env,
                   stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True)


def _analyze_raw(path, rate, channels, window_sec):
    """RMS/peak envelope of a raw s16le interleaved capture (left channel)."""
    with open(path, "rb") as fh:
        data = fh.read()
    frame = 2 * channels
    n = len(data) // frame
    peak = 0
    total_sq = 0.0
    win = max(1, int(rate * window_sec))
    env = []
    wsum = 0.0
    wcount = 0
    for i in range(n):
        l = _struct.unpack_from("<h", data, i * frame)[0]
        a = -l if l < 0 else l
        if a > peak:
            peak = a
        sq = float(l) * l
        total_sq += sq
        wsum += sq
        wcount += 1
        if wcount >= win:
            env.append(int(_math.sqrt(wsum / wcount)))
            wsum = 0.0
            wcount = 0
    if wcount:
        env.append(int(_math.sqrt(wsum / wcount)))
    rms = _math.sqrt(total_sq / n) if n else 0.0
    return {"samples": n, "seconds": (n / rate) if rate else 0.0,
            "peak": peak, "rms": rms, "env": env}


def _raw_to_wav(raw_path, wav_path, rate, channels):
    with open(raw_path, "rb") as fh:
        data = fh.read()
    w = _wave.open(wav_path, "wb")
    w.setnchannels(channels)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(data)
    w.close()


@dispatchtest_step
def test_record_audio_start(sink_name="sidcap", source=None, out_path=None,
                            rate=44100, channels=2, loopback=True,
                            make_default=True, **kwargs):
    """Start capturing emulator audio from a fresh null sink.

    Creates the null sink, makes it the default so the emulator (launched by a
    later test_emulator_start) renders into it, optionally loops it back to the
    old default sink (so it's still audible over RDP), and starts `parec`.
    State is stashed in context['_audio_rec'] for test_record_audio_verify.
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    env = _pulse_env()

    source = source or f"{sink_name}.monitor"
    out_path = _resolve_path(out_path or config.get("audio_capture_path")
                            or "/tmp/{cmainfile}_capture.raw", config)
    rate = int(rate)
    channels = int(channels)

    prev_default = _pactl(env, "get-default-sink").stdout.strip()
    log.append(f"previous default sink: {prev_default!r}")

    if prev_default == sink_name:
        # A prior run's teardown never ran (e.g. it was killed mid-test), so
        # the default sink is still pointed at the capture sink from that run
        # rather than a real output device. Looping back to it here would
        # route sink_name.monitor into sink_name itself -- a literal
        # feedback loop -- and storing it as this run's prev_default would
        # have test_record_audio_verify's teardown restore the default back
        # to sink_name again, perpetuating the corruption into every run
        # after this one. Treat it as unknown instead of compounding it.
        log.append(f"WARNING: previous default sink is already {sink_name!r} -- "
                    "a prior run's teardown likely did not run, so this is "
                    "leftover pollution, not a real output device. Skipping "
                    "loopback and not stashing it, so this run's teardown "
                    "will not perpetuate it either.")
        prev_default = None

    r = _pactl(env, "load-module", "module-null-sink",
               f"sink_name={sink_name}",
               f"sink_properties=device.description={sink_name}")
    if r.returncode != 0:
        return False, "\n".join(log + [f"failed to load null sink: {r.stdout}"])
    null_mod = r.stdout.strip()
    log.append(f"null-sink '{sink_name}' module={null_mod}")

    loop_mod = None
    if loopback and prev_default:
        rl = _pactl(env, "load-module", "module-loopback",
                    f"source={source}", f"sink={prev_default}", "latency_msec=60")
        if rl.returncode == 0:
            loop_mod = rl.stdout.strip()
            log.append(f"loopback {source} -> {prev_default} module={loop_mod}")
        else:
            log.append(f"WARNING: loopback failed (non-fatal): {rl.stdout.strip()}")

    if make_default:
        _pactl(env, "set-default-sink", sink_name)
        log.append(f"default sink -> {sink_name}")

    rec = _sp.Popen(["parec", "--device", source,
                     "--format=s16le", f"--rate={rate}", f"--channels={channels}",
                     out_path],
                    env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    context["_audio_rec"] = {
        "proc": rec, "path": out_path, "rate": rate, "channels": channels,
        "source": source, "sink_name": sink_name, "null_mod": null_mod,
        "loop_mod": loop_mod, "prev_default": prev_default,
    }
    log.append(f"recording {source} -> {out_path} (parec pid {rec.pid})")
    return True, "\n".join(log)


@dispatchtest_step
def test_record_audio_verify(min_rms=150, min_active_windows=3, window_sec=0.5,
                             wav_out=None, keep_raw=False, **kwargs):
    """Stop the capture started by test_record_audio_start and verify signal.

    Passes when at least `min_active_windows` of the `window_sec` RMS windows
    exceed `min_rms` (i.e. the program actually produced sustained sound).
    Tears down the null sink / loopback, restores the previous default sink,
    and emits the capture as a listenable WAV artifact.
    """
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    env = _pulse_env()

    rec = context.get("_audio_rec")
    if not rec:
        return False, "no active audio recording (call test_record_audio_start first)"

    proc = rec["proc"]
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log.append(f"stopped recorder (parec pid {proc.pid})")

    if rec.get("loop_mod"):
        _pactl(env, "unload-module", rec["loop_mod"])
    if rec.get("null_mod"):
        _pactl(env, "unload-module", rec["null_mod"])
    if rec.get("prev_default"):
        _pactl(env, "set-default-sink", rec["prev_default"])
        log.append(f"restored default sink -> {rec['prev_default']}")
    context.pop("_audio_rec", None)

    raw_path = rec["path"]
    if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        return False, "\n".join(log + [f"capture file missing/empty: {raw_path}"])

    a = _analyze_raw(raw_path, rec["rate"], rec["channels"], float(window_sec))
    active = sum(1 for e in a["env"] if e >= float(min_rms))
    dbfs = 20 * _math.log10(a["rms"] / 32768 + 1e-9)
    log.append(f"capture: {a['samples']} samples ({a['seconds']:.1f}s) "
               f"peak={a['peak']} rms={a['rms']:.1f} ({dbfs:.1f} dBFS)")
    log.append(f"envelope ({window_sec}s windows): {a['env']}")
    log.append(f"active windows (rms>={min_rms}): {active} "
               f"(need >= {min_active_windows})")

    wav_out = _resolve_path(wav_out or config.get("audio_wav_path")
                           or "/tmp/{cmainfile}_capture.wav", config)
    try:
        _raw_to_wav(raw_path, wav_out, rec["rate"], rec["channels"])
        log.append(f"ARTIFACT: {wav_out}")
    except Exception as e:
        log.append(f"WARNING: could not write wav artifact: {e}")
    if not keep_raw:
        try:
            os.remove(raw_path)
        except OSError:
            pass

    ok = active >= int(min_active_windows) and a["peak"] > 0
    log.append("RESULT: PASS (audio present)" if ok
               else "RESULT: FAIL (silent / too quiet)")
    return ok, "\n".join(log)


# only the steps leave this module; dispatch_functions star-imports it
__all__ = [n for n, f in list(globals().items()) if getattr(f, "_is_teststep", False)]
