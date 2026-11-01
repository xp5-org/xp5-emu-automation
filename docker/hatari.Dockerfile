# syntax=docker/dockerfile:1
# Hatari (Atari ST emulator), latest tag built from source into /usr. The
# m68k-atari-mintelf cross-compiler comes from a PPA and is installed by the
# final Dockerfile, since it is an apt package rather than a build product.
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    /docker/apt-install.sh /docker/pkgs/hatari.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp; \
    HATARI_TAG=$(python3 -c "import urllib.request, json; print(json.load(urllib.request.urlopen('https://framagit.org/api/v4/projects/hatari%2Fhatari/repository/tags?per_page=1'))[0]['name'])") && \
    echo "Building Hatari $HATARI_TAG" && \
    curl -L -o /tmp/hatari.tar.bz2 "https://framagit.org/hatari/hatari/-/archive/${HATARI_TAG}/hatari-${HATARI_TAG}.tar.bz2" && \
    mkdir -p /tmp/hatari && \
    tar -xjf /tmp/hatari.tar.bz2 -C /tmp/hatari --strip-components=1 && \
    cd /tmp/hatari && \
    ./configure --prefix=/usr && \
    make -j"$(nproc)" && \
    make install && \
    cd /tmp && rm -rf /tmp/hatari /tmp/hatari.tar.bz2; \
    mkdir /out && \
    find /usr /etc -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
