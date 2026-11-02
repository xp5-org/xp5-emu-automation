#!/bin/sh
# Select the bakebuilder builder, creating it if missing. Bake's target:
# contexts need a docker-container builder.
# BUILD_PARALLELISM caps how many build steps BuildKit runs at once (default 2).
# Lower it on small hosts, or when apt-cacher-ng gives "Hash Sum mismatch" from
# parallel layers fetching the same files; raise it on big ones.
# BUILD_STORAGE_DIR (optional) is a host directory for BuildKit's cache and
# working data, for hosts whose Docker data-root disk is small. Default: a
# Docker-managed volume under the daemon's data-root.
# Both only apply when the builder is created: `docker buildx rm bakebuilder`
# first to change them.
set -eu
docker buildx use bakebuilder 2>/dev/null && exit 0
if [ -n "${BUILD_STORAGE_DIR:-}" ]; then
    mkdir -p "$BUILD_STORAGE_DIR"
    dir=$(cd "$BUILD_STORAGE_DIR" && pwd)
    # buildx mounts this named volume at /var/lib/buildkit; point it at the dir.
    docker volume rm -f buildx_buildkit_bakebuilder0_state >/dev/null
    docker volume create --driver local \
        --opt type=none --opt o=bind --opt device="$dir" \
        buildx_buildkit_bakebuilder0_state >/dev/null
fi
cfg=$(mktemp)
trap 'rm -f "$cfg"' EXIT
printf '[worker.oci]\n  max-parallelism = %s\n' "${BUILD_PARALLELISM:-2}" > "$cfg"
docker buildx create --name bakebuilder --driver docker-container \
    --buildkitd-config "$cfg" --use
