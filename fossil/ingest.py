"""Ingest a target into a case workspace, read-only.

Accepted sources:
  - GitHub shorthand:        owner/repo
  - Git URL:                 https://github.com/owner/repo(.git), git@..., any git URL
  - Local directory:         ./path  (with or without .git)
  - ZIP archive:             ./project.zip

Remote repositories are cloned (never pushed to). Local inputs are copied,
never modified.
"""

import os
import shutil
import subprocess
import zipfile

from . import github as gh

CLONE_TIMEOUT = 600

# Directories never copied from a local tree (build junk, dependency dumps).
_SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", "env",
    "target", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
}


class IngestError(Exception):
    pass


def detect_source(source):
    s = (source or "").strip()
    if os.path.isdir(s):
        return ("local", os.path.abspath(s))
    if os.path.isfile(s) and s.lower().endswith(".zip"):
        return ("zip", os.path.abspath(s))
    parsed = gh.parse_github_source(s)
    if parsed:
        owner, repo = parsed
        return ("github", f"https://github.com/{owner}/{repo}.git")
    if s.startswith(("http://", "https://", "git@", "ssh://", "file://")):
        return ("git-url", s)
    raise IngestError(
        f"Cannot understand source {source!r}: expected owner/repo, a git URL, "
        "a local directory, or a .zip file."
    )


def _run(cmd, timeout):
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise IngestError(f"Timed out after {timeout}s: {' '.join(cmd)}")
    if p.returncode != 0:
        raise IngestError(f"{' '.join(cmd)} failed: {p.stderr.strip()[:400]}")
    return p.stdout.strip()


def _clone(url, dest, depth, full_history):
    if os.path.exists(dest):
        shutil.rmtree(dest)
    cmd = ["git", "clone", "--quiet"]
    if not full_history:
        cmd += ["--depth", str(depth)]
    cmd += [url, dest]
    _run(cmd, CLONE_TIMEOUT)


def _copy_local(src, dest):
    if os.path.exists(dest):
        shutil.rmtree(dest)

    def _ignore(d, names):
        return [n for n in names if n in _SKIP_DIRS]

    # A local checkout with .git clones fast and keeps history; otherwise copy.
    if os.path.isdir(os.path.join(src, ".git")):
        _run(["git", "clone", "--quiet", "--local", src, dest], CLONE_TIMEOUT)
    else:
        shutil.copytree(src, dest, ignore=_ignore)


def _unzip(src, dest):
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with zipfile.ZipFile(src) as z:
        # Guard against zip-slip.
        for m in z.namelist():
            target = os.path.realpath(os.path.join(dest, m))
            if not target.startswith(os.path.realpath(dest) + os.sep):
                raise IngestError(f"Unsafe path in zip archive: {m!r}")
        z.extractall(dest)
    # Collapse a single top-level wrapper directory.
    entries = [e for e in os.listdir(dest) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        inner = os.path.join(dest, entries[0])
        tmp = dest + "__unwrapped"
        os.rename(inner, tmp)
        shutil.rmtree(dest)
        os.rename(tmp, dest)


def ingest(source, dest, depth=150, full_history=False):
    """Ingest `source` into `dest`. Returns an info dict for the evidence bundle."""
    kind, norm = detect_source(source)
    if kind in ("github", "git-url"):
        _clone(norm, dest, depth, full_history)
    elif kind == "local":
        _copy_local(norm, dest)
    elif kind == "zip":
        _unzip(norm, dest)

    info = {
        "kind": kind,
        "source": source,
        "normalized": norm,
        "dest": dest,
        "depth": None if full_history else depth,
        "full_history": bool(full_history),
    }
    gi = git_info(dest)
    if gi:
        info["git"] = gi
    return info


def git_info(tree):
    """History facts from a checkout. Returns None when there is no git history."""
    gitdir = os.path.join(tree, ".git")
    if not os.path.exists(gitdir):
        return None
    try:
        count = int(_run(["git", "-C", tree, "rev-list", "--count", "HEAD"], 60))
        first = _run(
            ["git", "-C", tree, "log", "--reverse", "--format=%H%x00%aI%x00%s", "HEAD"],
            60,
        ).splitlines()[0].split("\x00")
        last = _run(
            ["git", "-C", tree, "log", "-1", "--format=%H%x00%aI%x00%s", "HEAD"], 60
        ).split("\x00")
        authors = _run(["git", "-C", tree, "shortlog", "-sne", "HEAD"], 60).splitlines()[:10]
        tags = _run(["git", "-C", tree, "tag"], 60).splitlines()
        head = _run(["git", "-C", tree, "rev-parse", "HEAD"], 30)
        # All SHAs present (bounded) — used for shared-commit detection.
        shas = _run(
            ["git", "-C", tree, "log", "--format=%H", "-n", "20000", "HEAD"], 120
        ).splitlines()
    except (IngestError, IndexError, ValueError):
        return None
    return {
        "commits": count,
        "head": head,
        "first": {"sha": first[0], "date": first[1], "subject": first[2][:120]},
        "last": {"sha": last[0], "date": last[1], "subject": last[2][:120]},
        "top_authors": [a.strip() for a in authors],
        "tags": len(tags),
        "shas": shas,  # in-memory only; stripped before persisting
    }
