# syntax=docker/dockerfile:1
# QEMU built from source. Result is an overlay tree (/usr/local/...) that the
# final image COPYs onto /. Needs the shared base: `FROM base` is a bake named
# context; standalone use: --build-context base=docker-image://xp5-rdp-testbase
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    /docker/apt-install.sh /docker/pkgs/qemu.apt && \
    (apt-get build-dep -y qemu-system-misc ninja || true) && \
    rm -rf /var/lib/apt/lists/*

# QEMU_VERSION=latest resolves to the newest vX.Y.Z tag at build time
ARG QEMU_VERSION=latest
WORKDIR /tmp
RUN set -eux; \
    if [ "$QEMU_VERSION" = "latest" ]; then \
        QEMU_VERSION="$(git ls-remote --tags --refs https://github.com/qemu/qemu.git \
            | sed -n 's|.*refs/tags/\(v[0-9]*\.[0-9]*\.[0-9]*\)$|\1|p' \
            | sort -V | tail -1)"; \
    fi; \
    echo "Building QEMU $QEMU_VERSION"; \
    git clone --depth 1 --branch "$QEMU_VERSION" \
        https://github.com/qemu/qemu.git /tmp/qemu; \
    cd /tmp/qemu; \
    ./configure \
        --target-list=i386-softmmu,m68k-softmmu,ppc-softmmu,sparc-softmmu,arm-softmmu,aarch64-softmmu \
        --enable-gtk --enable-sdl --enable-slirp --enable-tools --disable-docs; \
    make -j"$(nproc)"; \
    make install DESTDIR=/out; \
    /out/usr/local/bin/qemu-system-i386 --version | head -1

FROM scratch
COPY --from=build /out/ /
