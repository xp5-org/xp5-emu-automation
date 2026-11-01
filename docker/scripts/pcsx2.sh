#!/bin/sh
# PCSX2 launcher. Upstream ships only an AppImage and FUSE isn't available in
# a container, so it's extracted to /opt/pcsx2 (see the Dockerfile's pcsx2
# component). Verified against a real v2.8.2 AppImage extract: unlike 86Box's,
# this binary's interpreter/rpath already resolve correctly as extracted (see
# `readelf -l`/`ldd` on usr/bin/pcsx2-qt) -- no patchelf needed. What IS
# required is QT_PLUGIN_PATH pointed at the AppImage's own plugins/ directory
# (not a system Qt6 path); without it Qt can't find the xcb platform plugin
# and refuses to start at all.
A=/opt/pcsx2
export QT_PLUGIN_PATH="$A/usr/plugins"
export LD_LIBRARY_PATH="$A/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
exec "$A/usr/bin/pcsx2-qt" "$@"
