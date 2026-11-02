# syntax=docker/dockerfile:1
# nxdk (open-source Xbox homebrew SDK) -- Linux-hosted clang/lld cross compiler
# for the original Xbox. A test_hostbuild step invokes `make` against this to
# produce a bootable .iso that an xemu instance then boots.
#
# Built from source (nxdk ships no prebuilt tarball). Ubuntu's clang 20.1.2 is
# in the 19.x-20.1.2 range nxdk's bin/activate warns has an optimization-related
# codegen bug (https://github.com/llvm/llvm-project/issues/134607); samples/hello
# and samples/triangle build fine regardless, see sourcedir/xemu_cuberotate/NOTES.txt.
# The smoke build also compiles nxdk's own cxbe/extract-xiso, so those are
# prebuilt in the layer.
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    sh /docker/apt-install.sh /docker/pkgs/nxdk.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /opt
RUN set -eux; \
    git clone --recursive https://github.com/XboxDev/nxdk.git /opt/nxdk; \
    cd /opt/nxdk/samples/hello; \
    eval "$(/opt/nxdk/bin/activate -s)"; \
    make; \
    test -f bin/default.xbe; \
    make clean; \
    chmod -R a+rX /opt/nxdk; \
    echo "nxdk cross-build to Xbox .xbe OK"; \
    mkdir -p /out/opt && cp -a /opt/nxdk /out/opt/

FROM scratch
COPY --from=build /out/ /
