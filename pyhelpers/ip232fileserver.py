"""Delta-audio file server for a C64 talking over a Swiftlink/ip232 link.

Reuses ip232relayserver for the wire framing (0xFF escaping, DTR/RTS,
line-status keep-alive) and plugs its own handler in place of the relay's
broadcast, so the only thing here is the command protocol.

Protocol, commands and response headers are CR-terminated ASCII:

    C64 -> server   LIST
    server -> C64   <name> <size>      (one line per item)
                    ...
                    END

    C64 -> server   READ <name> <off> <len>
    server -> C64   OK <n>             then exactly <n> raw bytes
    server -> C64   OK 0                at/after end of file

    C64 -> server   GET <name>         (whole clip; see the warning below)
    server -> C64   OK <size>          then exactly <size> raw bytes
    server -> C64   ERR <reason>       on a bad name or malformed READ

READ is what the player uses. GET is kept for anything that wants the whole
clip in one go, but it is a bad fit for this link: the OK header ends up
behind the entire payload, and the C64 does not see those 12 bytes until
roughly the whole file has streamed past at the emulated ACIA baud (measured
~80s for a 311 KB clip, against a 6ms server turnaround). Bounded READs keep
every header at the front of a short reply.

The payload after an OK header is raw delta-code, not text -- a $FF in it is
escaped by ip232relayserver.reply() and unescaped by VICE, so it arrives
byte-exact.

Items are the packed .bin files produced offline by dcmpack8.py (which needs
scipy, not available to the runner). Names go on the wire uppercase because
that is what a cc65 program can compare against without charmap surprises.
"""
import os
import threading
import time

import ip232relayserver

# Timestamped trace of what crosses the link, so a stall can be attributed to
# a side. The C64 shows how long it waits between sending GET and seeing the
# reply header; this shows when the server saw that command and when it
# answered. If the command lands here late the delay is C64->VICE->server; if
# it lands immediately and the answer goes out immediately, the delay is on
# the way back. Incoming traffic only plus reply sizes -- the payload itself
# is far too big to log.
_TRACE = "/tmp/dcmfileserver_trace.log"

# Ceiling on a single READ reply. Small enough that the OK header is never
# stuck behind a long payload; the C64 asks for less than this anyway.
MAX_READ = 4096


def _trace(msg):
    try:
        with open(_TRACE, "a") as f:
            f.write("%.3f %s\n" % (time.time(), msg))
    except OSError:
        pass

PORT = 6510

# Default catalog: the encoded clips that ship with the swiftlink project.
DEFAULT_ROOT = "/testsrc/sourcedir/c64src/deltaaudio_swiftlink/dcm"

_state_lock = threading.Lock()
_partial = {}
_root = DEFAULT_ROOT
ITEMS = {}


def load_catalog(root=None):
    """Index root's .bin files as name -> (path, size). Called at start so the
    sizes on the wire are always the real ones."""
    global _root, ITEMS
    with _state_lock:
        _root = root or DEFAULT_ROOT
        items = {}
        if os.path.isdir(_root):
            for fn in sorted(os.listdir(_root)):
                if not fn.lower().endswith(".bin"):
                    continue
                path = os.path.join(_root, fn)
                items[fn.upper()] = (path, os.path.getsize(path))
        ITEMS = items
    return ITEMS


def _cmd_list():
    with _state_lock:
        items = dict(ITEMS)
    out = "".join(f"{name} {size}\r" for name, (_, size) in items.items())
    return (out + "END\r").encode("ascii")


def _cmd_get(arg):
    with _state_lock:
        entry = ITEMS.get(arg.upper())
    if not entry:
        return f"ERR NO SUCH FILE\r".encode("ascii")
    path, size = entry
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        return f"ERR {e.__class__.__name__}\r".encode("ascii")
    # Re-measure: the header must match the payload exactly, since the C64
    # counts down from it and would otherwise hang waiting for bytes that
    # never come.
    return f"OK {len(blob)}\r".encode("ascii") + blob


def _cmd_read(arg):
    """READ <name> <offset> <len> -> OK <n>\\r then n raw bytes.

    The bounded counterpart to GET. GET answers with the entire clip, which
    buries the OK header behind hundreds of KB: measured, the server replied
    6ms after the request and the C64 still did not see that 12-byte header
    for ~80s, tracking the time to stream the whole file at the emulated ACIA
    baud. A ranged read keeps every reply small enough to arrive at once and
    lets the C64 pace the transfer, which also means aborting a clip costs one
    chunk instead of the rest of the file.
    """
    parts = arg.split()
    if len(parts) != 3:
        return b"ERR BAD READ\r"
    name, off_s, len_s = parts
    try:
        off = int(off_s)
        want = int(len_s)
    except ValueError:
        return b"ERR BAD READ\r"
    if off < 0 or want <= 0:
        return b"ERR BAD READ\r"

    with _state_lock:
        entry = ITEMS.get(name.upper())
    if not entry:
        return b"ERR NO SUCH FILE\r"
    path, size = entry
    if off >= size:
        return b"OK 0\r"                    # clean end-of-file marker

    want = min(want, MAX_READ, size - off)
    try:
        with open(path, "rb") as f:
            f.seek(off)
            blob = f.read(want)
    except OSError as e:
        return f"ERR {e.__class__.__name__}\r".encode("ascii")
    return f"OK {len(blob)}\r".encode("ascii") + blob


def _handle_line(line):
    parts = line.strip().split(None, 1)
    if not parts:
        return b""
    cmd = parts[0].upper()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "LIST":
        out = _cmd_list()
    elif cmd == "READ":
        out = _cmd_read(arg)
    elif cmd == "GET":
        out = _cmd_get(arg)
    else:
        out = f"ERR UNKNOWN {cmd}\r".encode("ascii")
    # READ is issued hundreds of times per clip, so trace it at one line per
    # request rather than the three GET used to get.
    _trace(f"{cmd} {arg!r} -> {len(out)} bytes")
    return out


def _on_bytes(server, data):
    """ip232relayserver handler: accumulate until CR, answer whole lines.

    Decoding goes through petscii_bytes_to_str so shifted-range letters
    ($C1-$DA, what a cc65 uppercase literal puts on the wire) are understood
    as well as the plain $41-$5A the client is expected to send.
    """
    with _state_lock:
        buf = _partial.get(id(server), "") + ip232relayserver.petscii_bytes_to_str(data)
        # petscii_bytes_to_str renders CR as \n, so split on that alone.
        *lines, _partial[id(server)] = buf.split("\n")

    return b"".join(_handle_line(line) for line in lines)


def start_server(host=ip232relayserver.HOST, port=PORT, root=None):
    load_catalog(root)
    ip232relayserver.start_server(host=host, port=port, on_bytes=_on_bytes)


def stop_server(instance_names=None):
    with _state_lock:
        _partial.clear()
    return ip232relayserver.stop_server(instance_names=instance_names)
