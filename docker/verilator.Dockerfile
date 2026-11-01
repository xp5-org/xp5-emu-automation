# syntax=docker/dockerfile:1
# Verilator built from github.com/verilator/verilator (VERILATOR_REF, default
# `stable` = latest release; use `master` for the development head). Installed
# under /usr/local. Build deps are in pkgs/verilator.apt (final image; g++/make
# are needed there anyway since Verilator compiles its generated C++).
FROM base AS build
ARG APT_PROXY=
ARG VERILATOR_REF=stable
RUN --mount=type=bind,source=docker,target=/docker \
    /docker/apt-install.sh /docker/pkgs/verilator.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp; \
    git clone --branch "$VERILATOR_REF" https://github.com/verilator/verilator.git && \
    cd verilator && autoconf && \
    ./configure --prefix=/usr/local && \
    make -j3 && make install && \
    cd /tmp && rm -rf verilator && \
    mkdir /out && \
    find /usr/local -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
