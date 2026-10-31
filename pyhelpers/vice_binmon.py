"""
Client for VICE's binary monitor protocol (-binarymonitor -binarymonitoraddress).

Unlike the text remote monitor (used elsewhere in this project via plain
`m $start $end` commands over a line-based socket), the binary protocol lets
us set checkpoints/watchpoints that the emulator triggers on its own the
instant a memory location is written -- e.g. the sprite pointer table or the
VIC bank register -- instead of us polling and hoping to catch the moment a
scene changes.

Protocol reference: VICE manual, "Binary monitor" chapter.
"""

import socket
import struct
import threading
import queue
import time

STX = 0x02
API_VERSION = 0x02

# command types
CMD_MEMORY_GET = 0x01
CMD_MEMORY_SET = 0x02
CMD_CHECKPOINT_GET = 0x11
CMD_CHECKPOINT_SET = 0x12
CMD_CHECKPOINT_DELETE = 0x13
CMD_CHECKPOINT_LIST = 0x14
CMD_CHECKPOINT_TOGGLE = 0x15
CMD_CONDITION_SET = 0x22
CMD_REGISTERS_GET = 0x31
CMD_ADVANCE_INSTRUCTIONS = 0x71   # single-step, host-load independent: VICE
                                  # stays paused and only moves on our command
CMD_PING = 0x81
CMD_BANKS_AVAILABLE = 0x82
CMD_VICE_INFO = 0x85
CMD_EXIT = 0xaa
CMD_QUIT = 0xbb

# response/event types
RESP_CHECKPOINT_INFO = 0x11
RESP_REGISTER_INFO = 0x31
RESP_JAM = 0x61
RESP_STOPPED = 0x62
RESP_RESUMED = 0x63

# checkpoint operation flags
OP_LOAD = 0x01
OP_STORE = 0x02
OP_EXEC = 0x04

UNSOLICITED = 0xffffffff


class BinMonError(Exception):
    pass


class ViceBinMon:
    """
    One binary-monitor connection. Runs a background reader thread that
    demultiplexes incoming frames: replies to our own requests go to the
    caller that's waiting on them, everything else (checkpoint hits,
    stopped/resumed events) goes to `on_event` if one is registered.
    """

    def __init__(self, host="127.0.0.1", port=6502, on_event=None):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(None)
        self._next_request_id = 1
        self._lock = threading.Lock()
        self._pending = {}  # request_id -> queue.Queue for the reply
        self._pending_lock = threading.Lock()
        self.on_event = on_event
        self._stop = threading.Event()
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # -- low level framing --------------------------------------------

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("VICE binary monitor connection closed")
            buf += chunk
        return bytes(buf)

    def _reader(self):
        try:
            while not self._stop.is_set():
                header = self._recv_exact(12)
                stx, api, length, resp_type, err, req_id = struct.unpack(
                    "<BBIBBI", header
                )
                body = self._recv_exact(length) if length else b""
                frame = (resp_type, err, req_id, body)

                if req_id != UNSOLICITED:
                    with self._pending_lock:
                        q = self._pending.get(req_id)
                    if q is not None:
                        q.put(frame)
                        continue

                # unsolicited event (checkpoint hit, stopped, resumed, jam)
                # or a reply nobody's waiting for anymore
                if self.on_event is not None:
                    try:
                        self.on_event(resp_type, err, body)
                    except Exception as e:
                        print("[vice_binmon] on_event handler raised:", e)
        except (ConnectionError, OSError):
            pass

    def request(self, command_type, body=b"", timeout=10):
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1

        q = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = q

        header = struct.pack(
            "<BBIIB", STX, API_VERSION, len(body), request_id, command_type
        )
        try:
            self.sock.sendall(header + body)
            resp_type, err, req_id, resp_body = q.get(timeout=timeout)
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        if err != 0:
            raise BinMonError(
                f"command 0x{command_type:02x} failed with error code 0x{err:02x}"
            )
        return resp_type, resp_body

    # -- convenience wrappers -------------------------------------------

    def ping(self):
        self.request(CMD_PING)

    def memory_get(self, start, end, side_effects=0, memspace=0, bank_id=0):
        body = struct.pack("<BHHBH", side_effects, start, end, memspace, bank_id)
        _, resp_body = self.request(CMD_MEMORY_GET, body)
        (mem_len,) = struct.unpack_from("<H", resp_body, 0)
        data = resp_body[2:2 + mem_len]
        return list(data)

    def checkpoint_set(self, start, end, operation, stop_when_hit=True,
                        enabled=True, temporary=False, memspace=0):
        body = struct.pack(
            "<HHBBBBB",
            start, end,
            1 if stop_when_hit else 0,
            1 if enabled else 0,
            operation,
            1 if temporary else 0,
            memspace,
        )
        _, resp_body = self.request(CMD_CHECKPOINT_SET, body)
        (checknum,) = struct.unpack_from("<I", resp_body, 0)
        return checknum

    def checkpoint_delete(self, checknum):
        body = struct.pack("<I", checknum)
        self.request(CMD_CHECKPOINT_DELETE, body)

    def exit_monitor(self):
        """Resume execution (equivalent to typing 'x' at the monitor prompt)."""
        self.request(CMD_EXIT)

    def advance_instructions(self, count=1, step_over=False):
        """Execute exactly `count` instructions then stop again -- VICE does
        not run in the meantime, so this is deterministic regardless of host
        load. `step_over` skips a JSR's callee instead of stepping into it."""
        body = struct.pack("<BH", 1 if step_over else 0, count)
        self.request(CMD_ADVANCE_INSTRUCTIONS, body)


def parse_checkpoint_info(body):
    (
        checknum, hit, start, end, stop_when_hit, enabled,
        operation, temporary, hit_count, ignore_count, has_cond, memspace,
    ) = struct.unpack_from("<IBHHBBBBIIBB", body, 0)
    return {
        "checknum": checknum,
        "currently_hit": bool(hit),
        "start": start,
        "end": end,
        "stop_when_hit": bool(stop_when_hit),
        "enabled": bool(enabled),
        "operation": operation,
        "temporary": bool(temporary),
        "hit_count": hit_count,
        "ignore_count": ignore_count,
        "has_condition": bool(has_cond),
        "memspace": memspace,
    }


if __name__ == "__main__":
    # quick smoke test against a running `x64sc -binarymonitor
    # -binarymonitoraddress 127.0.0.1:6502` instance
    events = []
    mon = ViceBinMon(on_event=lambda t, e, b: events.append((t, e, b)))
    mon.ping()
    print("ping ok")
    data = mon.memory_get(0xD400, 0xD41C)
    print("SID regs:", [f"{b:02x}" for b in data])
    checknum = mon.checkpoint_set(0xD018, 0xD018, OP_STORE, stop_when_hit=True)
    print("checkpoint set, number:", checknum)
    mon.checkpoint_delete(checknum)
    print("checkpoint deleted")
    time.sleep(0.5)
    print("unsolicited events seen:", events)
    mon.close()
