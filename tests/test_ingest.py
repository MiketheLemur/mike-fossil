"""Ingestion: local dirs (with/without git), zips, source detection."""

import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from fossil.ingest import detect_source, ingest, IngestError


class TestDetect(unittest.TestCase):
    def test_github_shorthand(self):
        kind, norm = detect_source("octocat/hello-world")
        self.assertEqual(kind, "github")
        self.assertIn("octocat/hello-world", norm)

    def test_github_url(self):
        kind, _norm = detect_source("https://github.com/octocat/hello-world.git")
        self.assertEqual(kind, "github")

    def test_garbage(self):
        with self.assertRaises(IngestError):
            detect_source("not a source at all !!!")


class TestIngestLocal(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp()
        with open(os.path.join(self.src, "main.py"), "w") as f:
            f.write("print('hi')\n")
        self.dest = tempfile.mkdtemp() + "-dest"

    def tearDown(self):
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_copy_plain_dir(self):
        info = ingest(self.src, self.dest)
        self.assertEqual(info["kind"], "local")
        self.assertTrue(os.path.exists(os.path.join(self.dest, "main.py")))
        # Source untouched.
        self.assertTrue(os.path.exists(os.path.join(self.src, "main.py")))

    def test_skips_node_modules(self):
        nm = os.path.join(self.src, "node_modules", "dep")
        os.makedirs(nm)
        with open(os.path.join(nm, "index.js"), "w") as f:
            f.write("x\n")
        ingest(self.src, self.dest)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "node_modules")))

    def test_local_git_clone_keeps_history(self):
        subprocess.run(["git", "init", "-q", self.src], check=True)
        subprocess.run(["git", "-C", self.src, "config", "user.email", "t@t"],
                       check=True)
        subprocess.run(["git", "-C", self.src, "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", self.src, "add", "."], check=True)
        subprocess.run(["git", "-C", self.src, "commit", "-qm", "init"], check=True)
        info = ingest(self.src, self.dest)
        self.assertIn("git", info)
        self.assertEqual(info["git"]["commits"], 1)


class TestIngestZip(unittest.TestCase):
    def test_unzip_and_unwrap(self):
        d = tempfile.mkdtemp()
        zp = os.path.join(d, "p.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("wrapper/app.py", "x=1\n")
        dest = d + "-dest"
        try:
            info = ingest(zp, dest)
            self.assertEqual(info["kind"], "zip")
            self.assertTrue(os.path.exists(os.path.join(dest, "app.py")))
        finally:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(dest, ignore_errors=True)

    def test_zip_slip_rejected(self):
        d = tempfile.mkdtemp()
        zp = os.path.join(d, "evil.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("../../evil.py", "x=1\n")
        try:
            with self.assertRaises(IngestError):
                ingest(zp, d + "-dest")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
