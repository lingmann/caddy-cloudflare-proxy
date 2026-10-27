# Configuration and operations

## Configuration

| Variable | Requirement |
| --- | --- |
| `PROXY_IMAGE` | Published version tag, optional digest pin from release notes, or the local development tag. |
| `PROXY_HOSTNAMES` | One registered name or single-space-separated names in one zone; quote lists in `.env`. No URLs, ports, commas, wildcards, or trailing dots. |
| `PROXY_UPSTREAM` | One `host:port`, optionally `http://` or `https://`. Explicit port 1–65535; no path, credentials, or whitespace. Bracket IPv6 addresses. |
| `CADDY_BIND_IP` | Explicit interface IP on the Docker host. Used only by Compose, not Caddy. |
| `CADDY_HTTPS_PORT` | Host TCP port, default `443`; Caddy always listens on container TCP 443. Used only by Compose. |
| `PROXY_DNS_TARGET` | One IPv4 address, IPv6 address, or full hostname shared by all configured names. Required for DNS commands and `prepare`, not `serve`. |
| `CLOUDFLARE_ZONE_ID` | Zone ID, not account ID. Required for DNS commands and `prepare`, not `serve`. |
| `CLOUDFLARE_API_TOKEN` | Zone-scoped **DNS Edit** and **Zone Read** token. The pinned provider expects 40 characters; serving validation checks this without echoing the value. |
| `ACME_CA` | Optional. Only the Let's Encrypt production or staging directory URLs are accepted. Production is the default. |
| `PROXY_VERIFY_PATH` | Safe GET path, optionally with a query; default `/`. No fragments, credentials, or secrets. |
| `PROXY_VERIFY_STATUS` | Expected HTTP status, 100–599; default `200`. Redirects are not followed. Set `401`/`403` for an expected auth boundary. |
| `PROXY_VERIFY_BODY` | Optional literal UTF-8 substring required within the first 64 KiB of the response. Not printed. |
| `PROXY_VERIFY_TIMEOUT` | Total verification deadline for all names, 1–600 seconds; default `120`. |

Compose reads `.env` for interpolation, and exported variables take precedence. The example's `environment` section passes only selected settings to the container environment. Credentials belong in runtime environment variables, never build arguments, source files, or committed examples. Docker administrators can inspect container secrets; do not dump resolved Compose configuration or container environments into logs.

The template supports one zone, one DNS target, and one upstream. Separate proxy instances or a deliberately extended configuration are needed for other layouts. Do not treat the image as a Docker auto-discovery controller: it has no Docker socket access and does not find backends automatically.

## Container commands

The entrypoint is `proxy`; its default command is `serve`. The shell examples below use `compose.yaml` and service `proxy`; substitute your configured filename and service name when different. The executable inside the image is always `proxy`.

| Command | Behavior |
| --- | --- |
| `check` | Validate serving inputs and Caddy configuration. No address-record API calls or certificate issuance with the built-in configuration. |
| `dns-plan` | Validate DNS inputs, read Cloudflare, print changes without writing. |
| `dns-sync` | Reconcile address records only. Does not validate or start the web proxy. |
| `prepare` | Validate serving and DNS inputs, validate Caddy, then reconcile address records. Does not start a listener. |
| `serve` | Validate serving inputs and Caddy, then `exec` Caddy as PID 1. Never reconcile address records. |
| `reload` | Validate and reload the mounted Caddyfile using the current container environment. |
| `healthcheck` | Check the local TCP 443 listener. Does not prove certificate validity, upstream readiness, or client reachability. |
| `preflight` | Read resolved Compose JSON on stdin; validate one explicit host-IP/TCP-443 mapping and select a Docker bind probe or reuse. The private `_proxy-preflight` recipe runs it before preparation. |
| `bind-check` | Exit successfully after Docker allocates service ports. Start no listener or DNS operation. The preflight recipe invokes it only when needed. |
| `verify` | Check trusted HTTPS and the expected application response for every name. Default client port 443; `--port` overrides it, or `--from-compose` reads the published port from Compose JSON on stdin. |

`preflight` and `verify --from-compose` accept `--service NAME` (default `proxy`) to select the service in Compose JSON. The supplied recipes pass the configured service name automatically. They use `config --format json NAME` to reduce unnecessary input, not to enforce credential isolation. Resolved Compose JSON may contain credentials, including shared application settings and service dependencies. Never log it. Existing Compose dependencies are allowed, but the recipes use `--no-deps`; start the application through its own workflow.

One-shot commands use the same image and data volumes as the serving container:

```sh
docker compose -f compose.yaml run --rm --no-deps proxy check
docker compose -f compose.yaml run --rm --no-deps proxy dns-plan
```

Compose `exec` does not use the image entrypoint. Include the executable explicitly:

```sh
docker compose -f compose.yaml exec -T proxy proxy reload
```

Use recreation for changed `.env` values, image versions, or host port mappings. A restart or reload does not replace the container environment. Recreation briefly interrupts service.

## Integrate with existing project recipes

Set `PROXY_COMPOSE_FILE` and `PROXY_SERVICE` at the top of the consumer `justfile`; defaults are `compose.yaml` and `proxy`. These are host-side Just variables, not container settings or `.env` inputs. Preserve an existing service name rather than renaming it for this integration. For a temporary override:

```sh
just PROXY_COMPOSE_FILE=compose.production.yaml PROXY_SERVICE=edge proxy-plan
```

Use the same settings for preparation, deployment, verification, and direct Compose maintenance commands. Pair the recipes with the matching image release; older images might not accept `--service`.

Keep the public integration small: `proxy-plan`, `proxy-up`, and `proxy-verify`. The `proxy-` prefix belongs to consumer recipes; the container commands keep their existing names. `proxy-plan` previews DNS changes only, not container changes or a complete deployment.

If the application already has a deployment recipe, omit `proxy-up` and keep only `proxy-plan` and `proxy-verify` as new public recipes. Copy the host-side settings and all private helpers from `examples/justfile`. Make `_proxy-prepare` a dependency of the existing deployment recipe, before any command or dependency that stops or replaces either service. It runs preflight, validates Caddy, and reconciles DNS. Do not use `proxy-up` as preparation: it also replaces the proxy.

After preparation succeeds, deploy the application and recreate the proxy with a bounded health wait. Preserve the proxy's `--force-recreate --wait --wait-timeout 90` behavior so prepared configuration takes effect even if Compose detects no service changes. Run `just proxy-verify` after both services are running. Keep it public for independent checks. See [deployment behavior](#deployment-behavior) for failure and verification limits.

Reuse the application's logs, status, and stop recipes for both services. Stopping must preserve the certificate and application volumes. Do not add duplicate proxy-only wrappers; use the maintenance commands below when needed. Keep `set default-list` so bare `just` lists recipes without taking action. Private `_proxy-*` helpers stay out of that list.

## Installation, migration, and recovery

### Before deployment

For a new installation, select the explicit host binding, client-reachable DNS target, application network, and persistent volumes. For a migration, first inventory the existing hostnames, routing, authentication, published ports, service names, Compose project identity, and actual certificate volume names. Preserve application security policy in its custom Caddyfile; do not replace it with the default just to adopt this image.

Keep the existing project identity and `/data` and `/config` volume mappings. If importing volumes from another Compose project, explicitly reference those existing volumes rather than letting Compose create empty ones. Keep the service name where possible. Moving a binding owned by another service is not automated: preflight refuses the conflict. Plan a separate cutover instead of disabling the guard.

Before changing a running installation:

1. Retain the previous image's immutable reference or local image, Compose files, custom Caddyfile, deployment settings, and usable credentials. Store secret-bearing backups privately, not in Git or logs. Back up certificate state securely. An old container alone is not a recovery plan.
2. Pull the chosen proxy image and build or pull application images before cutover. Do not build this proxy image in consumer projects. Keep the previous images available until verification succeeds.
3. Validate Compose and Caddy, review `proxy-plan`, and finish `_proxy-prepare` before any deployment step stops or replaces a service. The integrated deployment recipe must enforce that ordering. Preparation can partially change DNS even when it fails.
4. Record the commands that replace services, verify HTTPS, and restore the previous deployment. State which services can be interrupted. Use the normal deployment workflow, then verify from Docker and an intended client. There is no automatic rollback or no-op/reload classification.

### Recover a failed replacement

Restore the previous Compose files, Caddyfile, image reference, and deployment settings, using the same project identity and volumes. Ensure the previous image is available locally. Validate the restored configuration and review a fresh DNS plan before running the normal preparation/deployment workflow. Restore affected application services through their own supported recovery procedure, then verify trusted HTTPS and the expected responses again.

Restoring files does not change the running container's environment or undo DNS writes. Recreate with the restored settings; do not rely on a restart alone. DNS reconciliation is not transactional and does not delete records for names removed from configuration. Inspect partial writes and newly created records separately; resolve conflicts deliberately. Never use `down -v` or delete certificate volumes as a recovery step. Rehearse the application's cutover and recovery procedure with disposable resources before relying on it.

## Maintenance commands

Run these from the consumer project, substituting the configured Compose filename and service name. They do not need public Just recipes.

### Validate without DNS writes

Run the private binding helper, then validate serving inputs and Caddy. Preflight can briefly allocate a new host binding, but does not stop the existing proxy. Neither command reconciles DNS.

```sh
just _proxy-preflight && docker compose -f compose.yaml run --rm --no-deps -T proxy check
```

For CI that only needs configuration validation, use the second command alone. It does not check host port availability. Tests must use fake credentials, isolated resources, and no public ACME service.

### Reconcile DNS without deploying

Review the plan first. The second command writes address records but does not validate or start the web proxy. Normal deployment already includes this reconciliation.

```sh
just proxy-plan
# After reviewing the plan:
docker compose -f compose.yaml run --rm --no-deps -T proxy dns-sync
```

### Reload a custom mounted Caddyfile

Validate and reload using the running container's environment:

```sh
docker compose -f compose.yaml exec -T proxy proxy reload
```

For changed environment values, image versions, or port mappings, use the normal deployment recipe instead. Reload does not reconcile address records or replace the container environment.

### Inspect or stop only the proxy

Show status, including stopped containers:

```sh
docker compose -f compose.yaml ps --all proxy
```

Follow the latest 50 log lines; Ctrl+C leaves the proxy running:

```sh
docker compose -f compose.yaml logs --tail=50 --follow proxy
```

Stop only the proxy, preserving the application and certificate volumes:

```sh
docker compose -f compose.yaml stop proxy
```

## DNS behavior and limits

The reconciler uses Cloudflare's zone API and requires zone-scoped DNS Edit plus Zone Read. It validates zone membership, checks ancestor names for NS delegations below the zone apex, and reads all pages before writing anything. Refuse a delegated child name in its parent zone: use the authoritative child zone instead.

- IPv4 targets become `A`, IPv6 targets become `AAAA`, and hostname targets become `CNAME` without local resolution.
- Records use `proxied=false` and TTL 300. An unchanged apply performs no writes.
- Updates PATCH only changed content, proxy status, or TTL. Preserve comments, tags, unrelated records, and Caddy's `_acme-challenge` TXT records.
- Refuse duplicate address records, mixed A/AAAA sets, type migrations, and CNAME conflicts. Never delete records automatically, including old names after configuration changes.
- Reject direct CNAME loops to configured names. Indirect loops and reachability are not checked.
- Keep the shared lock at `/data/.dns-sync.lock`. It coordinates operator containers sharing that volume, not other volumes, hosts, or external DNS writers. Never unlink it while operations can run.
- Writes across names are not atomic and are not retried automatically. After a partial failure, inspect a fresh plan and reconcile again. A network error can occur after Cloudflare accepted a write.

The script blocks redirects, applies a request timeout, and redacts transport errors. Syntax and exact-name guards constrain this program, not the zone-wide power of a stolen token.

## Network and authentication boundary

DNS-01 does not need inbound port 80 or public access to the origin. Private LAN/Tailscale clients can use publicly trusted certificates without router forwarding. They still need routing and firewall access to the explicit host IP and selected TCP port. Public deployments need public routing/NAT and a reviewed authentication boundary.

The template publishes no HTTP port, HTTP/3 UDP port, or Caddy admin port. Keep the backend off public host ports. A loopback-published backend remains reachable by host processes, bypassing Caddy. HTTPS is not authentication: the default Caddyfile preserves the application's Authorization header and does not add authentication.

Use an upstream reachable from the container:

- Same Compose network: `app:8080`, with the app listening on its container interface.
- Separate Compose project: join an explicitly shared external network and use a stable alias.
- Host service on Docker Desktop/OrbStack: test `host.docker.internal:PORT`; host-loopback behavior varies.
- Linux host service: add `extra_hosts: ["host.docker.internal:host-gateway"]` when using that name. A host-loopback-only backend generally cannot be reached through the bridge gateway.

Do not use container `localhost` for a separate backend. Do not bypass upstream TLS verification. Configure application public URLs and trusted-proxy settings, especially with custom HTTPS ports.

Private-IP DNS answers can encounter rebinding protection; use narrow resolver exceptions or matching internal DNS records. Private/Tailscale CNAME targets require client resolvers that can follow them. Cloudflare always flattens apex CNAMEs, so avoid private targets at the apex and disable optional flattening for non-apex private aliases. Prefer a stable private IP when aliases do not resolve from clients.

Names and DNS targets are public, and certificate names appear in Certificate Transparency. IPv6 needs working host, Docker, firewall, and client support. Caddy needs outbound HTTPS and DNS, including the configured resolvers `1.1.1.1` and `1.0.0.1`. Check delegation and CAA when issuance fails; do not remove intentional challenge delegation or unrelated TXT records automatically.

## Custom configuration and persistent state

For routing or authentication beyond the default, mount a complete custom Caddyfile at `/etc/caddy/Caddyfile:ro`. Start from this repository's `Caddyfile`. Keep the environment contract: serving commands still validate hostnames, upstream, token, and ACME URL. Changing custom routing does not change which names the DNS reconciler owns.

`check` validates a custom file but cannot enforce every custom security or network property. A custom configuration is trusted code; review its authentication, issuer, storage, admin listener, and logging choices. The healthcheck assumes TCP 443 remains available.

Preserve `/data` (certificates, account keys, and shared lock) and `/config`. Keep the Compose project identity stable so a rename does not silently select empty volumes and trigger new issuance. Back up private keys securely. Do not delete volumes or use `down -v` to resolve routine problems.

For repeated ACME experiments use `ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory` with a separate Compose project and volumes. Staging certificates are intentionally untrusted. Restore production configuration and verify public trust for the final service. Keep Caddy running with persistent state and valid credentials for automatic renewal.

## Deployment behavior

`proxy-up` finishes preflight and preparation before replacing the running proxy. A failure in those phases leaves the old container running, although preparation can partially update DNS. Replacement itself can fail after preparation; there is no automatic rollback. The recipe waits up to 90 seconds for listener health, not working TLS or backend access.

Preflight rejects invalid host ports before DNS writes. For a new binding it briefly asks Docker to allocate the configured port, without starting a listener. It skips that probe when the running proxy owns the identical binding. This catches many bind conflicts, but cannot reserve the port across deployment or prove client routing. It never stops the existing proxy to make room.

`proxy-verify` checks all configured names from a temporary client container using normal DNS, trusted TLS, and an expected HTTP status/optional body marker. It has a total deadline, does not follow redirects, and sends no authentication credentials. Configure a safe GET endpoint. Repeat verification from an intended client without bypassing certificate checks: Docker routing and DNS can differ from the client's.

