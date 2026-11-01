# syntax=docker/dockerfile:1
# PS2SDK/ps2dev -- Linux-hosted EE (Emotion Engine) cross-compiler for PS2
# homebrew (real compiler name mips64r5900el-ps2-elf-gcc, invoked as $(EE_CC)
# by ps2sdk's own samples/Makefile.pref -- there is no "ee-gcc" binary). A
# test_hostbuild step invokes `make` against this to produce a .elf that a
# PCSX2 instance then boots.
#
# Installed from ps2dev's own prebuilt release tarball (built by their GitHub
# Actions CI) rather than any installer script. It already bundles gsKit at
# ps2dev/gsKit, so no separate gsKit build is needed. Pinned to v2.0.0 so it
# doesn't drift under the tests.
FROM base AS build
WORKDIR /opt
RUN set -eux; \
    curl -sL -o /tmp/ps2dev.tar.gz \
        https://github.com/ps2dev/ps2dev/releases/download/v2.0.0/ps2dev-ubuntu-latest.tar.gz; \
    mkdir -p /opt/ps2dev; \
    tar xzf /tmp/ps2dev.tar.gz -C /opt/ps2dev --strip-components=1; \
    rm -f /tmp/ps2dev.tar.gz; \
    chmod -R a+rX /opt/ps2dev; \
    printf 'int main(void){return 0;}\n' > /tmp/ps2test.c; \
    cd /tmp && PATH="/opt/ps2dev/bin:/opt/ps2dev/ee/bin:$PATH" \
        mips64r5900el-ps2-elf-gcc -I/opt/ps2dev/ps2sdk/ee/include \
            -o /tmp/ps2test.elf /tmp/ps2test.c \
            -L/opt/ps2dev/ps2sdk/ee/lib -Wl,-zmax-page-size=128 \
            -T/opt/ps2dev/ps2sdk/ee/startup/linkfile \
            -L/opt/ps2dev/ps2sdk/ee/startup -lkernel; \
    test -f /tmp/ps2test.elf; \
    echo "PS2SDK cross-build to EE .elf OK"; \
    mkdir -p /out/opt && cp -a /opt/ps2dev /out/opt/

FROM scratch
COPY --from=build /out/ /
