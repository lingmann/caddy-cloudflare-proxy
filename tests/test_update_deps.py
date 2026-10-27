"""Dependency updates use fake metadata and disposable checkouts, never live APIs."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_deps as updater

OLD_BUILDER = "sha256:" + "a" * 64
OLD_RUNTIME = "sha256:" + "b" * 64
NEW_BUILDER = "sha256:" + "c" * 64
NEW_RUNTIME = "sha256:" + "d" * 64
FILES = {
    "Dockerfile": f"""# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM caddy:2.11.7-builder@{OLD_BUILDER} AS builder
ARG TARGETOS TARGETARCH
RUN GOOS=$TARGETOS GOARCH=$TARGETARCH \\
    xcaddy build v2.11.7 --with github.com/caddy-dns/cloudflare@v0.2.4
FROM caddy:2.11.7@{OLD_RUNTIME} AS runtime
COPY src/ /opt/proxy/
""",
    "tests/integration.sh": """[ "${version%% *}" = v2.11.7 ]
[ "$modules" = 'dns.providers.cloudflare v0.2.4' ]
""",
    ".github/workflows/ci.yml": "steps:\n  - uses: actions/checkout@v4\n  - uses: docker/setup-buildx-action@v3 # keep comment\n  - uses: actions/checkout@v4\n",
}


class FakeSources:
    def __init__(self):
        self.versions = {"caddyserver/caddy": "v2.12.0", "caddy-dns/cloudflare": "v0.3.0",
                         "actions/checkout": "v6.1.2", "docker/setup-buildx-action": "v4.2.3"}

    def release(self, repo):
        return self.versions[repo]

    def verify_action_tag(self, repo, tag):
        pass

    def provider_version(self):
        return self.versions["caddy-dns/cloudflare"]

    def image_digest(self, tag):
        return NEW_BUILDER if tag.endswith("-builder") else NEW_RUNTIME


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, content in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (self.root / "README.md").write_text("Documentation is not managed by the updater.\n")
        self.sources = FakeSources()
        output = contextlib.redirect_stdout(io.StringIO())
        self.output = output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def snapshot(self):
        return {name: (self.root / name).read_text() for name in updater.MANAGED}

    def test_updates_all_pins_references_and_is_idempotent(self):
        self.assertEqual(set(updater.update(self.root, self.sources)), set(updater.MANAGED))
        files = self.snapshot()
        self.assertIn("caddy:2.12.0-builder@" + NEW_BUILDER, files["Dockerfile"])
        self.assertIn("caddy:2.12.0@" + NEW_RUNTIME, files["Dockerfile"])
        self.assertIn("xcaddy build v2.12.0 --with github.com/caddy-dns/cloudflare@v0.3.0", files["Dockerfile"])
        self.assertIn("# syntax=docker/dockerfile:1", files["Dockerfile"])
        self.assertEqual((self.root / "README.md").read_text(), "Documentation is not managed by the updater.\n")
        self.assertIn("'dns.providers.cloudflare v0.3.0'", files["tests/integration.sh"])
        self.assertEqual(files[updater.WORKFLOW].count("actions/checkout@v6\n"), 2)
        self.assertIn("docker/setup-buildx-action@v4", files[updater.WORKFLOW])
        self.assertIn("# keep comment", files[updater.WORKFLOW])
        self.assertEqual(updater.update(self.root, self.sources), [])
        self.assertEqual(self.snapshot(), files)

    def test_same_versions_refresh_official_image_digests(self):
        self.sources.versions.update({"caddyserver/caddy": "v2.11.7", "caddy-dns/cloudflare": "v0.2.4",
                                     "actions/checkout": "v4.3.0", "docker/setup-buildx-action": "v3.2.0"})
        self.assertEqual(updater.update(self.root, self.sources), ["Dockerfile"])
        self.assertIn(NEW_BUILDER, (self.root / "Dockerfile").read_text())
        self.assertIn(NEW_RUNTIME, (self.root / "Dockerfile").read_text())

    def test_dry_run_shows_diff_without_writes(self):
        self.assertTrue(updater.update(self.root, self.sources, dry_run=True))
        self.assertEqual(self.snapshot(), FILES)
        self.assertIn("+FROM caddy:2.12.0@", self.output.getvalue())

    def test_downgrades_and_prereleases_fail_without_writes(self):
        for repo, value in (("caddyserver/caddy", "v2.10.0"), ("caddy-dns/cloudflare", "v0.1.0"),
                            ("actions/checkout", "v3.9.0"), ("caddyserver/caddy", "v3.0.0-rc.1")):
            sources = FakeSources()
            sources.versions[repo] = value
            with self.subTest(repo=repo, value=value), self.assertRaises(updater.UpdateError):
                updater.update(self.root, sources)
            self.assertEqual(self.snapshot(), FILES)

    def test_late_lookup_failure_leaves_every_file_untouched(self):
        original_release = self.sources.release
        def release(repo):
            if repo == "docker/setup-buildx-action":
                raise updater.UpdateError("Simulated source failure")
            return original_release(repo)
        self.sources.release = release
        with self.assertRaises(updater.UpdateError):
            updater.update(self.root, self.sources)
        self.assertEqual(self.snapshot(), FILES)

    def test_missing_action_alias_prevents_all_writes(self):
        self.sources.verify_action_tag = Mock(side_effect=updater.UpdateError("Alias not published yet"))
        with self.assertRaises(updater.UpdateError):
            updater.update(self.root, self.sources)
        self.assertEqual(self.snapshot(), FILES)

    def test_symlinked_parent_cannot_escape_checkout(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "integration.sh"
            external.write_text(FILES["tests/integration.sh"])
            (self.root / "tests").rename(self.root / "original-tests")
            (self.root / "tests").symlink_to(outside, target_is_directory=True)
            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run), self.assertRaises(updater.UpdateError):
                    updater.update(self.root, self.sources, dry_run=dry_run)
            self.assertEqual(external.read_text(), FILES["tests/integration.sh"])

    def test_drift_and_unsupported_refs_fail_closed(self):
        for name, old, new in (("Dockerfile", "caddy:2.11.7@", "caddy:2.11.6@"),
                               ("tests/integration.sh", "dns.providers.cloudflare v0.2.4", "provider version elsewhere"),
                               (updater.WORKFLOW, "actions/checkout@v4", "actions/checkout@" + "f" * 40)):
            files = dict(FILES)
            files[name] = files[name].replace(old, new)
            with self.subTest(name=name), self.assertRaises(updater.UpdateError):
                updater.prepare(files, self.sources)

    def test_preserves_action_tag_precision(self):
        files = dict(FILES)
        files[updater.WORKFLOW] = files[updater.WORKFLOW].replace("@v4", "@v4.2.1", 1)
        updated = updater.prepare(files, self.sources)
        self.assertIn("actions/checkout@v6.1.2\n", updated[updater.WORKFLOW])
        self.assertIn("actions/checkout@v6\n", updated[updater.WORKFLOW])

    def test_help_and_invalid_arguments_have_no_network_calls(self):
        for args, status in ((["--help"], 0), (["--unknown"], 2)):
            with patch.object(updater, "update") as update, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                updater.main(args)
            self.assertEqual(caught.exception.code, status)
            update.assert_not_called()


class SourceTests(unittest.TestCase):
    def test_release_is_stable_and_cached(self):
        sources = updater.Sources()
        sources.json = Mock(return_value={"draft": False, "prerelease": False, "tag_name": "v2.12.0"})
        self.assertEqual(sources.release("caddyserver/caddy"), "v2.12.0")
        self.assertEqual(sources.release("caddyserver/caddy"), "v2.12.0")
        sources.json.assert_called_once()
        for result in ({"draft": True, "prerelease": False, "tag_name": "v2.12.0"},
                       {"draft": False, "prerelease": True, "tag_name": "v3.0.0-rc.1"},
                       {"draft": False, "prerelease": False, "tag_name": "latest"}):
            sources = updater.Sources()
            sources.json = Mock(return_value=result)
            with self.assertRaises(updater.UpdateError):
                sources.release("caddyserver/caddy")

    def test_action_tag_must_exist_and_is_cached(self):
        sources = updater.Sources()
        sources.json = Mock(return_value={"ref": "refs/tags/v6", "object": {"type": "commit", "sha": "f" * 40}})
        sources.verify_action_tag("actions/checkout", "v6")
        sources.verify_action_tag("actions/checkout", "v6")
        sources.json.assert_called_once()
        for result in ({}, {"ref": "refs/heads/v7", "object": {"type": "commit", "sha": "f" * 40}}):
            sources.json.return_value = result
            with self.assertRaises(updater.UpdateError):
                sources.verify_action_tag("actions/checkout", "v7")

    def test_provider_uses_highest_stable_go_module_tag(self):
        sources = updater.Sources()
        sources.get = Mock(return_value=(b"v0.2.4\nv0.3.0-rc.1\nv0.2.3\nv0.10.0\n", {}))
        self.assertEqual(sources.provider_version(), "v0.10.0")
        sources.get.return_value = (b"v0.3.0-beta.1\n", {})
        with self.assertRaises(updater.UpdateError):
            sources.provider_version()

    def test_registry_checksum_and_platform_validation(self):
        for architectures, valid_checksum, success in ((["amd64", "arm64"], True, True),
                                                       (["amd64"], True, False),
                                                       (["amd64", "arm64"], False, False)):
            sources = updater.Sources()
            sources.json = Mock(return_value={"token": "fake-pull-token"})
            body = json.dumps({"manifests": [{"platform": {"os": "linux", "architecture": arch}} for arch in architectures]}).encode()
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            sources.get = Mock(return_value=(body, {"Docker-Content-Digest": digest if valid_checksum else OLD_RUNTIME}))
            if success:
                self.assertEqual(sources.image_digest("2.12.0"), digest)
            else:
                with self.assertRaises(updater.UpdateError):
                    sources.image_digest("2.12.0")

    def test_tokens_are_scoped_redirects_blocked_and_errors_redacted(self):
        sources = updater.Sources()
        sources.opener = MagicMock()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "fake-github-secret"}):
            sources.opener.open.return_value.__enter__.return_value.read.return_value = b"{}"
            sources.get("https://api.github.com/repos/example/repo/releases/latest")
            request = sources.opener.open.call_args.args[0]
            self.assertEqual(request.unredirected_hdrs["Authorization"], "Bearer fake-github-secret")
            self.assertNotIn("Authorization", request.headers)
            sources.get("https://auth.docker.io/token")
            self.assertNotIn("Authorization", sources.opener.open.call_args.args[0].unredirected_hdrs)
            sources.opener.open.side_effect = urllib.error.HTTPError("https://api.github.com", 403, "fake-github-secret", {}, None)
            with self.assertRaises(updater.UpdateError) as caught:
                sources.get("https://api.github.com/repos/example/repo/releases/latest")
            self.assertNotIn("fake-github-secret", str(caught.exception))
        self.assertIsNone(updater.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))


if __name__ == "__main__":
    unittest.main()
