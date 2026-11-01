# syntax=docker/dockerfile:1
# OpenWatcom 2.0 -- Linux-hosted cross compiler for 16-bit DOS targets, so DOS
# test programs (sourcedir/sb16_isapc_echo/src_sb16echo etc.) can be built by a
# test_hostbuild step instead of by booting DOS and driving BUILD.BAT.
#
# Installed from the release SNAPSHOT TARBALL rather than the official
# open-watcom-2_0-c-linux-x64 installer: that installer segfaults immediately
# when run headless (with -h, and with silent-install flags). The tarball is the
# same build, just unpacked.
#
# Only binl64 (Linux x64 host binaries), h (headers), lib286 and lib386 are
# extracted: ~101MB unpacked against ~600MB for the full tree, which also
# carries DOS/OS2/Windows-hosted binaries this image can never run.
#
# Pinned to a dated build tag, not Current-build/Last-CI-build: those two are
# rolling and would silently change the compiler under the tests.
FROM base AS build
WORKDIR /opt
RUN set -eux; \
    curl -sL -o /tmp/ow-snapshot.tar.xz \
        https://github.com/open-watcom/open-watcom-v2/releases/download/2026-09-01-Build/ow-snapshot.tar.xz; \
    mkdir -p /opt/watcom; \
    tar xJf /tmp/ow-snapshot.tar.xz -C /opt/watcom --strip-components=1 \
        ./binl64 ./h ./lib286 ./lib386; \
    rm -f /tmp/ow-snapshot.tar.xz; \
    chmod -R a+rX /opt/watcom; \
    printf 'int main(void){return 0;}\n' > /tmp/owtest.c; \
    cd /tmp && WATCOM=/opt/watcom INCLUDE=/opt/watcom/h \
        PATH="/opt/watcom/binl64:$PATH" wcl -bt=dos -ms -q owtest.c; \
    test -f /tmp/owtest.exe; \
    echo "OpenWatcom cross-build to 16-bit DOS OK"; \
    mkdir -p /out/opt && cp -a /opt/watcom /out/opt/

FROM scratch
COPY --from=build /out/ /
