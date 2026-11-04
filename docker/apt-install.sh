#!/bin/sh
# Install the union of the given package-list files (docker/pkgs/*.apt).
# Lists are one-or-more packages per line, '#' starts a comment. Overlapping
# entries across lists (libslirp-dev in qemu+86box, libsdl2-dev in qemu+
# basilisk, ...) collapse naturally into one apt transaction.
# Caller is responsible for cleaning /var/lib/apt/lists afterwards.
set -eu
pkgs=$(sed 's/#.*//' "$@" | tr -s ' \t\n' ' ' | tr ' ' '\n' | sort -u | tr '\n' ' ')
[ -n "${pkgs% }" ] || { echo "apt-install: nothing to install"; exit 0; }
# Optional internal caching mirror; Pipeline-Depth 0 works around
# apt-cacher-ng corrupting pipelined responses (see Dockerfile.base).
if [ -n "${APT_PROXY:-}" ]; then
    printf '%s\n' "Acquire::http::Proxy \"$APT_PROXY\";" \
        'Acquire::http::Pipeline-Depth "0";' 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/01proxy
fi
apt-get update
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $pkgs
