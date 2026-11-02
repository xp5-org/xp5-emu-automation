# syntax=docker/dockerfile:1
# Final "buffet" image: shared xrdp/xfce/pipewire base + whichever emulator/
# toolchain components were selected. Each heavy component (QEMU, 86Box,
# Retro68, ...) is built by its own docker/<name>.Dockerfile and arrives here as
# a pre-built overlay via a named build context `ctx-<name>`; unselected ones
# are bound to an empty image (docker/empty.Dockerfile), so the COPYs below are
# no-ops for them and their build never runs.
#
# Apt packages are NOT per-component layers: the union of the selected
# components' docker/pkgs/*.apt lists goes in ONE apt transaction, so packages
# shared between components (libslirp-dev, libsdl2-dev, ninja-build, ...) are
# installed once.
#
# Build via bake (it wires the contexts):
#   docker buildx bake -f docker/docker-bake.hcl                          # everything
#   COMPONENTS=86box docker buildx bake -f docker/docker-bake.hcl         # 86Box only, no QEMU
#   COMPONENTS=86box,simh,watcom docker buildx bake -f docker/docker-bake.hcl
#   docker buildx bake -f docker/docker-bake.hcl --print                  # show the resolved plan
# Components: qemu simh dosbox 86box retro68 verilator eda basilisk watcom
#             ps2sdk pcsx2 nxdk xemu vice hatari linapple webtools kicad gnuradio
# Defaults for unselected components; bake's named context ctx-<name> replaces
# the matching stage for the selected ones.
FROM scratch AS ctx-qemu
FROM scratch AS ctx-86box
FROM scratch AS ctx-retro68
FROM scratch AS ctx-basilisk
FROM scratch AS ctx-watcom
FROM scratch AS ctx-ps2sdk
FROM scratch AS ctx-pcsx2
FROM scratch AS ctx-nxdk
FROM scratch AS ctx-xemu
FROM scratch AS ctx-vice
FROM scratch AS ctx-hatari
FROM scratch AS ctx-linapple
FROM scratch AS ctx-webtools
FROM scratch AS ctx-kicad
FROM scratch AS ctx-gnuradio
FROM scratch AS ctx-simh
FROM scratch AS ctx-verilator
FROM scratch AS ctx-dosbox
FROM scratch AS ctx-eda

FROM base

# Internal caching apt proxy/mirror, e.g. APT_PROXY=http://host:3142. Set
# independently from Dockerfile.base's; a proxy baked into the base carries over.
ARG APT_PROXY=

# Comma-separated component list (or "all"); bake derives this from COMPONENTS.
ARG COMPONENTS=all
ENV COMPONENTS=${COMPONENTS}
RUN printf '%s\n' \
        '#!/bin/sh' \
        'case ",$COMPONENTS," in' \
        '  *,all,*|*,$1,*) exit 0 ;;' \
        '  *) exit 1 ;;' \
        'esac' \
        > /usr/local/bin/has-component && \
    chmod +x /usr/local/bin/has-component

# One apt layer for the union of the selected components' package lists.
RUN --mount=type=bind,source=docker,target=/docker \
    set -eux; \
    known="qemu simh dosbox 86box retro68 verilator eda basilisk watcom ps2sdk pcsx2 nxdk xemu vice hatari linapple webtools kicad gnuradio"; \
    for c in $(echo "$COMPONENTS" | tr ',' ' '); do \
        case " all $known " in *" $c "*) ;; *) echo "unknown component: $c (known: $known)" >&2; exit 1 ;; esac; \
    done; \
    lists=""; \
    for c in $known; do \
        if has-component "$c" && [ -f "/docker/pkgs/$c.apt" ]; then lists="$lists /docker/pkgs/$c.apt"; fi; \
    done; \
    sh /docker/apt-install.sh $lists; \
    # so testlists can build their own patched QEMU (e.g. sb16_isapc_echo)
    if has-component qemu; then apt-get build-dep -y qemu-system-misc ninja || true; fi; \
    # m68k-atari-mintelf cross-tools (Atari ST/MiNT sources). The PPA publishes
    # an empty plucky (25.04) suite, so pin it to noble; those binaries only
    # need libc6/libgmp10/libisl23/libmpc3/libmpfr6/libzstd1/zlib1g.
    if has-component hatari; then \
        add-apt-repository -y --no-update ppa:vriviere/mintelf && \
        sed -i 's/^Suites: .*/Suites: noble/' /etc/apt/sources.list.d/vriviere-ubuntu-mintelf-*.sources && \
        apt-get update && \
        apt-get install -y --no-install-recommends cross-mintelf-essential; \
    fi; \
    apt-get autoremove -y; \
    rm -rf /var/cache/apt /var/lib/apt/lists/*

# Pre-built component overlays (see header). Order is irrelevant.
COPY --from=ctx-qemu     / /
COPY --from=ctx-basilisk / /
COPY --from=ctx-86box    / /
COPY --from=ctx-pcsx2    / /
COPY --from=ctx-xemu     / /
COPY --from=ctx-retro68  / /
COPY --from=ctx-watcom   / /
COPY --from=ctx-ps2sdk   / /
COPY --from=ctx-nxdk     / /
COPY --from=ctx-vice     / /
COPY --from=ctx-hatari   / /
COPY --from=ctx-linapple / /
COPY --from=ctx-webtools / /
COPY --from=ctx-kicad    / /
COPY --from=ctx-gnuradio / /
COPY --from=ctx-simh     / /
COPY --from=ctx-verilator / /
COPY --from=ctx-dosbox   / /
COPY --from=ctx-eda      / /
RUN ldconfig

# Toolchain env, exported image-wide so a testlist's build.sh needs no
# per-project setup. Harmless when the component is absent (dirs just don't
# exist). wcl reads WATCOM/INCLUDE/PATH; nothing else on Linux consumes INCLUDE.
ENV WATCOM=/opt/watcom \
    INCLUDE=/opt/watcom/h \
    PS2DEV=/opt/ps2dev \
    PS2SDK=/opt/ps2dev/ps2sdk \
    GSKIT=/opt/ps2dev/gsKit \
    NXDK_DIR=/opt/nxdk
ENV PATH="/opt/watcom/binl64:/opt/ps2dev/bin:/opt/ps2dev/ee/bin:${PATH}"
