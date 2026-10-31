"""Snapshot timeline for a live run: QEMU internal snapshots (HMP savevm/loadvm), SIMH/Nova SAVE/RESTORE, 86Box remote-monitor snapshots.
Producers: dispatch_functions.test_qemu_snap / test_qemu_rollback, and runner RunSession (step "snap": True, CONFIG["snapshot_every_step"], timeline-strip rollback).
No-op unless CONFIG["snapshots"] is on.

Timeline = context["_snapshots"], dicts in the order taken:
    {"id": 3, "tag": "tl3", "label": "after C", "source": "step|checkbox|every", "instances": ["qemu1"], "taken_at": 1727..., "seconds": 0.6}
RunSession adds "cursor", "uid", "ordinal", "title".

An instance qualifies with save_snapshot/load_snapshot/delete_snapshot (QemuInstance, NovaSimhInstance, Box86Instance) and can_snapshot not False.
QEMU needs qcow2 for every writable disk; SIMH copies backing files alongside the .sav; 86Box snapshots live in process memory (lost on exit). Failures return "Error: ..." lines.
"""

import time

from qemuhelpers import hmp_error

KEY = "_snapshots"


def enabled(config):
    """CONFIG["snapshots"] global switch (bool from the editor or string in a hand-edited .py)."""
    v = (config or {}).get("snapshots", False)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def every_step(config):
    v = (config or {}).get("snapshot_every_step", False)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def instances(context, name=None):
    """[(key, instance)] for live snapshot-capable instances (each QEMU registered under its own name and "qemu1"; listed once)."""
    seen, out = {}, []
    for key, val in (context or {}).items():
        if not (hasattr(val, "save_snapshot") and hasattr(val, "load_snapshot")):
            continue
        if not getattr(val, "can_snapshot", True):
            continue
        proc = getattr(val, "process", None)
        try:
            if proc is None or proc.poll() is not None:
                continue
        except Exception:
            continue
        if id(val) in seen:
            if key == getattr(val, "name", None):
                out[seen[id(val)]] = (key, val)
            continue
        seen[id(val)] = len(out)
        out.append((key, val))
    if name:
        out = [(k, v) for k, v in out if k == name or getattr(v, "name", None) == name]
    return out


def timeline(context):
    return context.setdefault(KEY, [])


def find(context, ref):
    """Timeline entry by id (int or numeric string), tag, label, or the title of the step that took it."""
    tl = (context or {}).get(KEY) or []
    for key in ("id", "tag", "label", "title"):
        for s in reversed(tl):
            if s.get(key) is not None and str(s.get(key)) == str(ref):
                return s
    return None


def take(context, label="", source="step", name=None, tag=None):
    """savevm on every snapshot-capable instance (or just `name`) -> (entry | None, message)."""
    targets = instances(context, name)
    if not targets:
        return None, ("no running snapshot-capable (QEMU/SIMH/86Box with monitor_port) instance" + (f" named {name!r}" if name else "")
                      + " to snapshot")
    tl = timeline(context)
    sid = max((s["id"] for s in tl), default=0) + 1
    tag = tag or f"tl{sid}"
    started = time.time()
    done, log = [], []
    for key, inst in targets:
        err = hmp_error(inst.save_snapshot(tag))
        if err:
            # partial snapshot: undo the VMs that succeeded
            for k2, i2 in targets:
                if k2 in done:
                    try:
                        i2.delete_snapshot(tag)
                    except Exception:
                        pass
            return None, f"{key}: savevm {tag} failed: {err}"
        done.append(key)
        log.append(f"{key}: savevm {tag} ok")
    entry = {"id": sid, "tag": tag, "label": label or tag, "source": source,
             "instances": done, "taken_at": started,
             "seconds": round(time.time() - started, 2)}
    tl.append(entry)
    return entry, "\n".join(log) + f"\nsnapshot #{sid} {entry['label']!r} ({entry['seconds']}s)"


def restore(context, entry):
    """loadvm `entry` on every instance it was taken on. (ok, message)"""
    live = dict(instances(context))
    log = []
    for key in entry.get("instances") or []:
        inst = live.get(key)
        if inst is None:
            return False, f"{key}: not running any more -- can't roll it back"
        err = hmp_error(inst.load_snapshot(entry["tag"]))
        if err:
            return False, f"{key}: loadvm {entry['tag']} failed: {err}"
        log.append(f"{key}: loadvm {entry['tag']} ok")
    return True, "\n".join(log) + f"\nrolled back to snapshot #{entry['id']} {entry['label']!r}"


def drop(context, entries):
    """delvm and forget these entries."""
    live = dict(instances(context))
    tl = timeline(context)
    for entry in entries:
        for key in entry.get("instances") or []:
            inst = live.get(key)
            if inst is not None:
                try:
                    inst.delete_snapshot(entry["tag"])
                except Exception:
                    pass
        if entry in tl:
            tl.remove(entry)
