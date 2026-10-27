# Project conventions

- `src/proxy.py` owns the container command interface and local serving validation. `src/cloudflare_dns.py` owns DNS planning and apply. Keep `serve` free of application address-record reconciliation.
- Preserve the official Caddy builder/runtime digest pins and explicit Caddy/provider versions unless a change is requested. Keep compilation before project source copies, and keep multiarchitecture builds on GitHub. Runtime dependencies belong in the image; consumer projects need only Docker/Compose, Just, and environment configuration.
- Run `just check`, `just test`, and `just test-integration`. Tests use fake credentials and isolated resources. Never use a real `.env`, Cloudflare account, or public ACME service for tests.
- Keep preparation failure ahead of container replacement. Keep Caddy as PID 1 and preserve `/data`, `/config`, the shared lock, redacted errors, and conservative DNS conflict handling.
- Consumer examples live in `examples/`. Update the companion skill in `~/code/skills/skills/caddy-cloudflare-proxy` when changing those interfaces. Do not publish images or create remote repositories without authorization.
