# syntax=docker/dockerfile:1
# 86Box AppImage + ROMs, extracted (no FUSE in containers) to /opt/86box and
# patchelf'd; see 86box.sh for the launcher and why it must not call the loader
# explicitly. The 86Box *build deps* (for testlists that build a patched fork)
# are in pkgs/86box.apt and get installed by the final image.
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    sh /docker/apt-install.sh /docker/pkgs/86box.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /opt
RUN set -eux; \
    curl -sL -o /tmp/86box.AppImage \
        https://github.com/86Box/86Box/releases/download/v6.0/86Box-Linux-x86_64-b9001.AppImage && \
    chmod +x /tmp/86box.AppImage && \
    cd /tmp && ./86box.AppImage --appimage-extract > /dev/null && \
    mv /tmp/squashfs-root /opt/86box && \
    rm -f /tmp/86box.AppImage && \
    git clone --depth 1 https://github.com/86Box/roms.git /opt/86box/roms && \
    rm -rf /opt/86box/roms/.git && \
    # fix appimage broken path
    patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 \
             --set-rpath '/opt/86box/lib/x86_64-linux-gnu:/opt/86box/usr/lib:/opt/86box/usr/lib/x86_64-linux-gnu' \
             /opt/86box/usr/local/bin/86Box && \
    chmod -R a+rX /opt/86box && \
    mkdir -p /out/opt && cp -a /opt/86box /out/opt/

FROM scratch
COPY --from=build /out/ /
COPY --chmod=755 docker/scripts/86box.sh /usr/local/bin/86box
