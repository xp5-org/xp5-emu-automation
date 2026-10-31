"""Which X display an emulator window opens on: the xfce/RDP session app.py runs in (os.environ["DISPLAY"], else the live xrdp session found here)."""

import os
import re
import subprocess


def resolve_display(explicit=None):
    """Display for emulator windows: explicit, else ambient DISPLAY, else the running xrdp session."""
    if explicit:
        return explicit
    if os.environ.get("DISPLAY"):
        return os.environ["DISPLAY"]
    return find_rdp_display() or ":20"


def find_rdp_display():
    """Live xorgxrdp session display, e.g. ':20' (Xorg with an xrdp config, else highest-numbered live Xorg); None if none."""
    try:
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    except Exception:
        return None

    candidates = []
    for line in ps.splitlines():
        if "Xorg" not in line:
            continue
        m = re.search(r"Xorg\s+(:\d+)", line)
        if not m:
            continue
        disp = m.group(1)
        is_rdp = "xrdp" in line or "xorg.conf" in line
        candidates.append((is_rdp, int(disp[1:]), disp))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]
