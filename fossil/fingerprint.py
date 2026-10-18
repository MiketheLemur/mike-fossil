"""Deterministic fingerprinting of a source tree.

Everything here is reproducible byte-level measurement, no model involved:
file inventories, content hashes, token shingles (bottom-k minhash sketches),
identifier vocabularies, dependency manifests, license guesses, README
signals, and git history facts.

Conventions:
  - fingerprint() returns a plain JSON-serializable dict.
  - Keys starting with '_' hold in-memory-only auxiliaries (full per-file
    shingle sets for comparison) and are stripped before the evidence bundle
    is written to disk.
"""

import hashlib
import json
import os
import re
from collections import Counter

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

# Files too big for shingle analysis still get a whole-file hash.
MAX_SHINGLE_BYTES = 400 * 1024
PER_FILE_SHINGLES = 1500
GLOBAL_MINHASH = 4096

CODE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".c", ".h", ".cc",
    ".cpp", ".hpp", ".cxx", ".rs", ".go", ".java", ".rb", ".php", ".swift",
    ".kt", ".kts", ".scala", ".cs", ".m", ".mm", ".pl", ".pm", ".sh",
    ".bash", ".lua", ".r", ".jl", ".ex", ".exs", ".erl", ".hs", ".ml",
    ".vim", ".el", ".ps1", ".sql", ".vue", ".svelte",
}
TEXT_EXTS = CODE_EXTS | {
    ".md", ".markdown", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".xml", ".html", ".htm", ".css", ".scss",
}

LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++",
    ".rs": "Rust", ".go": "Go", ".java": "Java", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".kt": "Kotlin", ".cs": "C#", ".sh": "Shell",
    ".lua": "Lua", ".r": "R", ".md": "Markdown",
}

# Words too generic to count as "distinctive identifiers": plain English plus
# keywords of the common languages (a stoplist, not a parser — over-inclusion
# is safe because these words carry no authorship signal anyway).
STOPWORDS = frozenset("""
a about above after again against all also always an and any are as at be
because been before being between both but by can cannot could did do does
doing done down during each else error errors even every false falsey for
from further get gets getting had has have having her here hers himself his
how however into is it its itself just like main make makes many me more
most my new next no none not null now of off old on once one ones only or
other our out over own same self set sets should since small some such test
tests than that the their them then there these they this those through to
too true under until up use used uses using value values var version very
via was way we were what when where which while who will with within
without would you your
index main app src lib test tests dist build docs doc example examples
tmp temp util utils common core base default config configs data info
configuration configurations description descriptions options option
settings setting params parameters arguments argument instance instances
""".split())

KEYWORDS = frozenset("""
import from def return class lambda assert yield raise except finally while
with pass global nonlocal elif function const export default extends
instanceof await async throw catch switch break continue include define
struct typedef static extern sizeof enum union unsigned signed goto
fn mut impl trait match loop move crate func package interface select range
fallthrough defer public private protected implements final throws
abstract native synchronized transient volatile boolean module require echo
print println
""".split())

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TOKEN_RE = re.compile(r"[a-z0-9_]+")

# "X is a <relation> of Y" — acknowledgment signals. Recorded as candidate
# generators and supporting evidence, never as verdicts by themselves.
ACK_PATTERNS = [
    (r"(?:forked?\s+from|a\s+fork\s+of)\s+([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", "fork-of"),
    (r"(?:based\s+on|derived\s+from)\s+([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", "based-on"),
    (r"(?:inspired\s+by)\s+([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", "inspired-by"),
    (r"(?:port\s+of|a\s+(?:\w+\s+)?port\s+of)\s+([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", "port-of"),
    (r"(?:forked?\s+from|a\s+fork\s+of)\s+\"?([A-Za-z0-9_.\-]+)\"?", "fork-of-name"),
    (r"(?:based\s+on|derived\s+from|inspired\s+by)\s+\"?([A-Za-z0-9_.\-]+)\"?", "based-on-name"),
    (r"(?:port\s+of)\s+\"?([A-Za-z0-9_.\-]+)\"?", "port-of-name"),
    (r"(?:alternative\s+to|replacement\s+for|successor\s+to|rewritten?\s+(?:from|in)|clone\s+of)\s+\"?([A-Za-z0-9_.\-]+)\"?", "alt-of-name"),
    (r"(?:implementation\s+of)\s+\"?([A-Za-z0-9_.\-]+)\"?", "implementation-of-name"),
]

# Any github.com/owner/repo link in docs is a lead worth chasing
# (upstream repos, inspirations, and vendored sources all get linked).
GITHUB_LINK_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)")

LICENSE_PHRASES = [
    (("mit license", "permission is hereby granted"), "MIT"),
    (("apache license", "version 2.0"), "Apache-2.0"),
    (("gnu general public license", "version 3"), "GPL-3.0"),
    (("gnu general public license", "version 2"), "GPL-2.0"),
    (("gnu lesser general public license",), "LGPL"),
    (("redistribution and use in source and binary forms", "neither the name"), "BSD-3-Clause"),
    (("redistribution and use in source and binary forms",), "BSD-2-Clause"),
    (("isc license",), "ISC"),
    (("mozilla public license", "version 2.0"), "MPL-2.0"),
    (("european union public licence",), "EUPL-1.2"),
    (("this is free and unencumbered software released into the public domain",), "Unlicense"),
]


def _sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _looks_binary(path, size):
    if size == 0:
        return False
    with open(path, "rb") as f:
        head = f.read(4096)
    return b"\x00" in head


def _read_text(path, limit=1_000_000):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(limit)


def _shingle_hashes(text, k=5):
    toks = TOKEN_RE.findall(text.lower())
    if len(toks) < k:
        return set()
    out = set()
    for i in range(len(toks) - k + 1):
        gram = " ".join(toks[i:i + k])
        out.add(int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big"))
    return out


def _bottom_k(hashes, k):
    return sorted(hashes)[:k]


def _strip_noise(text, ext):
    """Remove comments and string literals (approximate, regex-based).

    Identifier vocabularies should reflect naming choices, not prose in
    comments or string contents. This is intentionally rough: it can
    misfire on exotic quoting, which only costs us a few identifiers.
    Shingle analysis keeps the raw text — shared comments are legitimate
    evidence of copying, just not of *naming*.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)          # /* block */
    if ext == ".py":
        text = re.sub(r'"""(?:.|\n)*?"""', " ", text)           # docstrings
        text = re.sub(r"'''(?:.|\n)*?'''", " ", text)
    text = re.sub(r"'(?:[^'\\\n]|\\.)*'", " ", text)             # '...'
    text = re.sub(r'"(?:[^"\\\n]|\\.)*"', " ", text)             # "..."
    if ext in (".go", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
        text = re.sub(r"`(?:[^`\\]|\\.)*`", " ", text)           # `raw`
    if ext in {".py", ".rb", ".sh", ".bash", ".r", ".pl", ".pm",
               ".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        text = re.sub(r"#[^\n]*", " ", text)                    # # comment
    else:
        text = re.sub(r"//[^\n]*", " ", text)                   # // comment
    return text


def _identifiers(text, ext=""):
    out = []
    for m in IDENT_RE.finditer(_strip_noise(text, ext)):
        w = m.group(0)
        # Skip numeric literals like 1_000_000.
        if len(w) >= 4 and not re.fullmatch(r"_?[0-9_]+", w):
            out.append(w.lower())
    return out


def _parse_manifests(tree):
    manifests = []
    for root, _dirs, files in os.walk(tree):
        if ".git" in root.split(os.sep):
            continue
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, tree)
            try:
                if fn == "package.json":
                    d = json.loads(_read_text(p, 200000))
                    deps = list((d.get("dependencies") or {}).keys()) + \
                           list((d.get("devDependencies") or {}).keys())
                    manifests.append({"file": rel, "name": d.get("name", ""),
                                      "version": d.get("version", ""),
                                      "description": d.get("description", ""),
                                      "license": d.get("license", ""),
                                      "deps": sorted(set(deps))})
                elif fn in ("Cargo.toml", "pyproject.toml") and tomllib:
                    d = tomllib.loads(_read_text(p, 200000))
                    if fn == "Cargo.toml":
                        pkg = d.get("package", {})
                        deps = list((d.get("dependencies") or {}).keys())
                        manifests.append({"file": rel, "name": pkg.get("name", ""),
                                          "version": pkg.get("version", ""),
                                          "description": pkg.get("description", ""),
                                          "license": pkg.get("license", ""),
                                          "deps": sorted(set(deps))})
                    else:
                        proj = d.get("project", {})
                        deps = [str(x).split()[0] for x in (proj.get("dependencies") or [])]
                        manifests.append({"file": rel, "name": proj.get("name", ""),
                                          "version": proj.get("version", ""),
                                          "description": proj.get("description", ""),
                                          "license": str(proj.get("license", "")),
                                          "deps": sorted(set(deps))})
                elif fn == "go.mod":
                    txt = _read_text(p, 100000)
                    m = re.search(r"^module\s+(\S+)", txt, re.M)
                    deps = re.findall(r"^\s+(\S+)\s+v\S+", txt, re.M)
                    manifests.append({"file": rel, "name": m.group(1) if m else "",
                                      "version": "", "description": "", "license": "",
                                      "deps": sorted(set(deps))})
                elif fn == "requirements.txt":
                    deps = []
                    for line in _read_text(p, 100000).splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            deps.append(re.split(r"[=<>!;\s\[]", line)[0])
                    manifests.append({"file": rel, "name": "", "version": "",
                                      "description": "", "license": "",
                                      "deps": sorted(set(d for d in deps if d))})
                elif fn == "composer.json":
                    d = json.loads(_read_text(p, 200000))
                    deps = list((d.get("require") or {}).keys())
                    manifests.append({"file": rel, "name": d.get("name", ""),
                                      "version": "", "description": d.get("description", ""),
                                      "license": str(d.get("license", "")),
                                      "deps": sorted(set(deps))})
            except Exception:
                continue
    return manifests


def _guess_license(text):
    low = text.lower()
    for phrases, spdx in LICENSE_PHRASES:
        if all(p in low for p in phrases):
            return spdx
    return "unknown"


def _readme_signals(tree):
    for root, _dirs, files in os.walk(tree):
        if ".git" in root.split(os.sep):
            continue
        for fn in files:
            if fn.lower().startswith("readme"):
                p = os.path.join(root, fn)
                try:
                    text = _read_text(p, 200000)
                except OSError:
                    continue
                signals = []
                taken_spans = []  # owner/repo matches win over bare-name ones
                for pat, kind in ACK_PATTERNS:
                    for m in re.finditer(pat, text, re.IGNORECASE):
                        span = m.span()
                        if kind.endswith("-name") and any(
                                s <= span[0] < e or s < span[1] <= e
                                for s, e in taken_spans):
                            continue
                        if not kind.endswith("-name"):
                            taken_spans.append(span)
                        quote = m.group(0).strip()
                        if len(quote) > 160:
                            quote = quote[:160] + "…"
                        target = m.group(1).rstrip(".,:;!?\"'[]()")
                        signals.append({"kind": kind, "quote": quote,
                                        "target": target})
                links = []
                for m in GITHUB_LINK_RE.finditer(text):
                    link = m.group(1).rstrip(".,:;!?\"'[]()")
                    # Drop deep links (actions, issues, ...) to owner/repo.
                    link = "/".join(link.split("/")[:2])
                    if link not in links:
                        links.append(link)
                for link in links:
                    signals.append({"kind": "readme-link",
                                    "quote": f"github.com/{link}",
                                    "target": link})
                return {"file": os.path.relpath(p, tree),
                        "chars": len(text),
                        "head": text[:2000],
                        "signals": signals,
                        "github_links": links}
    return {"file": None, "chars": 0, "head": "", "signals": [],
            "github_links": []}


def fingerprint(tree, repo_hint=""):
    files = []
    content_index = {}
    languages = Counter()
    ident_counter = Counter()
    per_file_shingles = {}
    global_hashes = set()
    total_bytes = 0
    notes = []

    for root, dirs, filenames in os.walk(tree):
        # Never descend into .git or dependency/build dumps.
        dirs[:] = [d for d in dirs
                   if d != ".git" and d not in
                   {"node_modules", "__pycache__", "target", "dist", "build", ".venv"}]
        for fn in filenames:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, tree)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            total_bytes += size
            try:
                binary = _looks_binary(p, size)
            except OSError:
                continue
            sha1 = _sha1_file(p)
            kind = "binary" if binary else "text"
            files.append({"path": rel, "size": size, "sha1": sha1, "kind": kind})
            content_index.setdefault(sha1, []).append(rel)

            ext = os.path.splitext(fn)[1].lower()
            if ext in LANG_BY_EXT:
                languages[LANG_BY_EXT[ext]] += 1

            if not binary and ext in TEXT_EXTS and size <= 2 * 1024 * 1024:
                try:
                    text = _read_text(p)
                except OSError:
                    continue
                if ext in CODE_EXTS:
                    for ident in _identifiers(text, ext):
                        ident_counter[ident] += 1
                if size <= MAX_SHINGLE_BYTES:
                    sh = _shingle_hashes(text)
                    if sh:
                        kept = set(_bottom_k(sh, PER_FILE_SHINGLES))
                        per_file_shingles[rel] = kept
                        global_hashes.update(kept)

    distinctive = sorted(
        w for w in ident_counter
        if len(w) >= 6 and w not in STOPWORDS and w not in KEYWORDS
    )
    rare_idents = sorted(w for w in distinctive if len(w) >= 10)

    manifests = _parse_manifests(tree)
    licenses = []
    for f in files:
        base = os.path.basename(f["path"]).lower()
        if base.startswith(("license", "copying", "licence")) and f["kind"] == "text":
            try:
                guess = _guess_license(_read_text(os.path.join(tree, f["path"]), 200000))
            except OSError:
                guess = "unknown"
            licenses.append({"file": f["path"], "sha1": f["sha1"], "guess": guess})

    readme = _readme_signals(tree)
    top_dirs = sorted({f["path"].split(os.sep)[0]
                       for f in files if os.sep in f["path"]})[:30]

    fp = {
        "repo_hint": repo_hint,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "files": sorted(files, key=lambda f: f["path"]),
        "content_index": content_index,
        "languages": dict(languages.most_common()),
        "top_dirs": top_dirs,
        "manifests": manifests,
        "licenses": licenses,
        "readme": readme,
        "identifiers": {
            "distinct_count": len(ident_counter),
            "top": ident_counter.most_common(40),
            "distinctive": distinctive,
            "rare": rare_idents,
        },
        "minhash": _bottom_k(global_hashes, GLOBAL_MINHASH),
        "notes": notes,
        # In-memory only: full per-file shingle sets for comparison.
        "_full_shingles": {k: sorted(v) for k, v in per_file_shingles.items()},
    }
    return fp


def manifest_names(fp):
    names = []
    for m in fp.get("manifests", []):
        if m.get("name"):
            names.append(m["name"])
    if fp.get("repo_hint"):
        names.append(fp["repo_hint"])
    return names


def all_deps(fp):
    deps = set()
    for m in fp.get("manifests", []):
        deps.update(m.get("deps", []))
    return deps
