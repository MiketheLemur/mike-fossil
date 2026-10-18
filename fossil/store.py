"""Durable case storage: every investigation gets a directory with inputs,
evidence, a human-readable report, and a machine-readable trace log."""

import datetime
import json
import os
import re


def data_dir():
    return os.environ.get("FOSSIL_DATA", os.path.expanduser("~/.fossil"))


def _slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "case").lower()).strip("-")
    return slug or "case"


def new_case(name_hint):
    """Create a fresh case directory. Returns (case_id, case_dir)."""
    base = os.path.join(data_dir(), "cases")
    os.makedirs(base, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    case_id = f"{_slug(name_hint)}-{stamp}"
    case_dir = os.path.join(base, case_id)
    n = 1
    while os.path.exists(case_dir):
        n += 1
        case_id = f"{_slug(name_hint)}-{stamp}-{n}"
        case_dir = os.path.join(base, case_id)
    os.makedirs(case_dir)
    return case_id, case_dir


def list_cases():
    base = os.path.join(data_dir(), "cases")
    if not os.path.isdir(base):
        return []
    out = []
    for cid in sorted(os.listdir(base)):
        cdir = os.path.join(base, cid)
        inputs = os.path.join(cdir, "inputs.json")
        if os.path.isdir(cdir) and os.path.exists(inputs):
            try:
                with open(inputs) as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
            out.append((cid, meta))
    return out


def _strip_private(obj):
    """Drop keys starting with '_' (in-memory-only auxiliaries) before persisting."""
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


def save_json(path, obj):
    obj = _strip_private(obj)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class CaseLog:
    """Append-only JSONL trace of the investigation: hypotheses formed,
    tests run, leads accepted or rejected, and why."""

    def __init__(self, case_dir):
        self.path = os.path.join(case_dir, "log.jsonl")
        self._f = open(self.path, "a", encoding="utf-8")

    def event(self, kind, **fields):
        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "kind": kind,
        }
        rec.update(fields)
        self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self):
        self._f.close()
