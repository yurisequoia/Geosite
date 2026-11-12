import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_release


REPOSITORY = "owner/repo"
RELEASES = f"repos/{REPOSITORY}/releases"


def response(body, status=200):
    if not isinstance(body, str):
        body = json.dumps(body)
    return subprocess.CompletedProcess(
        [], 0 if status == 200 else 1,
        stdout=f"HTTP/2.0 {status} Status\nContent-Type: application/json\n\n{body}",
        stderr="" if status == 200 else f"gh: request failed (HTTP {status})",
    )


class CheckReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "geosite.dat"
        self.data.write_bytes(b"current geosite content")
        self.digest = hashlib.sha256(self.data.read_bytes()).hexdigest()
        self.output = self.root / "output"
        self.summary = self.root / "summary"
        self.request = patch("scripts.check_release.subprocess.run").start()
        self.addCleanup(patch.stopall)
        self.release = {
            "tag_name": "geosite-previous",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": "geosite.dat", "digest": f"sha256:{self.digest}"}],
        }

    def run_check(self):
        with contextlib.redirect_stdout(io.StringIO()):
            check_release.check_release(REPOSITORY, self.data, self.output, self.summary)

    def assert_stopped(self, message):
        with self.assertRaisesRegex((ValueError, RuntimeError), message):
            self.run_check()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.summary.exists())

    def test_same_digest_skips_release_and_records_baseline(self):
        self.request.return_value = response(self.release)
        self.run_check()
        self.assertEqual(self.output.read_text(), "publish=false\n")
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn("Unchanged; release skipped.", summary)
        self.assertIn("geosite-previous", summary)
        self.assertEqual(summary.count(self.digest), 2)
        self.assertEqual(self.request.call_args.args[0][-1], f"{RELEASES}/latest")
        self.assertEqual(self.request.call_count, 1)

    def test_different_digest_publishes(self):
        self.release["assets"][0]["digest"] = "sha256:" + "0" * 64
        self.request.return_value = response(self.release)
        self.run_check()
        self.assertEqual(self.output.read_text(), "publish=true\n")
        self.assertIn("Changed; publishing.", self.summary.read_text())

    def test_confirmed_absence_publishes_first_release(self):
        for releases in ([], [{"draft": True, "prerelease": False},
                              {"draft": False, "prerelease": True}]):
            with self.subTest(releases=releases):
                self.request.side_effect = [response({}, 404), response(releases)]
                self.run_check()
                self.assertIn("First release; publishing.", self.summary.read_text())
                self.assertEqual(self.output.read_text(), "publish=true\n")
                self.assertEqual(self.request.call_args.args[0][-1],
                                 f"{RELEASES}?per_page=100&page=1")
                self.output.unlink()
                self.summary.unlink()

    def test_absence_check_follows_pagination(self):
        drafts = [{"draft": True, "prerelease": False}] * 100
        self.request.side_effect = [response({}, 404), response(drafts), response([])]
        self.run_check()
        self.assertEqual(self.output.read_text(), "publish=true\n")
        self.assertEqual(self.request.call_args.args[0][-1], f"{RELEASES}?per_page=100&page=2")

    def test_404_with_existing_stable_release_stops(self):
        drafts = [{"draft": True, "prerelease": False}] * 100
        self.request.side_effect = [response({}, 404), response(drafts), response([self.release])]
        self.assert_stopped("published stable release exists")

    def test_release_queries_fail_closed(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.request.side_effect = None
                self.request.return_value = response({}, status)
                self.assert_stopped(f"HTTP {status}")

    def test_inaccessible_release_list_is_not_first_release(self):
        self.request.side_effect = [response({}, 404), response({}, 404)]
        self.assert_stopped("HTTP 404")

    def test_transport_failure_stops(self):
        self.request.return_value = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="connection failed",
        )
        self.assert_stopped("connection failed")

    def test_timeout_emits_no_publishing_decision(self):
        self.request.side_effect = subprocess.TimeoutExpired("gh api", 60)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_check()
        self.assertFalse(self.output.exists())

    def test_invalid_json_stops(self):
        self.request.return_value = response("invalid json")
        self.assert_stopped("Expecting value")

    def test_invalid_latest_metadata_stops(self):
        cases = [[], {}, {**self.release, "draft": True},
                 {**self.release, "prerelease": True}, {**self.release, "tag_name": ""},
                 {**self.release, "assets": None}, {**self.release, "assets": [None]},
                 {**self.release, "assets": []}]
        for release in cases:
            with self.subTest(release=release):
                self.request.return_value = response(release)
                self.assert_stopped("invalid|not a published|exactly one")

    def test_invalid_digest_stops_without_fallback(self):
        for digest in ("", "sha256:bad", "sha512:" + "a" * 64, 123):
            with self.subTest(digest=digest):
                self.release["assets"][0]["digest"] = digest
                self.request.return_value = response(self.release)
                self.assert_stopped("invalid geosite.dat asset digest")

    def use_checksum_fallback(self):
        self.release["assets"][0].pop("digest")
        self.release["assets"].append({"name": "geosite.dat.sha256", "id": 42})

    def test_missing_api_digest_uses_checksum_asset(self):
        self.use_checksum_fallback()
        self.request.side_effect = [response(self.release),
                                    response(f"{self.digest.upper()}  geosite.dat\n")]
        self.run_check()
        self.assertEqual(self.output.read_text(), "publish=false\n")
        command = self.request.call_args.args[0]
        self.assertIn("Accept: application/octet-stream", command)
        self.assertEqual(command[-1], f"{RELEASES}/assets/42")

    def test_missing_digest_and_checksum_stops(self):
        self.release["assets"][0]["digest"] = None
        self.request.return_value = response(self.release)
        self.assert_stopped("no digest")

    def test_invalid_checksum_stops(self):
        self.use_checksum_fallback()
        for checksum in ("bad", f"{self.digest}  other.dat\n",
                         f"{self.digest}  geosite.dat\nextra", ""):
            with self.subTest(checksum=checksum):
                self.request.side_effect = [response(self.release), response(checksum)]
                self.assert_stopped("invalid geosite.dat.sha256 contents")

    def test_checksum_request_failure_stops(self):
        self.use_checksum_fallback()
        self.request.side_effect = [response(self.release), response({}, 404)]
        self.assert_stopped("HTTP 404")


if __name__ == "__main__":
    unittest.main()
