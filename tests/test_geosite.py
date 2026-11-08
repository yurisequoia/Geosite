import re
import tempfile
import unittest
from pathlib import Path

from scripts import convert


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
