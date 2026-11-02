# syntax=docker/dockerfile:1
# SIMH built from github.com/simh/simh (SIMH_REF, default master = latest). All
# simulators land in /usr/local/bin; `dgnova` is a symlink to `nova` because
# pyhelpers/simhhelpers.py launches the Data General Nova simulator by that name.
# Build/runtime libs are in pkgs/simh.apt (final image).
FROM base AS build
ARG APT_PROXY=
ARG SIMH_REF=master
RUN --mount=type=bind,source=docker,target=/docker \
    sh /docker/apt-install.sh /docker/pkgs/simh.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    git clone --depth 1 --branch "$SIMH_REF" https://github.com/simh/simh.git && \
    make -C simh TESTS=0 -j"$(nproc)" && \
    mkdir -p /out/usr/local/bin && \
    find simh/BIN -maxdepth 1 -type f -exec cp -a -t /out/usr/local/bin/ {} + && \
    ln -s nova /out/usr/local/bin/dgnova && \
    git -C simh rev-parse HEAD > /out/usr/local/bin/.simh-revision && \
    rm -rf simh

FROM scratch
COPY --from=build /out/ /
