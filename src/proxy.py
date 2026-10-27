#!/usr/bin/env python3
"""Container commands for a Caddy reverse proxy with Cloudflare DNS-01."""
import argparse
import ipaddress
import os
import re
import socket
import subprocess
import sys
from urllib.parse import urlsplit

import cloudflare_dns as dns
import operations

CADDYFILE = "/etc/caddy/Caddyfile"
PRODUCTION_CA = "https://acme-v02.api.letsencrypt.org/directory"
STAGING_CA = "https://acme-staging-v02.api.letsencrypt.org/directory"


def serving_config(env):
    """Validate values before they expand into Caddyfile syntax. Do not contact DNS."""
    required = ("PROXY_HOSTNAMES", "PROXY_UPSTREAM", "CLOUDFLARE_API_TOKEN")
    missing = [key for key in required if not env.get(key, "").strip()]
    if missing:
        raise dns.SyncError("Set " + ", ".join(missing) + " in the container environment.")
    dns.hostnames(env["PROXY_HOSTNAMES"])
    # The pinned Caddy provider expects a 40-character token. Reject bad tokens
    # before its validation errors can include the token value.
    if not re.fullmatch(r"[A-Za-z0-9_-]{40}", env["CLOUDFLARE_API_TOKEN"]):
        raise dns.SyncError("CLOUDFLARE_API_TOKEN must be a 40-character Cloudflare API token.")
    upstream = env["PROXY_UPSTREAM"]
    if not re.fullmatch(r"[A-Za-z0-9.:/\[\]_-]+", upstream):
        raise dns.SyncError("PROXY_UPSTREAM must be one host:port, optionally prefixed with http:// or https://.")
    try:
        parsed = urlsplit(upstream if "://" in upstream else "http://" + upstream)
        if (parsed.scheme not in ("http", "https") or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None
                or not parsed.hostname or parsed.port is None or not 1 <= parsed.port <= 65535):
            raise ValueError
        host = parsed.hostname
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif not re.fullmatch(r"[a-z0-9](?:[a-z0-9_.-]*[a-z0-9])?", host):
            raise ValueError
    except ValueError:
        raise dns.SyncError("PROXY_UPSTREAM must be one host:port, with port 1–65535 and no path or credentials.") from None
    if env.get("ACME_CA", PRODUCTION_CA) not in (PRODUCTION_CA, STAGING_CA):
        raise dns.SyncError("ACME_CA must be the Let's Encrypt production or staging directory URL.")


def caddy_command(command):
    return ["caddy", command, "--config", CADDYFILE, "--adapter", "caddyfile"]


def validate_caddy():
    result = subprocess.run(caddy_command("validate"), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=60)
    if result.returncode:
        # A custom Caddyfile may generate errors with interpolated values.
        output = result.stdout.replace(os.environ.get("CLOUDFLARE_API_TOKEN", "") or "\0", "[redacted]")
        print(output.rstrip(), file=sys.stderr)
        raise dns.SyncError("Caddy configuration validation failed; no DNS records changed.")


def sync(config, apply):
    with dns.locked(dns.LOCK_PATH):
        return dns.reconcile(config, dns.Cloudflare(config), apply=apply)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="Examples: proxy check; proxy dns-plan; proxy verify --port 9443")
    commands = parser.add_subparsers(dest="command")
    descriptions = {
        "check": "Validate serving settings and Caddy without DNS access.",
        "dns-plan": "Read Cloudflare and preview address changes without writes.",
        "dns-sync": "Reconcile application address records only.",
        "prepare": "Validate serving settings and Caddy, then reconcile DNS.",
        "serve": "Run Caddy as PID 1 without address reconciliation (default).",
        "reload": "Validate and reload Caddy using the existing environment.",
        "healthcheck": "Check only the local TCP 443 listener.",
        "preflight": "Validate resolved Compose JSON on stdin and select reuse or probe.",
        "bind-check": "Exit successfully after Docker has allocated the published port; start no listener.",
        "verify": "Verify DNS/connectivity, trusted HTTPS, and the expected application response.",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description,
                                      epilog=f"Example: proxy {name}" if name not in ("preflight", "verify") else
                                      ("Example: just _proxy-preflight" if name == "preflight" else "Example: proxy verify --port 9443"))
        if name == "preflight":
            command.add_argument("--running", default="", help="JSON from docker compose ps --status running --format json proxy")
        if name == "verify":
            port = command.add_mutually_exclusive_group()
            port.add_argument("--port", type=int, default=443, help="Client-facing HTTPS port (default: 443)")
            port.add_argument("--from-compose", action="store_true", help="Read the published port from resolved Compose JSON on stdin")
    args = parser.parse_args(argv)
    args.command = args.command or "serve"
    try:
        if args.command == "preflight":
            print(operations.preflight(operations.compose_input(sys.stdin), args.running))
            return 0
        if args.command == "bind-check":
            print("Docker allocated the configured host port. No listener or DNS operation started.")
            return 0
        if args.command == "verify":
            port = operations.published_port(operations.compose_input(sys.stdin))[1] if args.from_compose else args.port
            operations.verify(os.environ, port)
            return 0
        if args.command == "healthcheck":
            # Listener health only: this does not prove TLS issuance or upstream health.
            with socket.create_connection(("127.0.0.1", 443), timeout=3):
                pass
            return 0
        if args.command in ("dns-plan", "dns-sync"):
            sync(dns.Config.from_env(os.environ), apply=args.command == "dns-sync")
            return 0
        serving_config(os.environ)
        # Check all deployment inputs before provisioning Caddy or changing DNS.
        config = dns.Config.from_env(os.environ) if args.command == "prepare" else None
        validate_caddy()
        if args.command == "prepare":
            sync(config, apply=True)
        elif args.command == "serve":
            # Never reconcile address records here. Restarts must not depend on that API call.
            os.execvp("caddy", caddy_command("run"))
        elif args.command == "reload":
            os.execvp("caddy", caddy_command("reload") + ["--force"])
        else:
            print("Configuration valid. No DNS changes or certificate requests made.")
    except (dns.SyncError, OSError, subprocess.SubprocessError) as exc:
        if args.command == "healthcheck":
            return 1
        message = str(exc) if isinstance(exc, dns.SyncError) else "Cannot run Caddy or access the shared /data lock; check permissions and container dependencies."
        print(f"Proxy command failed: {message}", file=sys.stderr)
        if args.command in ("prepare", "dns-sync"):
            print("After an interrupted write, run dns-plan and reconcile again. Writes are not retried automatically.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
