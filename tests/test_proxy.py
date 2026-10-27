import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import proxy

ENV = {
    "PROXY_HOSTNAMES": "service.example.com api.example.com",
    "PROXY_UPSTREAM": "app:8080",
    "CLOUDFLARE_API_TOKEN": "0" * 40,
}
DNS_ENV = ENV | {"CLOUDFLARE_ZONE_ID": "a" * 32, "PROXY_DNS_TARGET": "192.168.1.10"}


class ServingConfigTests(unittest.TestCase):
    def test_valid_upstreams(self):
        for upstream in ("app:8080", "app-service:80", "host.docker.internal:3000", "192.168.1.3:8080",
                         "[fd00::1]:8080", "https://app.example.com:443", "http://app:8080"):
            proxy.serving_config(ENV | {"PROXY_UPSTREAM": upstream})

    def test_bad_values(self):
        cases = {
            "PROXY_UPSTREAM": ("app", "app:0", "app:65536", "app:bad", "app:80/path", "http://app:80/",
                "http://user:pass@app:80", "app:80?x=y", "app:80#fragment", "app:80\nrespond hacked",
                "app:80 other:80", "{$SECRET}", "ftp://app:80", "[bad::ipv6]:80", "app:80/../", ""),
            "PROXY_HOSTNAMES": ("*.example.com", "a.example.com.", "a.example.com\nb.example.com",
                "a.example.com {", "a.example.com a.example.com", "a.example.com,b.example.com", ""),
            "CLOUDFLARE_API_TOKEN": ("short", "x" * 39, "x" * 41, "x" * 39 + "\n", ""),
            "ACME_CA": ("", "http://example.com/acme", "https://evil.example/directory", "x\n}")}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(proxy.dns.SyncError):
                    proxy.serving_config(ENV | {key: value})
        for key in ENV:
            with self.assertRaises(proxy.dns.SyncError):
                proxy.serving_config({k: v for k, v in ENV.items() if k != key})

    def test_only_letsencrypt_production_and_staging(self):
        for ca in (proxy.PRODUCTION_CA, proxy.STAGING_CA):
            proxy.serving_config(ENV | {"ACME_CA": ca})


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for stream in (contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            stream.__enter__()
            self.addCleanup(stream.__exit__, None, None, None)

    def test_check_never_contacts_cloudflare_or_executes_server(self):
        with patch.object(proxy, "validate_caddy") as validate, patch.object(proxy, "sync") as sync, patch.object(proxy.os, "execvp") as execute:
            self.assertEqual(proxy.main(["check"]), 0)
            validate.assert_called_once_with()
            sync.assert_not_called()
            execute.assert_not_called()

    def test_serve_and_default_ignore_invalid_dns_configuration(self):
        # Bad or missing DNS operation settings must not stop a restart.
        with patch.dict(os.environ, {"PROXY_DNS_TARGET": "invalid", "CLOUDFLARE_ZONE_ID": "bad"}):
            for args in ([], ["serve"]):
                with patch.object(proxy, "validate_caddy") as validate, patch.object(proxy, "sync") as sync, patch.object(proxy.os, "execvp") as execute:
                    self.assertEqual(proxy.main(args), 0)
                    validate.assert_called_once_with()
                    sync.assert_not_called()
                    execute.assert_called_once_with("caddy", proxy.caddy_command("run"))

    def test_validation_failure_prevents_serve_and_prepare(self):
        with patch.dict(os.environ, DNS_ENV):
            for command in ("serve", "prepare", "reload"):
                with patch.object(proxy, "validate_caddy", side_effect=proxy.dns.SyncError("bad")), patch.object(proxy, "sync") as sync, patch.object(proxy.os, "execvp") as execute:
                    self.assertEqual(proxy.main([command]), 1)
                    sync.assert_not_called()
                    execute.assert_not_called()

    def test_prepare_order_and_no_server_start(self):
        events = []
        with patch.dict(os.environ, DNS_ENV), patch.object(proxy, "validate_caddy", side_effect=lambda: events.append("validate")), patch.object(proxy, "sync", side_effect=lambda *a, **kw: events.append(("sync", kw["apply"]))) as sync, patch.object(proxy.os, "execvp") as execute:
            self.assertEqual(proxy.main(["prepare"]), 0)
            self.assertEqual(events, ["validate", ("sync", True)])
            self.assertEqual(sync.call_args.args[0].target, DNS_ENV["PROXY_DNS_TARGET"])
            execute.assert_not_called()

    def test_prepare_bad_dns_config_prevents_caddy_validation(self):
        with patch.object(proxy, "validate_caddy") as validate, patch.object(proxy, "sync") as sync:
            self.assertEqual(proxy.main(["prepare"]), 1)
            validate.assert_not_called()
            sync.assert_not_called()

    def test_dns_commands_do_not_need_serving_settings(self):
        for command, apply in (("dns-plan", False), ("dns-sync", True)):
            with patch.dict(os.environ, {k: v for k, v in DNS_ENV.items() if k != "PROXY_UPSTREAM"}, clear=True), patch.object(proxy, "sync") as sync, patch.object(proxy, "validate_caddy") as validate:
                self.assertEqual(proxy.main([command]), 0)
                self.assertEqual(sync.call_args.kwargs, {"apply": apply})
                validate.assert_not_called()

    def test_reload_uses_current_config_and_environment(self):
        with patch.object(proxy, "validate_caddy") as validate, patch.object(proxy.os, "execvp") as execute:
            self.assertEqual(proxy.main(["reload"]), 0)
            validate.assert_called_once()
            execute.assert_called_once_with("caddy", proxy.caddy_command("reload") + ["--force"])

    def test_failed_write_not_retried(self):
        with patch.dict(os.environ, DNS_ENV), patch.object(proxy, "validate_caddy"), patch.object(proxy, "sync", side_effect=proxy.dns.SyncError("timeout")) as sync:
            self.assertEqual(proxy.main(["prepare"]), 1)
            sync.assert_called_once()

    def test_invalid_command_is_side_effect_free(self):
        for args in (["unknown"], ["serve", "--bad"], ["prepare", "extra"]):
            with patch.object(proxy, "validate_caddy") as validate, patch.object(proxy, "sync") as sync:
                with self.assertRaises(SystemExit):
                    proxy.main(args)
                validate.assert_not_called()
                sync.assert_not_called()

    def test_sync_uses_shared_volume_lock(self):
        events = []
        @contextlib.contextmanager
        def locked(path):
            self.assertEqual(str(path), "/data/.dns-sync.lock")
            events.append("lock")
            yield
            events.append("unlock")
        with patch.object(proxy.dns, "locked", locked), patch.object(proxy.dns, "Cloudflare"), patch.object(proxy.dns, "reconcile", side_effect=lambda *a, **kw: events.append("sync")):
            proxy.sync(Mock(), True)
        self.assertEqual(events, ["lock", "sync", "unlock"])

    def test_validation_errors_redact_token(self):
        output = io.StringIO()
        result = subprocess.CompletedProcess([], 1, "error " + ENV["CLOUDFLARE_API_TOKEN"])
        with patch.object(proxy.subprocess, "run", return_value=result), contextlib.redirect_stderr(output):
            with self.assertRaises(proxy.dns.SyncError):
                proxy.validate_caddy()
        self.assertNotIn(ENV["CLOUDFLARE_API_TOKEN"], output.getvalue())
        self.assertIn("[redacted]", output.getvalue())

    def test_operator_commands_do_not_validate_serving_or_write_dns(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(proxy, "sync") as sync, patch.object(proxy, "validate_caddy") as validate:
            self.assertEqual(proxy.main(["bind-check"]), 0)
            with patch.object(proxy.operations, "compose_input", return_value={}), patch.object(proxy.operations, "preflight", return_value="probe"):
                self.assertEqual(proxy.main(["preflight"]), 0)
            with patch.object(proxy.operations, "verify") as verify:
                self.assertEqual(proxy.main(["verify", "--port", "9443"]), 0)
                verify.assert_called_once_with(os.environ, 9443)
            sync.assert_not_called()
            validate.assert_not_called()

    def test_every_command_has_side_effect_free_help(self):
        for command in ("check", "dns-plan", "dns-sync", "prepare", "serve", "reload", "healthcheck", "preflight", "bind-check", "verify"):
            with self.subTest(command=command), patch.object(proxy, "sync") as sync, patch.object(proxy, "validate_caddy") as validate:
                with self.assertRaises(SystemExit) as caught:
                    proxy.main([command, "--help"])
                self.assertEqual(caught.exception.code, 0)
                sync.assert_not_called()
                validate.assert_not_called()

    def test_healthcheck_needs_no_environment(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(proxy.socket, "create_connection") as connect:
            self.assertEqual(proxy.main(["healthcheck"]), 0)
            connect.assert_called_once_with(("127.0.0.1", 443), timeout=3)
        with patch.object(proxy.socket, "create_connection", side_effect=OSError("down")):
            self.assertEqual(proxy.main(["healthcheck"]), 1)


if __name__ == "__main__":
    unittest.main()
