"""Fingerprinting on synthetic trees."""

import os
import shutil
import tempfile
import unittest

from fossil.fingerprint import fingerprint


MIT_TEXT = """MIT License

Copyright (c) 2026 Test

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction.
"""


def make_tree():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "pkg"))
    with open(os.path.join(d, "pkg", "__init__.py"), "w") as f:
        f.write('VERSION = "0.1.0"\n')
    with open(os.path.join(d, "pkg", "engine.py"), "w") as f:
        f.write(
            "def hyperdrive_thrust_calculator(velocity_vector):\n"
            "    quantum_flux_capacitor = velocity_vector * 2\n"
            "    return quantum_flux_capacitor\n"
        )
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("# warpdrive\n\nA drive inspired by octocat/starship.\n")
    with open(os.path.join(d, "LICENSE"), "w") as f:
        f.write(MIT_TEXT)
    with open(os.path.join(d, "pyproject.toml"), "w") as f:
        f.write('[project]\nname = "warpdrive"\nversion = "0.1.0"\n'
                'description = "faster than light travel"\n'
                'dependencies = ["numpy", "scipy"]\n')
    return d


class TestFingerprint(unittest.TestCase):
    def setUp(self):
        self.tree = make_tree()

    def tearDown(self):
        shutil.rmtree(self.tree)

    def test_inventory(self):
        fp = fingerprint(self.tree)
        self.assertEqual(fp["file_count"], 5)
        self.assertEqual(fp["languages"].get("Python"), 2)
        self.assertIn("pkg", fp["top_dirs"])

    def test_manifest(self):
        fp = fingerprint(self.tree)
        self.assertEqual(len(fp["manifests"]), 1)
        m = fp["manifests"][0]
        self.assertEqual(m["name"], "warpdrive")
        self.assertIn("numpy", m["deps"])

    def test_license_guess(self):
        fp = fingerprint(self.tree)
        self.assertEqual(len(fp["licenses"]), 1)
        self.assertEqual(fp["licenses"][0]["guess"], "MIT")

    def test_readme_signal(self):
        fp = fingerprint(self.tree)
        sigs = fp["readme"]["signals"]
        self.assertTrue(any(s["kind"] == "inspired-by" and
                            s["target"] == "octocat/starship" for s in sigs),
                        f"signals: {sigs}")

    def test_identifiers(self):
        fp = fingerprint(self.tree)
        distinctive = fp["identifiers"]["distinctive"]
        self.assertIn("hyperdrive_thrust_calculator", distinctive)
        self.assertIn("quantum_flux_capacitor", distinctive)
        # Generic words must not count as distinctive.
        self.assertNotIn("def", distinctive)
        self.assertNotIn("return", distinctive)

    def test_minhash_present(self):
        fp = fingerprint(self.tree)
        self.assertTrue(len(fp["minhash"]) > 0)
        self.assertIn("_full_shingles", fp)

    def test_content_index(self):
        fp = fingerprint(self.tree)
        # Every file's sha1 maps back to its path.
        for f in fp["files"]:
            self.assertIn(f["path"], fp["content_index"][f["sha1"]])


if __name__ == "__main__":
    unittest.main()
