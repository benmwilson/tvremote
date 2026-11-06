"""The remote is one big script in app/index.html. A single syntax error blanks the whole page, and none of the Python tests would notice,
so the script is syntax-checked here (with Node, when it is installed)."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PageScript(unittest.TestCase):
    def scripts(self):
        html = (ROOT / "app" / "index.html").read_text(encoding="utf-8")
        return re.findall(r"<script>(.*?)</script>", html, re.S)

    def test_the_page_has_its_script(self):
        self.assertTrue(self.scripts() and max(len(s) for s in self.scripts()) > 50_000)

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_the_page_script_has_no_syntax_errors(self):
        for i, src in enumerate(self.scripts()):
            with tempfile.TemporaryDirectory() as d:
                f = os.path.join(d, f"page{i}.js")
                Path(f).write_text(src, encoding="utf-8")
                r = subprocess.run(["node", "--check", f], capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, "index.html script %d: %s" % (i, r.stderr[:600]))


if __name__ == "__main__":
    unittest.main()
