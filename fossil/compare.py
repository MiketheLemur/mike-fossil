"""Deterministic comparison of two fingerprints.

Every metric is a counted, reproducible measurement. Nothing here decides
what anything *means* — hypotheses.py turns these numbers into verdicts.
"""

import difflib
import os

from .fingerprint import manifest_names, all_deps

VERY_SIMILAR = 0.85  # shingle-Jaccard threshold for "very similar file"


def _jaccard(a, b):
    if not a and not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    if inter == 0:
        return 0.0
    return inter / len(sa | sb)


def _core_name(n):
    return n.split("/")[-1].lower()


def compare(fp_a, fp_b, name_a="A", name_b="B"):
    """Compare two fingerprints. `a` is conventionally the target."""
    ev = {"compared": [name_a, name_b]}

    # --- File identity -------------------------------------------------
    by_path_a = {f["path"]: f for f in fp_a["files"]}
    by_path_b = {f["path"]: f for f in fp_b["files"]}
    common_paths = set(by_path_a) & set(by_path_b)

    identical_same = sorted(
        p for p in common_paths
        if by_path_a[p]["sha1"] == by_path_b[p]["sha1"]
        and by_path_a[p]["kind"] == "text"
    )
    ev["identical_same_path"] = {
        "count": len(identical_same),
        "examples": identical_same[:15],
    }

    # Byte-identical content under *different* paths (renames / moves).
    idx_a = fp_a["content_index"]
    idx_b = fp_b["content_index"]
    moved = []
    for sha, paths_a in idx_a.items():
        paths_b = idx_b.get(sha)
        if not paths_b:
            continue
        for pa in paths_a:
            for pb in paths_b:
                if pa != pb and by_path_a.get(pa, {}).get("kind") == "text":
                    moved.append((pa, pb))
    ev["identical_diff_path"] = {
        "count": len(moved),
        "examples": [f"{a}  ->  {b}" for a, b in moved[:15]],
    }

    # --- Per-file similarity (same path, shingle Jaccard) --------------
    sh_a = fp_a.get("_full_shingles", {})
    sh_b = fp_b.get("_full_shingles", {})
    very_similar = []
    sim_scores = []
    for p in common_paths:
        sa, sb = sh_a.get(p), sh_b.get(p)
        if not sa or not sb:
            continue
        j = _jaccard(sa, sb)
        sim_scores.append(j)
        if j >= VERY_SIMILAR and p not in identical_same:
            very_similar.append((p, round(j, 3)))
    very_similar.sort(key=lambda t: -t[1])
    ev["very_similar_files"] = {
        "count": len(very_similar),
        "examples": [f"{p} ({j})" for p, j in very_similar[:15]],
    }
    ev["mean_path_similarity"] = (
        round(sum(sim_scores) / len(sim_scores), 3) if sim_scores else 0.0
    )

    # --- Global shingle similarity (minhash sketches) -------------------
    ev["global_shingle_jaccard"] = round(
        _jaccard(fp_a.get("minhash", []), fp_b.get("minhash", [])), 4
    )

    # --- Identifier vocabularies ---------------------------------------
    id_a = set(fp_a["identifiers"]["distinctive"])
    id_b = set(fp_b["identifiers"]["distinctive"])
    ev["identifier_jaccard"] = round(_jaccard(id_a, id_b), 4)
    shared_rare = sorted(set(fp_a["identifiers"]["rare"]) & set(fp_b["identifiers"]["rare"]),
                       key=lambda w: (-len(w), w))
    ev["shared_rare_identifiers"] = {
        "count": len(shared_rare),
        "examples": shared_rare[:20],
    }

    # --- Dependencies ---------------------------------------------------
    deps_a, deps_b = all_deps(fp_a), all_deps(fp_b)
    overlap = sorted(deps_a & deps_b)
    ev["dep_overlap"] = {
        "names": overlap,
        "count": len(overlap),
        "a_total": len(deps_a),
        "b_total": len(deps_b),
    }

    # --- Licenses --------------------------------------------------------
    lic_a = sorted({l["guess"] for l in fp_a["licenses"] if l["guess"] != "unknown"})
    lic_b = sorted({l["guess"] for l in fp_b["licenses"] if l["guess"] != "unknown"})
    sha_a = sorted({l["sha1"] for l in fp_a["licenses"]})
    sha_b = sorted({l["sha1"] for l in fp_b["licenses"]})
    ev["license"] = {
        "a": lic_a, "b": lic_b,
        "guess_match": bool(lic_a and lic_a == lic_b),
        "byte_identical_license_file": bool(set(sha_a) & set(sha_b)),
    }

    # --- Names ------------------------------------------------------------
    # Compare the final path segment only: "jqlang/jq" should match "gojq"
    # on "jq", not be penalized for the owner prefix.
    def _core(n):
        return n.split("/")[-1].lower()
    names_a, names_b = manifest_names(fp_a), manifest_names(fp_b)
    best = 0.0
    best_pair = None
    for na in names_a:
        for nb in names_b:
            r = difflib.SequenceMatcher(None, _core(na), _core(nb)).ratio()
            if r > best:
                best, best_pair = r, (na, nb)
    ev["name_similarity"] = {"score": round(best, 3), "pair": best_pair}

    # --- README ------------------------------------------------------------
    ra = fp_a["readme"].get("head", "")
    rb = fp_b["readme"].get("head", "")
    ev["readme_jaccard"] = round(_jaccard(ra.lower().split(), rb.lower().split()), 4) \
        if (ra or rb) else 0.0

    # --- Acknowledgment --------------------------------------------------
    # Does A's docs point at B? Only specific pointers count strongly:
    # owner/repo links must match B exactly (a "/jq" suffix matches every
    # jq-named repo on GitHub), bare words must be >= 3 chars, and A
    # mentioning itself is never evidence.
    def _identities(names):
        ids = set()
        for n in names:
            nl = n.lower()
            parts = nl.split("/")
            ids.add(nl)
            ids.add(parts[-1])
            if len(parts) >= 2:
                ids.add("/".join(parts[-2:]))
        return ids

    a_ids = _identities(manifest_names(fp_a))
    b_names = manifest_names(fp_b)
    b_ids = _identities(b_names)
    b_core = _core_name(b_names[0]) if b_names else ""
    b_bare_ok = bool(b_names) and "/" not in b_names[0]
    ack = []
    for sig in fp_a["readme"].get("signals", []):
        tgt = (sig.get("target") or "").lower()
        if not tgt or tgt in a_ids:
            continue  # empty or self-mention
        tcore = tgt.split("/")[-1]
        if "/" in tgt:
            if tgt in b_ids or (b_bare_ok and tgt.endswith("/" + b_core)):
                ack.append(sig)
        elif tcore == b_core and len(tcore) >= 3:
            ack.append(sig)
    ev["acknowledgment_in_a"] = ack

    # --- Languages / structure ---------------------------------------------
    langs_a = set(fp_a["languages"])
    langs_b = set(fp_b["languages"])
    ev["languages"] = {
        "a": fp_a["languages"], "b": fp_b["languages"],
        "shared": sorted(langs_a & langs_b),
    }
    ev["file_counts"] = {"a": fp_a["file_count"], "b": fp_b["file_count"]}
    return ev
