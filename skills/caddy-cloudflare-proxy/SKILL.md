---
name: caddy-cloudflare-proxy
description: Add or update HTTPS for Docker Compose applications with lingmann/caddy-cloudflare-proxy. Use when a project needs Caddy, Cloudflare DNS, DNS-01 certificates, or an HTTPS reverse proxy for public, LAN, or Tailscale access.
---

# Caddy Cloudflare Proxy

Use [the repository](https://github.com/lingmann/caddy-cloudflare-proxy) as the source of truth. Read its `README.md` and `examples/` for the selected release. Read `docs/reference.md` only for settings, networking, custom Caddyfiles, or DNS behavior not covered there.

## Consumer workflow

1. Confirm Docker/Compose 2.18+, Just, a Cloudflare-managed domain, the app's upstream, and the clients' network access. Select a published image version; do not build the image or install Python in the consumer project.
2. Merge `examples/compose.yaml` and `examples/justfile` into the application. Preserve existing services and secrets. Share the app's Docker network; use its service name and port, not host `localhost`. Adjust the recipes' Compose filename if needed. Preserve `/data` and `/config` volumes.
3. Use `examples/.env.example` to configure hostnames, upstream, an explicit bind IP, a client-reachable DNS target, image version, and Cloudflare zone credentials. Scope the token to that zone with Zone DNS Edit and Zone Read. Merge `examples/gitignore.fragment`; keep `.env` private with mode `600`. Never print resolved Compose configuration or container environments: they contain the Cloudflare token. Use `docker compose -f compose.yaml config --quiet` for validation.
4. Run `just proxy-check`, then `just dns-plan`. Review proposed DNS changes before deployment. With deployment authorization, start the app normally and run `just proxy-up`. Keep this recipe's preparation-before-replacement sequence; a container restart does not reconcile application DNS.
5. Run `just proxy-verify` with a safe path and the expected response. This checks from Docker; also verify HTTPS from an intended client without bypassing certificate checks, or report that check as outstanding. Check `just proxy-status` and `just proxy-logs` if needed. Use `just proxy-stop` to preserve the app and certificates.

Do not require public inbound access or port 80 for certificate issuance. Ensure clients can reach HTTPS. Do not treat HTTPS as application authentication.
