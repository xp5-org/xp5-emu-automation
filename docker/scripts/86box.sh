#!/bin/sh
# 86Box launcher. Upstream ships only an AppImage and FUSE isn't available in a
# container, so it's extracted to /opt/86box and the binary is patchelf'd to use
# the system loader plus a DT_RPATH pointing at the bundled libs.
#
# Do NOT reintroduce an explicit `ld-linux-x86-64.so.2 .../86Box` invocation
# here: 86Box's VM Manager spawns VMs by re-executing /proc/self/exe, which
# under that form resolves to the loader, so it ends up running
# `ld-linux-x86-64.so.2 --vmpath ...` and dies with "unrecognized option".
#
# -R defaults the ROM path but comes first, so a caller's own -R still wins.
A=/opt/86box
export QT_PLUGIN_PATH="$A/usr/lib/x86_64-linux-gnu/qt5/plugins"
# patchelf sets an absolute interpreter + RPATH on the binary itself (see the
# Dockerfile's 86Box install step), but RPATH/RUNPATH on the main executable
# doesn't propagate to the *transitive* deps of its bundled libs (Qt5's own
# libicui18n/libmd4c/etc. carry no RPATH of their own) -- only
# LD_LIBRARY_PATH is honored for those, hence both.
export LD_LIBRARY_PATH="$A/lib/x86_64-linux-gnu:$A/usr/lib:$A/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
exec "$A/usr/local/bin/86Box" -R "$A/roms" "$@"
