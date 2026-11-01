# syntax=docker/dockerfile:1
# KiCad built from git (KICAD_REF, default master = latest) plus the stock
# symbol/footprint libraries from their own repos. Installed with prefix /usr so
# the libraries land in /usr/share/kicad/symbols, where
# sourcedir/eda_mixed_signal/mk_pilot_sch.py reads them (this replaces the
# distro kicad/kicad-symbols packages). KiCad wants wxWidgets >= 3.2.8, newer
# than Ubuntu ships, so wx 3.2.x is built first into /usr/local. Build/runtime
# libs are in pkgs/kicad.apt (final image); 3D models are left out (huge).
FROM base AS build
ARG APT_PROXY=
ARG KICAD_REF=master
ARG WX_VERSION=3.2.8
RUN --mount=type=bind,source=docker,target=/docker \
    /docker/apt-install.sh /docker/pkgs/kicad.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp; \
    # wxWidgets
    curl -fsSL "https://github.com/wxWidgets/wxWidgets/releases/download/v${WX_VERSION}/wxWidgets-${WX_VERSION}.tar.bz2" | tar -xj && \
    cd wxWidgets-${WX_VERSION} && \
    ./configure --prefix=/usr/local --with-gtk=3 --with-opengl --enable-webview \
        --enable-unicode --enable-graphics_ctx --enable-mediactrl --enable-display \
        --disable-tests --disable-demos --disable-debug_flag && \
    make -j"$(nproc)" && make install && ldconfig && \
    cd /tmp && rm -rf wxWidgets-${WX_VERSION}; \
    # KiCad
    git clone --depth 1 --branch "$KICAD_REF" https://gitlab.com/kicad/code/kicad.git && \
    cmake -S kicad -B kicad/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr -DKICAD_BUILD_I18N=OFF -DKICAD_BUILD_QA_TESTS=OFF \
        -DKICAD_SCRIPTING_WXPYTHON=OFF && \
    cmake --build kicad/build && cmake --install kicad/build && \
    git -C kicad rev-parse HEAD > /usr/share/kicad/GIT_REVISION && \
    rm -rf kicad; \
    # stock libraries
    for lib in symbols footprints; do \
        git clone --depth 1 "https://gitlab.com/kicad/libraries/kicad-$lib.git" && \
        cmake -S kicad-$lib -B kicad-$lib/build -DCMAKE_INSTALL_PREFIX=/usr && \
        cmake --install kicad-$lib/build && rm -rf kicad-$lib; \
    done; \
    mkdir /out && \
    find /usr -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
