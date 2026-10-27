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

Compose reads `.env` for interpolation, and exported variables take precedence. It passes only selected settings to the container. Credentials belong in runtime environment variables, never build arguments, source files, or committed examples. Docker administrators can inspect container secrets; do not dump resolved Compose configuration or container environments into logs.

The template supports one zone, one DNS target, and one upstream. Separate proxy instances or a deliberately extended configuration are needed for other layouts. Do not treat the image as a Docker auto-discovery controller: it has no Docker socket access and does not find backends automatically.

## Container commands

The entrypoint is `proxy`; its default command is `serve`.

| Command | Behavior |
| --- | --- |
| `check` | Validate serving inputs and Caddy configuration. No address-record API calls or certificate issuance with the built-in configuration. |
| `dns-plan` | Validate DNS inputs, read Cloudflare, print changes without writing. |
| `dns-sync` | Reconcile address records only. Does not validate or start the web proxy. |
| `prepare` | Validate serving and DNS inputs, validate Caddy, then reconcile address records. Does not start a listener. |
| `serve` | Validate serving inputs and Caddy, then `exec` Caddy as PID 1. Never reconcile address records. |
| `reload` | Validate and reload the mounted Caddyfile using the current container environment. |
| `healthcheck` | Check the local TCP 443 listener. Does not prove certificate validity, upstream readiness, or client reachability. |
| `preflight` | Read resolved Compose JSON on stdin; validate one explicit host-IP/TCP-443 mapping and select a Docker bind probe or reuse. Normally use `just proxy-preflight`. |
| `bind-check` | Exit successfully after Docker allocates service ports. Start no listener or DNS operation. The preflight recipe invokes it only when needed. |
| `verify` | Check trusted HTTPS and the expected application response for every name. Default client port 443; `--port` overrides it, or `--from-compose` reads the published port from Compose JSON on stdin. |

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

`proxy-verify` checks all configured names from a temporary client container using normal DNS, trusted TLS, and an expected HTTP status/optional body marker. It has a total deadline, does not follow redirects, and sends no authentication credentials. Configure a safe GET endpoint. Repeat verification from an intended client: Docker routing and DNS can differ from the client's. Open the application from an intended client without bypassing certificate verification.

