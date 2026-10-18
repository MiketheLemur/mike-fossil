"""Read-only GitHub REST client (stdlib only).

Works without a token (60 requests/hour) but a token is strongly
recommended for discovery runs: pass --token or set GITHUB_TOKEN.
Fossil never writes to GitHub — GET requests only.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class RateLimited(GitHubError):
    def __init__(self, reset_in):
        self.reset_in = reset_in
        super().__init__(
            f"GitHub API rate limit exhausted (resets in ~{int(reset_in)}s). "
            "Re-run with --token or set GITHUB_TOKEN to raise the limit."
        )


class NotFound(GitHubError):
    pass


def _headers(token):
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"mike-fossil/{__version__} (software-archaeology research tool)",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def api_get(path, token=None, params=None, timeout=30):
    """GET a GitHub API path. Returns parsed JSON, or None on 404."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (403, 429):
            remaining = e.headers.get("X-RateLimit-Remaining")
            if remaining == "0" or e.code == 429:
                reset = e.headers.get("X-RateLimit-Reset")
                wait = 60
                if reset:
                    try:
                        wait = max(1, int(reset) - int(time.time()))
                    except ValueError:
                        pass
                raise RateLimited(wait)
            body = e.read(2000).decode("utf-8", "replace")
            raise GitHubError(f"GitHub API refused the request ({e.code}): {body[:300]}")
        body = e.read(2000).decode("utf-8", "replace")
        raise GitHubError(f"GitHub API error {e.code} for {path}: {body[:300]}")
    except urllib.error.URLError as e:
        raise GitHubError(f"Network error reaching api.github.com: {e.reason}")


def repo_meta(full_name, token=None):
    """Repository metadata: dates, fork flag, parent, description, language."""
    data = api_get(f"/repos/{full_name}", token=token)
    if data is None:
        raise NotFound(f"GitHub repository not found: {full_name}")
    parent = (data.get("parent") or {}).get("full_name")
    return {
        "full_name": data.get("full_name"),
        "description": data.get("description") or "",
        "homepage": data.get("homepage") or "",
        "language": data.get("language") or "",
        "created_at": data.get("created_at"),
        "pushed_at": data.get("pushed_at"),
        "default_branch": data.get("default_branch") or "HEAD",
        "fork": bool(data.get("fork")),
        "parent": parent,
        "archived": bool(data.get("archived")),
        "stargazers": data.get("stargazers_count", 0),
        "html_url": data.get("html_url"),
    }


def search_repos(query, token=None, per_page=8):
    """Repository search. Returns list of repo dicts (same shape as repo_meta)."""
    data = api_get(
        "/search/repositories",
        token=token,
        params={"q": query, "per_page": per_page},
    )
    if not data:
        return []
    out = []
    for r in data.get("items", []):
        out.append(
            {
                "full_name": r.get("full_name"),
                "description": r.get("description") or "",
                "language": r.get("language") or "",
                "created_at": r.get("created_at"),
                "pushed_at": r.get("pushed_at"),
                "default_branch": r.get("default_branch") or "HEAD",
                "fork": bool(r.get("fork")),
                "stargazers": r.get("stargazers_count", 0),
                "html_url": r.get("html_url"),
            }
        )
    # Be polite to the search endpoint (10 req/min unauthenticated).
    time.sleep(1.0)
    return out


def parse_github_source(source):
    """Extract (owner, repo) from 'owner/repo', an https git URL, or None."""
    s = (source or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[: -len(".git")]
    # owner/repo shorthand
    if "/" in s and "://" not in s and "@" not in s and " " not in s:
        parts = s.split("/")
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]
        return None
    # https://github.com/owner/repo...
    if "github.com" in s:
        try:
            path = urllib.parse.urlparse(s).path.strip("/")
            parts = path.split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
        except Exception:
            pass
    return None
