---
name: caddy-cloudflare-proxy
description: Add or update HTTPS for Docker Compose applications with lingmann/caddy-cloudflare-proxy. Use when a project needs Caddy, Cloudflare DNS, DNS-01 certificates, or an HTTPS reverse proxy for public, LAN, or Tailscale access.
---

# Caddy Cloudflare Proxy

Use [the repository](https://github.com/lingmann/caddy-cloudflare-proxy) as the source of truth. Read its `README.md` and `examples/` for the selected release. Read `docs/reference.md` only for settings, networking, custom Caddyfiles, or DNS behavior not covered there.

## Consumer workflow

1. Confirm Docker/Compose 2.18+, Just, a Cloudflare-managed domain, the app's upstream, and the clients' network access. Select a published image version; do not build the image or install Python in the consumer project.
2. Inspect the application's existing recipes before merging `examples/compose.yaml` and `examples/justfile`. Expose only `proxy-plan`, `proxy-up`, and `proxy-verify`; keep `_proxy-*` helpers private. If an existing application recipe owns deployment, omit `proxy-up` and follow `docs/reference.md#integrate-with-existing-project-recipes`: run `_proxy-prepare` before replacing either service, then deploy and verify. Reuse existing logs, status, and stop recipes for both services; do not add duplicate wrappers. Preserve existing services and secrets. Share the app's Docker network; use its service name and port, not host `localhost`. Adjust the recipes' Compose filename if needed. Preserve `/data` and `/config` volumes.
3. Use `examples/.env.example` to configure hostnames, upstream, an explicit bind IP, a client-reachable DNS target, image version, and Cloudflare zone credentials. Scope the token to that zone with Zone DNS Edit and Zone Read. Merge `examples/gitignore.fragment`; keep `.env` private with mode `600`. Never print resolved Compose configuration or container environments: they contain the Cloudflare token. Use `docker compose -f compose.yaml config --quiet` for validation.
4. Run `just proxy-plan` and review proposed DNS changes; it is not a full deployment plan. With deployment authorization, use the existing integrated deployment recipe, or start the app normally and run `just proxy-up`. Keep preflight and preparation ahead of replacement, preserve the proxy recreation and bounded health wait, and verify afterward. A preparation failure leaves existing containers running but can partially update DNS; a deployment or verification failure does not roll back changes. A container restart does not reconcile application DNS.
5. Configure a safe verification path and the expected response, then run `just proxy-verify`. This checks from Docker; also verify HTTPS from an intended client without bypassing certificate checks, or report that check as outstanding. Use the project's existing operations recipes. For proxy-only operations, validation, DNS-only reconciliation, or custom Caddyfile reloads, use `docs/reference.md#maintenance-commands` rather than adding public recipes. Stopping must preserve application data and certificates.

Do not require public inbound access or port 80 for certificate issuance. Ensure clients can reach HTTPS. Do not treat HTTPS as application authentication.
