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
    # The final image is `FROM scratch` -- it has NO system libs of its own,
    # so anything upstream's AppImage packaging excluded on the assumption
    # that "every Linux host has this" (e.g. libopenal.so.1, pulled in only
    # as an optional/dlopen'd audio backend) is simply absent at runtime,
    # and 86Box aborts before its window appears. ldd lies about this at
    # build time too: it reports such libs "not found" against THIS
    # container's loader, same as it would on any host, so the gap was easy
    # to miss. Sweep every ELF file actually bundled, resolve each "not
    # found" dependency from this build stage's own ldconfig cache (it has
    # libopenal-dev's runtime lib from 86box.apt, see pkgs/86box.apt), vendor
    # it into the bundle, and fail the build instead of a host months later
    # if something still can't be resolved.
    missing=$(find /opt/86box -type f \( -name '*.so*' -o -perm -u+x \) -exec sh -c \
        'file -b "$1" | grep -q ELF' _ {} \; -print 2>/dev/null | \
        xargs -r ldd 2>/dev/null | grep 'not found' | awk '{print $1}' | sort -u) && \
    mkdir -p /opt/86box/lib/x86_64-linux-gnu && \
    for lib in $missing; do \
        src=$(ldconfig -p | awk -v l="$lib" '$1==l {print $NF; exit}'); \
        if [ -z "$src" ]; then \
            echo "FATAL: $lib not bundled in the AppImage and not available in the build image either" >&2; \
            exit 1; \
        fi; \
        echo "vendoring $lib from $src (excluded from upstream AppImage)"; \
        cp -n "$src" /opt/86box/lib/x86_64-linux-gnu/; \
    done && \
    chmod -R a+rX /opt/86box && \
    mkdir -p /out/opt && cp -a /opt/86box /out/opt/

FROM scratch
COPY --from=build /out/ /
COPY --chmod=755 docker/scripts/86box.sh /usr/local/bin/86box
