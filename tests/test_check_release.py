"""Release guards use fake metadata and credentials; no network or publication."""
import base64
import contextlib
import io
import http.client
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_release as release


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.sources = Mock()
        self.sources.get.side_effect = [[], [], {"token": "fake-pull-token"}, None]

    def check(self, version="v1.0.1", repository="Example/Proxy", actor="example", token="fake-github-secret"):
        release.check_available(version, repository, actor, token, self.sources)

    def test_unused_version_checks_all_namespaces_with_scoped_credentials(self):
        self.check()
        calls = self.sources.get.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0].args, ("https://api.github.com/repos/Example/Proxy/git/matching-refs/tags/v1.0.1", "Bearer fake-github-secret"))
        self.assertEqual(calls[1].args, ("https://api.github.com/repos/Example/Proxy/releases?per_page=100&page=1", "Bearer fake-github-secret"))
        self.assertEqual(calls[2].args, ("https://ghcr.io/token?service=ghcr.io&scope=repository%3Aexample%2Fproxy%3Apull",
                                       "Basic " + base64.b64encode(b"example:fake-github-secret").decode()))
        self.assertEqual(calls[3].args, ("https://ghcr.io/v2/example/proxy/manifests/v1.0.1", "Bearer fake-pull-token"))
        self.assertEqual(calls[3].kwargs, {"missing_codes": ("MANIFEST_UNKNOWN", "NAME_UNKNOWN")})

    def test_invalid_versions_and_missing_environment_fail_before_lookups(self):
        for version in ("", "1.0.1", "v01.0.1", "v1.00.1", "v1.0.01", "v1.0.1-rc.1", "v1.0.1+build", "v1.0.1\n", "--help", "v1.0.1; echo bad", "v" + "1" * 124 + ".0.0"):
            with self.subTest(version=version), self.assertRaises(release.ReleaseError):
                self.check(version=version)
        for kwargs in ({"repository": ""}, {"repository": "../proxy"}, {"repository": "example/../proxy"}, {"actor": ""}, {"token": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(release.ReleaseError):
                self.check(**kwargs)
        self.sources.get.assert_not_called()

    def test_version_tag_length_limit(self):
        release.validate_version("v" + "1" * 123 + ".0.0")
        with self.assertRaises(release.ReleaseError):
            release.validate_version("v" + "1" * 124 + ".0.0")

    def test_each_existing_namespace_blocks_publication(self):
        cases = [
            ([{"ref": "refs/tags/v1.0.1"}],),
            ([], [{"tag_name": "v1.0.1", "draft": True}]),
            ([], [], {"token": "fake-pull-token"}, {"manifests": []}),
        ]
        for responses in cases:
            with self.subTest(responses=responses):
                self.sources.get.reset_mock(side_effect=True)
                self.sources.get.side_effect = responses
                with self.assertRaises(release.ReleaseError):
                    self.check()
                self.assertEqual(self.sources.get.call_count, len(responses))

    def test_prefix_tags_do_not_block_and_release_pages_are_checked(self):
        first_page = [{"tag_name": f"v0.0.{i}"} for i in range(100)]
        self.sources.get.side_effect = [[{"ref": "refs/tags/v1.0.10"}], first_page, [{"tag_name": "v1.0.1"}]]
        with self.assertRaisesRegex(release.ReleaseError, "release already exists"):
            self.check()
        self.assertTrue(self.sources.get.call_args.args[0].endswith("page=2"))

    def test_malformed_metadata_is_not_absence(self):
        for responses in (({},), ([{}],), ([], {}), ([], [{}]), ([], [], {}), ([], [], {"token": 123})):
            self.sources.get.side_effect = responses
            with self.subTest(responses=responses), self.assertRaises(release.ReleaseError):
                self.check()

    def test_lookup_failures_stop_immediately(self):
        for prefix in ([], [[]], [[], []], [[], [], {"token": "fake-pull-token"}]):
            self.sources.get.reset_mock(side_effect=True)
            self.sources.get.side_effect = [*prefix, release.ReleaseError("Lookup failed")]
            with self.assertRaisesRegex(release.ReleaseError, "Lookup failed"):
                self.check()
            self.assertEqual(self.sources.get.call_count, len(prefix) + 1)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.sources = release.Sources()
        self.sources.opener = MagicMock()
        self.url = "https://ghcr.io/v2/example/proxy/manifests/v1.0.1"
        self.codes = ("MANIFEST_UNKNOWN", "NAME_UNKNOWN")

    def http_error(self, status, body):
        return urllib.error.HTTPError(self.url, status, "fake-secret", {}, io.BytesIO(body))

    def test_only_explicit_registry_missing_responses_mean_absence(self):
        for code in self.codes:
            self.sources.opener.open.side_effect = self.http_error(404, json.dumps({"errors": [{"code": code}]}).encode())
            self.assertIsNone(self.sources.get(self.url, "Bearer fake-secret", self.codes))
        for status, body in (
            (401, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'),
            (403, b'{}'), (429, b'{}'), (500, b'{}'),
            (404, b'not JSON'), (404, b'{}'), (404, b'{"errors":[]}'),
            (404, b'{"errors":[{"code":"DENIED"}]}'),
            (404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"},{"code":"DENIED"}]}'),
        ):
            self.sources.opener.open.side_effect = self.http_error(status, body)
            with self.subTest(status=status, body=body), self.assertRaises(release.ReleaseError) as caught:
                self.sources.get(self.url, "Bearer fake-secret", self.codes)
            self.assertNotIn("fake-secret", str(caught.exception))

    def test_error_body_read_failures_are_redacted(self):
        for failure in (OSError("fake-secret"), http.client.HTTPException("fake-secret")):
            body = io.BytesIO()
            body.read = Mock(side_effect=failure)
            self.sources.opener.open.side_effect = urllib.error.HTTPError(self.url, 404, "fake-secret", {}, body)
            with self.assertRaises(release.ReleaseError) as caught:
                self.sources.get(self.url, "Bearer fake-secret", self.codes)
            self.assertNotIn("fake-secret", str(caught.exception))
            self.assertTrue(body.closed)

    def test_github_404_is_not_absence(self):
        self.sources.opener.open.side_effect = self.http_error(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
        with self.assertRaises(release.ReleaseError):
            self.sources.get("https://api.github.com/repos/example/proxy/releases", "Bearer fake-secret")

    def test_network_failures_and_invalid_json_are_redacted(self):
        for error in (urllib.error.URLError("fake-secret"), OSError("fake-secret")):
            self.sources.opener.open.side_effect = error
            with self.assertRaises(release.ReleaseError) as caught:
                self.sources.get(self.url, "Bearer fake-secret", self.codes)
            self.assertNotIn("fake-secret", str(caught.exception))
        self.sources.opener.open.side_effect = None
        for body in (b"fake-secret", b"null"):
            self.sources.opener.open.return_value.__enter__.return_value.read.return_value = body
            with self.assertRaises(release.ReleaseError):
                self.sources.get(self.url, "Bearer fake-secret", self.codes)

    def test_credentials_do_not_follow_redirects(self):
        self.sources.opener.open.return_value.__enter__.return_value.read.return_value = b'{}'
        self.assertEqual(self.sources.get(self.url, "Bearer fake-secret"), {})
        request = self.sources.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.unredirected_hdrs["Authorization"], "Bearer fake-secret")
        self.assertNotIn("Authorization", request.headers)
        self.assertIsNone(release.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))


class CliTests(unittest.TestCase):
    def test_help_invalid_args_and_validate_only_have_no_network_calls(self):
        with patch.object(release, "Sources") as sources, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for args, status in ((["--help"], 0), ([], 2), (["--unknown"], 2)):
                with self.assertRaises(SystemExit) as caught:
                    release.main(args)
                self.assertEqual(caught.exception.code, status)
            self.assertEqual(release.main(["--version", "v1.0.1", "--validate-only"]), 0)
            self.assertEqual(release.main(["--version", "bad", "--validate-only"]), 1)
            sources.assert_not_called()

    def test_environment_and_failed_check_exit_status(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/proxy", "GITHUB_ACTOR": "example", "GH_TOKEN": "fake-secret"}), \
                patch.object(release, "check_available") as check, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(release.main(["--version", "v1.0.1"]), 0)
            check.assert_called_once_with("v1.0.1", "example/proxy", "example", "fake-secret")
            check.side_effect = release.ReleaseError("Version exists")
            self.assertEqual(release.main(["--version", "v1.0.1"]), 1)
            self.assertIn("Version exists", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
