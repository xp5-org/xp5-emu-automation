# syntax=docker/dockerfile:1
# xemu (original Xbox emulator, itself an embedded QEMU 10.2.0). Like PCSX2's
# AppImage, interpreter/rpath already resolve as extracted -- no patchelf.
#
# Its monitor (-monitor tcp:...) answers `info status`/`sendkey` but does NOT
# support `screendump` (xemu renders through its own `-display xemu` SDL/OpenGL
# frontend), and `-vnc` is rejected at xemu's argv layer. So
# pyhelpers/xemuhelpers.py's XemuInstance is an xdotool/`import`/tesseract class
# (same shape as Box86Instance/Pcsx2Instance), not a QemuInstance subclass, with
# the monitor as a secondary opt-in channel for status/input only.
FROM base AS build
WORKDIR /opt
RUN set -eux; \
    curl -sL -o /tmp/xemu.AppImage \
        https://github.com/xemu-project/xemu/releases/download/v0.8.136/xemu-0.8.136-x86_64.AppImage && \
    chmod +x /tmp/xemu.AppImage && \
    cd /tmp && ./xemu.AppImage --appimage-extract > /dev/null && \
    mv /tmp/squashfs-root /opt/xemu && \
    rm -f /tmp/xemu.AppImage && \
    chmod -R a+rX /opt/xemu && \
    mkdir -p /out/opt && cp -a /opt/xemu /out/opt/

FROM scratch
COPY --from=build /out/ /
COPY --chmod=755 docker/scripts/xemu.sh /usr/local/bin/xemu
