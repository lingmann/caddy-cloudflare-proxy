"""Offline tests: no real credentials, DNS changes, or network calls."""
import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cloudflare_dns as dns

ENV = {
    "CLOUDFLARE_API_TOKEN": "fake-token-never-sent",
    "CLOUDFLARE_ZONE_ID": "a" * 32,
    "PROXY_HOSTNAMES": "service.example.com api.example.com",
    "PROXY_DNS_TARGET": "100.64.0.10",
}
NAMES = ENV["PROXY_HOSTNAMES"].split()


def address(name=NAMES[0], **overrides):
    return {"id": "b" * 32, "type": "A", "name": name,
            "content": ENV["PROXY_DNS_TARGET"], "ttl": 300, "proxied": False, **overrides}


def listing(records, page=1, per_page=100, total_count=None):
    total = len(records) if total_count is None else total_count
    return {"success": True, "result": records, "result_info": {
        "page": page, "per_page": per_page, "count": len(records),
        "total_count": total, "total_pages": max(1, (total + per_page - 1) // per_page)}}


class FakeCloudflare(dns.Cloudflare):
    def __init__(self, config, records=()):
        self.config = config
        self.state = copy.deepcopy(list(records))
        self.calls = []
        self.fail_write = None
        self.fail_after_write = None

    def request(self, method, path="", payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path == "":
            return {"success": True, "result": {"name": "example.com"}}
        if method == "GET":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            name = query["name.exact"][0]
            page, size = int(query["page"][0]), int(query["per_page"][0])
            records = [r for r in self.state if r["name"] == name]
            return listing(copy.deepcopy(records[(page - 1) * size:page * size]),
                           page, size, len(records))
        if self.fail_write == len(self.writes):
            raise dns.SyncError("Simulated write failure")
        if method == "POST":
            record = {"id": f"{len(self.state) + 1:032x}", **payload}
            self.state.append(record)
        elif method == "PATCH":
            record = next(r for r in self.state if r["id"] == path.rsplit("/", 1)[-1])
            record.update(payload)
        else:
            raise AssertionError(method)
        if self.fail_after_write == len(self.writes):
            raise dns.SyncError("Simulated lost response after accepted write")
        return {"success": True, "result": copy.deepcopy(record)}

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class ConfigTests(unittest.TestCase):
    def test_one_or_many_names(self):
        for count in (1, 2, 5):
            names = [f"service{i}.example.com" for i in range(count)]
            config = dns.Config.from_env(ENV | {"PROXY_HOSTNAMES": " ".join(names)})
            self.assertEqual(config.names, tuple(names))
            self.assertNotIn(ENV["CLOUDFLARE_API_TOKEN"], repr(config))

    def test_normalization(self):
        config = dns.Config.from_env(ENV | {"PROXY_HOSTNAMES": "Service.Example.Com",
                                           "PROXY_DNS_TARGET": "fd00:0:0:0:0:0:0:1"})
        self.assertEqual(config.names, (NAMES[0],))
        self.assertEqual((config.record_type, config.target), ("AAAA", "fd00::1"))
        config = dns.Config.from_env(ENV | {"PROXY_DNS_TARGET": "Host.Example.Net."})
        self.assertEqual((config.record_type, config.target), ("CNAME", "host.example.net"))

    def test_missing_and_invalid_inputs_fail_before_network(self):
        cases = {key: [""] for key in ENV}
        cases["PROXY_HOSTNAMES"] += ["*.example.com", "https://example.com", "example.com:443",
            "localhost", "-bad.example.com", "bad_.example.com", "a" * 64 + ".example.com",
            "service.example.com.", "api.example.com service.example.com.",
            "service.example.com SERVICE.EXAMPLE.COM", "service.example.com,api.example.com",
            "service.example.com\napi.example.com", "service.example.com\tapi.example.com",
            " service.example.com", "service.example.com ", "a.example.com  b.example.com"]
        cases["PROXY_DNS_TARGET"] += ["not-an-ip", "100.64.0.1/32", "100.64.0.1:443", "0.0.0.0",
            "::", "127.0.0.1", "224.0.0.1", "255.255.255.255", "fe80::1", "fd00::1%en0", "999.1.1.1",
            "100.064.0.1", "100.64.0.1.", "100.64.0", "https://host.example.com",
            "host.example.com:443", "*.example.com", "host.example.com/path",
            "bad_.example.com", "host.example.com..", " host.example.com", "host.example.com\n",
            "SERVICE.EXAMPLE.COM.", "API.EXAMPLE.COM"]
        cases["CLOUDFLARE_ZONE_ID"] += ["../zone", "wrong", "g" * 32]
        cases["CLOUDFLARE_API_TOKEN"] += ["token\nsecret", " token", "x\r\nHeader:y"]
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    with patch.dict(os.environ, ENV | {key: value}, clear=True):
                        with patch.object(dns, "Cloudflare") as api, contextlib.redirect_stderr(io.StringIO()):
                            self.assertEqual(dns.main(["--apply"]), 1)
                        api.assert_not_called()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.config = dns.Config.from_env(ENV)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_plan_and_repeat_apply_for_each_type_and_name_count(self):
        for count in (1, 2, 5):
            for target in ("192.168.1.10", "fd00::1", "host.example.net"):
                config = dns.Config.from_env(ENV | {"PROXY_DNS_TARGET": target,
                    "PROXY_HOSTNAMES": " ".join(f"app{i}.example.com" for i in range(count))})
                api = FakeCloudflare(config)
                with patch("socket.getaddrinfo", side_effect=AssertionError("Must not resolve target")):
                    dns.reconcile(config, api)
                    self.assertEqual(api.writes, [])
                    dns.reconcile(config, api, apply=True)
                    self.assertEqual(len(api.writes), count)
                    for record in api.state:
                        self.assertEqual(record["type"], config.record_type)
                        self.assertEqual(record["ttl"], 300)
                        self.assertIs(record["proxied"], False)
                    api.calls.clear()
                    dns.reconcile(config, api, apply=True)
                    self.assertEqual(len(api.calls), count + 1)
                    self.assertEqual(api.writes, [])

    def test_patch_changed_fields_and_preserve_metadata(self):
        for target, kind in (("100.64.0.10", "A"), ("host.example.net", "CNAME")):
            config = dns.Config.from_env(ENV | {"PROXY_DNS_TARGET": target})
            old = "192.168.1.11" if kind == "A" else "old.example.net"
            api = FakeCloudflare(config, [address(type=kind, content=old, comment="keep", tags=["app:test"]),
                address(NAMES[1], id="c" * 32, type=kind, content=target, ttl=1, proxied=True)])
            dns.reconcile(config, api, apply=True)
            self.assertEqual([c[2] for c in api.writes], [{"content": target}, {"proxied": False, "ttl": 300}])
            self.assertEqual(api.state[0]["comment"], "keep")
            self.assertEqual(api.state[0]["tags"], ["app:test"])
            self.assertTrue(all(c[0] == "PATCH" for c in api.writes))

    def test_equivalent_existing_targets_unchanged(self):
        for target, content, kind in (("fd00::1", "fd00:0:0:0:0:0:0:1", "AAAA"),
                                       ("host.example.net", "HOST.EXAMPLE.NET.", "CNAME")):
            config = dns.Config.from_env(ENV | {"PROXY_DNS_TARGET": target})
            change = dns.plan_record(config, NAMES[0], [address(type=kind, content=content)])
            self.assertEqual(change.action, "unchanged")

    def test_conflict_on_last_name_prevents_all_writes(self):
        for target, types in (("100.64.0.10", ["CNAME", "NS", "AAAA"]),
                              ("host.example.net", ["A", "AAAA", "TXT", "MX", "NS", "CAA", "SRV", "HTTPS"])):
            config = dns.Config.from_env(ENV | {"PROXY_DNS_TARGET": target})
            for kind in types:
                api = FakeCloudflare(config, [address(NAMES[-1], type=kind)])
                with self.subTest(target=target, kind=kind), self.assertRaises(dns.SyncError):
                    dns.reconcile(config, api, apply=True)
                self.assertEqual(api.writes, [])
        for records in ([address(NAMES[1]), address(NAMES[1], id="c" * 32)],
                        [address(NAMES[1], id="../../bad")],
                        [address(NAMES[1]), address(NAMES[1], type="AAAA")]):
            api = FakeCloudflare(self.config, records)
            with self.assertRaises(dns.SyncError):
                dns.reconcile(self.config, api, apply=True)
            self.assertEqual(api.writes, [])

    def test_zone_boundary(self):
        for name in ("badexample.com", "other.example.net"):
            config = dns.Config.from_env(ENV | {"PROXY_HOSTNAMES": NAMES[0] + " " + name})
            api = FakeCloudflare(config)
            with self.assertRaises(dns.SyncError):
                dns.reconcile(config, api, apply=True)
            self.assertEqual(len(api.calls), 1)
            self.assertEqual(api.writes, [])

    def test_ancestor_delegation_prevents_all_writes(self):
        config = dns.Config.from_env(ENV | {"PROXY_HOSTNAMES": "service.example.com app.deep.delegated.example.com"})
        for ancestor in ("deep.delegated.example.com", "delegated.example.com"):
            api = FakeCloudflare(config, [address(ancestor, type="NS", content="ns.other.net")])
            with self.subTest(ancestor=ancestor), self.assertRaisesRegex(dns.SyncError, "delegated subzone"):
                dns.reconcile(config, api, apply=True)
            self.assertEqual(api.writes, [])

    def test_ancestor_reads_are_reused_and_apex_is_not_a_delegation(self):
        config = dns.Config.from_env(ENV | {"PROXY_HOSTNAMES": "app.sub.example.com api.sub.example.com"})
        api = FakeCloudflare(config, [address("example.com", type="NS", content="ns.cloudflare.com")])
        dns.reconcile(config, api, apply=True)
        self.assertEqual(len([c for c in api.calls if c[0] == "GET"]), 4)
        self.assertEqual(len(api.writes), 2)

    def test_acme_txt_other_records_and_old_names_untouched(self):
        records = [address(type="TXT", content="keep"),
            address("_acme-challenge.service.example.com", type="TXT", content="challenge"),
            address("old.example.com")]
        api = FakeCloudflare(self.config, records)
        dns.reconcile(self.config, api, apply=True)
        self.assertEqual(api.state[:3], records)

    def test_partial_failure_rerun_creates_only_missing_record(self):
        api = FakeCloudflare(self.config)
        api.fail_write = 2
        with self.assertRaises(dns.SyncError):
            dns.reconcile(self.config, api, apply=True)
        self.assertEqual(len(api.state), 1)
        self.assertEqual(len(api.writes), 2)
        api.calls.clear()
        api.fail_write = None
        dns.reconcile(self.config, api, apply=True)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.writes[0][2]["name"], NAMES[1])

    def test_lost_success_response_is_not_retried_or_duplicated(self):
        api = FakeCloudflare(self.config)
        api.fail_after_write = 1
        with self.assertRaises(dns.SyncError):
            dns.reconcile(self.config, api, apply=True)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(len(api.state), 1)
        api.calls.clear()
        api.fail_after_write = None
        dns.reconcile(self.config, api, apply=True)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.writes[0][2]["name"], NAMES[1])
        self.assertEqual(len(api.state), 2)

    def test_pagination_finds_late_conflict(self):
        records = [address(NAMES[1], type="TXT", content="keep") for _ in range(100)]
        records.append(address(NAMES[1], type="CNAME", content="other.example.com"))
        api = FakeCloudflare(self.config, records)
        with self.assertRaises(dns.SyncError):
            dns.reconcile(self.config, api, apply=True)
        self.assertEqual(len(api.calls), 4)
        self.assertEqual(api.writes, [])

    def test_invalid_or_changing_pagination(self):
        bodies = [{"success": True, "result": []}, listing([]) | {"result_info": None},
            listing([]) | {"result": "not a list"}, listing([address()], total_count=2),
            listing([address("other.example.com")])]
        for key, value in (("page", 2), ("count", 1), ("total_pages", "1"),
                           ("total_count", -1), ("per_page", 0), ("total_pages", 5)):
            body = listing([])
            body["result_info"][key] = value
            bodies.append(body)
        for body in bodies:
            api = dns.Cloudflare(self.config)
            api.request = Mock(return_value=body)
            with self.subTest(body=body), self.assertRaises(dns.SyncError):
                api.records(NAMES[0])
        api.request = Mock(side_effect=[listing([address()] * 100, total_count=101),
            listing([address()], page=2, total_count=102)])
        with self.assertRaises(dns.SyncError):
            api.records(NAMES[0])

    def test_api_names_normalize(self):
        api = dns.Cloudflare(self.config)
        record = address(NAMES[0].upper() + ".")
        api.request = Mock(return_value=listing([record]))
        self.assertEqual(api.records(NAMES[0]), [record])


class TransportAndLockTests(unittest.TestCase):
    def setUp(self):
        self.config = dns.Config.from_env(ENV)
        self.api = dns.Cloudflare(self.config)
        self.api.opener = Mock()

    def test_request_shape(self):
        self.api.opener.open.return_value = io.BytesIO(b'{"success":true,"result":{}}')
        self.api.request("PATCH", "/dns_records/" + "b" * 32, {"ttl": 300})
        req = self.api.opener.open.call_args.args[0]
        self.assertEqual(req.method, "PATCH")
        self.assertEqual(req.full_url, dns.API_URL + "/zones/" + "a" * 32 + "/dns_records/" + "b" * 32)
        self.assertEqual(req.get_header("Authorization"), "Bearer " + self.config.token)
        self.assertEqual(json.loads(req.data), {"ttl": 300})
        self.assertEqual(self.api.opener.open.call_args.kwargs, {"timeout": 20})

    def test_errors_redact_token_and_do_not_retry(self):
        secret = self.config.token
        errors = [urllib.error.HTTPError("https://example.com", code, secret, {}, None)
                  for code in (301, 401, 403, 429, 500)]
        errors += [urllib.error.URLError(secret), TimeoutError(secret), http.client.IncompleteRead(secret.encode())]
        for error in errors:
            self.api.opener.open.reset_mock()
            self.api.opener.open.side_effect = error
            with self.assertRaises(dns.SyncError) as caught:
                self.api.request("POST", "/dns_records", {})
            self.assertNotIn(secret, str(caught.exception))
            self.assertEqual(self.api.opener.open.call_count, 1)

    def test_invalid_responses_redacted(self):
        for body in (b'not json', b'[]', b'{"success":false,"errors":["fake-token-never-sent"]}',
                     b'{"success":true}', b'{"success":"true","result":{}}'):
            self.api.opener.open.return_value = io.BytesIO(body)
            with self.assertRaises(dns.SyncError) as caught:
                self.api.request("GET")
            self.assertNotIn(self.config.token, str(caught.exception))

    def test_redirects_disabled(self):
        self.assertIsNone(dns.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.com"))

    def test_lock_exclusion_and_stable_inode(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lock"
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                with dns.locked(path):
                    inode = path.stat().st_ino
                    with self.assertRaises(dns.SyncError):
                        with dns.locked(path):
                            self.fail("Overlapping lock acquired")
                    raise RuntimeError("test failure")
            with dns.locked(path):
                self.assertEqual(path.stat().st_ino, inode)

    def test_explicit_mode_required(self):
        for args in ([], ["--plan", "--apply"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                dns.main(args)


if __name__ == "__main__":
    unittest.main()
