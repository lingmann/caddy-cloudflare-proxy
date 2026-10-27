# Caddy Cloudflare Proxy

Automatic HTTPS for an existing Docker Compose application, using Let's Encrypt and Cloudflare DNS. No application code changes or custom Caddyfile needed.

## Quickstart

You need Docker/Compose 2.18+, Just, and a domain managed by Cloudflare.

**1. Add the proxy to your application.** Merge the service and volumes from [examples/compose.yaml](examples/compose.yaml) into your existing `compose.yaml`, and the recipes from [examples/justfile](examples/justfile) into your `justfile`. Keep your app service and share its Docker network with the proxy; the app does not need a published host port. Set `PROXY_COMPOSE_FILE` and `PROXY_SERVICE` at the top of your `justfile` to match your existing filename and service name.

If your project already has a deployment recipe, [integrate preparation into it](docs/reference.md#integrate-with-existing-project-recipes) instead of adding `proxy-up`. If replacing an existing proxy, first read [migration and recovery](docs/reference.md#installation-migration-and-recovery).

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

Replace `app:8080` with your app's Compose service name and port. Use a hostname in your zone and an IP reachable by your clients. The token needs **Zone DNS Edit** and **Zone Read**, scoped to that zone. Keep `.env` ignored by Git; [examples/gitignore.fragment](examples/gitignore.fragment) provides the rules.

See [examples/.env.example](examples/.env.example) for optional settings and the [configuration reference](docs/reference.md#configuration) for accepted values.

The image is public. You do not need a GitHub account or registry login. [Releases](https://github.com/lingmann/caddy-cloudflare-proxy/releases) list available versions, matching examples, and optional digest pins.

**3. Start it.** The standalone proxy recipes use `--no-deps` and do not start your app. Start the app through its normal workflow, then run:

```sh
chmod 600 .env
docker compose -f compose.yaml pull proxy
just proxy-plan    # Review DNS changes only; not a full deployment plan
just proxy-up      # Validate, apply DNS, and start/update only the proxy
just proxy-verify  # Verify trusted HTTPS and the expected application response
```

Direct Compose commands above use the default filename and service name; substitute your configured values when different. For an integrated deployment, run the commands above with your deployment recipe in place of `just proxy-up`. It must prepare before replacing either service and verify after both services start.

Open `https://app.example.com`. Certificates are issued and renewed automatically. No port 80 or public inbound access is needed for certificate issuance; clients still need a route to the proxy. HTTPS does not add application authentication.

Consumers do not need to build this image or install Python. For a different HTTPS port, set `CADDY_HTTPS_PORT` and include it in the URL.

## Common commands

| Command | Purpose |
| --- | --- |
| `just proxy-plan` | Preview Cloudflare DNS changes without writing. |
| `just proxy-up` | Prepare and start/update only the proxy. |
| `just proxy-verify` | Verify trusted HTTPS and the expected application response from Docker. |

`proxy-verify` expects HTTP 200 by default; configure the expected status/path for your app. Also verify HTTPS from an intended client without bypassing certificate checks. Use your project's existing recipes for logs, status, and stopping services. See [maintenance commands](docs/reference.md#maintenance-commands) for proxy-only operations, validation, DNS-only reconciliation, and custom Caddyfile reloads.

## Update the image

Retain the previous image reference and configuration for [recovery](docs/reference.md#recover-a-failed-replacement). Set `PROXY_IMAGE` in `.env` to the new release's version tag and use the matching recipes. Pull the image with `docker compose -f compose.yaml pull proxy` (substitute your filename and service), then run your deployment recipe and `just proxy-verify`. For the standalone workflow, the deployment recipe is `just proxy-up`.

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

See [maintainer instructions](docs/development.md) for dependency updates and publishing. Tests use fake credentials, an isolated network, and a disposable CA. They never use live Cloudflare or public ACME.
