"""qcow2reader.py -- read-only pure-Python access to a qcow2's INTERNAL snapshots (snaptimeline.py's savevm kind), no qemu-img or QEMU.
  Qcow2Image(path).open(tag) -> file-like BlockView (diskfattools walks it).
  Per-snapshot change fingerprint from the L2 mapping (COPIED flag masked): equal => same contents; "changed" means "written to"; content_sha256() for exact.
Snapshots are immutable (safe to read while QEMU runs); the ACTIVE state of a running VM is a best-effort peek.
Supported: qcow2 v2/v3, 8-byte L2 entries, zlib clusters, zero clusters, raw/qcow2 backing. Raises on extended L2, zstd, external data files, encryption.
"""

import hashlib
import os
import struct
import zlib

QCOW_MAGIC = b"QFI\xfb"

L1_OFFSET_MASK = 0x00FFFFFFFFFFFE00
L2_OFFSET_MASK = 0x00FFFFFFFFFFFE00
FLAG_COPIED = 1 << 63
FLAG_COMPRESSED = 1 << 62
FLAG_ZERO = 1

INCOMPAT_DATA_FILE = 1 << 2
INCOMPAT_COMPRESSION = 1 << 3
INCOMPAT_EXTL2 = 1 << 4


class Qcow2Error(Exception):
    pass


def is_qcow2(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == QCOW_MAGIC
    except OSError:
        return False


class Qcow2Image:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.f = open(self.path, "rb")
        self._parse_header()
        self._backing = None

    def close(self):
        self.f.close()
        if self._backing is not None and hasattr(self._backing, "close"):
            self._backing.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _pread(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def _parse_header(self):
        h = self._pread(0, 104)
        if h[:4] != QCOW_MAGIC:
            raise Qcow2Error(f"{self.path}: not a qcow2 image")
        (_, self.version, bf_off, bf_size, self.cluster_bits, self.size,
         crypt, self.l1_size, self.l1_offset, _rc_off, _rc_clusters,
         self.nb_snapshots, self.snapshots_offset) = struct.unpack(">4sIQIIQIIQQIIQ", h[:72])
        if crypt:
            raise Qcow2Error("encrypted qcow2 not supported")
        incompat = 0
        self.compression_type = 0
        if self.version >= 3:
            incompat, = struct.unpack(">Q", h[72:80])
            hdr_len, = struct.unpack(">I", h[100:104])
            if incompat & INCOMPAT_EXTL2:
                raise Qcow2Error("extended L2 entries (subclusters) not supported")
            if incompat & INCOMPAT_DATA_FILE:
                raise Qcow2Error("external data file not supported")
            if incompat & INCOMPAT_COMPRESSION and hdr_len > 104:
                self.compression_type = self._pread(104, 1)[0]
        self.cluster_size = 1 << self.cluster_bits
        self.l2_entries = self.cluster_size // 8
        self.backing_file = (self._pread(bf_off, bf_size).decode("utf-8", "replace")
                             if bf_off and bf_size else None)
        self.backing_format = None
        if self.version >= 3:
            self._parse_extensions(hdr_len)

    def _parse_extensions(self, hdr_len):
        off = hdr_len
        while off + 8 <= self.cluster_size:
            typ, ln = struct.unpack(">II", self._pread(off, 8))
            if typ == 0:
                break
            data = self._pread(off + 8, ln)
            if typ == 0xE2792ACA:
                self.backing_format = data.decode("utf-8", "replace")
            off += 8 + ((ln + 7) & ~7)


    def snapshots(self):
        """[{id, tag, l1_offset, l1_size, vm_state_size, disk_size, date, vm_clock_ns, icount}] in table order."""
        out, off = [], self.snapshots_offset
        for _ in range(self.nb_snapshots):
            hdr = self._pread(off, 40)
            (l1_off, l1_size, id_len, name_len, date_s, date_ns, clock_ns,
             vm_state32, extra_len) = struct.unpack(">QIHHIIQII", hdr)
            extra = self._pread(off + 40, extra_len)
            vm_state = vm_state32
            disk_size = self.size
            icount = None
            if extra_len >= 8:
                vm_state = struct.unpack(">Q", extra[0:8])[0]
            if extra_len >= 16:
                disk_size = struct.unpack(">Q", extra[8:16])[0]
            if extra_len >= 24:
                ic = struct.unpack(">Q", extra[16:24])[0]
                icount = None if ic == 0xFFFFFFFFFFFFFFFF else ic
            p = off + 40 + extra_len
            sid = self._pread(p, id_len).decode("utf-8", "replace")
            tag = self._pread(p + id_len, name_len).decode("utf-8", "replace")
            out.append({"id": sid, "tag": tag, "l1_offset": l1_off, "l1_size": l1_size,
                        "vm_state_size": vm_state, "disk_size": disk_size,
                        "date": date_s + date_ns / 1e9, "vm_clock_ns": clock_ns,
                        "icount": icount})
            off = (p + id_len + name_len + 7) & ~7
        return out

    def find(self, tag):
        """A snapshot by tag or id; None = the active (live) state."""
        if tag in (None, "", "live", "active"):
            return None
        for s in self.snapshots():
            if s["tag"] == str(tag) or s["id"] == str(tag):
                return s
        raise Qcow2Error(f"{self.path}: no snapshot {tag!r}")


    def _l1(self, snap):
        off, n = ((snap["l1_offset"], snap["l1_size"]) if snap else
                  (self.l1_offset, self.l1_size))
        raw = self._pread(off, n * 8)
        return struct.unpack(f">{n}Q", raw)

    def _l2(self, l1_entry, cache):
        l2_off = l1_entry & L1_OFFSET_MASK
        if not l2_off:
            return None
        t = cache.get(l2_off)
        if t is None:
            t = struct.unpack(f">{self.l2_entries}Q", self._pread(l2_off, self.cluster_size))
            cache[l2_off] = t
        return t

    def mapping(self, tag=None):
        """(guest_cluster, L2 entry with COPIED masked) for allocated clusters only (0 = unallocated here)."""
        snap = self.find(tag)
        # stop at the disk's last cluster; vmstate past it is not guest data
        limit = -(-(snap["disk_size"] if snap else self.size) // self.cluster_size)
        cache = {}
        for i, l1e in enumerate(self._l1(snap)):
            if i * self.l2_entries >= limit:
                break
            t = self._l2(l1e, cache)
            if t is None:
                continue
            base = i * self.l2_entries
            for j, e in enumerate(t):
                if base + j >= limit:
                    break
                e &= ~FLAG_COPIED
                if e:
                    yield base + j, e

    def fingerprint(self, tag=None):
        """sha1 over the allocation map; equal => equal disk contents."""
        h = hashlib.sha1()
        for c, e in self.mapping(tag):
            h.update(struct.pack(">QQ", c, e))
        return h.hexdigest()

    def changed_clusters(self, tag_a, tag_b):
        """Guest cluster numbers whose mapping differs between two states (None = active); superset of byte-changed clusters."""
        a, b = dict(self.mapping(tag_a)), dict(self.mapping(tag_b))
        return sorted(c for c in set(a) | set(b) if a.get(c) != b.get(c))

    def allocated_bytes(self, tag=None):
        return sum(1 for _ in self.mapping(tag)) * self.cluster_size


    def _backing_view(self):
        if self._backing is None:
            if not self.backing_file:
                self._backing = False
            else:
                bp = self.backing_file
                if not os.path.isabs(bp):
                    bp = os.path.join(os.path.dirname(self.path), bp)
                if is_qcow2(bp):
                    self._backing = Qcow2Image(bp).open(None)
                else:
                    self._backing = RawView(bp)
        return self._backing or None

    def _read_cluster(self, entry, cache_ok=True):
        if entry & FLAG_COMPRESSED:
            if self.compression_type != 0:
                raise Qcow2Error("zstd-compressed clusters not supported")
            x = 62 - (self.cluster_bits - 8)
            host = entry & ((1 << x) - 1)
            nsect = ((entry >> x) & ((1 << (self.cluster_bits - 8)) - 1)) + 1
            n = nsect * 512 - (host & 511)
            data = zlib.decompressobj(-12).decompress(self._pread(host, n), self.cluster_size)
            return data.ljust(self.cluster_size, b"\0")
        if entry & FLAG_ZERO:
            return None
        return self._pread(entry & L2_OFFSET_MASK, self.cluster_size)

    def open(self, tag=None):
        return BlockView(self, self.find(tag))


class RawView:
    """Same interface as BlockView over a plain raw file."""
    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.fstat(self.f.fileno()).st_size

    def pread(self, off, n):
        self.f.seek(off)
        return self.f.read(n).ljust(max(0, min(n, self.size - off)), b"\0")

    def close(self):
        self.f.close()


class BlockView:
    """Random-access read of the guest disk at one snapshot (or active)."""
    def __init__(self, img, snap):
        self.img = img
        self.snap = snap
        self.size = snap["disk_size"] if snap else img.size
        self._l1 = img._l1(snap)
        self._l2cache = {}

    def _cluster(self, idx):
        img = self.img
        cs = img.cluster_size
        l1i, l2i = divmod(idx, img.l2_entries)
        entry = 0
        if l1i < len(self._l1):
            t = img._l2(self._l1[l1i], self._l2cache)
            if t is not None:
                entry = t[l2i] & ~FLAG_COPIED
        if entry & ~FLAG_ZERO or entry & FLAG_COMPRESSED:
            data = img._read_cluster(entry)
            if data is not None:
                return data
            return b"\0" * cs
        if entry & FLAG_ZERO:
            return b"\0" * cs
        back = img._backing_view()
        if back is None:
            return b"\0" * cs
        return back.pread(idx * cs, cs).ljust(cs, b"\0")

    def pread(self, off, n):
        n = max(0, min(n, self.size - off))
        cs = self.img.cluster_size
        out = bytearray()
        while n > 0:
            idx, within = divmod(off, cs)
            take = min(cs - within, n)
            out += self._cluster(idx)[within:within + take]
            off += take
            n -= take
        return bytes(out)

    def close(self):
        pass


def open_image(path, tag=None):
    """BlockView at `tag` for a qcow2, or RawView for a raw image (tag must be None)."""
    if is_qcow2(path):
        return Qcow2Image(path).open(tag)
    if tag not in (None, "", "live", "active"):
        raise Qcow2Error(f"{path}: raw image has no snapshot {tag!r}")
    return RawView(path)


def content_sha256(path, tag=None, chunk=1 << 20):
    """Full guest-content hash -- exact, but reads the whole disk."""
    v = open_image(path, tag)
    h = hashlib.sha256()
    off = 0
    while off < v.size:
        h.update(v.pread(off, chunk))
        off += chunk
    return h.hexdigest()


def snapshot_report(path):
    """One qcow2's snapshots with sizes and a change flag vs the previous (snap registry API, per disk)."""
    with Qcow2Image(path) as img:
        out, prev_fp = [], None
        prev_map = None
        for s in img.snapshots():
            m = dict(img.mapping(s["tag"]))
            fp = hashlib.sha1(b"".join(struct.pack(">QQ", c, e)
                                        for c, e in sorted(m.items()))).hexdigest()
            delta = (sorted(c for c in set(m) | set(prev_map) if m.get(c) != prev_map.get(c))
                     if prev_map is not None else sorted(m))
            out.append({"id": s["id"], "tag": s["tag"], "date": s["date"],
                        "vm_clock_ns": s["vm_clock_ns"],
                        "vm_state_size": s["vm_state_size"],
                        "disk_size": s["disk_size"],
                        "allocated_bytes": len(m) * img.cluster_size,
                        "fingerprint": fp,
                        "changed": prev_fp is None or fp != prev_fp,
                        "changed_bytes": len(delta) * img.cluster_size,
                        "changed_clusters": len(delta)})
            prev_fp, prev_map = fp, m
        return {"path": img.path, "cluster_size": img.cluster_size,
                "virtual_size": img.size, "file_size": os.path.getsize(img.path),
                "backing_file": img.backing_file, "snapshots": out}
