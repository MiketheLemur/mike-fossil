"""Deterministic comparison on synthetic tree pairs."""

import os
import shutil
import tempfile
import unittest

from fossil.fingerprint import fingerprint
from fossil.compare import compare


ENGINE = (
    "def hyperdrive_thrust_calculator(velocity_vector):\n"
    "    quantum_flux_capacitor = velocity_vector * 2\n"
    "    return quantum_flux_capacitor\n"
)


def write_tree(files):
    d = tempfile.mkdtemp()
    for rel, content in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return d


class TestCompare(unittest.TestCase):
    def tearDown(self):
        for d in getattr(self, "_dirs", []):
            shutil.rmtree(d, ignore_errors=True)
        self._dirs = []

    def _fp(self, files):
        d = write_tree(files)
        self._dirs = getattr(self, "_dirs", []) + [d]
        return fingerprint(d)

    def test_identical_trees(self):
        files = {"a.py": ENGINE, "b.py": "x = 1\n"}
        comp = compare(self._fp(files), self._fp(files), "A", "B")
        self.assertEqual(comp["identical_same_path"]["count"], 2)
        self.assertGreater(comp["global_shingle_jaccard"], 0.9)
        self.assertGreater(comp["identifier_jaccard"], 0.9)

    def test_moved_file_detected(self):
        fa = self._fp({"src/engine.py": ENGINE})
        fb = self._fp({"lib/engine.py": ENGINE})
        comp = compare(fa, fb, "A", "B")
        self.assertEqual(comp["identical_diff_path"]["count"], 1)
        self.assertIn("src/engine.py  ->  lib/engine.py",
                      comp["identical_diff_path"]["examples"])

    def test_modified_file_similar(self):
        body = "".join(f"line_{i} = compute_value({i})\n" for i in range(25))
        fa = self._fp({"e.py": body})
        fb = self._fp({"e.py": body + "# a small comment added here\n"})
        comp = compare(fa, fb, "A", "B")
        self.assertEqual(comp["very_similar_files"]["count"], 1)

    def test_unrelated_trees_low_scores(self):
        fa = self._fp({"main.py": "def calculate_total(items):\n    return sum(items)\n"})
        fb = self._fp({"app.py": "def render_widget(canvas):\n    canvas.draw()\n"})
        comp = compare(fa, fb, "A", "B")
        self.assertEqual(comp["identical_same_path"]["count"], 0)
        self.assertLess(comp["global_shingle_jaccard"], 0.3)
        self.assertEqual(comp["shared_rare_identifiers"]["count"], 0)

    def test_shared_rare_identifier(self):
        fa = self._fp({"a.py": "xenotransplantation_protocol = 1\n"})
        fb = self._fp({"b.py": "xenotransplantation_protocol = 2\n"})
        comp = compare(fa, fb, "A", "B")
        self.assertIn("xenotransplantation_protocol",
                      comp["shared_rare_identifiers"]["examples"])

    def test_acknowledgment_detection(self):
        fa = self._fp({"README.md": "# forked\n\nForked from octocat/upstream.\n",
                       "main.py": "x=1\n"})
        fb = self._fp({"main.py": "x=1\n"})
        # Pretend B's manifest name is 'upstream'
        fb["manifests"] = [{"name": "upstream", "deps": []}]
        comp = compare(fa, fb, "A", "B")
        self.assertTrue(comp["acknowledgment_in_a"])


if __name__ == "__main__":
    unittest.main()
