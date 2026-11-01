# syntax=docker/dockerfile:1
# PCSX2 (PS2 emulator). Unlike 86Box's AppImage, its interpreter/rpath already
# resolve as extracted (clean `readelf -l`/`ldd` on usr/bin/pcsx2-qt), so no
# patchelf. What IS required (and took several failed attempts to find) is
# pcsx2.sh's QT_PLUGIN_PATH pointing at usr/plugins specifically -- NOT a
# system Qt path -- or Qt refuses to start ("Could not find the Qt platform
# plugin"). Pinned to a dated tag.
FROM base AS build
WORKDIR /opt
RUN set -eux; \
    curl -sL -o /tmp/pcsx2.AppImage \
        https://github.com/PCSX2/pcsx2/releases/download/v2.8.2/pcsx2-v2.8.2-linux-appimage-x64-Qt.AppImage && \
    chmod +x /tmp/pcsx2.AppImage && \
    cd /tmp && ./pcsx2.AppImage --appimage-extract > /dev/null && \
    mv /tmp/squashfs-root /opt/pcsx2 && \
    rm -f /tmp/pcsx2.AppImage && \
    chmod -R a+rX /opt/pcsx2 && \
    mkdir -p /out/opt && cp -a /opt/pcsx2 /out/opt/

FROM scratch
COPY --from=build /out/ /
COPY --chmod=755 docker/scripts/pcsx2.sh /usr/local/bin/pcsx2
