#!/usr/bin/env python3
"""Refresh this project's stable dependency pins without building or deploying."""
import argparse
import difflib
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/ci.yml"
MANAGED = ("Dockerfile", "tests/integration.sh", WORKFLOW)
DIGEST = r"sha256:[0-9a-f]{64}"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"
ACTION = re.compile(r"^([ \t]*(?:-[ \t]+)?uses:[ \t]+)([\w.-]+/[\w.-]+)@(v[0-9]+(?:\.[0-9]+){0,2})([ \t]*(?:#.*)?)$", re.M)


class UpdateError(Exception):
    """An update cannot be resolved or applied safely."""


def version(value, partial=False):
    pattern = r"v?([0-9]+(?:\.[0-9]+){0,2})" if partial else rf"v?({VERSION})"
    match = re.fullmatch(pattern, value) if isinstance(value, str) else None
    if not match:
        raise UpdateError("Expected a numeric stable version, not a prerelease or arbitrary ref.")
    parts = tuple(map(int, match[1].split(".")))
    return parts + (0,) * (3 - len(parts))


def no_downgrade(name, current, candidate):
    if version(candidate, partial=True) < version(current, partial=True):
        raise UpdateError(f"{name}: refusing downgrade from {current} to {candidate}.")


def single(pattern, text, label):
    matches = list(re.finditer(pattern, text, re.M))
    if len(matches) != 1:
        raise UpdateError(f"Expected exactly one {label}; review the updater after changing the file layout.")
    return matches[0]


def replace_once(text, old, new, label):
    if text.count(old) != 1:
        raise UpdateError(f"Cannot identify the expected {label}; no files changed.")
    return text.replace(old, new, 1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Sources:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())
        self.releases = {}
        self.action_tags = set()

    def get(self, url, headers=None):
        headers = {"User-Agent": "caddy-cloudflare-proxy-updater", **(headers or {})}
        host = urllib.parse.urlsplit(url).hostname
        if host == "api.github.com" and os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
        authorization = headers.pop("Authorization", None)
        request = urllib.request.Request(url, headers=headers)
        if authorization:
            request.add_unredirected_header("Authorization", authorization)
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.read(), response.headers
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise UpdateError(f"Metadata request to {host} failed (HTTP {status}); check availability, access, or rate limits.") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise UpdateError(f"Metadata request to {host} failed; check network/TLS access.") from None

    def json(self, url):
        try:
            return json.loads(self.get(url)[0])
        except ValueError:
            raise UpdateError("A dependency source returned invalid JSON.") from None

    def release(self, repo):
        if repo not in self.releases:
            result = self.json(f"https://api.github.com/repos/{repo}/releases/latest")
            if not isinstance(result, dict) or result.get("draft") is not False or result.get("prerelease") is not False:
                raise UpdateError(f"{repo}: expected a published stable release.")
            tag = result.get("tag_name")
            version(tag)
            self.releases[repo] = tag
        return self.releases[repo]

    def verify_action_tag(self, repo, tag):
        if (repo, tag) not in self.action_tags:
            result = self.json(f"https://api.github.com/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='')}")
            target = result.get("object", {}) if isinstance(result, dict) else {}
            if (not isinstance(result, dict) or result.get("ref") != f"refs/tags/{tag}"
                    or not isinstance(target, dict) or target.get("type") not in ("commit", "tag")
                    or not re.fullmatch(r"[0-9a-f]{40}", target.get("sha", ""))):
                raise UpdateError(f"{repo}: proposed action tag {tag} is not available.")
            self.action_tags.add((repo, tag))

    def provider_version(self):
        # This module publishes Go version tags, not GitHub Releases.
        body, _ = self.get("https://proxy.golang.org/github.com/caddy-dns/cloudflare/@v/list")
        candidates = [tag for tag in body.decode("utf-8").splitlines() if re.fullmatch(rf"v{VERSION}", tag)]
        if not candidates:
            raise UpdateError("Cloudflare provider: no stable Go module version found.")
        return max(candidates, key=version)

    def image_digest(self, tag):
        auth = self.json("https://auth.docker.io/token?" + urllib.parse.urlencode({
            "service": "registry.docker.io", "scope": "repository:library/caddy:pull",
        }))
        token = auth.get("token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not token:
            raise UpdateError("Docker Hub did not return a pull token.")
        body, headers = self.get(f"https://registry-1.docker.io/v2/library/caddy/manifests/{tag}", {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
        })
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if headers.get("Docker-Content-Digest") != digest:
            raise UpdateError(f"caddy:{tag}: registry manifest checksum mismatch.")
        try:
            platforms = {(m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture"))
                         for m in json.loads(body)["manifests"]}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UpdateError(f"caddy:{tag}: expected a multi-platform image index.") from None
        if not {("linux", "amd64"), ("linux", "arm64")} <= platforms:
            raise UpdateError(f"caddy:{tag}: both linux/amd64 and linux/arm64 must be published before updating.")
        return digest


def prepare(original, sources):
    updated = dict(original)
    dockerfile = original["Dockerfile"]
    builder = single(rf"^FROM --platform=\$BUILDPLATFORM caddy:({VERSION})-builder@({DIGEST}) AS builder$", dockerfile, "official builder pin")
    runtime = single(rf"^FROM caddy:({VERSION})@({DIGEST}) AS runtime$", dockerfile, "official runtime pin")
    compile_pin = single(rf"^    xcaddy build v({VERSION}) --with github.com/caddy-dns/cloudflare@v({VERSION})$", dockerfile, "Caddy/provider build pin")
    current, provider = compile_pin[1], compile_pin[2]
    if builder[1] != current or runtime[1] != current:
        raise UpdateError("Caddy builder, runtime, and compiled versions differ; align them before updating.")
    latest = sources.release("caddyserver/caddy").removeprefix("v")
    latest_provider = sources.provider_version().removeprefix("v")
    no_downgrade("Caddy", current, latest)
    no_downgrade("Cloudflare provider", provider, latest_provider)
    # Refresh digests even if the Caddy version did not change (OS/toolchain fixes).
    builder_digest = sources.image_digest(latest + "-builder")
    runtime_digest = sources.image_digest(latest)
    dockerfile = dockerfile.replace(builder[0], f"FROM --platform=$BUILDPLATFORM caddy:{latest}-builder@{builder_digest} AS builder")
    dockerfile = dockerfile.replace(runtime[0], f"FROM caddy:{latest}@{runtime_digest} AS runtime")
    dockerfile = dockerfile.replace(compile_pin[0], f"    xcaddy build v{latest} --with github.com/caddy-dns/cloudflare@v{latest_provider}")
    updated["Dockerfile"] = dockerfile

    path = "tests/integration.sh"
    for old, new in ((f'"${{version%% *}}" = v{current}', f'"${{version%% *}}" = v{latest}'),
                     (f"'dns.providers.cloudflare v{provider}'", f"'dns.providers.cloudflare v{latest_provider}'")):
        updated[path] = replace_once(updated[path], old, new, "integration-test version reference")

    text = original[WORKFLOW]
    uses = re.findall(r"^[ \t]*(?:-[ \t]+)?uses:[ \t]+.*$", text, re.M)
    if len(uses) != len(list(ACTION.finditer(text))):
        raise UpdateError(f"{WORKFLOW}: unsupported action ref; review the updater rather than leaving a dependency behind.")
    def action(match):
        repo, old = match[2], match[3]
        candidate = sources.release(repo)
        no_downgrade(repo, old, candidate)
        # Preserve the existing precision: @v4 stays a rolling major tag.
        width = len(old.removeprefix("v").split("."))
        new = "v" + ".".join(candidate.removeprefix("v").split(".")[:width])
        sources.verify_action_tag(repo, new)
        return match[1] + repo + "@" + new + match[4]
    updated[WORKFLOW] = ACTION.sub(action, text)
    return updated


def checkout_paths(root):
    root = root.resolve()
    for path in MANAGED:
        target = root / path
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise UpdateError("Managed files must stay inside this checkout, without file symlinks.")
    return root


def update(root=ROOT, sources=None, dry_run=False):
    root = checkout_paths(root)
    original = {path: (root / path).read_text() for path in MANAGED}
    updated = prepare(original, sources or Sources())
    changed = [path for path in MANAGED if original[path] != updated[path]]
    for path in changed:
        if not dry_run:
            (root / path).write_text(updated[path])
        print(("Would update " if dry_run else "Updated ") + path)
        if dry_run:
            print("".join(difflib.unified_diff(original[path].splitlines(keepends=True), updated[path].splitlines(keepends=True),
                                               fromfile=path, tofile=path)), end="")
    if changed:
        print("No build or deployment performed. Review git diff, then run just check, just test, and just test-integration."
              if not dry_run else "Dry run: no files changed. Run just update-deps to apply fresh lookups.")
    else:
        print("All dependency pins are current; no files changed.")
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="Examples: just update-deps; python3 scripts/update_deps.py --dry-run")
    parser.add_argument("--dry-run", action="store_true", help="resolve updates and list affected files without writing")
    args = parser.parse_args(argv)
    try:
        update(dry_run=args.dry_run)
    except (UpdateError, OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, UpdateError) else "Cannot read dependency metadata or write managed files; check their format and permissions."
        print("Dependency update failed: " + message, file=sys.stderr)
        print("Review git diff before retrying; any written changes remain in the working tree.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
