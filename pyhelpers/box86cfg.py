"""Read an 86Box VM's <vm_dir>/86box.cfg and describe the machine it was started with.
  describe(vm_dir) -> ordered {key: value}
  report(vm_dir)   -> text block: human header, then one `REPORT_TAG: key=value` line per param (reporthelper.extract_tags files them as report tags)
CLI:  python3 box86cfg.py report <vm_dir> [--label TEXT]
Never fails the calling step: missing/unreadable cfg prints nothing useful, exit 0.
"""

import os
import re
import sys

# cfg key -> (tag key, section); first match wins, None = any section
_WANTED = [
    ("machine", "machine"),
    ("cpu_family", "cpu"),
    ("cpu_speed", "cpu_speed_hz"),
    ("cpu_multi", "cpu_multiplier"),
    ("cpu_use_dynarec", "dynarec"),
    ("fpu_type", "fpu"),
    ("mem_size", "mem_kb"),
    ("gfxcard", "video"),
    ("sndcard", "sound"),
    ("net_card", "network"),
    ("hdd_controller", "hdd_controller"),
    ("vid_renderer", "renderer"),
    ("emu_build_dynarec_type", "dynarec_type"),
]
_PORT_RE = re.compile(r"^(serial\d+_device|lpt\d+_device)$")


def read_cfg(path):
    """86box.cfg -> {section: {key: value}} (flat INI, no interpolation)."""
    out, sec = {}, ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith((";", "#")):
                continue
            if line.startswith("[") and line.endswith("]"):
                sec = line[1:-1]
                out.setdefault(sec, {})
            elif "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(sec, {})[k.strip()] = v.strip()
    return out


def _flat(cfg):
    flat = {}
    for sect in cfg.values():
        for k, v in sect.items():
            flat.setdefault(k, v)
    return flat


def describe(vm_dir):
    """Ordered {tag_key: value} for the machine in vm_dir/86box.cfg."""
    cfg_path = os.path.join(vm_dir, "86box.cfg")
    flat = _flat(read_cfg(cfg_path))
    params = {}
    for cfg_key, tag in _WANTED:
        if flat.get(cfg_key) not in (None, ""):
            params[tag] = flat[cfg_key]
    if params.get("cpu_speed_hz", "").isdigit():
        params["cpu_mhz"] = f"{int(params['cpu_speed_hz']) / 1e6:g}"
    for k in sorted(flat):
        if _PORT_RE.match(k) and flat[k] not in ("", "none"):
            params[k.replace("_device", "")] = flat[k]
    return params


def _headline(p):
    bits = []
    if p.get("cpu_mhz") or p.get("cpu"):
        bits.append(f"{p.get('cpu_mhz', '?')} MHz {p.get('cpu', '?')}")
    if p.get("machine"):
        bits.append(f"machine {p['machine']}")
    if p.get("mem_kb", "").isdigit():
        bits.append(f"{int(p['mem_kb']) // 1024} MB RAM")
    return " · ".join(bits)


def report(vm_dir, label=None):
    try:
        p = describe(vm_dir)
    except OSError as e:
        return f"86Box cfg unreadable ({vm_dir}/86box.cfg): {e}"
    lines = []
    if label:
        lines.append(label)
    lines.append(_headline(p))
    lines += [f"{k} : {v}" for k, v in p.items() if k.startswith(("serial", "lpt"))]
    lines.append("=" * 84)
    lines += [f"REPORT_TAG: {k}={v}" for k, v in p.items()]
    return "\n".join(l for l in lines if l)


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "report":
        label = args[args.index("--label") + 1] if "--label" in args[2:] else None
        print(report(args[1], label))
    else:
        print("usage: box86cfg.py report <vm_dir> [--label TEXT]")
    sys.exit(0)
