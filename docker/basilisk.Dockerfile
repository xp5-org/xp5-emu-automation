# syntax=docker/dockerfile:1
# Basilisk II (68k Mac emulator), built from source into /usr/local.
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    /docker/apt-install.sh /docker/pkgs/basilisk.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    git clone --depth 1 https://github.com/kanjitalk755/macemu.git /tmp/macemu; \
    cd /tmp/macemu/BasiliskII/src/Unix; \
    NO_CONFIGURE=1 ./autogen.sh; \
    ./configure --with-esd=no; \
    make -j"$(nproc)"; \
    make install DESTDIR=/out; \
    /out/usr/local/bin/BasiliskII --help > /dev/null 2>&1 || true

FROM scratch
COPY --from=build /out/ /
