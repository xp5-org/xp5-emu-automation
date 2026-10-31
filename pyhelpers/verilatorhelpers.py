#!/usr/bin/env python3
"""Verilator projects, binaries, monitors and stats, for the runner and the manager.
  - verilator.json   per-project manifest (beside the RTL): what the project is, how it builds, which binaries it
                     produces (with their default args), which live monitors can watch them, where they publish stats.
  - discover()       every manifest under sourcedir/ -> VerilatorProject
  - VerilatorProject build(), status() (is the binary stale vs the RTL?), launch(), stats_path()
  - StatsReader      reads the JSON snapshot a testbench writes with /testsrc/verilator_common/vstats.hpp
  - CLI              python3 verilatorhelpers.py list | status [project] | stats <path|project/binary> | build <project>

Manifest (all paths relative to the manifest's directory; "{name}", "{binary}", "{live}" are substituted):
    {
      "name": "verilator_vga",            required
      "top": "vga_hello",                 top module
      "rtl": ["src/vga_hello.v", ...],    sources; newest mtime is compared with each binary for stale detection
      "build": "./build.sh",              build command (run with cwd = project dir)
      "binaries": {
        "hello": {"path": "obj_dir/Vvga_hello", "args": ["--live", "{live}"],
                  "live": "/dev/shm/vgafb-{name}", "description": "..."}
      },
      "monitors": {"fb": {"kind": "fb", "live": "/dev/shm/vgafb-{name}"}},
      "stats_dir": "/dev/shm"             stats file is <stats_dir>/vstats-<project>-<binary>.json
    }
"""

import json
import os
import re
import subprocess
import sys
import time

HELPERDIR = os.path.dirname(os.path.abspath(__file__))
if HELPERDIR not in sys.path:
    sys.path.insert(0, HELPERDIR)

SOURCE_ROOT = "/testsrc/sourcedir"
MANIFEST = "verilator.json"
DEFAULT_STATS_DIR = "/dev/shm"
STATS_SCHEMA = 1
# a running testbench flushes at least every 0.25 s; past this the snapshot is considered abandoned
STALE_AFTER = 5.0

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ManifestError(Exception):
    pass


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def stats_path_for(project, binary, stats_dir=DEFAULT_STATS_DIR):
    return os.path.join(stats_dir, f"vstats-{project}-{binary}.json")


class StatsReader(object):
    """Reader of one testbench stats snapshot (vstats.hpp). read() never raises: unreadable -> None."""

    def __init__(self, path):
        self.path = path

    def read(self, now=None):
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != STATS_SCHEMA:
            return None
        now = time.time() if now is None else now
        data["age"] = round(max(0.0, now - float(data.get("updated") or 0)), 3)
        # state says "running" until the testbench finishes; a dead pid or old snapshot means it did not
        if data.get("state") == "running" and (not _pid_alive(data.get("pid")) or data["age"] > STALE_AFTER):
            data["state"] = "lost"
        data["alive"] = data.get("state") == "running"
        return data

    def counter(self, key, default=0):
        data = self.read()
        return default if data is None else data.get("counters", {}).get(key, default)

    def wait_for(self, predicate, timeout=10.0, poll=0.05):
        """Poll until predicate(stats_dict) is true -> (True, stats) or (False, last_stats_or_None)."""
        deadline = time.time() + timeout
        data = None
        while True:
            data = self.read()
            if data is not None and predicate(data):
                return True, data
            if time.time() >= deadline:
                return False, data
            time.sleep(poll)


class VerilatorProject(object):
    def __init__(self, directory, manifest):
        self.dir = os.path.abspath(directory)
        self.manifest = manifest
        self.name = manifest["name"]
        self.top = manifest.get("top", "")
        self.rtl = list(manifest.get("rtl") or [])
        self.build_cmd = manifest.get("build")
        self.binaries = dict(manifest.get("binaries") or {})
        self.monitors = dict(manifest.get("monitors") or {})
        self.stats_dir = manifest.get("stats_dir", DEFAULT_STATS_DIR)

    def _sub(self, text, binary=None):
        live = None
        if binary is not None:
            live = (self.binaries.get(binary) or {}).get("live")
        out = str(text).replace("{name}", self.name)
        if binary is not None:
            out = out.replace("{binary}", binary)
        if live:
            out = out.replace("{live}", live.replace("{name}", self.name).replace("{binary}", binary or ""))
        return out

    def binary_path(self, binary):
        spec = self.binaries.get(binary)
        if spec is None:
            raise KeyError(f"{self.name}: no binary '{binary}' (have: {', '.join(self.binaries) or 'none'})")
        return os.path.join(self.dir, spec["path"])

    def live_path(self, binary):
        live = (self.binaries.get(binary) or {}).get("live")
        return self._sub(live, binary) if live else None

    def stats_path(self, binary):
        return stats_path_for(self.name, binary, self.stats_dir)

    def reader(self, binary):
        return StatsReader(self.stats_path(binary))

    def argv(self, binary, extra=()):
        spec = self.binaries[binary]
        args = [self._sub(a, binary) for a in spec.get("args", [])]
        return [self.binary_path(binary)] + args + ["--stats", self.stats_path(binary)] + list(extra)

    def _rtl_mtime(self):
        newest = 0.0
        for rel in self.rtl:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(self.dir, rel)))
            except OSError:
                pass
        return newest

    def status(self):
        """Per-binary build state and last-run stats: the manager's project overview."""
        rtl = self._rtl_mtime()
        bins = {}
        for key, spec in self.binaries.items():
            path = self.binary_path(key)
            exists = os.path.exists(path)
            mtime = os.path.getmtime(path) if exists else None
            stats = self.reader(key).read()
            bins[key] = {
                "path": path,
                "built": exists,
                "stale": bool(exists and rtl and mtime < rtl),
                "mtime": mtime,
                "description": spec.get("description", ""),
                "live": self.live_path(key),
                "stats_path": self.stats_path(key),
                "stats": stats,
            }
        return {
            "name": self.name,
            "dir": self.dir,
            "top": self.top,
            "rtl": self.rtl,
            "monitors": self.monitors,
            "binaries": bins,
        }

    def build(self, timeout=600):
        """Run the manifest's build command -> (ok, tail_of_output)."""
        if not self.build_cmd:
            return False, f"{self.name}: manifest has no build command"
        try:
            res = subprocess.run(self.build_cmd, shell=True, cwd=self.dir, timeout=timeout,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired:
            return False, f"{self.name}: build timed out after {timeout}s"
        out = res.stdout.decode("utf-8", "replace")
        return res.returncode == 0, out[-2000:]

    def launch(self, binary, extra=(), name=None, seconds=0):
        """Start a binary with the stats path wired in -> ((ok, msg), VerilatorLiveInstance)."""
        from verilatorvgahelpers import VerilatorLiveInstance
        try:
            os.remove(self.stats_path(binary))  # never read the previous run's snapshot as this run's
        except OSError:
            pass
        # manifest args already carry --live; the instance adds --stats itself
        args = [self._sub(a, binary) for a in self.binaries[binary].get("args", [])]
        inst = VerilatorLiveInstance(name or f"{self.name}-{binary}", binary=self.binary_path(binary),
                                     cwd=self.dir, seconds=seconds,
                                     stats_path=self.stats_path(binary), extra_args=args + list(extra))
        return inst.start(), inst


def load_manifest(directory):
    path = os.path.join(directory, MANIFEST)
    try:
        with open(path) as f:
            manifest = json.load(f)
    except OSError as exc:
        raise ManifestError(f"{path}: {exc}")
    except ValueError as exc:
        raise ManifestError(f"{path}: invalid JSON: {exc}")
    if not isinstance(manifest, dict):
        raise ManifestError(f"{path}: top level must be an object")
    name = manifest.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ManifestError(f"{path}: 'name' must match {_NAME_RE.pattern}")
    binaries = manifest.get("binaries") or {}
    if not isinstance(binaries, dict):
        raise ManifestError(f"{path}: 'binaries' must be an object")
    for key, spec in binaries.items():
        if not _NAME_RE.match(key):
            raise ManifestError(f"{path}: binary name '{key}' must match {_NAME_RE.pattern}")
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
            raise ManifestError(f"{path}: binary '{key}' needs a string 'path'")
        if not isinstance(spec.get("args", []), list):
            raise ManifestError(f"{path}: binary '{key}' 'args' must be a list")
    return manifest


def discover(root=SOURCE_ROOT):
    """{name: VerilatorProject} for every <root>/<dir>/verilator.json. Broken manifests are skipped with a note on stderr."""
    found = {}
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return found
    for ent in entries:
        d = os.path.join(root, ent)
        if not os.path.isfile(os.path.join(d, MANIFEST)):
            continue
        try:
            proj = VerilatorProject(d, load_manifest(d))
        except ManifestError as exc:
            print(f"verilatorhelpers: skipping {exc}", file=sys.stderr)
            continue
        if proj.name in found:
            print(f"verilatorhelpers: duplicate project name '{proj.name}' in {d}, skipping", file=sys.stderr)
            continue
        found[proj.name] = proj
    return found


def overview(root=SOURCE_ROOT):
    """JSON-ready status of every project: the payload for the manager."""
    return [p.status() for p in discover(root).values()]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "list"
    projects = discover()
    if cmd == "list":
        print(json.dumps(overview(), indent=2))
        return 0
    if cmd == "status" and argv:
        proj = projects.get(argv[0])
        if proj is None:
            print(f"unknown project {argv[0]}", file=sys.stderr)
            return 2
        print(json.dumps(proj.status(), indent=2))
        return 0
    if cmd == "status":
        print(json.dumps(overview(), indent=2))
        return 0
    if cmd == "stats" and argv:
        target = argv[0]
        path = target
        if "/" in target and not os.path.isabs(target) and target.split("/")[0] in projects:
            pname, bname = target.split("/", 1)
            path = projects[pname].stats_path(bname)
        data = StatsReader(path).read()
        print(json.dumps(data, indent=2))
        return 0 if data else 1
    if cmd == "build" and argv:
        proj = projects.get(argv[0])
        if proj is None:
            print(f"unknown project {argv[0]}", file=sys.stderr)
            return 2
        ok, out = proj.build()
        print(out)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
