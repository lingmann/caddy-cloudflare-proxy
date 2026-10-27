# Caddy Cloudflare Proxy

Automatic HTTPS for an existing Docker Compose application, using Let's Encrypt and Cloudflare DNS. No application code changes or custom Caddyfile needed.

## Quickstart

You need Docker/Compose 2.18+, Just, and a domain managed by Cloudflare.

**1. Add the proxy to your application.** Merge the service and volumes from [examples/compose.yaml](examples/compose.yaml) into your existing `compose.yaml`, and the recipes from [examples/justfile](examples/justfile) into your `justfile`. Keep your existing app service. The proxy must share its Docker network; your app does not need a published host port. Adjust the recipes' `-f compose.yaml` if your Compose file has another name.

**2. Set the environment.** Add these values to your application's private `.env`:

```dotenv
PROXY_IMAGE=ghcr.io/lingmann/caddy-cloudflare-proxy:v1.0.0
PROXY_HOSTNAMES=app.example.com
PROXY_UPSTREAM=app:8080
CADDY_BIND_IP=192.168.1.10
PROXY_DNS_TARGET=192.168.1.10
CLOUDFLARE_ZONE_ID=<your-zone-id>
CLOUDFLARE_API_TOKEN=<your-token>
```

Replace `app:8080` with your app's Compose service name and port. Use a hostname in your zone and an IP reachable by your clients. The token needs **Zone DNS Edit** and **Zone Read**, scoped to that zone. Keep `.env` ignored by Git.

The image is public: no GitHub account or registry login is needed. [Releases](https://github.com/lingmann/caddy-cloudflare-proxy/releases) list available versions, matching examples, and optional digest pins.

**3. Start it.** Start your app through its normal workflow, then run:

```sh
chmod 600 .env
just dns-plan     # Review the proposed DNS changes
just proxy-up    # Apply DNS and start/update only the proxy
```

Open `https://app.example.com`. Certificates are issued and renewed automatically. No port 80 or public inbound access is needed for certificate issuance; clients still need a route to the proxy. HTTPS does not add application authentication.

That's it—consumers do not build this image or install Python. For a different HTTPS port, set `CADDY_HTTPS_PORT` and include it in the URL.

## Common commands

| Command | Purpose |
| --- | --- |
| `just proxy-check` | Check port settings and Caddy configuration without DNS writes. |
| `just proxy-verify` | Verify trusted HTTPS and the expected application response from Docker. |
| `just proxy-status` | Show proxy status. |
| `just proxy-logs` | Follow proxy logs. |
| `just proxy-stop` | Stop only the proxy, preserving certificates and the app. |

`proxy-verify` expects HTTP 200 by default; configure the expected status/path for your app. See [configuration and operations](docs/reference.md) for all settings, networking, custom Caddyfiles, and DNS behavior.

## Update the image

Set `PROXY_IMAGE` in `.env` to the new release's version tag, then run:

```sh
docker compose -f compose.yaml pull proxy
just proxy-up
just proxy-verify
```

Keep the existing certificate volumes. Container restarts do not reconcile application DNS records; Caddy still manages its own certificate challenge records.

## Development

These commands are for maintainers of **this repository**, not consuming applications:

```sh
just build            # Native smoke-test image
just check
just test
just test-integration
just update-deps      # Update dependency pins only; no build or deployment
```

[Dockerfile](Dockerfile) is the source of truth for Caddy and Cloudflare provider versions and official image digest pins. Cloudflare is the only non-standard Caddy module. Local builds stay native; GitHub handles amd64/arm64 builds. Consumers need Docker/Compose and Just, not Go or Python.

See [maintainer instructions](docs/development.md) for dependency updates and publishing. Tests use fake credentials, an isolated network, and a disposable CA—never live Cloudflare or public ACME.
