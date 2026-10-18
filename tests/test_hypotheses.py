"""Verdict rules: forks recognized, weak leads rejected, restraint honored."""

import unittest

from fossil.hypotheses import evaluate_pair, THRESHOLDS


def fp(top_dirs=None, first_date=None):
    return {
        "top_dirs": top_dirs or ["src", "docs"],
        "_git_info": {"first": {"date": first_date}} if first_date else {},
    }


def meta(created=None):
    return {"full_name": "x/y", "created_at": created}


def comp(**kw):
    base = {
        "identical_same_path": {"count": 0, "examples": []},
        "identical_diff_path": {"count": 0, "examples": []},
        "very_similar_files": {"count": 0, "examples": []},
        "identifier_jaccard": 0.0,
        "shared_rare_identifiers": {"count": 0, "examples": []},
        "dep_overlap": {"names": [], "count": 0, "a_total": 0, "b_total": 0},
        "name_similarity": {"score": 0.0, "pair": None},
        "readme_jaccard": 0.0,
        "acknowledgment_in_a": [],
        "languages": {"shared": ["Python"], "a": {"Python": 1}, "b": {"Python": 1}},
        "global_shingle_jaccard": 0.0,
    }
    base.update(kw)
    return base


class TestVerdicts(unittest.TestCase):
    def test_shared_commits_means_direct_descendant(self):
        c = comp()
        v = evaluate_pair(fp(first_date="2024-01-01T00:00:00+00:00"),
                          [f"sha{i}" for i in range(100)],
                          meta("2024-01-01T00:00:00Z"),
                          fp(first_date="2020-01-01T00:00:00+00:00"),
                          [f"sha{i}" for i in range(100)],
                          meta("2020-01-01T00:00:00Z"),
                          c, "new", "old")
        self.assertEqual(v["type"], "direct_descendant")
        self.assertEqual(v["confidence"], "high")

    def test_many_identical_files_means_direct_descendant(self):
        c = comp(identical_same_path={"count": 50, "examples": []})
        v = evaluate_pair(fp(first_date="2024-01-01T00:00:00+00:00"), None,
                          meta("2024-01-01T00:00:00Z"),
                          fp(first_date="2020-01-01T00:00:00+00:00"), None,
                          meta("2020-01-01T00:00:00Z"),
                          c, "new", "old")
        self.assertEqual(v["type"], "direct_descendant")

    def test_acknowledgment_with_support(self):
        c = comp(
            acknowledgment_in_a=[{"kind": "fork-of-name", "quote": "forked from old",
                                  "target": "old"}],
            name_similarity={"score": 0.8, "pair": ("new", "old")},
        )
        v = evaluate_pair(fp(first_date="2024-01-01T00:00:00+00:00"), None,
                          meta("2024-01-01T00:00:00Z"),
                          fp(first_date="2020-01-01T00:00:00+00:00"), None,
                          meta("2020-01-01T00:00:00Z"),
                          c, "new", "old")
        self.assertEqual(v["type"], "acknowledged_derivation")

    def test_shared_code_localized_is_vendored(self):
        c = comp(
            identifier_jaccard=0.2,
            shared_rare_identifiers={"count": 10,
                                     "examples": ["xenotransplantation_protocol"]},
            very_similar_files={"count": 3,
                                "examples": ["vendor/z.py (0.9)", "vendor/y.py (0.95)"]},
            identical_same_path={"count": 2, "examples": ["vendor/z.py", "vendor/y.py"]},
        )
        v = evaluate_pair(fp(top_dirs=["src", "vendor", "docs", "tests", "bin"],
                              first_date="2024-01-01T00:00:00+00:00"), None,
                          meta("2024-01-01T00:00:00Z"),
                          fp(first_date="2020-01-01T00:00:00+00:00"), None,
                          meta("2020-01-01T00:00:00Z"),
                          c, "new", "old")
        self.assertEqual(v["type"], "vendored_component")

    def test_cross_language_port(self):
        c = comp(
            name_similarity={"score": 0.67, "pair": ("gojq", "jq")},
            languages={"shared": [], "a": {"Go": 10}, "b": {"C": 20}},
        )
        v = evaluate_pair(fp(first_date="2021-01-01T00:00:00+00:00"), None,
                          meta("2021-01-01T00:00:00Z"),
                          fp(first_date="2012-01-01T00:00:00+00:00"), None,
                          meta("2012-01-01T00:00:00Z"),
                          c, "gojq", "jq")
        self.assertEqual(v["type"], "port_or_reimplementation")

    def test_dep_overlap_alone_is_rejected(self):
        """Shared libraries must never count as ancestry. The restraint test."""
        c = comp(dep_overlap={"names": ["minimist", "chalk"], "count": 2,
                              "a_total": 5, "b_total": 6},
                 name_similarity={"score": 0.2, "pair": ("meow", "commander")})
        v = evaluate_pair(fp(), None, meta(), fp(), None, meta(), c, "meow", "commander")
        self.assertEqual(v["type"], "no_meaningful_relationship")
        self.assertIn("minimist", v["summary"])

    def test_nothing_shared_is_rejected(self):
        v = evaluate_pair(fp(), None, meta(), fp(), None, meta(),
                          comp(), "aaa", "bbb")
        self.assertEqual(v["type"], "no_meaningful_relationship")

    def test_reversed_timeline_caps_confidence(self):
        """If the 'derivative' is older than the 'upstream', something is off."""
        c = comp(identical_same_path={"count": 50, "examples": []})
        v = evaluate_pair(fp(first_date="2018-01-01T00:00:00+00:00"), None,
                          meta("2018-01-01T00:00:00Z"),
                          fp(first_date="2024-01-01T00:00:00+00:00"), None,
                          meta("2024-01-01T00:00:00Z"),
                          c, "older", "newer")
        self.assertEqual(v["type"], "direct_descendant")
        self.assertEqual(v["confidence"], "low")
        self.assertTrue(any("inconsistent" in (u or "") for u in v["uncertainty"]))

    def test_thresholds_are_persisted_constants(self):
        self.assertIn("shared_commits_direct_descendant", THRESHOLDS)


if __name__ == "__main__":
    unittest.main()
