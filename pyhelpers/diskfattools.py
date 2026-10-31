"""diskfattools.py -- host-side read-only FAT12/FAT16 block walker and differ; reads raw and qcow2 images (incl. internal snapshots via qcow2reader).

Commands:
    info    boot sector / BPB, geometry, region layout, FAT type
    walk    every dirent (raw slot, decoded fields, cluster chain) and every cluster's owner (path, directory, free, bad, reserved, LOST, CROSS)
    check   fsck-style findings
    diff    two states of a volume: sectors by region, FAT entries, dirents by slot, files added/removed/modified/renamed, changed data clusters and owners
    ls / cat  list a directory, dump a file

CLI
    python3 diskfattools.py info  IMG[@TAG]
    python3 diskfattools.py ls    IMG[@TAG] [PATH]
    python3 diskfattools.py walk  IMG[@TAG] [--clusters]
    python3 diskfattools.py check IMG[@TAG]
    python3 diskfattools.py diff  IMG[@TAG] IMG2[@TAG2]   (or IMG@A @B)
    python3 diskfattools.py cat   IMG[@TAG] PATH > out
    python3 diskfattools.py snaps IMG.qcow2
    python3 diskfattools.py timeline IMG.qcow2    (diff every snapshot vs the one before)
  --json: machine form; --offset N (bytes) or --partition K (1-4) picks a volume (default: BPB at sector 0, else first FAT MBR partition).
  TAG = qcow2 snapshot tag/id; omit or 'live' for the current state.

Python use
    vol = diskfattools.open_volume("a_drive.qcow2", tag="tl3")
    w = vol.walk()
    d = diskfattools.diff_volumes(vol, diskfattools.open_volume(p, tag="tl5"))
"""

import argparse
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qcow2reader  # noqa: E402

FAT_PART_TYPES = {0x01, 0x04, 0x06, 0x0E, 0x11, 0x14, 0x16, 0x1E}

ATTR_RO, ATTR_HIDDEN, ATTR_SYSTEM, ATTR_VOLUME, ATTR_DIR, ATTR_ARCHIVE = 1, 2, 4, 8, 16, 32
ATTR_LFN = 0x0F


class FatError(Exception):
    pass


def _bpb_ok(bs):
    if len(bs) < 512:
        return False
    bps, spc = struct.unpack_from("<H", bs, 11)[0], bs[13]
    nfats = bs[16]
    return (bps in (512, 1024, 2048, 4096) and spc in (1, 2, 4, 8, 16, 32, 64, 128)
            and nfats in (1, 2) and struct.unpack_from("<H", bs, 14)[0] >= 1)


def _mbr_partitions(sec0):
    out = []
    if sec0[510:512] != b"\x55\xaa":
        return out
    for i in range(4):
        e = sec0[446 + 16 * i: 462 + 16 * i]
        ptype = e[4]
        lba, count = struct.unpack_from("<II", e, 8)
        if ptype and count:
            out.append({"index": i + 1, "type": ptype, "lba": lba, "sectors": count,
                        "boot": e[0] == 0x80})
    return out


class Volume:
    def __init__(self, dev, offset=0, label=""):
        self.dev = dev
        self.offset = offset
        self.label = label
        bs = dev.pread(offset, 512)
        if not _bpb_ok(bs):
            raise FatError(f"{label}: no FAT boot sector at byte {offset}")
        self.boot = bs
        u16 = lambda o: struct.unpack_from("<H", bs, o)[0]
        u32 = lambda o: struct.unpack_from("<I", bs, o)[0]
        self.oem = bs[3:11].decode("latin-1").rstrip()
        self.bps = u16(11)
        self.spc = bs[13]
        self.reserved = u16(14)
        self.nfats = bs[16]
        self.root_entries = u16(17)
        total16 = u16(19)
        self.media = bs[21]
        self.spf = u16(22)
        self.spt = u16(24)
        self.heads = u16(26)
        self.hidden = u32(28)
        self.total_sectors = total16 or u32(32)
        if self.spf == 0:
            raise FatError(f"{label}: sectors-per-FAT is 0 -- FAT32 is not supported")
        ext = bs[38] == 0x29
        self.serial = u32(39) if ext else None
        self.vol_label = bs[43:54].decode("latin-1").rstrip() if ext else ""
        self.fs_type_str = bs[54:62].decode("latin-1").rstrip() if ext else ""
        self.cluster_bytes = self.bps * self.spc
        self.fat_start = self.reserved
        self.root_start = self.fat_start + self.nfats * self.spf
        self.root_sectors = (self.root_entries * 32 + self.bps - 1) // self.bps
        self.data_start = self.root_start + self.root_sectors
        self.clusters = (self.total_sectors - self.data_start) // self.spc
        if self.clusters < 4085:
            self.fat_bits = 12
        elif self.clusters < 65525:
            self.fat_bits = 16
        else:
            raise FatError(f"{label}: {self.clusters} clusters -- FAT32, not supported")
        self.max_cluster = self.clusters + 1
        self._fats = None

    def read_sectors(self, lba, count=1):
        return self.dev.pread(self.offset + lba * self.bps, count * self.bps)

    def cluster_lba(self, c):
        return self.data_start + (c - 2) * self.spc

    def read_cluster(self, c):
        return self.read_sectors(self.cluster_lba(c), self.spc)

    def region_of(self, lba):
        if lba < self.fat_start:
            return "boot"
        if lba < self.root_start:
            return f"fat{(lba - self.fat_start) // self.spf + 1}"
        if lba < self.data_start:
            return "root"
        return "data"

    def sector_owner_cluster(self, lba):
        return None if lba < self.data_start else (lba - self.data_start) // self.spc + 2

    def fat(self, copy=0):
        if self._fats is None:
            self._fats = [self._decode_fat(self.read_sectors(self.fat_start + i * self.spf, self.spf))
                          for i in range(self.nfats)]
        return self._fats[copy]

    def _decode_fat(self, raw):
        n = self.max_cluster + 1
        if self.fat_bits == 16:
            return list(struct.unpack_from(f"<{n}H", raw.ljust(n * 2, b"\0")))
        out = []
        raw = raw.ljust((n * 3 + 1) // 2 + 2, b"\0")
        for c in range(n):
            o = c * 3 // 2
            v = raw[o] | (raw[o + 1] << 8)
            out.append((v >> 4) if c & 1 else (v & 0xFFF))
        return out

    @property
    def eoc(self):
        return 0xFF8 if self.fat_bits == 12 else 0xFFF8

    @property
    def bad(self):
        return 0xFF7 if self.fat_bits == 12 else 0xFFF7

    def chain(self, start, fat=None, limit=None):
        """(clusters, problem) following the FAT from `start`."""
        fat = fat or self.fat()
        out, seen, c = [], set(), start
        limit = limit or self.clusters + 1
        while True:
            if c < 2 or c > self.max_cluster:
                return out, (f"chain points outside the volume ({c:#x})" if out or c else None)
            if c in seen:
                return out, f"loop at cluster {c}"
            seen.add(c)
            out.append(c)
            nxt = fat[c]
            if nxt >= self.eoc:
                return out, None
            if nxt == 0:
                return out, f"chain runs into a FREE cluster after {c}"
            if nxt == self.bad:
                return out, f"chain runs into a BAD cluster after {c}"
            if len(out) > limit:
                return out, "chain too long"
            c = nxt

    def info(self):
        return {
            "label": self.label, "offset": self.offset, "oem": self.oem,
            "fat_bits": self.fat_bits, "bytes_per_sector": self.bps,
            "sectors_per_cluster": self.spc, "cluster_bytes": self.cluster_bytes,
            "reserved_sectors": self.reserved, "fats": self.nfats,
            "sectors_per_fat": self.spf, "root_entries": self.root_entries,
            "total_sectors": self.total_sectors, "media": f"{self.media:#04x}",
            "sectors_per_track": self.spt, "heads": self.heads,
            "hidden_sectors": self.hidden, "serial": (f"{self.serial >> 16:04X}-{self.serial & 0xFFFF:04X}"
                                                      if self.serial is not None else None),
            "volume_label": self.vol_label, "fs_type_string": self.fs_type_str,
            "clusters": self.clusters,
            "regions": {
                "boot": [0, self.fat_start - 1],
                **{f"fat{i + 1}": [self.fat_start + i * self.spf,
                                   self.fat_start + (i + 1) * self.spf - 1] for i in range(self.nfats)},
                "root": [self.root_start, self.data_start - 1],
                "data": [self.data_start, self.total_sectors - 1],
            },
        }

    def _dir_slots(self, where):
        """Yield (slot_location, 32-byte raw) for a directory; where = "root" or a start cluster."""
        if where == "root":
            raw = self.read_sectors(self.root_start, self.root_sectors)
            for i in range(self.root_entries):
                lba = self.root_start + (i * 32) // self.bps
                yield {"region": "root", "lba": lba, "byte": (i * 32) % self.bps, "index": i}, raw[i * 32:i * 32 + 32]
            return
        clusters, _ = self.chain(where)
        idx = 0
        for c in clusters:
            raw = self.read_cluster(c)
            for k in range(self.cluster_bytes // 32):
                off = k * 32
                yield {"region": "data", "cluster": c, "lba": self.cluster_lba(c) + off // self.bps,
                       "byte": off % self.bps, "index": idx}, raw[off:off + 32]
                idx += 1

    def listdir(self, where="root", include_deleted=True):
        """Decoded entries of one directory with raw slot locations; stops at the first 0x00 slot."""
        out, lfn = [], []
        for loc, e in self._dir_slots(where):
            first = e[0]
            if first == 0x00:
                break
            attr = e[11]
            if attr == ATTR_LFN:
                if first != 0xE5:
                    lfn.append(e)
                continue
            ent = decode_dirent(e)
            ent["slot"] = loc
            if lfn:
                ent["long_name"] = _lfn_name(lfn)
            lfn = []
            if ent["deleted"] and not include_deleted:
                continue
            out.append(ent)
        return out

    def walk(self):
        """Block walk: every dirent reachable from root, its chain, per-cluster ownership."""
        fat = self.fat()
        owners = {}
        entries = []
        problems = []

        def visit(where, path, depth):
            if depth > 32:
                problems.append(f"{path}: directory nesting deeper than 32 -- loop?")
                return
            for ent in self.listdir(where):
                name = ent["name"]
                if name in (".", ".."):
                    continue
                full = (path.rstrip("/") + "/" + name) if not ent["volume_label"] else f"<label {name}>"
                ent["path"] = full
                if ent["deleted"]:
                    ent["chain"] = []
                    entries.append(ent)
                    continue
                if ent["volume_label"]:
                    entries.append(ent)
                    continue
                if ent["start_cluster"]:
                    ch, prob = self.chain(ent["start_cluster"], fat)
                else:
                    ch, prob = [], None
                ent["chain"] = ch
                ent["chain_problem"] = prob
                if prob:
                    problems.append(f"{full}: {prob}")
                if not ent["is_dir"]:
                    need = -(-ent["size"] // self.cluster_bytes) if ent["size"] else 0
                    if need != len(ch):
                        ent["size_mismatch"] = {"size": ent["size"], "clusters_needed": need,
                                                "clusters_in_chain": len(ch)}
                        problems.append(f"{full}: size {ent['size']} needs {need} cluster(s), "
                                        f"chain has {len(ch)}")
                for c in ch:
                    owners.setdefault(c, []).append(full)
                entries.append(ent)
                if ent["is_dir"] and ch:
                    visit(ent["start_cluster"], full, depth + 1)

        visit("root", "/", 0)

        cmap = {}
        lost, cross, free, badc = [], [], 0, []
        for c in range(2, self.max_cluster + 1):
            v = fat[c]
            who = owners.get(c)
            if v == 0:
                state = "free"
                free += 1
                if who:
                    problems.append(f"cluster {c} is FREE in the FAT but used by {', '.join(who)}")
            elif v == self.bad:
                state = "bad"
                badc.append(c)
            elif who and len(who) > 1:
                state = "cross"
                cross.append({"cluster": c, "owners": who})
            elif who:
                state = "used"
            else:
                state = "lost"
                lost.append(c)
            cmap[c] = {"state": state, "next": v, "owner": who[0] if who and len(who) == 1 else who}
        if lost:
            problems.append(f"{len(lost)} lost cluster(s) (allocated, no owner): "
                            + _ranges(lost))
        for x in cross:
            problems.append(f"cluster {x['cluster']} cross-linked: {', '.join(x['owners'])}")
        fat_mismatch = []
        for i in range(1, self.nfats):
            other = self.fat(i)
            diff = [c for c in range(len(fat)) if fat[c] != other[c]]
            if diff:
                fat_mismatch.append({"copy": i + 1, "clusters": diff})
                problems.append(f"FAT{i + 1} differs from FAT1 at {len(diff)} entr(ies): {_ranges(diff)}")
        return {"info": self.info(), "entries": entries, "clusters": cmap,
                "summary": {"files": sum(1 for e in entries if not e["is_dir"] and not e["deleted"]
                                         and not e["volume_label"]),
                            "dirs": sum(1 for e in entries if e["is_dir"] and not e["deleted"]),
                            "deleted": sum(1 for e in entries if e["deleted"]),
                            "free_clusters": free, "free_bytes": free * self.cluster_bytes,
                            "lost_clusters": len(lost), "cross_linked": len(cross),
                            "bad_clusters": len(badc)},
                "fat_mismatch": fat_mismatch, "problems": problems}

    def find(self, path):
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        where, ent = "root", None
        for i, p in enumerate(parts):
            ent = next((e for e in self.listdir(where, include_deleted=False)
                        if e["name"].upper() == p.upper()
                        or (e.get("long_name") or "").upper() == p.upper()), None)
            if ent is None:
                raise FatError(f"{path}: {p!r} not found")
            if i < len(parts) - 1:
                if not ent["is_dir"]:
                    raise FatError(f"{path}: {p!r} is not a directory")
                where = ent["start_cluster"]
        return ent

    def read_file(self, path_or_ent):
        ent = self.find(path_or_ent) if isinstance(path_or_ent, str) else path_or_ent
        if not ent["start_cluster"]:
            return b""
        ch, _ = self.chain(ent["start_cluster"])
        data = b"".join(self.read_cluster(c) for c in ch)
        return data if ent["is_dir"] else data[:ent["size"]]


def decode_dirent(e):
    first = e[0]
    deleted = first == 0xE5
    base = bytes([0xE5 if first == 0x05 else first]) + e[1:8]
    name = base.decode("cp437").rstrip()
    ext = e[8:11].decode("cp437").rstrip()
    if deleted:
        name = "?" + name[1:]
    attr = e[11]
    ctime, cdate, adate, hi, wtime, wdate, lo, size = struct.unpack_from("<HHHHHHHI", e, 14)
    return {
        "name": name + ("." + ext if ext else "") if not attr & ATTR_VOLUME else (name + ext).rstrip(),
        "raw": e.hex(),
        "attr": attr, "attr_str": _attr_str(attr),
        "is_dir": bool(attr & ATTR_DIR), "volume_label": bool(attr & ATTR_VOLUME) and attr != ATTR_LFN,
        "deleted": deleted,
        "start_cluster": lo | (hi << 16),
        "size": size,
        "created": _dostime(cdate, ctime), "written": _dostime(wdate, wtime),
        "accessed": _dostime(adate, 0)[:10] if adate else None,
        "nt_res": e[12], "ctime_tenths": e[13],
    }


def _attr_str(a):
    return "".join(ch if a & bit else "-" for ch, bit in
                   (("A", ATTR_ARCHIVE), ("D", ATTR_DIR), ("V", ATTR_VOLUME),
                    ("S", ATTR_SYSTEM), ("H", ATTR_HIDDEN), ("R", ATTR_RO)))


def _dostime(d, t):
    if not d and not t:
        return None
    y, m, dd = 1980 + (d >> 9), (d >> 5) & 15, d & 31
    hh, mm, ss = t >> 11, (t >> 5) & 63, (t & 31) * 2
    return f"{y:04d}-{m:02d}-{dd:02d} {hh:02d}:{mm:02d}:{ss:02d}"


def _lfn_name(parts):
    chars = b""
    for e in reversed(parts):
        chars += e[1:11] + e[14:26] + e[28:32]
    s = chars.decode("utf-16-le", "replace")
    return s.split("\x00", 1)[0].rstrip("￿")


def _ranges(nums):
    nums = sorted(nums)
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    s = ",".join(out)
    return s if len(s) < 300 else s[:300] + "..."


def open_volume(path, tag=None, offset=None, partition=None):
    """Volume over a raw or qcow2 image at snapshot `tag` (None = live); partition auto-detect: BPB at sector 0, else first FAT MBR partition."""
    dev = qcow2reader.open_image(path, tag)
    label = f"{os.path.basename(path)}@{tag or 'live'}"
    if offset is None:
        sec0 = dev.pread(0, 512)
        parts = _mbr_partitions(sec0)
        if partition:
            p = next((x for x in parts if x["index"] == int(partition)), None)
            if p is None:
                raise FatError(f"{label}: no partition {partition}")
            offset = p["lba"] * 512
        elif _bpb_ok(sec0) and not (parts and sec0[0] not in (0xEB, 0xE9)):
            offset = 0
        else:
            p = next((x for x in parts if x["type"] in FAT_PART_TYPES), None)
            if p is None:
                raise FatError(f"{label}: no FAT boot sector and no FAT partition in the MBR")
            offset = p["lba"] * 512
    return Volume(dev, offset, label)


DIRENT_FIELDS = ("name", "attr_str", "start_cluster", "size", "written", "created", "accessed", "deleted")


def diff_volumes(a, b, sector_detail=64):
    """Everything that changed from volume state `a` to `b` (same geometry)."""
    if a.total_sectors != b.total_sectors or a.data_start != b.data_start:
        raise FatError("the two volumes have different layouts -- not two states of one disk")
    out = {"from": a.label, "to": b.label}

    changed = []
    chunk = 64
    for lba in range(0, a.total_sectors, chunk):
        n = min(chunk, a.total_sectors - lba)
        ra, rb = a.read_sectors(lba, n), b.read_sectors(lba, n)
        if ra == rb:
            continue
        for k in range(n):
            if ra[k * a.bps:(k + 1) * a.bps] != rb[k * a.bps:(k + 1) * a.bps]:
                changed.append(lba + k)
    by_region = {}
    for lba in changed:
        by_region.setdefault(a.region_of(lba), []).append(lba)
    out["sectors"] = {"changed": len(changed),
                      "by_region": {r: {"count": len(v), "lbas": _ranges(v)} for r, v in by_region.items()}}
    detail = []
    for lba in changed[:sector_detail]:
        ra, rb = a.read_sectors(lba), b.read_sectors(lba)
        bytes_diff = [i for i in range(a.bps) if ra[i] != rb[i]]
        detail.append({"lba": lba, "region": a.region_of(lba),
                       "cluster": a.sector_owner_cluster(lba),
                       "bytes_changed": len(bytes_diff),
                       "first_byte": bytes_diff[0], "last_byte": bytes_diff[-1]})
    out["sector_detail"] = detail

    fa, fb = a.fat(), b.fat()
    fat_changes = [{"cluster": c, "from": fa[c], "to": fb[c],
                    "meaning": _fat_meaning(a, fa[c]) + " -> " + _fat_meaning(b, fb[c])}
                   for c in range(2, a.max_cluster + 1) if fa[c] != fb[c]]
    out["fat_changes"] = fat_changes

    wa, wb = a.walk(), b.walk()

    def slots(w):
        return {_slotkey(e["slot"]): e for e in w["entries"]}
    sa, sb = slots(wa), slots(wb)
    slot_changes = []
    for k in sorted(set(sa) | set(sb)):
        ea, eb = sa.get(k), sb.get(k)
        if ea and eb and ea["raw"] == eb["raw"]:
            continue
        rec = {"slot": (eb or ea)["slot"], "path": (eb or ea).get("path")}
        if ea is None:
            rec["kind"] = "created"
            rec["to"] = _dirent_brief(eb)
        elif eb is None:
            rec["kind"] = "gone"
            rec["from"] = _dirent_brief(ea)
        else:
            rec["kind"] = ("deleted" if eb["deleted"] and not ea["deleted"] else
                           "undeleted" if ea["deleted"] and not eb["deleted"] else "modified")
            rec["fields"] = {f: [ea.get(f), eb.get(f)] for f in DIRENT_FIELDS if ea.get(f) != eb.get(f)}
            rec["bytes_changed"] = [i for i in range(32)
                                    if ea["raw"][2 * i:2 * i + 2] != eb["raw"][2 * i:2 * i + 2]]
        slot_changes.append(rec)
    out["dirent_changes"] = slot_changes

    def live(w):
        return {e["path"]: e for e in w["entries"] if not e["deleted"] and not e["volume_label"]}
    la, lb = live(wa), live(wb)
    added = [p for p in lb if p not in la]
    removed = [p for p in la if p not in lb]
    renamed = []
    for p in list(removed):
        sc = la[p]["start_cluster"]
        match = next((q for q in added if sc and lb[q]["start_cluster"] == sc), None)
        if match:
            renamed.append({"from": p, "to": match})
            removed.remove(p)
            added.remove(match)
    def file_changes(ea, eb):
        ch = {f: [ea.get(f), eb.get(f)] for f in ("size", "start_cluster", "attr_str", "written")
              if ea.get(f) != eb.get(f)}
        common = set(ea["chain"])
        data_changed = [c for c in eb["chain"] if c in common and a.read_cluster(c) != b.read_cluster(c)]
        if ea["chain"] != eb["chain"]:
            ch["chain"] = {"from": _ranges(ea["chain"]), "to": _ranges(eb["chain"]),
                           "added": _ranges(sorted(set(eb["chain"]) - common)),
                           "freed": _ranges(sorted(common - set(eb["chain"])))}
        if data_changed:
            ch["data_clusters_changed"] = _ranges(data_changed)
        return ch

    for r in renamed:
        r["changes"] = file_changes(la[r["from"]], lb[r["to"]])
    modified = []
    for p in sorted(set(la) & set(lb)):
        ch = file_changes(la[p], lb[p])
        if ch:
            modified.append({"path": p, "changes": ch})
    out["files"] = {"added": [{"path": p, "size": lb[p]["size"], "chain": _ranges(lb[p]["chain"])}
                              for p in sorted(added)],
                    "removed": [{"path": p, "size": la[p]["size"], "chain": _ranges(la[p]["chain"])}
                                for p in sorted(removed)],
                    "renamed": renamed, "modified": modified}

    dc = sorted({a.sector_owner_cluster(l) for l in by_region.get("data", [])})
    out["data_clusters"] = [{"cluster": c,
                             "before": wa["clusters"].get(c, {}).get("state"),
                             "after": wb["clusters"].get(c, {}).get("state"),
                             "owner_before": wa["clusters"].get(c, {}).get("owner"),
                             "owner_after": wb["clusters"].get(c, {}).get("owner")}
                            for c in dc]
    out["problems"] = {"from": wa["problems"], "to": wb["problems"],
                       "new": [p for p in wb["problems"] if p not in wa["problems"]]}
    out["identical"] = not changed
    return out


def _slotkey(slot):
    return (slot["region"], slot.get("cluster", 0), slot["lba"], slot["byte"])


def _dirent_brief(e):
    return {f: e.get(f) for f in DIRENT_FIELDS}


def _fat_meaning(v, x):
    if x == 0:
        return "free"
    if x >= v.eoc:
        return "EOC"
    if x == v.bad:
        return "bad"
    return str(x)


def fmt_info(i):
    lines = [f"{i['label']}: FAT{i['fat_bits']}  oem={i['oem']!r}  label={i['volume_label']!r}  serial={i['serial']}",
             f"  {i['total_sectors']} sectors x {i['bytes_per_sector']}B, cluster {i['cluster_bytes']}B "
             f"({i['sectors_per_cluster']} sec), {i['clusters']} clusters, media {i['media']}",
             f"  geometry {i['heads']}h x {i['sectors_per_track']}spt, hidden {i['hidden_sectors']}, "
             f"volume at byte {i['offset']}"]
    for r, (s, e) in i["regions"].items():
        lines.append(f"  {r:5s} LBA {s:>7}-{e:<7} ({e - s + 1} sectors)")
    return "\n".join(lines)


def fmt_ls(ents):
    lines = []
    for e in ents:
        if e["name"] in (".", ".."):
            continue
        s = e["slot"]
        where = f"root#{s['index']}" if s["region"] == "root" else f"c{s['cluster']}#{s['index']}"
        lines.append(f"{'<DEL>' if e['deleted'] else '     '} {e['name']:<13} {e['attr_str']} "
                     f"{'<DIR>' if e['is_dir'] else e['size']:>9} {e['written'] or '':19} "
                     f"start={e['start_cluster']:<5} slot={where} lba={s['lba']}+{s['byte']}"
                     + (f"  [{e['long_name']}]" if e.get("long_name") else ""))
    return "\n".join(lines)


def fmt_walk(w, clusters=False):
    lines = [fmt_info(w["info"]), ""]
    for e in w["entries"]:
        if e["volume_label"]:
            continue
        tag = "<DEL>" if e["deleted"] else "<DIR>" if e["is_dir"] else ""
        lines.append(f"{e['path']:<32} {tag:5} {e['size']:>9} start={e['start_cluster']:<5} "
                     f"chain={_ranges(e['chain']) or '-'}"
                     + (f"  !! {e['chain_problem']}" if e.get("chain_problem") else ""))
    s = w["summary"]
    lines.append("")
    lines.append(f"{s['files']} files, {s['dirs']} dirs, {s['deleted']} deleted entries; "
                 f"{s['free_clusters']} free clusters ({s['free_bytes']} bytes), "
                 f"{s['lost_clusters']} lost, {s['cross_linked']} cross-linked, {s['bad_clusters']} bad")
    if clusters:
        lines.append("")
        lines.append("cluster map (. free  # used  L lost  X cross  B bad):")
        sym = {"free": ".", "used": "#", "lost": "L", "cross": "X", "bad": "B"}
        cm = w["clusters"]
        keys = sorted(cm)
        for i in range(0, len(keys), 64):
            row = "".join(sym[cm[c]["state"]] for c in keys[i:i + 64])
            lines.append(f"  {keys[i]:>6} {row}")
    return "\n".join(lines)


def fmt_check(w):
    if not w["problems"]:
        return f"{w['info']['label']}: clean"
    return f"{w['info']['label']}: {len(w['problems'])} problem(s)\n" + "\n".join("  - " + p for p in w["problems"])


def fmt_diff(d):
    if d["identical"]:
        return f"{d['from']} -> {d['to']}: identical"
    lines = [f"{d['from']} -> {d['to']}: {d['sectors']['changed']} sector(s) changed"]
    for r, v in d["sectors"]["by_region"].items():
        lines.append(f"  {r:5s} {v['count']:>5} sector(s): {v['lbas']}")
    f = d["files"]
    for p in f["added"]:
        lines.append(f"  + {p['path']}  {p['size']}B  chain {p['chain']}")
    for p in f["removed"]:
        lines.append(f"  - {p['path']}  {p['size']}B  chain {p['chain']}")
    def chg(changes):
        return "; ".join(f"{k} {v['from']} -> {v['to']}" if isinstance(v, dict)
                         else f"{k} {v}" if isinstance(v, str) else f"{k} {v[0]} -> {v[1]}"
                         for k, v in changes.items())
    for r in f["renamed"]:
        lines.append(f"  R {r['from']} -> {r['to']}" + (f": {chg(r['changes'])}" if r["changes"] else "")
                     + "  (same start cluster)")
    for m in f["modified"]:
        lines.append(f"  M {m['path']}: {chg(m['changes'])}")
    if d["dirent_changes"]:
        lines.append("  dirent slots:")
        for c in d["dirent_changes"]:
            s = c["slot"]
            where = f"root#{s['index']}" if s["region"] == "root" else f"c{s['cluster']}#{s['index']}"
            desc = (", ".join(f"{k}: {v[0]} -> {v[1]}" for k, v in c.get("fields", {}).items())
                    if "fields" in c else json.dumps(c.get("to") or c.get("from")))
            lines.append(f"    {c['kind']:9s} {where:<10} lba {s['lba']}+{s['byte']:<3} {c.get('path') or ''}  {desc}")
    if d["fat_changes"]:
        lines.append(f"  FAT entries ({len(d['fat_changes'])}):")
        for c in d["fat_changes"][:80]:
            lines.append(f"    cluster {c['cluster']:>5}: {c['meaning']}")
        if len(d["fat_changes"]) > 80:
            lines.append(f"    ... {len(d['fat_changes']) - 80} more")
    if d["problems"]["new"]:
        lines.append("  NEW problems:")
        lines += ["    - " + p for p in d["problems"]["new"]]
    return "\n".join(lines)


def timeline(path, offset=None, partition=None, as_json=False):
    """Diff every snapshot of a qcow2 against the one before (first only listed), then live against the last; unchanged cluster maps are not read. Returns 0, or 1 if any state has fs problems."""
    rep = qcow2reader.snapshot_report(path)
    tags = [s["tag"] for s in rep["snapshots"]]
    out, bad, prev = [], False, None
    for s in rep["snapshots"] + [{"tag": None, "changed": True}]:
        vol = open_volume(path, s["tag"], offset, partition)
        w = vol.walk()
        bad = bad or bool(w["problems"])
        if prev is None:
            rec = {"tag": s["tag"], "files": [
                {"path": e["path"], "size": e["size"], "chain": _ranges(e["chain"])}
                for e in w["entries"] if not e["deleted"] and not e["volume_label"]],
                "problems": w["problems"]}
        elif s["tag"] is not None and not s["changed"]:
            rec = {"tag": s["tag"], "from": prev.label, "unchanged": True}
        else:
            rec = {"tag": s["tag"] or "live", "diff": diff_volumes(prev, vol)}
        out.append(rec)
        prev = vol
    if as_json:
        print(json.dumps({"path": path, "tags": tags, "states": out}, indent=2, default=str))
        return 1 if bad else 0
    print(f"{path}: {len(tags)} snapshot(s) {' '.join(tags)}")
    for rec in out:
        if "files" in rec:
            print(f"== {rec['tag']} (first)")
            for f in rec["files"]:
                print(f"   {f['path']:<24} {f['size']:>8}  chain {f['chain'] or '-'}")
            for p in rec["problems"]:
                print(f"   !! {p}")
        elif rec.get("unchanged"):
            print(f"== {rec['tag']}: disk unchanged since {rec['from']}")
        else:
            print(f"== {rec['tag']}")
            print("   " + fmt_diff(rec["diff"]).replace("\n", "\n   "))
    return 1 if bad else 0


def _split_spec(spec, default_path=None):
    """'img.qcow2@tl3' -> (path, tag); '@tl5' reuses default_path."""
    if "@" in spec:
        p, t = spec.rsplit("@", 1)
        return (p or default_path), (t or None)
    return spec, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="FAT12/16 block walker and differ (read-only)")
    ap.add_argument("cmd", choices=("info", "ls", "walk", "check", "diff", "cat", "snaps", "timeline"))
    ap.add_argument("image", help="IMG[@TAG]")
    ap.add_argument("arg", nargs="?", help="path (ls/cat) or IMG2[@TAG2] / @TAG2 (diff)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--clusters", action="store_true", help="walk: print the cluster map")
    ap.add_argument("--offset", type=int)
    ap.add_argument("--partition", type=int)
    a = ap.parse_args(argv)

    if a.cmd == "snaps":
        r = qcow2reader.snapshot_report(a.image)
        if a.json:
            print(json.dumps(r, indent=2))
        else:
            print(f"{r['path']}  cluster {r['cluster_size']}  virtual {r['virtual_size']}  "
                  f"file {r['file_size']}  backing {r['backing_file']}")
            for s in r["snapshots"]:
                print(f"  {s['id']:>3} {s['tag']:<12} vmstate {s['vm_state_size']:>9}  "
                      f"alloc {s['allocated_bytes']:>9}  "
                      f"{'CHANGED' if s['changed'] else 'same   '} {s['changed_bytes']:>8}B  {s['fingerprint'][:12]}")
        return 0

    if a.cmd == "timeline":
        return timeline(a.image, a.offset, a.partition, a.json)

    path, tag = _split_spec(a.image)
    vol = open_volume(path, tag, a.offset, a.partition)
    if a.cmd == "info":
        r = vol.info()
        print(json.dumps(r, indent=2) if a.json else fmt_info(r))
    elif a.cmd == "ls":
        where = "root"
        if a.arg and a.arg.strip("/\\"):
            ent = vol.find(a.arg)
            if not ent["is_dir"]:
                raise FatError(f"{a.arg} is not a directory")
            where = ent["start_cluster"]
        r = vol.listdir(where)
        print(json.dumps(r, indent=2) if a.json else fmt_ls(r))
    elif a.cmd == "walk":
        r = vol.walk()
        if a.json:
            r = dict(r, clusters={str(k): v for k, v in r["clusters"].items()} if a.clusters else None)
            print(json.dumps(r, indent=2))
        else:
            print(fmt_walk(r, a.clusters))
    elif a.cmd == "check":
        r = vol.walk()
        print(json.dumps({"problems": r["problems"], "summary": r["summary"]}, indent=2)
              if a.json else fmt_check(r))
        return 1 if r["problems"] else 0
    elif a.cmd == "cat":
        sys.stdout.buffer.write(vol.read_file(a.arg or ""))
    elif a.cmd == "diff":
        if not a.arg:
            ap.error("diff needs a second IMG[@TAG] (or @TAG for the same image)")
        p2, t2 = _split_spec(a.arg, path)
        other = open_volume(p2, t2, a.offset, a.partition)
        r = diff_volumes(vol, other)
        print(json.dumps(r, indent=2, default=str) if a.json else fmt_diff(r))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FatError, qcow2reader.Qcow2Error) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
