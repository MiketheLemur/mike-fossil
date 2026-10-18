"""The investigator: forms hypotheses, tests them against deterministic
evidence, and rejects weak leads — with every step logged.

A hypothesis is a guess that candidate X stands in some relation to the
target. Testing means: ingest X, fingerprint it, compare deterministically,
and run the verdict rules. Rejection reasons are recorded, not hidden.

Verdict rules are explicit thresholds (see THRESHOLDS, persisted into every
evidence bundle). A similarity never becomes a verdict on its own: each
verdict type requires corroborating evidence, and alternative explanations
(independent implementation, shared libraries, common templates, shared
upstream) must be addressed in the report.
"""

import datetime
import os
import re

from . import __version__
from . import github as gh
from .compare import compare
from .fingerprint import fingerprint, manifest_names
from .ingest import ingest, IngestError
from .report import render_report
from .store import new_case, save_json, CaseLog

# --------------------------------------------------------------------------
# Verdict thresholds. Persisted into evidence.json so findings stay
# interpretable after the fact.
# --------------------------------------------------------------------------
THRESHOLDS = {
    "shared_commits_direct_descendant": 30,
    "identical_files_direct_descendant": 15,
    "identical_files_high_confidence": 40,
    "identifier_jaccard_shared_code": 0.12,
    "rare_identifiers_shared_code": 8,
    "name_similarity_port": 0.5,
    "readme_jaccard_acknowledged": 0.15,
    "very_similar_file_threshold": 0.85,
}

COMMON_NAME_TOKENS = frozenset("""
app lib core api cli tool tools kit framework engine sys system hub base
node py js go rs ts web data info manager client server utils common
""".split())


def _parse_date(s):
    if not s:
        return None
    try:
        # Handles 2024-03-01T12:00:00Z and git's +00:00 offsets.
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_date(fp_git, meta):
    """Best available 'birth date' for a project.

    Prefer the GitHub repo creation date: for a fork, the first commit in
    the clone is the *parent's* first commit, which would wrongly date the
    fork to the parent's birth. Fall back to first commit for non-GitHub
    or offline inputs.
    """
    if meta and meta.get("created_at"):
        return _parse_date(meta["created_at"]), "repo creation"
    if fp_git and fp_git.get("first", {}).get("date"):
        return _parse_date(fp_git["first"]["date"]), "first commit"
    return None, "unknown"


def timeline(target_label, a_fp, a_meta, b_label, b_fp, b_meta):
    events = []
    seen = set()

    def add(label, fp, meta):
        d, kind = _first_date((fp or {}).get("_git"), meta)
        if d and (label, d.date().isoformat(), kind) not in seen:
            seen.add((label, d.date().isoformat(), kind))
            events.append({"date": d.isoformat(), "label": label, "kind": kind})
        # Second opinion: first commit date, when it differs from creation.
        git = (fp or {}).get("_git") or {}
        fd = git.get("first", {}).get("date")
        d2 = _parse_date(fd) if fd else None
        if d2 and (not d or abs((d2 - d).days) > 30):
            key = (label, d2.date().isoformat(), "first commit")
            if key not in seen:
                seen.add(key)
                events.append({"date": d2.isoformat(), "label": label,
                               "kind": "first commit in clone"})
    add(target_label, a_fp, a_meta)
    add(b_label, b_fp, b_meta)
    events.sort(key=lambda e: e["date"])
    direction = None
    delta_days = None
    # Direction from the *preferred* birth dates (repo creation when known),
    # never from a fork's inherited first commit, which belongs to its parent.
    da, _sa = _first_date((a_fp or {}).get("_git"), a_meta)
    db, _sb = _first_date((b_fp or {}).get("_git"), b_meta)
    if da and db:
        delta_days = abs((da - db).days)
        if da < db:
            direction = f"{target_label} predates {b_label} by {delta_days} days"
        elif db < da:
            direction = f"{b_label} predates {target_label} by {delta_days} days"
        else:
            direction = "same day"
    return {"events": events, "direction": direction, "delta_days": delta_days}


# --------------------------------------------------------------------------
# Candidate discovery (the "discovery" test exercises this path)
# --------------------------------------------------------------------------
def _name_tokens(fp):
    toks = []
    for name in manifest_names(fp):
        # go.mod module paths like github.com/itchyny/gojq -> last segment
        name = name.split("/")[-1]
        for t in re.split(r"[-_.]+", name):
            t = t.strip().lower()
            if len(t) >= 3 and t not in COMMON_NAME_TOKENS:
                toks.append(t)
    # Distinctive first, preserve order.
    seen, out = set(), []
    for t in sorted(toks, key=len, reverse=True):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:4]


def _description_keywords(meta):
    """Distinctive words from the repo description.

    Short words are kept: in "Pure Go implementation of jq", the load-bearing
    word is "jq", not "implementation". Generic descriptor words are dropped.
    """
    desc = (meta or {}).get("description", "") or ""
    words = [w.strip(".,:;!?()\"'").lower() for w in desc.split()]
    out, seen = [], set()
    for w in words:
        if (len(w) >= 2 and w not in DESC_COMMON and w not in COMMON_NAME_TOKENS
                and re.fullmatch(r"[a-z0-9_.\-]+", w) and w not in seen):
            seen.add(w)
            out.append(w)
    return out[:4]


DESC_COMMON = frozenset("""
implementation pure simple fast modern alternative replacement successor
library libraries tool tools command commands application app framework
client server written language languages based using use useful handy tiny
minimal powerful awesome collection collections list lists data info manager
api cli web official unofficial free open source
go python javascript typescript rust java ruby php swift kotlin dart haskell
lua shell bash
a an the of for and to in on with by said says say does did made makes make
using use uses used allows allow provides provide includes include based
""".split())

LANG_PREFIXES = ("go", "py", "js", "ts", "rs", "rb")


def _name_cores(tokens):
    """Strip language affixes: gojq -> jq, python-foo -> foo, bar-rs -> bar."""
    cores = []
    for t in tokens:
        for p in LANG_PREFIXES:
            if t.startswith(p) and len(t) - len(p) >= 2:
                cores.append(t[len(p):])
                break
        m = re.match(r"^(.+?)[-_](go|py|js|ts|rs|rb)$", t)
        if m and len(m.group(1)) >= 2 and m.group(1) not in cores:
            cores.append(m.group(1))
    seen, out = set(), []
    for c in cores:
        if c not in seen and c not in COMMON_NAME_TOKENS:
            seen.add(c)
            out.append(c)
    return out


def discovery_signals(fp, meta, self_name=None):
    """Signals used to hunt for upstream candidates, strongest first.

    Each is a guess, not a fact — candidates still have to survive
    deterministic comparison before any verdict is drawn.
    """
    signals = []
    # 1. GitHub links in the README: the project literally pointing at repos.
    for link in fp["readme"].get("github_links", []):
        if self_name and link.lower() == self_name.lower():
            continue
        signals.append({"kind": "readme-link", "value": link, "direct": link})
    # 2. Acknowledgment phrasing ("port of X", "implementation of Y", ...).
    for sig in fp["readme"].get("signals", []):
        if sig["kind"] == "readme-link":
            continue
        tgt = sig.get("target", "")
        if "/" in tgt:
            signals.append({"kind": f"readme:{sig['kind']}", "value": tgt,
                            "direct": tgt})
        elif len(tgt) >= 2:
            signals.append({"kind": f"readme:{sig['kind']}", "value": tgt,
                            "query": f"{tgt} in:name"})
    # 3. Name cores ("gojq" -> "jq") and raw name tokens.
    tokens = _name_tokens(fp)
    for core in _name_cores(tokens):
        signals.append({"kind": "name-core", "value": core,
                        "query": f"{core} in:name"})
    for tok in tokens[:2]:
        signals.append({"kind": "name-token", "value": tok,
                        "query": f"{tok} in:name"})
    # 4. Distinctive description words ("jq" in "Pure Go implementation of jq").
    for kw in _description_keywords(meta)[:2]:
        signals.append({"kind": "description-keyword", "value": kw,
                        "query": f"{kw} in:name"})
    return signals


def discover_candidates(fp, meta, token, max_candidates=5, log=None, self_name=None):
    """Turn signals into a ranked candidate list via GitHub search."""
    signals = discovery_signals(fp, meta, self_name)
    if log:
        log.event("signals", signals=[{k: s[k] for k in ("kind", "value")} for s in signals])
    hits = {}  # full_name -> {"meta":..., "via": set()}
    searches = 0
    for sig in signals:
        if searches >= 6 and not sig.get("direct"):
            continue
        try:
            if sig.get("direct"):
                m = gh.repo_meta(sig["direct"], token)
                if m and m["full_name"] != self_name:
                    hits.setdefault(m["full_name"], {"meta": m, "via": set()})
                    hits[m["full_name"]]["via"].add(f"{sig['kind']}:{sig['value']}")
                continue
            searches += 1
            for r in gh.search_repos(sig["query"], token, per_page=8):
                if r["full_name"] == self_name:
                    continue
                hits.setdefault(r["full_name"], {"meta": r, "via": set()})
                hits[r["full_name"]]["via"].add(f"{sig['kind']}:{sig['value']}")
        except gh.GitHubError as e:
            if log:
                log.event("search_failed", query=sig.get("query"), error=str(e)[:200])
            continue
    ranked = sorted(hits.values(),
                    key=lambda h: (-len(h["via"]), h["meta"].get("full_name", "")))
    cands = []
    for h in ranked[:max_candidates]:
        cands.append({"full_name": h["meta"]["full_name"],
                      "via": sorted(h["via"]), "meta": h["meta"]})
    if log:
        log.event("candidates", candidates=[c["full_name"] for c in cands])
    return signals, cands


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------
def _shared_commit_count(shas_a, shas_b):
    if not shas_a or not shas_b:
        return 0
    return len(set(shas_a[:20000]) & set(shas_b[:20000]))


def evaluate_pair(a_fp, a_shas, a_meta, b_fp, b_shas, b_meta, comp,
                  a_label="target", b_label="candidate"):
    """Judge the relationship of A to B from deterministic comparison evidence.

    Returns a verdict dict: {type, confidence, summary, evidence[], uncertainty[]}.
    Types: direct_descendant, acknowledged_derivation, likely_shared_code,
    vendored_component, port_or_reimplementation, no_meaningful_relationship,
    inconclusive.
    """
    T = THRESHOLDS
    n_identical = (comp["identical_same_path"]["count"]
                   + comp["identical_diff_path"]["count"])
    n_shared_commits = _shared_commit_count(a_shas, b_shas)
    ident_j = comp["identifier_jaccard"]
    rare_n = comp["shared_rare_identifiers"]["count"]
    name_sim = comp["name_similarity"]["score"]
    ack = comp["acknowledgment_in_a"]
    dep_n = comp["dep_overlap"]["count"]

    tl = timeline(a_label, {"_git": (a_fp.get("_git_info"))}, a_meta,
                  b_label, {"_git": (b_fp.get("_git_info"))}, b_meta)
    b_older = tl["delta_days"] is not None and tl["direction"] is not None \
        and tl["direction"].startswith(b_label)

    def _uncertain_if_reversed(conf):
        if not b_older and tl["delta_days"]:
            return "low", (f"Timeline is inconsistent with {b_label} being upstream: "
                           f"{tl['direction']}. ")
        return conf, ""

    ev_summary = {
        "shared_commits": n_shared_commits,
        "identical_files": n_identical,
        "identifier_jaccard": ident_j,
        "shared_rare_identifiers": rare_n,
        "name_similarity": name_sim,
        "acknowledgment": bool(ack),
        "dep_overlap": dep_n,
        "timeline": tl["direction"],
    }

    # 1. Shared history: the strongest possible evidence of direct descent.
    if n_shared_commits >= T["shared_commits_direct_descendant"]:
        conf, flag = _uncertain_if_reversed("high")
        return {"type": "direct_descendant", "confidence": conf,
                "summary": (f"{a_label} shares {n_shared_commits} commits with {b_label} — "
                            "they have common git history."),
                "evidence": ev_summary,
                "uncertainty": [flag + "Shared commits prove common ancestry, not "
                                "which direction the fork went; the timeline orders them."]}

    # 2. Many byte-identical files: fork, copy, or shared template.
    if n_identical >= T["identical_files_direct_descendant"]:
        conf = "high" if n_identical >= T["identical_files_high_confidence"] else "medium"
        conf, flag = _uncertain_if_reversed(conf)
        return {"type": "direct_descendant", "confidence": conf,
                "summary": (f"{n_identical} byte-identical files between {a_label} and "
                            f"{b_label} — far beyond coincidence or shared libraries."),
                "evidence": ev_summary,
                "uncertainty": [flag + "Identical files prove copying or common origin; "
                                "check the timeline for direction."]}

    # 3. The project itself names its upstream.
    if ack and (name_sim >= T["name_similarity_port"]
                or ident_j >= 0.10 or comp["readme_jaccard"] >= T["readme_jaccard_acknowledged"]):
        specific = any("/" in (s.get("target") or "") for s in ack)
        conf = "high" if (specific and b_older) else "medium"
        conf, flag = _uncertain_if_reversed(conf)
        quotes = [s["quote"] for s in ack[:2]]
        return {"type": "acknowledged_derivation", "confidence": conf,
                "summary": (f"{a_label} acknowledges {b_label} in its own docs: "
                            + "; ".join(f'"{q}"' for q in quotes)),
                "evidence": ev_summary,
                "uncertainty": [flag + "Self-reported ancestry is strong but not infallible; "
                                "corroborating code/name evidence is also present."]}

    # 4. Shared distinctive code: copied component or vendored dependency.
    if rare_n >= T["rare_identifiers_shared_code"] and ident_j >= T["identifier_jaccard_shared_code"]:
        # Localized to a corner of the tree? -> vendored component.
        shared_paths = ([e.split(" (")[0] for e in comp["very_similar_files"]["examples"]]
                        + comp["identical_same_path"]["examples"][:10])
        topdirs = {p.split("/")[0] for p in shared_paths if "/" in p}
        a_topdirs = len(a_fp.get("top_dirs", [])) or 1
        localized = bool(shared_paths) and len(topdirs) <= 2 \
            and len(topdirs) / max(a_topdirs, 1) < 0.4
        vtype = "vendored_component" if localized else "likely_shared_code"
        conf, flag = _uncertain_if_reversed("medium")
        return {"type": vtype, "confidence": conf,
                "summary": (f"{rare_n} rare identifiers shared between {a_label} and {b_label} "
                            f"(identifier Jaccard {ident_j})"
                            + (" — concentrated in a subtree, suggesting a vendored component."
                               if localized else ".")),
                "evidence": ev_summary,
                "uncertainty": [flag + "Shared identifiers can also come from a common "
                                "third-party source both projects drew on."]}

    # 5. Different language, same name/shape: port or reimplementation.
    shared_langs = set(comp["languages"]["shared"]) - {"Markdown"}
    if not shared_langs and name_sim >= T["name_similarity_port"] and b_older:
        conf = "medium"
        return {"type": "port_or_reimplementation", "confidence": conf,
                "summary": (f"{a_label} appears to be a {comp['languages']['a']} "
                            f"reimplementation of the {comp['languages']['b']} project "
                            f"{b_label} (name similarity {name_sim}, no shared code language)."),
                "evidence": ev_summary,
                "uncertainty": ["No copied code is claimed: different implementation "
                                "languages mean similarity lives in API, behavior, and naming, "
                                "not in shared text. Independent clean-room reimplementation "
                                "cannot be ruled out from these signals alone."]}

    # 6/7. Nothing rose above the bar.
    reasons = []
    if dep_n:
        reasons.append(f"shares {dep_n} dependenc"
                       f"{'ies' if dep_n != 1 else 'y'} "
                       f"({', '.join(comp['dep_overlap']['names'][:5])}) — common "
                       "libraries are not ancestry")
    if comp["languages"]["shared"]:
        reasons.append("written in the same language(s) — convention, not evidence")
    if name_sim >= 0.3:
        reasons.append(f"name similarity {name_sim} without supporting code evidence")
    if not reasons:
        reasons.append("no shared commits, files, identifiers, or acknowledgments found")
    return {"type": "no_meaningful_relationship", "confidence": "medium",
            "summary": (f"No meaningful ancestry established between {a_label} and {b_label}. "
                        + "; ".join(reasons) + "."),
            "evidence": ev_summary,
            "uncertainty": ["Absence of evidence is not proof of independence — only "
                            "that nothing in the examined material supports a relationship. "
                            "Private history, deleted repos, or offline influence would not appear here."]}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _label(source, meta):
    if meta and meta.get("full_name"):
        return meta["full_name"]
    parsed = gh.parse_github_source(source)
    if parsed:
        return f"{parsed[0]}/{parsed[1]}"
    return os.path.basename(source.rstrip("/")) or source


def _ingest_and_fingerprint(source, workdir, log, depth, full_history, token, tag):
    info = ingest(source, workdir, depth=depth, full_history=full_history)
    fp = fingerprint(workdir, repo_hint=_label(source, None))
    fp["_git_info"] = (info.get("git") or {})
    meta = None
    parsed = gh.parse_github_source(source)
    if parsed:
        try:
            meta = gh.repo_meta(f"{parsed[0]}/{parsed[1]}", token)
            fp["repo_hint"] = meta["full_name"]
        except gh.GitHubError as e:
            log.event("meta_failed", source=source, error=str(e)[:200])
    log.event("ingested", tag=tag, source=source, source_kind=info["kind"],
              files=fp["file_count"],
              commits=(info.get("git") or {}).get("commits"))
    return info, fp, meta


def investigate(source, token=None, max_candidates=5, depth=150,
                full_history=False, data_dir=None):
    """Full pipeline: ingest -> fingerprint -> discover -> test -> report."""
    from . import store as _store
    if data_dir:
        os.environ["FOSSIL_DATA"] = data_dir
    case_id, case_dir = new_case(_label(source, None))
    log = CaseLog(case_dir)
    log.event("start", source=source, fossil_version=__version__)

    target_dir = os.path.join(case_dir, "target")
    t_info, t_fp, t_meta = _ingest_and_fingerprint(
        source, target_dir, log, depth, full_history, token, "target")
    t_label = _label(source, t_meta)
    self_name = (t_meta or {}).get("full_name")

    signals, candidates = discover_candidates(
        t_fp, t_meta, token, max_candidates, log, self_name)

    results = []
    for cand in candidates:
        cdir = os.path.join(case_dir, "candidates",
                            cand["full_name"].replace("/", "__"))
        hypo = {"id": f"H{len(results)+1}", "candidate": cand["full_name"],
                "basis": "; ".join(cand["via"])}
        log.event("hypothesis", **hypo)
        try:
            c_info, c_fp, c_meta = _ingest_and_fingerprint(
                cand["full_name"], cdir, log, 60, False, token, "candidate")
            comp = compare(t_fp, c_fp, t_label, cand["full_name"])
            verdict = evaluate_pair(
                t_fp, (t_info.get("git") or {}).get("shas"),
                t_meta, c_fp, (c_info.get("git") or {}).get("shas"),
                c_meta or cand["meta"], comp, t_label, cand["full_name"])
            log.event("hypothesis_tested", id=hypo["id"],
                      candidate=cand["full_name"], verdict=verdict["type"],
                      confidence=verdict["confidence"])
        except (IngestError, gh.GitHubError, OSError) as e:
            verdict = {"type": "inconclusive", "confidence": "low",
                       "summary": f"Could not test {cand['full_name']}: {str(e)[:200]}",
                       "evidence": {}, "uncertainty": ["Candidate could not be fetched."]}
            comp = {}
            log.event("hypothesis_failed", id=hypo["id"],
                      candidate=cand["full_name"], error=str(e)[:200])
        results.append({"candidate": cand, "verdict": verdict, "comparison": comp})

    accepted = [r for r in results
                if r["verdict"]["type"] not in ("no_meaningful_relationship", "inconclusive")]
    rejected = [r for r in results if r not in accepted]
    if accepted:
        # Strongest verdict first: direct_descendant > acknowledged > shared/port.
        order = {"direct_descendant": 0, "acknowledged_derivation": 1,
                 "likely_shared_code": 2, "vendored_component": 2,
                 "port_or_reimplementation": 3}
        accepted.sort(key=lambda r: order.get(r["verdict"]["type"], 9))
        finding = {"kind": "relationship_found",
                   "primary": accepted[0]["candidate"]["full_name"],
                   "verdict": accepted[0]["verdict"]}
    else:
        finding = {"kind": "no_relationship_found",
                   "summary": (f"Investigated {t_label}: {len(candidates)} candidate(s) "
                               f"tested, none supported a meaningful ancestry."),
                   "checked": [r["candidate"]["full_name"] for r in results]}

    # Overall timeline across target + tested candidates.
    tl_events = []
    d0, s0 = _first_date(t_fp.get("_git_info"), t_meta)
    if d0:
        tl_events.append({"date": d0.isoformat(), "label": t_label, "kind": s0})
    for r in results:
        c_fp = None  # timeline from meta only for brevity
        m = r["candidate"]["meta"]
        d = _parse_date(m.get("created_at"))
        if d:
            tl_events.append({"date": d.isoformat(), "label": r["candidate"]["full_name"],
                              "kind": "repo creation"})
    tl_events.sort(key=lambda e: e["date"])

    evidence = {
        "fossil_version": __version__,
        "case_id": case_id,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "inputs": {"source": source, "max_candidates": max_candidates,
                   "depth": None if full_history else depth,
                   "full_history": full_history},
        "target": {"label": t_label, "meta": t_meta,
                   "files": t_fp["file_count"], "languages": t_fp["languages"],
                   "manifests": t_fp["manifests"], "licenses": t_fp["licenses"],
                   "readme_signals": t_fp["readme"]["signals"]},
        "target_git": {k: v for k, v in (t_info.get("git") or {}).items() if k != "shas"},
        "signals": signals,
        "candidates": [
            {"full_name": r["candidate"]["full_name"], "via": r["candidate"]["via"],
             "verdict": r["verdict"],
             "key_numbers": r["verdict"].get("evidence", {})}
            for r in results
        ],
        "finding": finding,
        "timeline": tl_events,
        "thresholds": THRESHOLDS,
        "note": ("In-memory comparison detail (per-file shingles, full identifier "
                 "lists) is recomputed on re-run; verdicts, thresholds, and key "
                 "numbers above are sufficient to reproduce the findings."),
    }
    save_json(os.path.join(case_dir, "evidence.json"), evidence)
    save_json(os.path.join(case_dir, "inputs.json"),
              {"source": source, "case_id": case_id,
               "fossil_version": __version__,
               "created": evidence["created"]})
    report = render_report(evidence, t_label)
    with open(os.path.join(case_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    log.event("done", finding=finding["kind"])
    log.close()
    return case_dir, evidence, report


def compare_pair(source_a, source_b, token=None, depth=150, full_history=False,
                 data_dir=None):
    """Focused two-repository comparison (verification / restraint tests)."""
    if data_dir:
        os.environ["FOSSIL_DATA"] = data_dir
    case_id, case_dir = new_case(f"compare-{_label(source_a, None)}-vs-{_label(source_b, None)}")
    log = CaseLog(case_dir)
    log.event("start", mode="compare", a=source_a, b=source_b,
              fossil_version=__version__)

    a_dir = os.path.join(case_dir, "a")
    b_dir = os.path.join(case_dir, "b")
    a_info, a_fp, a_meta = _ingest_and_fingerprint(
        source_a, a_dir, log, depth, full_history, token, "a")
    b_info, b_fp, b_meta = _ingest_and_fingerprint(
        source_b, b_dir, log, depth, full_history, token, "b")
    a_label, b_label = _label(source_a, a_meta), _label(source_b, b_meta)

    comp = compare(a_fp, b_fp, a_label, b_label)
    verdict = evaluate_pair(
        a_fp, (a_info.get("git") or {}).get("shas"), a_meta,
        b_fp, (b_info.get("git") or {}).get("shas"), b_meta,
        comp, a_label, b_label)
    log.event("verdict", type=verdict["type"], confidence=verdict["confidence"])
    tl = timeline(a_label, {"_git": a_fp.get("_git_info")}, a_meta,
                  b_label, {"_git": b_fp.get("_git_info")}, b_meta)

    # Persist full comparison detail for reproducibility.
    evidence = {
        "fossil_version": __version__,
        "case_id": case_id,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "inputs": {"mode": "compare", "a": source_a, "b": source_b,
                   "depth": None if full_history else depth,
                   "full_history": full_history},
        "a": {"label": a_label, "meta": a_meta, "files": a_fp["file_count"],
              "languages": a_fp["languages"], "manifests": a_fp["manifests"],
              "licenses": a_fp["licenses"]},
        "b": {"label": b_label, "meta": b_meta, "files": b_fp["file_count"],
              "languages": b_fp["languages"], "manifests": b_fp["manifests"],
              "licenses": b_fp["licenses"]},
        "a_git": {k: v for k, v in (a_info.get("git") or {}).items() if k != "shas"},
        "b_git": {k: v for k, v in (b_info.get("git") or {}).items() if k != "shas"},
        "comparison": comp,
        "verdict": verdict,
        "timeline": tl,
        "thresholds": THRESHOLDS,
    }
    save_json(os.path.join(case_dir, "evidence.json"), evidence)
    save_json(os.path.join(case_dir, "inputs.json"),
              {"mode": "compare", "a": source_a, "b": source_b,
               "case_id": case_id, "fossil_version": __version__,
               "created": evidence["created"]})
    report = render_report(evidence, f"{a_label} vs {b_label}", mode="compare")
    with open(os.path.join(case_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    log.event("done", verdict=verdict["type"])
    log.close()
    return case_dir, evidence, report
