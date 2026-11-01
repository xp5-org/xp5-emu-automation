# syntax=docker/dockerfile:1
# EDA toolchain built from each project's git head, installed under /usr/local:
# Icarus Verilog, Yosys (with submodules), ngspice (XSPICE + CIDER, OpenMP) and
# GTKWave (meson/GTK3). sox and the system-python libs the eda scripts need stay
# in pkgs/eda.apt together with these build/runtime libs (final image).
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    /docker/apt-install.sh /docker/pkgs/eda.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp

# Icarus Verilog
RUN set -eux; \
    git clone --depth 1 https://github.com/steveicarus/iverilog.git && \
    cd iverilog && sh autoconf.sh && ./configure --prefix=/usr/local && \
    make -j"$(nproc)" && make install && cd /tmp && rm -rf iverilog

# Yosys
RUN set -eux; \
    git clone --depth 1 --recurse-submodules --shallow-submodules https://github.com/YosysHQ/yosys.git && \
    cd yosys && cmake --preset release -DCMAKE_INSTALL_PREFIX=/usr/local && \
    cmake --build build-rel --parallel 4 && cmake --install build-rel && \
    cd /tmp && rm -rf yosys

# ngspice
RUN set -eux; \
    git clone --depth 1 https://git.code.sf.net/p/ngspice/ngspice && \
    cd ngspice && ./autogen.sh && mkdir release && cd release && \
    ../configure --prefix=/usr/local --with-x --with-readline=yes \
        --enable-xspice --enable-cider --enable-openmp --disable-debug && \
    make -j"$(nproc)" && make install && cd /tmp && rm -rf ngspice

# GTKWave
RUN set -eux; \
    git clone --depth 1 https://github.com/gtkwave/gtkwave.git && \
    meson setup gtkwave/build gtkwave --prefix=/usr/local && \
    meson compile -C gtkwave/build && meson install -C gtkwave/build && rm -rf gtkwave && \
    ldconfig

RUN set -eux; \
    mkdir /out && \
    find /usr/local -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
