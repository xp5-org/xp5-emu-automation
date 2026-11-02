# syntax=docker/dockerfile:1
# DOSBox-X built from github.com/joncampbell123/dosbox-x (DOSBOXX_REF, default
# master = latest), SDL2 build, installed under /usr/local. Build/runtime libs are
# in pkgs/dosbox.apt (final image).
FROM base AS build
ARG APT_PROXY=
ARG DOSBOXX_REF=master
RUN --mount=type=bind,source=docker,target=/docker \
    sh /docker/apt-install.sh /docker/pkgs/dosbox.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp; \
    git clone --depth 1 --branch "$DOSBOXX_REF" https://github.com/joncampbell123/dosbox-x.git && \
    cd dosbox-x && ./autogen.sh && \
    ./configure --prefix=/usr/local --enable-core-inline --enable-sdl2 && \
    make -j"$(nproc)" && make install && \
    cd /tmp && rm -rf dosbox-x && \
    mkdir /out && \
    find /usr/local -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
