"""Tests for verilatorhelpers (manifest, status, StatsReader), the vstats.hpp wire format and the liveview stats capability.
Run: python3 -m unittest /testsrc/pyhelpers/test_verilatorhelpers.py
The launch test needs the verilator_vga binary (sourcedir/verilator_vga/build.sh) and is skipped without it."""

import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import verilatorhelpers as vh

VGA_DIR = "/testsrc/sourcedir/verilator_vga"
VGA_BIN = os.path.join(VGA_DIR, "obj_dir", "Vvga_hello")


def write_manifest(d, **over):
    m = {"name": "demo", "top": "t", "rtl": ["a.v"], "build": "true",
         "binaries": {"b": {"path": "bin/b", "args": ["--live", "{live}"], "live": "/dev/shm/x-{name}"}}}
    m.update(over)
    with open(os.path.join(d, vh.MANIFEST), "w") as f:
        json.dump(m, f)


def snapshot(path, **over):
    d = {"schema": 1, "name": "n", "top": "t", "pid": os.getpid(), "state": "running",
         "started": time.time(), "updated": time.time(), "cycles": 1, "sim_time": 1,
         "counters": {"frames": 3}, "gauges": {}, "info": {}, "events": []}
    d.update(over)
    with open(path, "w") as f:
        json.dump(d, f)


class ManifestTests(unittest.TestCase):
    def test_discover_and_status(self):
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "demo")
            os.makedirs(os.path.join(p, "bin"))
            write_manifest(p)
            open(os.path.join(p, "a.v"), "w").close()
            proj = vh.discover(root)["demo"]
            st = proj.status()["binaries"]["b"]
            self.assertFalse(st["built"])
            self.assertEqual(st["live"], "/dev/shm/x-demo")
            self.assertEqual(proj.argv("b")[:3], [os.path.join(p, "bin/b"), "--live", "/dev/shm/x-demo"])
            self.assertIn("--stats", proj.argv("b"))
            # binary older than the RTL is stale
            b = os.path.join(p, "bin", "b")
            open(b, "w").close()
            os.utime(b, (1000, 1000))
            self.assertTrue(proj.status()["binaries"]["b"]["stale"])
            os.utime(b, None)
            self.assertFalse(proj.status()["binaries"]["b"]["stale"])
            ok, _ = proj.build()
            self.assertTrue(ok)

    def test_bad_manifests_are_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            for n, body in (("badjson", "{"), ("badname", '{"name": "a b"}'), ("badbin", '{"name":"x","binaries":{"b":{}}}')):
                os.makedirs(os.path.join(root, n))
                with open(os.path.join(root, n, vh.MANIFEST), "w") as f:
                    f.write(body)
            self.assertEqual(vh.discover(root), {})

    def test_unknown_binary(self):
        with tempfile.TemporaryDirectory() as root:
            write_manifest(root)
            with self.assertRaises(KeyError):
                vh.VerilatorProject(root, vh.load_manifest(root)).binary_path("nope")


class StatsReaderTests(unittest.TestCase):
    def test_states(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            self.assertIsNone(vh.StatsReader(path).read())
            snapshot(path)
            self.assertEqual(vh.StatsReader(path).read()["state"], "running")
            snapshot(path, updated=time.time() - 60)
            self.assertEqual(vh.StatsReader(path).read()["state"], "lost")
            snapshot(path, pid=2 ** 22 + 12345)
            self.assertEqual(vh.StatsReader(path).read()["state"], "lost")
            snapshot(path, state="done", pid=2 ** 22 + 12345)
            self.assertEqual(vh.StatsReader(path).read()["state"], "done")
            snapshot(path, schema=99)
            self.assertIsNone(vh.StatsReader(path).read())
            with open(path, "w") as f:
                f.write("{torn")
            self.assertIsNone(vh.StatsReader(path).read())


@unittest.skipUnless(os.path.exists(VGA_BIN), "verilator_vga not built")
class VgaTests(unittest.TestCase):
    def test_project_is_discovered(self):
        proj = vh.discover()["verilator_vga"]
        self.assertTrue(proj.status()["binaries"]["hello"]["built"])

    def test_oneshot_reports_done(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            subprocess.run([VGA_BIN, os.path.join(d, "f.ppm"), "--stats", path], cwd=VGA_DIR, check=True,
                           stdout=subprocess.DEVNULL)
            s = vh.StatsReader(path).read()
            self.assertEqual(s["state"], "done")
            self.assertEqual(s["counters"]["frames"], 1)
            self.assertGreater(s["cycles"], 0)

    def test_launch_live_publishes_running_stats(self):
        proj = vh.discover()["verilator_vga"]
        (ok, msg), inst = proj.launch("hello", name="t-vga")
        try:
            self.assertTrue(ok, msg)
            good, s = proj.reader("hello").wait_for(lambda d: d["counters"].get("frames", 0) >= 2, timeout=15)
            self.assertTrue(good, s)
            self.assertEqual(s["state"], "running")
            self.assertTrue(s["alive"])
            self.assertEqual(inst.stats()["pid"], inst.pid)
        finally:
            inst.stop()
        time.sleep(0.2)
        # SIGTERM stops the loop cleanly, so the final flush says done
        self.assertEqual(proj.reader("hello").read()["state"], "done")


class LiveviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # liveview pulls in qemuhelpers -> PIL/numpy/..., which only the runner image has: stub whatever is missing
        import importlib
        from unittest import mock
        liveview = None
        for _ in range(12):
            try:
                liveview = importlib.import_module("liveview")
                break
            except ModuleNotFoundError as exc:
                if not exc.name or exc.name.split(".")[0] in ("liveview", "qemuhelpers", "displayhelpers"):
                    raise unittest.SkipTest(f"liveview not importable here: {exc}")
                sys.modules[exc.name] = mock.MagicMock()
                sys.modules.pop("liveview", None)
            except Exception as exc:
                raise unittest.SkipTest(f"liveview not importable here: {exc}")
        if liveview is None:
            raise unittest.SkipTest("liveview not importable here")
        cls.lv = liveview


    def test_scan_finds_published_stats(self):
        path = vh.stats_path_for("lvtest", "bin")
        snapshot(path, name="lvtest")
        try:
            ents = [e for e in self.lv.list_instances() if e["kind"] == "verilator" and e["label"] == "lvtest"]
            self.assertEqual(len(ents), 1)
            self.assertEqual(ents[0]["capabilities"], ["stats"])
            ok, data = self.lv.stats(ents[0]["id"])
            self.assertTrue(ok)
            self.assertEqual(data["counters"]["frames"], 3)
        finally:
            os.remove(path)

    def test_stats_on_unknown_and_incapable(self):
        ok, msg = self.lv.stats("nope:1")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
