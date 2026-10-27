import contextlib
import io
import json
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import operations as ops
from cloudflare_dns import SyncError


def model(service="proxy", **overrides):
    return {"services": {service: {"ports": [{"target": 443, "published": "9443", "host_ip": "192.168.1.10", "protocol": "tcp", **overrides}]}}}


def running(service="proxy", **overrides):
    return json.dumps({"Service": service, "State": "running", "Publishers": [
        {"URL": "192.168.1.10", "TargetPort": 443, "PublishedPort": 9443, "Protocol": "tcp", **overrides}]})


class PreflightTests(unittest.TestCase):
    def test_new_or_changed_binding_requires_probe(self):
        self.assertEqual(ops.preflight(model(), ""), "probe")
        self.assertEqual(ops.preflight(model(), "[]"), "probe")
        self.assertEqual(ops.preflight(model(), running(PublishedPort=443)), "probe")
        self.assertEqual(ops.preflight(model(), running(URL="192.168.1.11")), "probe")

    def test_existing_binding_never_needs_disruption(self):
        for service in ("proxy", "edge"):
            self.assertEqual(ops.preflight(model(service), running(service), service), "reuse")
            self.assertEqual(ops.published_port(model(service), service), ("192.168.1.10", 9443))
        with self.assertRaises(SyncError):
            ops.preflight(model("edge"), running(), "edge")
        self.assertEqual(ops.preflight(model(), "[" + running() + "]"), "reuse")
        self.assertEqual(ops.preflight(model(host_ip="fd00::1"), running(URL="fd00:0:0:0:0:0:0:1")), "reuse")

    def test_invalid_ports_rejected(self):
        cases = [{"published": v} for v in ("0", "65536", "70000", "-1", "443-445", "https", "")]
        cases += [{"host_ip": v} for v in ("", "hostname", "999.1.1.1", "fe80::1%eth0", "224.0.0.1")]
        cases += [{"target": 80}, {"protocol": "udp"}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(SyncError):
                ops.preflight(model(**case), "")
        for value in ({}, None, {"services": {"proxy": {"ports": []}}}):
            with self.assertRaises(SyncError):
                ops.preflight(value, "")
        for port in ("1", "443", "65535"):
            self.assertEqual(ops.published_port(model(published=port))[1], int(port))

    def test_unknown_running_state_fails_closed(self):
        for value in ("bad", "null", "{}", running() + "\n" + running()):
            with self.subTest(value=value), self.assertRaises(SyncError):
                ops.preflight(model(), value)

    def test_compose_errors_do_not_echo_credentials(self):
        secret = "do-not-print-this-token"
        with self.assertRaises(SyncError) as caught:
            ops.compose_input(io.StringIO(secret))
        self.assertNotIn(secret, str(caught.exception))


class VerificationTests(unittest.TestCase):
    env = {"PROXY_HOSTNAMES": "app.example.com api.example.com"}

    def test_invalid_configuration(self):
        for key, values in {
            "PROXY_VERIFY_PATH": ("relative", "//other.example.com", "/a\nb", "/#secret", "/a b"),
            "PROXY_VERIFY_STATUS": ("99", "600", "bad"),
            "PROXY_VERIFY_TIMEOUT": ("0", "601", "bad"),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(SyncError):
                    ops.verification_config(self.env | {key: value}, 443)
        for port in (0, 65536):
            with self.assertRaises(SyncError):
                ops.verification_config(self.env, port)

    def test_all_names_retry_and_expected_response(self):
        with patch.object(ops, "bounded_request", side_effect=["not ready", None, None]) as request, patch.object(ops.time, "sleep"), contextlib.redirect_stdout(io.StringIO()) as output:
            ops.verify(self.env | {"PROXY_VERIFY_PATH": "/health", "PROXY_VERIFY_STATUS": "201", "PROXY_VERIFY_BODY": "ready"}, 9443)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args.args[0], ("app.example.com", 9443, "/health", 201, b"ready"))
        self.assertIn("Verified https://api.example.com:9443/health", output.getvalue())

    def test_total_deadline_includes_all_names(self):
        clock = [0]
        def attempt(*args):
            clock[0] = 1
            return "not ready"
        with patch.object(ops.time, "monotonic", side_effect=lambda: clock[0]), patch.object(ops.time, "sleep"), patch.object(ops, "bounded_request", side_effect=attempt) as request, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SyncError):
            ops.verify(self.env | {"PROXY_VERIFY_TIMEOUT": "1"}, 443)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], 1)

    def test_status_body_and_no_redirects(self):
        connection = Mock()
        response = connection.getresponse.return_value
        with patch.object(ops.http.client, "HTTPSConnection", return_value=connection):
            response.status = 200
            response.read.return_value = b"application ready"
            self.assertIsNone(ops.request_result("app.example.com", 443, "/", 200, b"ready"))
            self.assertIn("marker absent", ops.request_result("app.example.com", 443, "/", 200, b"wrong"))
            response.status = 302
            self.assertEqual(ops.request_result("app.example.com", 443, "/", 200, b""), "HTTP 302; expected 200")
            self.assertEqual(connection.request.call_count, 3)
        connection.close.assert_called()

    def test_certificate_errors_redacted(self):
        with patch.object(ops.http.client, "HTTPSConnection", side_effect=ssl.SSLCertVerificationError("secret")):
            error = ops.request_result("app.example.com", 443, "/", 200, b"")
        self.assertIn("certificate", error)
        self.assertNotIn("secret", error)

    def test_blocking_worker_is_terminated(self):
        process = Mock()
        process.is_alive.return_value = True
        context = Mock()
        context.Pipe.return_value = (Mock(), Mock())
        context.Process.return_value = process
        with patch.object(ops.multiprocessing, "get_context", return_value=context):
            self.assertIn("timed out", ops.bounded_request(("app.example.com", 443, "/", 200, b""), 0.1))
        process.terminate.assert_called_once()
        process.join.assert_any_call(0.1)
        process.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
