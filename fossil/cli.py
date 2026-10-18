"""Fossil command line interface."""

import argparse
import os
import sys

from . import __version__
from .github import GitHubError, RateLimited
from .store import list_cases, load_json
from .report import render_report


def _token(args):
    return args.token or os.environ.get("GITHUB_TOKEN")


def cmd_investigate(args):
    from .hypotheses import investigate
    try:
        case_dir, evidence, _report = investigate(
            args.source,
            token=_token(args),
            max_candidates=args.max_candidates,
            depth=args.depth,
            full_history=args.full_history,
        )
    except RateLimited as e:
        print(f"fossil: {e}", file=sys.stderr)
        return 2
    except GitHubError as e:
        print(f"fossil: GitHub error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"fossil: investigation failed: {e}", file=sys.stderr)
        return 1
    f = evidence["finding"]
    print(f"case:    {case_dir}")
    if f["kind"] == "relationship_found":
        v = f["verdict"]
        print(f"finding:  {v['type']} (confidence: {v['confidence']})")
        print(f"primary:  {f['primary']}")
    else:
        print("finding:  no meaningful relationship established")
    print(f"report:  {os.path.join(case_dir, 'report.md')}")
    return 0


def cmd_compare(args):
    from .hypotheses import compare_pair
    try:
        case_dir, evidence, _report = compare_pair(
            args.a, args.b,
            token=_token(args),
            depth=args.depth,
            full_history=args.full_history,
        )
    except RateLimited as e:
        print(f"fossil: {e}", file=sys.stderr)
        return 2
    except GitHubError as e:
        print(f"fossil: GitHub error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"fossil: comparison failed: {e}", file=sys.stderr)
        return 1
    v = evidence["verdict"]
    print(f"case:    {case_dir}")
    print(f"verdict: {v['type']} (confidence: {v['confidence']})")
    print(f"report:  {os.path.join(case_dir, 'report.md')}")
    return 0


def cmd_report(args):
    path = os.path.join(args.case, "evidence.json")
    if not os.path.exists(path):
        print(f"fossil: no such case: {args.case}", file=sys.stderr)
        return 1
    ev = load_json(path)
    mode = "compare" if ev.get("inputs", {}).get("mode") == "compare" else "investigate"
    if mode == "compare":
        title = f"{ev['a']['label']} vs {ev['b']['label']}"
    else:
        title = ev["target"]["label"]
    out = render_report(ev, title, mode=mode)
    dest = os.path.join(args.case, "report.md")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"re-rendered: {dest}")
    return 0


def cmd_list(_args):
    cases = list_cases()
    if not cases:
        print("no cases yet")
        return 0
    for cid, meta in cases:
        src = meta.get("source") or meta.get("a", "")
        print(f"{cid}  {src}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="fossil",
        description="Software archaeology: investigate the ancestry of code. "
                    "Read-only — never pushes or publishes anything.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--token", default=None,
                   help="GitHub token (or set GITHUB_TOKEN); raises API limits")
    p.add_argument("--data-dir", default=None,
                   help="Case storage root (default ~/.fossil)")

    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("investigate",
                         help="Investigate a repo/dir/zip; discover candidates on its own")
    inv.add_argument("source", help="owner/repo, git URL, local dir, or .zip")
    inv.add_argument("--max-candidates", type=int, default=5)
    inv.add_argument("--depth", type=int, default=150,
                     help="git clone depth (default 150)")
    inv.add_argument("--full-history", action="store_true",
                     help="clone complete git history (slower, stronger evidence)")
    inv.set_defaults(func=cmd_investigate)

    cmp_ = sub.add_parser("compare",
                          help="Focused comparison of two repositories")
    cmp_.add_argument("a", help="first repo (the suspected derivative)")
    cmp_.add_argument("b", help="second repo (the suspected upstream)")
    cmp_.add_argument("--depth", type=int, default=150)
    cmp_.add_argument("--full-history", action="store_true")
    cmp_.set_defaults(func=cmd_compare)

    rep = sub.add_parser("report", help="Re-render a case's report from its evidence")
    rep.add_argument("case", help="case directory")
    rep.set_defaults(func=cmd_report)

    lst = sub.add_parser("list", help="List past investigations")
    lst.set_defaults(func=cmd_list)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.data_dir:
        os.environ["FOSSIL_DATA"] = args.data_dir
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
