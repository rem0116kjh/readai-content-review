import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_distribution


class DistributionTests(unittest.TestCase):
    def test_allowlist_manifest_and_no_private_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/"source"
            root.mkdir()
            for name in (*build_distribution.FILES, "배포_안내.md"):
                target = root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("bundle fixture")
            for name in (".local/auth.json", ".local/reports/private.json", ".env", "private.csv"):
                target = root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("NEVER INCLUDE PRIVATE CONTENT")
            out = Path(folder)/"bundle.zip"
            with patch.object(build_distribution, "ROOT", root):
                result = build_distribution.build(out)
            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), len(build_distribution.FILES) + 2)
                self.assertFalse(any(".local" in name or ".env" in name or "private" in name for name in names))
                manifest = json.loads(archive.read("ReadAI-review/MANIFEST.json"))
                for name, digest in manifest.items():
                    self.assertEqual(hashlib.sha256(archive.read("ReadAI-review/"+name)).hexdigest(), digest)
                launcher = archive.getinfo("ReadAI-review/리드AI 시트 검토.command")
                self.assertEqual(launcher.external_attr >> 16 & 0o777, 0o755)
            self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), result["sha256"])


if __name__ == "__main__":
    unittest.main()
