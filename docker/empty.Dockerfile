# Placeholder bound to the ctx-<component> slot of every component NOT selected,
# so the final Dockerfile's `COPY --from=ctx-<component> / /` is a no-op for it.
FROM scratch
LABEL xp5.empty=1
