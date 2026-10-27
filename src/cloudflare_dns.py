#!/usr/bin/env python3
"""Reconcile DNS-only Cloudflare A, AAAA, or CNAME records using the standard library."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


API_URL = "https://api.cloudflare.com/client/v4"
TTL = 300
LOCK_PATH = Path("/data/.dns-sync.lock")


class SyncError(Exception):
    """A safe-to-display validation or Cloudflare error."""


def hostname(value):
    name = value.lower().removesuffix(".")
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise SyncError("Hostnames must be full DNS names (ASCII/punycode), without URLs or wildcards.")
    return name


def hostnames(value):
    raw_names = value.split(" ")
    if any(name.endswith(".") for name in raw_names):
        raise SyncError("PROXY_HOSTNAMES must not contain a trailing dot; remove it from the setting.")
    # Single spaces separate names; reject other whitespace and Caddyfile syntax.
    names = tuple(hostname(name) for name in raw_names)
    if len(set(names)) != len(names):
        raise SyncError("PROXY_HOSTNAMES must contain distinct DNS names separated by single spaces.")
    return names


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    zone_id: str
    names: tuple
    target: str
    record_type: str

    @classmethod
    def from_env(cls, env):
        keys = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID", "PROXY_HOSTNAMES", "PROXY_DNS_TARGET")
        missing = [key for key in keys if not env.get(key, "").strip()]
        if missing:
            raise SyncError("Set " + ", ".join(missing) + " in .env or the environment.")
        token = env["CLOUDFLARE_API_TOKEN"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise SyncError("CLOUDFLARE_API_TOKEN contains invalid characters.")
        zone_id = env["CLOUDFLARE_ZONE_ID"]
        if not re.fullmatch(r"[a-fA-F0-9]{32}", zone_id):
            raise SyncError("CLOUDFLARE_ZONE_ID must be a 32-character hexadecimal zone ID.")
        names = hostnames(env["PROXY_HOSTNAMES"])
        value = env["PROXY_DNS_TARGET"]
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            # Do not reinterpret malformed IPv4 literals as hostname targets.
            if re.fullmatch(r"[0-9.]+", value):
                raise SyncError("PROXY_DNS_TARGET looks like an invalid IP address.") from None
            try:
                target = hostname(value)
            except SyncError:
                raise SyncError("PROXY_DNS_TARGET must be an IPv4/IPv6 address or full DNS hostname, without a URL, port, or CIDR.") from None
            if target in names:
                raise SyncError("PROXY_DNS_TARGET must not equal any proxy hostname (CNAME loop).")
            return cls(token, zone_id.lower(), names, target, "CNAME")
        if (ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local
                or str(ip) == "255.255.255.255" or getattr(ip, "scope_id", None)):
            raise SyncError("PROXY_DNS_TARGET must be a reachable unicast address, not a local-only address.")
        return cls(token, zone_id.lower(), names, str(ip), "A" if ip.version == 4 else "AAAA")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API token to a redirect target.
        return None


class Cloudflare:
    def __init__(self, config):
        self.config = config
        self.base = f"{API_URL}/zones/{config.zone_id}"
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path="", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.config.token}",
                     "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not print response bodies, request headers, or exception URLs.
            code = exc.code
            exc.close()
            raise SyncError(f"Cloudflare {method} failed (HTTP {code}); check token/zone permissions or retry later.") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise SyncError(f"Cloudflare {method} failed: network, TLS, timeout, or invalid JSON response.") from None
        if not isinstance(body, dict) or body.get("success") is not True or "result" not in body:
            raise SyncError(f"Cloudflare {method} did not return a successful API result.")
        return body

    def validate_zone(self):
        zone = self.request("GET")["result"]
        if not isinstance(zone, dict) or not isinstance(zone.get("name"), str):
            raise SyncError("Cloudflare returned invalid zone metadata.")
        zone_name = hostname(zone["name"])
        if any(name != zone_name and not name.endswith("." + zone_name) for name in self.config.names):
            raise SyncError("All hostnames must belong to CLOUDFLARE_ZONE_ID; no records changed.")
        return zone_name

    def records(self, name):
        records = []
        page = 1
        pagination = None
        while True:
            query = urllib.parse.urlencode({"name.exact": name, "page": page, "per_page": 100})
            body = self.request("GET", "/dns_records?" + query)
            result = body["result"]
            info = body.get("result_info", {})
            if (not isinstance(result, list) or not isinstance(info, dict)
                    or any(type(info.get(key)) is not int for key in
                           ("page", "per_page", "count", "total_pages", "total_count"))):
                raise SyncError("Cloudflare returned invalid record pagination; no records changed.")
            total_pages, total_count, per_page = (info[key] for key in
                                                 ("total_pages", "total_count", "per_page"))
            if (info["page"] != page or not 1 <= per_page <= 1000
                    or not 0 <= total_pages <= 1000 or total_count < 0
                    or info["count"] != len(result) or len(result) > per_page):
                raise SyncError("Cloudflare returned inconsistent record pagination; no records changed.")
            expected_pages = (total_count + per_page - 1) // per_page
            if total_pages not in ({0, 1} if total_count == 0 else {expected_pages}):
                raise SyncError("Cloudflare returned incomplete record pagination; no records changed.")
            current_pagination = (total_pages, total_count, per_page)
            if pagination is not None and pagination != current_pagination:
                raise SyncError("Cloudflare records changed while reading pages; retry the operation.")
            pagination = current_pagination
            for record in result:
                if (not isinstance(record, dict) or not isinstance(record.get("name"), str)
                        or hostname(record["name"]) != name):
                    raise SyncError("Cloudflare returned records outside the exact requested hostname.")
            records.extend(result)
            if page >= total_pages:
                if len(records) != total_count:
                    raise SyncError("Cloudflare returned an incomplete record list; no records changed.")
                return records
            page += 1


@dataclass(frozen=True)
class Change:
    action: str
    name: str
    record_id: str | None
    payload: dict


def plan_record(config, name, records):
    if config.record_type == "CNAME":
        # A CNAME must stand alone: never remove TXT, MX, or other records to make room.
        if any(record.get("type") != "CNAME" for record in records):
            raise SyncError(f"{name}: CNAME conflicts with existing records; resolve them manually before syncing.")
        candidates = records
    else:
        if any(record.get("type") in ("CNAME", "NS") for record in records):
            raise SyncError(f"{name}: conflicting CNAME or NS; resolve it manually before syncing.")
        candidates = [record for record in records if record.get("type") in ("A", "AAAA")]
    if len(candidates) > 1:
        raise SyncError(f"{name}: multiple address or CNAME records; resolve duplicates/opposite-family records manually.")
    desired = {"type": config.record_type, "name": name, "content": config.target,
               "proxied": False, "ttl": TTL}
    if not candidates:
        return Change("create", name, None, desired)
    current = candidates[0]
    if current["type"] != config.record_type:
        raise SyncError(f"{name}: existing {current['type']} conflicts with {config.record_type}; resolve it manually.")
    record_id = current.get("id", "")
    if not isinstance(record_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", record_id):
        raise SyncError(f"{name}: Cloudflare returned an invalid record ID.")
    try:
        content = current.get("content")
        if config.record_type == "CNAME":
            current_target = hostname(content) if isinstance(content, str) else None
        else:
            current_target = str(ipaddress.ip_address(content))
    except (ValueError, SyncError):
        current_target = None
    # PATCH only the fields we own. Preserve comments, tags, and other settings.
    patch = {}
    if current_target != config.target:
        patch["content"] = config.target
    if current.get("proxied") is not False:
        patch["proxied"] = False
    if current.get("ttl") != TTL:
        patch["ttl"] = TTL
    return Change("update" if patch else "unchanged", name, record_id, patch)


def reconcile(config, api, apply=False):
    zone_name = api.validate_zone()
    # Check all names, ancestor delegations, and every page before any mutation.
    records = {name: api.records(name) for name in config.names}
    ancestors = set()
    for name in config.names:
        labels = name.split(".")
        for index in range(1, len(labels) - len(zone_name.split("."))):
            ancestors.add(".".join(labels[index:]))
    for ancestor in sorted(ancestors):
        if ancestor not in records:
            records[ancestor] = api.records(ancestor)
        if any(record.get("type") == "NS" for record in records[ancestor]):
            raise SyncError(f"{ancestor}: delegated subzone; use its authoritative Cloudflare zone instead. No records changed.")
    changes = [plan_record(config, name, records[name]) for name in config.names]
    for change in changes:
        fields = ", ".join(change.payload) if change.action == "update" else ""
        suffix = f"; changed fields: {fields}" if fields else ""
        print(f"{change.action}: {config.record_type} {change.name} -> {config.target} (DNS only, TTL {TTL}{suffix})")
    if not apply:
        print("Plan only: no records changed.")
        return changes
    for change in changes:
        if change.action == "create":
            api.request("POST", "/dns_records", change.payload)
        elif change.action == "update":
            api.request("PATCH", f"/dns_records/{change.record_id}", change.payload)
        if change.action != "unchanged":
            print(f"Applied {change.action}: {change.name}")
    print("DNS is up to date.")
    return changes


@contextmanager
def locked(path):
    # Keep the file: unlinking a lock file can let waiters lock different inodes.
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("Another DNS operation holds the shared volume lock; retry after it finishes.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="show proposed changes without writing DNS")
    mode.add_argument("--apply", action="store_true", help="create missing records and patch changed fields")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env(os.environ)
        with locked(LOCK_PATH):
            reconcile(config, Cloudflare(config), apply=args.apply)
    except (SyncError, OSError) as exc:
        message = str(exc) if isinstance(exc, SyncError) else "Cannot open or lock /data/.dns-sync.lock; mount a writable shared /data volume."
        print(f"DNS sync failed: {message}", file=sys.stderr)
        if args.apply:
            print("If a write was interrupted, rerun dns-plan/dns-sync to reconcile; writes are not retried automatically.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
