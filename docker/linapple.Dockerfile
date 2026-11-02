# syntax=docker/dockerfile:1
# linapple-pie (Apple ][/][+/IIe emulator) built from source into /usr. Its
# Apple firmware ROMs (copyrighted) ship baked into inc/resource.h in this
# fork, so unlike Hatari's TOS image there is no separate ROM file to fetch.
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    sh /docker/apt-install.sh /docker/pkgs/linapple.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp; \
    git clone --depth 1 https://github.com/linappleii/linapple.git /tmp/linapple && \
    cd /tmp/linapple && \
    cmake -B build -DBUILD_TESTING=OFF -DFRONTEND=sdl2 -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr && \
    cmake --build build -j"$(nproc)" && \
    cmake --install build && \
    cd /tmp && rm -rf /tmp/linapple && \
    mkdir /out && \
    find /usr /etc -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
