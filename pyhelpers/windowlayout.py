"""Round-robin screen tiling for emulator GUI windows on a shared X display (displayhelpers.resolve_display()).
Maps a tile slot to a rectangle; callers claim slots by launch order (dispatch_functions._next_window_slot, counted in the run's shared context).
"""

import os
import subprocess

DEFAULT_SCREEN_SIZE = (1920, 1080)

# (x, y) fractional top-left corners, round-robined per window.
_GRID = [(0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.5, 0.5)]


def screen_size(display=None):
    """(width, height) of the xRDP/Xvfb session at `display`; default only if the query fails."""
    try:
        env = dict(os.environ, DISPLAY=display) if display else None
        out = subprocess.run(["xdotool", "getdisplaygeometry"],
                             capture_output=True, text=True, env=env,
                             timeout=5).stdout.split()
        return int(out[0]), int(out[1])
    except Exception:
        return DEFAULT_SCREEN_SIZE


def window_geometry(wid, display=None):
    """Best-effort (width, height) of window `wid`, or None if unavailable."""
    rect = window_rect(wid, display)
    return rect[2:] if rect else None


def window_rect(wid, display=None):
    """Best-effort (x, y, width, height) of window `wid`, or None."""
    try:
        env = dict(os.environ, DISPLAY=display) if display else None
        out = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                             capture_output=True, text=True, env=env,
                             timeout=5).stdout
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"])
    except Exception:
        return None


def tile_position(slot, window_size=None, display=None):
    """Top-left (x, y) for tile `slot` on `display`. With window_size (w, h), clamped so the window's right/bottom edge stays inside the session and left/top is not negative."""
    screen_w, screen_h = screen_size(display)
    fx, fy = _GRID[(slot or 0) % len(_GRID)]
    x, y = int(screen_w * fx), int(screen_h * fy)
    if window_size:
        win_w, win_h = window_size
        x = max(0, min(x, screen_w - win_w))
        y = max(0, min(y, screen_h - win_h))
    return x, y
