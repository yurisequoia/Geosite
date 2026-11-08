import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import convert, publish


INPUTS = {
    "domainset/cdn.list": "exact.example\n+.suffix.example\n",
    "non_ip/cdn.list": "DOMAIN,EXACT.example\nDOMAIN-KEYWORD,-assets.example\n",
    "non_ip/microsoft_cdn.list": "DOMAIN-SUFFIX,ms.example\n",
    "domainset/download.list": "+.suffix.example\ninstaller.example\n",
    "non_ip/download.list": "DOMAIN-WILDCARD,cdn.*.example.net\nDOMAIN-WILDCARD,ftp.??.example.net\n",
}


class ConvertTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "mihomo"
        self.data = self.root / "data"
        for path, content in INPUTS.items():
            file = self.source / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("# Last Update: yesterday\n\n" + content, encoding="utf-8-sig")

    def test_categories_and_determinism(self):
        counts = convert.convert(self.source, self.data)
        self.assertEqual(counts["categories"], {"cdn": 4, "download": 4})
        cdn = (self.data / "cdn").read_text(encoding="utf-8").splitlines()
        self.assertEqual(cdn, [
            "domain:ms.example", "domain:suffix.example", "full:exact.example",
            "keyword:-assets.example",
        ])
        self.assertIn("domain:suffix.example", (self.data / "download").read_text())
        path = self.source / "domainset/cdn.list"
        path.write_text("# Last Update: today\n+.suffix.example\nexact.example\n", encoding="utf-8")
        other = self.root / "another"
        self.assertEqual(convert.convert(self.source, other), counts)
        for tag in counts["categories"]:
            self.assertEqual((self.data / tag).read_bytes(), (other / tag).read_bytes())

    def test_wildcard_matching(self):
        cases = [
            ("cdn.*.example.net", ("cdn.a.example.net", "cdn.a.b.example.net"),
             ("mycdn.a.example.net", "cdn.a.example.net.evil", "cdn.a.exampleXnet")),
            ("cdn-*.example.net", ("cdn-.example.net", "cdn-east.example.net"),
             ("cdn.example.net", "cdn-east.example.org")),
            ("ftp.??.example.net", ("ftp.hk.example.net",),
             ("ftp.h.example.net", "ftp.hkg.example.net", "ftp.hk.example.net.evil")),
        ]
        for pattern, hits, misses in cases:
            _, regex = convert.convert_line("DOMAIN-WILDCARD," + pattern, False).split(":", 1)
            for host in hits + misses:
                with self.subTest(host=host, pattern=pattern):
                    self.assertEqual(bool(re.search(regex, host)), host in hits)

    def test_missing_file_stops_before_output(self):
        (self.source / "non_ip/microsoft_cdn.list").unlink()
        with self.assertRaisesRegex(ValueError, r"microsoft_cdn\.list: cannot read input"):
            convert.convert(self.source, self.data)
        self.assertFalse(self.data.exists())

    def test_empty_file_stops_before_output(self):
        (self.source / "non_ip/microsoft_cdn.list").write_text("# empty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"microsoft_cdn\.list: no rules found"):
            convert.convert(self.source, self.data)
        self.assertFalse(self.data.exists())

    def test_bad_rules_report_source_line(self):
        bad = {
            "IP-CIDR,192.0.2.0/24": "unsupported rule type",
            "DOMAIN,example..net": "invalid domain",
            "DOMAIN,example.net,PROXY": "expected exactly",
        }
        for line, message in bad.items():
            with self.subTest(line=line):
                path = self.source / "non_ip/download.list"
                path.write_text("# comment\n\n" + line + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, rf"download\.list:3: {message}"):
                    convert.convert(self.source, self.data)
                self.assertFalse(self.data.exists())


class PublishTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dist = Path(temp.name)
        data = b"test geosite bytes"
        sha = hashlib.sha256(data).hexdigest()
        (self.dist / "geosite.dat").write_bytes(data)
        (self.dist / "geosite.dat.sha256").write_bytes(f"{sha}  geosite.dat\n".encode())
        self.digests = {
            p.name: f"sha256:{hashlib.sha256(p.read_bytes()).hexdigest()}"
            for p in self.dist.iterdir()
        }
        env = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "owner/test",
            "GITHUB_API_URL": "https://api.github.com",
            "GH_TOKEN": "unused",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_SHA": "b" * 40,
        })
        env.start()
        self.addCleanup(env.stop)
        self.summary = {"categories": {"cdn": 1, "download": 2}, "inputs": {"mihomo/file.list": 3}}
        self.source_sha = "a" * 40

    @patch("scripts.publish.subprocess.run")
    @patch("scripts.publish.latest_assets")
    def test_unchanged_skips(self, latest, gh):
        latest.return_value = self.digests
        self.assertFalse(publish.publish(self.dist, self.source_sha, self.summary))
        gh.assert_not_called()

    @patch("scripts.publish.subprocess.run")
    @patch("scripts.publish.latest_assets")
    def test_first_release_or_changed_assets_upload_together(self, latest, gh):
        for existing in ({}, {**self.digests, "geosite.dat.sha256": "sha256:old"}):
            with self.subTest(existing=existing):
                gh.reset_mock()
                latest.return_value = existing
                self.assertTrue(publish.publish(self.dist, self.source_sha, self.summary))
                gh.assert_called_once()
                args = gh.call_args.args[0]
                self.assertEqual(args[:4], ["gh", "release", "create", "geosite-123-2"])
                self.assertIn(str(self.dist / "geosite.dat"), args)
                self.assertIn(str(self.dist / "geosite.dat.sha256"), args)
                self.assertIn("--latest", args)
                self.assertIn(self.source_sha, gh.call_args.kwargs["input"])
                self.assertTrue(gh.call_args.kwargs["check"])

    @patch("scripts.publish.subprocess.run")
    @patch("scripts.publish.urlopen")
    def test_api_failure_does_not_publish(self, urlopen, gh):
        urlopen.side_effect = HTTPError("https://api.github.com", 503, "unavailable", {}, None)
        with self.assertRaises(HTTPError):
            publish.publish(self.dist, self.source_sha, self.summary)
        gh.assert_not_called()
