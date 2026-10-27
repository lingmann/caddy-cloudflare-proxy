# syntax=docker/dockerfile:1
# Compile on the build host; GitHub's multiarchitecture builds set the target below.
FROM --platform=$BUILDPLATFORM caddy:2.11.7-builder@sha256:c2fc374fd352439d76e1777b28d97d9f6d9c1dd86116db9227460374d46097d5 AS builder
ARG TARGETOS TARGETARCH
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    GOOS=$TARGETOS GOARCH=$TARGETARCH CGO_ENABLED=0 XCADDY_PRINT_VERSION=0 \
    xcaddy build v2.11.7 --with github.com/caddy-dns/cloudflare@v0.2.4

FROM caddy:2.11.7@sha256:504a44d7d74bd2bc28a66afc4774912fc21223ca8a67734af1c54ab2f999b489 AS runtime
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# Python is only an operator/startup dependency. Caddy remains the serving process.
RUN apk add --no-cache python3 \
    && caddy list-modules | grep -Fx dns.providers.cloudflare
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY src/ /opt/proxy/
COPY Caddyfile /etc/caddy/Caddyfile
RUN chmod 755 /opt/proxy/proxy.py \
    && ln -s /opt/proxy/proxy.py /usr/local/bin/proxy

# These are also shared by one-shot operator containers created through Compose.
VOLUME /data /config
ENTRYPOINT ["proxy"]
CMD ["serve"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["proxy", "healthcheck"]

FROM runtime AS test
COPY tests/test_*.py /opt/tests/
COPY scripts/ /opt/scripts/
ENV PYTHONPATH=/opt/proxy
RUN --network=none python3 -m unittest discover -s /opt/tests -v

# Keep the default build target the lean runtime, without tests.
FROM runtime AS release
