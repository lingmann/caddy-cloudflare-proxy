#!/usr/bin/env bash
# All resources are disposable. No host ports, real credentials, or external network.
set -euo pipefail
cd "$(dirname "$0")/.."
image="${TEST_IMAGE:-caddy-cloudflare-proxy:dev}"
prefix="caddy-proxy-test-$$-$(date +%s)"
network="$prefix-net"
data="$prefix-data"
config="$prefix-config"
backend="$prefix-backend"
backend2="$prefix-backend2"
server="$prefix-server"
locker="$prefix-locker"
fixtures="$(pwd)/tests/fixtures"
tmp="$(mktemp -d)"
cleanup() {
    code=$?
    if [ "$code" -ne 0 ]; then
        docker logs "$server" >&2 2>/dev/null || true
        docker logs "$backend" >&2 2>/dev/null || true
    fi
    docker rm -fv "$server" "$backend" "$backend2" "$locker" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    docker volume rm "$data" "$config" >/dev/null 2>&1 || true
    rm -rf "$tmp"
}
trap cleanup EXIT
common=(-e 'PROXY_HOSTNAMES=service.example.com' -e PROXY_UPSTREAM=backend:8080 -e CLOUDFLARE_API_TOKEN=0000000000000000000000000000000000000000)

# Keep the chosen versions and exactly one non-standard module in the runtime.
version="$(docker run --rm --network none --entrypoint caddy "$image" version)"
[ "${version%% *}" = v2.11.7 ]
modules="$(docker run --rm --network none --entrypoint caddy "$image" list-modules --skip-standard --versions)"
[ "$(printf '%s\n' "$modules" | grep -E '^[a-z_]+\.')" = 'dns.providers.cloudflare v0.2.4' ]
docker run --rm --network none --entrypoint sh "$image" -ec 'if command -v go || command -v xcaddy; then exit 1; fi'

# Built-in configuration validation works offline, including lists and staging.
docker run --rm --network none "${common[@]}" "$image" check
docker run --rm --network none "${common[@]}" -e 'PROXY_HOSTNAMES=service.example.com api.example.com' -e ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory "$image" check
if docker run --rm --network none "${common[@]}" -e CLOUDFLARE_API_TOKEN=invalid-test-secret "$image" check >"$tmp/error" 2>&1; then
    echo 'Invalid token unexpectedly accepted' >&2; exit 1
fi
if grep -q invalid-test-secret "$tmp/error"; then
    echo 'Validation leaked the token' >&2; exit 1
fi

# No external route exists for any serving container in this test.
docker network create --internal "$network" >/dev/null
docker volume create "$data" >/dev/null
docker volume create "$config" >/dev/null
docker run -d --name "$backend" --network "$network" --network-alias backend --no-healthcheck \
    --mount "type=bind,src=$fixtures,dst=/fixtures,readonly" --entrypoint python3 "$image" /fixtures/backend.py >/dev/null
docker run -d --name "$backend2" --network "$network" --network-alias backend2 --no-healthcheck -e RESPONSE_MARKER=second \
    --mount "type=bind,src=$fixtures,dst=/fixtures,readonly" --entrypoint python3 "$image" /fixtures/backend.py >/dev/null
cp "$fixtures/Caddyfile" "$tmp/Caddyfile"
start_proxy() {
    docker run -d --name "$server" --network "$network" --network-alias service.example.com \
        "${common[@]}" -v "$data:/data" -v "$config:/config" \
        --mount "type=bind,src=$tmp/Caddyfile,dst=/etc/caddy/Caddyfile,readonly" "$image" >/dev/null
}
client() {
    docker run --rm --network "$network" -v "$data:/trust:ro" \
        --mount "type=bind,src=$fixtures,dst=/fixtures,readonly" --entrypoint python3 "$image" /fixtures/client.py "$@"
}
start_proxy
client >"$tmp/first"
docker exec "$server" proxy healthcheck
[ "$(docker exec "$server" sh -c 'read name rest < /proc/1/comm; printf "%s" "$name"')" = caddy ]
# Replace the route in-place, preserving the bind-mounted file's inode.
printf '%s\n' '{' $'\tauto_https disable_redirects' $'\tskip_install_trust' '}' '' \
    '{$PROXY_HOSTNAMES} {' $'\ttls internal' $'\treverse_proxy backend2:8080' '}' > "$tmp/Caddyfile"
docker exec "$server" proxy reload
client second >"$tmp/reloaded"
cmp "$tmp/first" "$tmp/reloaded"

# The production verifier requires trust and an application-specific response.
verify() {
    docker run --rm --network "$network" -v "$data:/trust:ro" \
        -e PROXY_HOSTNAMES=service.example.com -e PROXY_VERIFY_PATH='/test?query=kept' \
        -e PROXY_VERIFY_TIMEOUT=2 "$@" "$image" verify
}
verify -e SSL_CERT_FILE=/trust/caddy/pki/authorities/local/root.crt -e 'PROXY_VERIFY_BODY="backend": "second"'
if verify >"$tmp/untrusted" 2>&1; then
    echo 'Verification accepted an untrusted certificate' >&2; exit 1
fi
grep -q 'certificate trust' "$tmp/untrusted"
if verify -e SSL_CERT_FILE=/trust/caddy/pki/authorities/local/root.crt -e PROXY_VERIFY_STATUS=401 >"$tmp/wrong-status" 2>&1; then
    echo 'Verification accepted the wrong application status' >&2; exit 1
fi
grep -q 'HTTP 200; expected 401' "$tmp/wrong-status"
if verify -e SSL_CERT_FILE=/trust/caddy/pki/authorities/local/root.crt -e PROXY_VERIFY_BODY=wrong-backend >"$tmp/wrong-body" 2>&1; then
    echo 'Verification accepted the wrong application body' >&2; exit 1
fi
grep -q 'body marker absent' "$tmp/wrong-body"

# Recreate the container with the same named volumes. DNS operation settings are absent.
docker stop --time 10 "$server" >/dev/null
[ "$(docker inspect --format '{{.State.ExitCode}}' "$server")" = 0 ]
docker rm -v "$server" >/dev/null
start_proxy
client second >"$tmp/recreated"
cmp "$tmp/first" "$tmp/recreated"

# Lock exclusion works across separate operator containers sharing /data.
docker run -d --name "$locker" --network none --no-healthcheck -v "$data:/data" \
    --entrypoint python3 "$image" -c 'import fcntl,time; f=open("/data/.dns-sync.lock","a"); fcntl.flock(f,fcntl.LOCK_EX); open("/data/lock-ready","w").close(); time.sleep(120)' >/dev/null
ready=0
for _ in $(seq 1 40); do
    if docker exec "$locker" test -f /data/lock-ready; then ready=1; break; fi
    sleep 0.25
done
[ "$ready" = 1 ]
if docker run --rm --network none "${common[@]}" -e CLOUDFLARE_ZONE_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    -e PROXY_DNS_TARGET=192.168.1.10 -v "$data:/data" "$image" dns-plan >"$tmp/locked" 2>&1; then
    echo 'Concurrent DNS operation unexpectedly acquired the lock' >&2; exit 1
fi
grep -q 'shared volume lock' "$tmp/locked"
docker rm -fv "$locker" >/dev/null
docker run --rm --network none -v "$data:/data" --entrypoint python3 "$image" \
    -c 'import sys; sys.path.insert(0,"/opt/proxy"); import cloudflare_dns as d; from pathlib import Path; f=Path("/data/.dns-sync.lock"); inode=f.stat().st_ino
with d.locked(f): assert f.stat().st_ino == inode'

echo 'Integration passed: offline validation, trusted TLS and response verification, proxy headers, PID 1, reload, persistence, and shared locking.'
