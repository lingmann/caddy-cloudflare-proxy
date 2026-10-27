#!/usr/bin/env python3
"""Check that a release version is unused on GitHub and GHCR; never write remotely."""
import argparse
import base64
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


class ReleaseError(Exception):
    """Publication cannot proceed safely."""


def validate_version(value):
    if len(value) > 128 or not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ReleaseError("Use a stable version such as v1.0.1, without leading zeros and at most 128 characters.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Sources:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, url, authorization, missing_codes=()):
        request = urllib.request.Request(url, headers={
            "User-Agent": "caddy-cloudflare-proxy-release-check",
            "Accept": ("application/vnd.github+json" if url.startswith("https://api.github.com/") else
                       "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/json"),
        })
        request.add_unredirected_header("Authorization", authorization)
        host = urllib.parse.urlsplit(url).hostname
        try:
            try:
                response = self.opener.open(request, timeout=30)
            except urllib.error.HTTPError as exc:
                with exc:
                    # Only a registry's explicit missing-manifest response proves absence.
                    if exc.code == 404 and missing_codes:
                        try:
                            errors = json.load(exc)["errors"]
                            if errors and all(error["code"] in missing_codes for error in errors):
                                return None
                        except (ValueError, KeyError, TypeError):
                            pass
                    raise ReleaseError(f"Release lookup at {host} failed (HTTP {exc.code}); check access or service availability.") from None
            with response as stream:
                value = json.load(stream)
                if value is None:
                    raise ReleaseError(f"Release lookup at {host} returned JSON null, not release metadata.")
                return value
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise ReleaseError(f"Release lookup at {host} failed; check network/TLS access.") from None
        except ValueError:
            raise ReleaseError(f"Release lookup at {host} returned invalid JSON.") from None


def check_available(version, repository, actor, token, sources=None):
    validate_version(version)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository) or not actor or not token:
        raise ReleaseError("Set GITHUB_REPOSITORY, GITHUB_ACTOR, and GH_TOKEN for the release check.")
    sources = sources or Sources()
    github_auth = "Bearer " + token
    base = f"https://api.github.com/repos/{repository}"
    # List endpoints must succeed. A failed lookup is never treated as an unused version.
    refs = sources.get(f"{base}/git/matching-refs/tags/{version}", github_auth)
    if not isinstance(refs, list) or any(not isinstance(ref, dict) or not isinstance(ref.get("ref"), str) for ref in refs):
        raise ReleaseError("GitHub returned an invalid tag list.")
    if any(ref["ref"] == f"refs/tags/{version}" for ref in refs):
        raise ReleaseError("The Git tag already exists; use a new version or recover the partial release.")
    page = 1
    while True:
        releases = sources.get(f"{base}/releases?per_page=100&page={page}", github_auth)
        if not isinstance(releases, list) or any(not isinstance(release, dict) or not isinstance(release.get("tag_name"), str) for release in releases):
            raise ReleaseError("GitHub returned an invalid release list.")
        if any(release["tag_name"] == version for release in releases):
            raise ReleaseError("The GitHub release already exists; use a new version.")
        if len(releases) < 100:
            break
        page += 1

    image = repository.lower()
    basic = base64.b64encode(f"{actor}:{token}".encode()).decode()
    auth = sources.get("https://ghcr.io/token?" + urllib.parse.urlencode({
        "service": "ghcr.io", "scope": f"repository:{image}:pull",
    }), "Basic " + basic)
    registry_token = auth.get("token") if isinstance(auth, dict) else None
    if not isinstance(registry_token, str) or not registry_token:
        raise ReleaseError("GHCR did not return a pull token; check package access.")
    manifest = sources.get(f"https://ghcr.io/v2/{image}/manifests/{version}", "Bearer " + registry_token,
                           missing_codes=("MANIFEST_UNKNOWN", "NAME_UNKNOWN"))
    if manifest is not None:
        raise ReleaseError("The image version already exists; use a new version or recover the partial release.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Examples: python3 scripts/check_release.py --version v1.0.1 --validate-only; "
        "python3 scripts/check_release.py --version v1.0.1 (requires GITHUB_REPOSITORY, GITHUB_ACTOR, GH_TOKEN)"))
    parser.add_argument("--version", required=True, help="new stable release version, such as v1.0.1")
    parser.add_argument("--validate-only", action="store_true", help="validate version syntax without credentials or network access")
    args = parser.parse_args(argv)
    try:
        validate_version(args.version)
        if not args.validate_only:
            check_available(args.version, os.environ.get("GITHUB_REPOSITORY", ""),
                            os.environ.get("GITHUB_ACTOR", ""), os.environ.get("GH_TOKEN", ""))
    except ReleaseError as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1
    print(f"{args.version}: " + ("valid version syntax" if args.validate_only else "version is unused on GitHub and GHCR"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
