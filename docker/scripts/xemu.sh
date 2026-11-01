#!/bin/sh
# xemu launcher. Upstream ships only an AppImage and FUSE isn't available in
# a container, so it's extracted to /opt/xemu (see the Dockerfile's xemu
# block). Verified against a real v0.8.136 AppImage extract: like PCSX2's,
# this binary's interpreter/rpath already resolve correctly as extracted (see
# `ldd` on usr/bin/xemu) -- no patchelf needed. Per-instance config directory
# (XDG_DATA_HOME) is set by pyhelpers/xemuhelpers.py's XemuInstance, not here.
A=/opt/xemu
export LD_LIBRARY_PATH="$A/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
exec "$A/usr/bin/xemu" "$@"
