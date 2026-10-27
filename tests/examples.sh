#!/usr/bin/env bash
# Compose parsing is real; every command that could deploy or write DNS is stubbed.
set -euo pipefail
cd "$(dirname "$0")/.."
root="$(pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cp examples/compose.yaml examples/justfile "$tmp/"
export PROXY_IMAGE=caddy-cloudflare-proxy:dev
export PROXY_HOSTNAMES='service.example.com api.example.com'
export PROXY_UPSTREAM=app:8080
export CADDY_BIND_IP=192.168.1.10 CADDY_HTTPS_PORT=443
export PROXY_DNS_TARGET=192.168.1.10 CLOUDFLARE_ZONE_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export CLOUDFLARE_API_TOKEN=0000000000000000000000000000000000000000
export ACME_CA=https://acme-v02.api.letsencrypt.org/directory
cd "$tmp"
docker compose --env-file /dev/null -f compose.yaml config --quiet
CADDY_HTTPS_PORT=9443 docker compose --env-file /dev/null -f compose.yaml config --quiet
if PROXY_IMAGE= docker compose --env-file /dev/null -f compose.yaml config --quiet 2>/dev/null; then
    echo 'PROXY_IMAGE must be required' >&2; exit 1
fi

# A real consumer model checks dependencies, service selection, and a filename containing spaces.
cat > 'custom compose.yaml' <<'COMPOSE'
services:
  edge:
    depends_on: [app]
    extends:
      file: compose.yaml
      service: proxy
  app:
    image: unused-test-image
volumes:
  caddy_data:
  caddy_config:
COMPOSE
mkdir bin
export REAL_DOCKER="$(command -v docker)"
export CALL_LOG="$tmp/calls" PHASE_LOG="$tmp/phases"
cat > bin/docker <<'STUB'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$CALL_LOG"
phase=''
case "$*" in
    *'config --format json'*)
        if [ "${FAIL_PHASE:-}" = json ]; then exit 2; fi
        exec "$REAL_DOCKER" "$@" ;;
    *"ps --status running --format json $PROXY_SERVICE")
        if [ "${FAIL_PHASE:-}" = ps ]; then exit 2; fi
        exit 0 ;;
    *" $PROXY_SERVICE preflight --service $PROXY_SERVICE "*) phase=preflight ;;
    *" $PROXY_SERVICE bind-check") phase=bind ;;
    *" $PROXY_SERVICE prepare") phase=prepare ;;
    *'up -d '*) phase=up ;;
    *" $PROXY_SERVICE verify --from-compose --service $PROXY_SERVICE") phase=verify ;;
esac
if [ "$phase" = preflight ] || [ "$phase" = verify ]; then
    # Consume pipeline input without logging its contents.
    while IFS= read -r _; do :; done
fi
if [ -n "$phase" ]; then
    printf '%s\n' "$phase" >> "$PHASE_LOG"
    if [ "${FAIL_PHASE:-}" = "$phase" ]; then exit 3; fi
fi
if [ "$phase" = preflight ]; then printf '%s\n' "${PREFLIGHT_MODE:-probe}"; fi
STUB
chmod +x bin/docker
export PATH="$tmp/bin:$PATH"
just --fmt --check
just --list > listed
just > default-list
diff -u listed default-list
[ "$(just --summary)" = 'proxy-plan proxy-up proxy-verify' ]
for recipe in proxy-plan proxy-up proxy-verify; do
    just --usage "$recipe" >/dev/null
    just --dry-run "$recipe" >/dev/null 2>&1
done
[ ! -e "$CALL_LOG" ]
just proxy-up >/dev/null 2>&1
printf '%s\n' preflight bind prepare up > expected
diff -u expected "$PHASE_LOG"
grep -q 'up -d --no-deps --force-recreate --wait --wait-timeout 90 proxy' "$CALL_LOG"
for phase in json ps preflight bind prepare up; do
    : > "$CALL_LOG"; : > "$PHASE_LOG"
    if FAIL_PHASE="$phase" just proxy-up >/dev/null 2>&1; then
        echo "Deployment ignored $phase failure" >&2; exit 1
    fi
    if [ "$phase" != up ] && grep -q '^up$' "$PHASE_LOG"; then echo 'Failed preflight/preparation still deployed' >&2; exit 1; fi
    if [ "$phase" != prepare ] && [ "$phase" != up ] && grep -q '^prepare$' "$PHASE_LOG"; then
        echo 'Failed preflight still allowed DNS preparation' >&2; exit 1
    fi
done
: > "$PHASE_LOG"
PREFLIGHT_MODE=reuse just proxy-up >/dev/null 2>&1
printf '%s\n' preflight prepare up > expected
diff -u expected "$PHASE_LOG"
if PREFLIGHT_MODE=unexpected just proxy-up >/dev/null 2>&1; then
    echo 'Unknown preflight result accepted' >&2; exit 1
fi
: > "$CALL_LOG"
just proxy-plan >/dev/null 2>&1
printf '%s\n' 'compose -f compose.yaml run --rm --no-deps -T proxy dns-plan' > expected
diff -u expected "$CALL_LOG"
# The reusable preparation helper never replaces a container.
: > "$PHASE_LOG"
just _proxy-prepare >/dev/null 2>&1
printf '%s\n' preflight bind prepare > expected
diff -u expected "$PHASE_LOG"
: > "$CALL_LOG"; : > "$PHASE_LOG"
just proxy-verify >/dev/null 2>&1
printf '%s\n' verify > expected
diff -u expected "$PHASE_LOG"
grep -q 'run --rm --no-deps -T proxy verify --from-compose' "$CALL_LOG"
for phase in json verify; do
    if FAIL_PHASE="$phase" just proxy-verify >/dev/null 2>&1; then
        echo "Verification ignored $phase failure" >&2; exit 1
    fi
done
# Exercise both pipelines and all public recipes with non-default settings and dependencies.
: > "$CALL_LOG"; : > "$PHASE_LOG"
just PROXY_COMPOSE_FILE='custom compose.yaml' PROXY_SERVICE=edge proxy-plan proxy-up proxy-verify >/dev/null
printf '%s\n' preflight bind prepare up verify > expected
diff -u expected "$PHASE_LOG"
grep -q 'compose -f custom compose.yaml config --format json edge' "$CALL_LOG"
grep -q 'run --rm --no-deps -T edge dns-plan' "$CALL_LOG"
grep -q 'up -d --no-deps --force-recreate --wait --wait-timeout 90 edge' "$CALL_LOG"
# The maintainer update recipe invokes only its script, with no build/deploy dependency.
cat > bin/python3 <<'STUB'
#!/usr/bin/env bash
printf 'python3 %s\n' "$*" >> "$CALL_LOG"
STUB
chmod +x bin/python3
: > "$CALL_LOG"
just --justfile "$root/justfile" --usage update-deps >/dev/null
just --justfile "$root/justfile" --dry-run update-deps >/dev/null 2>&1
[ ! -s "$CALL_LOG" ]
just --justfile "$root/justfile" update-deps >/dev/null 2>&1
printf '%s\n' 'python3 scripts/update_deps.py' > expected
diff -u expected "$CALL_LOG"
echo 'Operator examples passed: three public recipes, Compose/Just, private preparation, binding preflight, preparation guards, listener wait, verification, and update-only recipe.'
