# syntax=docker/dockerfile:1
# Retro68 (68k Mac cross toolchain); installs to /opt/Retro68/toolchain.
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    /docker/apt-install.sh /docker/pkgs/retro68.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /opt
RUN set -eux; \
    git clone --recursive https://github.com/autc04/Retro68.git /opt/Retro68 && \
    mkdir -p /opt/Retro68-build && \
    cd /opt/Retro68-build && \
    /opt/Retro68/build-toolchain.bash --no-ppc --no-carbon \
        --prefix=/opt/Retro68/toolchain --clean-after-build && \
    rm -rf /opt/Retro68-build; \
    mkdir -p /out/opt && cp -a /opt/Retro68 /out/opt/

FROM scratch
COPY --from=build /out/ /
