---
name: caddy-cloudflare-proxy
description: Add or update HTTPS for Docker Compose applications with lingmann/caddy-cloudflare-proxy. Use when a project needs Caddy, Cloudflare DNS, DNS-01 certificates, or an HTTPS reverse proxy for public, LAN, or Tailscale access.
---

# Caddy Cloudflare Proxy

Use [the repository](https://github.com/lingmann/caddy-cloudflare-proxy) as the source of truth. Read `README.md` and `examples/` for the selected image release. Read `docs/reference.md` for migration/recovery, custom Caddyfiles, networking, or settings not covered there.

## Choose the installation path

- **New installation:** confirm Docker/Compose 2.18+, Just, a Cloudflare-managed domain, the upstream, and client network access. Select a published image; do not build it or install Python in the consumer project.
- **Migration:** also inventory existing authentication, routing, hostnames, bindings, service names, Compose project identity, and actual certificate volumes before replacing configuration. Preserve application security policy in its Caddyfile. Follow `docs/reference.md#installation-migration-and-recovery`; retain the previous image and configuration, and record cutover, verification, and recovery commands before changing services.

## Consumer workflow

1. Inspect existing recipes, then merge `examples/compose.yaml` and the required recipes from `examples/justfile`. Set the Just variables `PROXY_COMPOSE_FILE` and `PROXY_SERVICE` to match the project; do not rename services unnecessarily. Expose only `proxy-plan`, `proxy-up`, and `proxy-verify`. If deployment already has a recipe, omit `proxy-up`, copy the settings and all private helpers, and run `_proxy-prepare` before replacing either service. Reuse existing logs, status, and stop recipes.
2. Preserve services, secrets, project identity, and `/data` and `/config` volumes. Share the application network; use its service name and port, not container `localhost`. Preserve existing Compose dependencies where needed; the recipes use `--no-deps`, so start the application through its own workflow. Never mount the Docker socket.
3. Configure `examples/.env.example`: hostnames, upstream, explicit bind IP, client-reachable DNS target, image version, and zone credentials. Scope the token to that zone with DNS Edit and Zone Read. Merge `examples/gitignore.fragment`; keep `.env` private with mode `600`. Service selection reduces unnecessary input but does not isolate credentials. Resolved Compose JSON may contain credentials, including shared application settings. Never log it or container environments. Use Compose `config --quiet` for validation.
4. Pull the proxy image and build or pull application images before cutover. Validate configuration, review `just proxy-plan` (DNS only), and deploy only with authorization. Keep preflight and preparation before service replacement, preserve forced proxy recreation and the bounded health wait, and verify afterward. Preparation failure leaves existing containers running but can partially update DNS. Later failure does not roll back deployment or DNS.
5. Configure a safe verification path and expected response. Run `just proxy-verify`, then check from an intended client without bypassing certificate validation, or report that check as outstanding. Use `docs/reference.md#maintenance-commands` for occasional operations rather than adding public recipes. Preserve application data and certificates when stopping.

Prefer supported interfaces. Report missing capabilities instead of silently creating wrappers or another deployment framework. Keep routing and authentication policy in the application. DNS-01 needs no public inbound access or port 80; clients still need a route to HTTPS. HTTPS is not application authentication.
