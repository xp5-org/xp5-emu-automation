# syntax=docker/dockerfile:1
# C64 tooling: latest VICE (x64 + c1541, GTK3), the xa assembler and OpenCBM.
# cc65 itself comes from apt (pkgs/vice.apt). Everything lands under /usr, so
# each step records what its `make install` wrote (files newer than a stamp)
# and copies just those into /out.
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    sh /docker/apt-install.sh /docker/pkgs/vice.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp; \
    # xa assembler
    wget -O /tmp/xa.tar.gz https://www.floodgap.com/retrotech/xa/dists/xa-2.4.1.tar.gz && \
    tar -xzf /tmp/xa.tar.gz -C /tmp && \
    make -C /tmp/xa-2.4.1 && \
    cp /tmp/xa-2.4.1/xa /usr/local/bin/ && \
    rm -rf /tmp/xa-2.4.1 /tmp/xa.tar.gz; \
    # latest VICE release
    VICE_URL=$(python3 -c "import urllib.request, re; html = urllib.request.urlopen('https://vice-emu.sourceforge.io/index.html').read().decode('utf-8'); print(re.search(r'href=\"(https://sourceforge.net/projects/vice-emu/files/releases/vice-[\d\.]+\.tar\.gz/download)\"', html).group(1))") && \
    curl -L -o /tmp/vice.tar.gz "$VICE_URL" && \
    tar -xzf /tmp/vice.tar.gz -C /tmp && \
    cd /tmp/vice-* && \
    CFLAGS="-O3" CXXFLAGS="-O3" ./configure \
        --prefix=/usr \
        --infodir=/usr/share/info \
        --mandir=/usr/share/man \
        --enable-html-docs \
        --enable-native-gtk3ui \
        --enable-x64 \
        --disable-parsid \
        --enable-hardsid \
        --with-resid \
        --enable-arch=no \
        --enable-x64-image \
        --with-png \
        --with-jpeg \
        --enable-lame \
        --disable-ffmpeg \
        --enable-rs232 \
        --enable-ipv6 \
        --with-oss && \
    make -j"$(nproc)" && \
    make install && \
    mkdir -p /usr/share/vice && \
    cp -a data/* /usr/share/vice/ && \
    cd /tmp && rm -rf /tmp/vice*; \
    # OpenCBM
    git clone --depth 1 https://github.com/OpenCBM/OpenCBM.git /tmp/OpenCBM && \
    cd /tmp/OpenCBM && \
    make -f LINUX/Makefile USB_LIBS="$(pkg-config --libs libusb-1.0)" USB_CFLAGS="$(pkg-config --cflags libusb-1.0)" opencbm && \
    make -f LINUX/Makefile install && \
    cd /tmp && rm -rf /tmp/OpenCBM; \
    # collect everything installed above
    mkdir /out && \
    find /usr /etc -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
