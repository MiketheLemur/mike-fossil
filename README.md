# Fossil

*Built by Mike — concept and spec by T; designed, coded, and shipped by Mike.*

Experimental software archaeology. Given a GitHub repository, a local
directory, or a ZIP archive, Fossil investigates its origins, ancestry, and
influences — and writes up what it finds with evidence, confidence levels,
and honest uncertainty.

Fossil is not a plagiarism detector. Its job is to discover interesting,
*verifiable* connections between projects: forks, ports, vendored components,
acknowledged derivations — and to say plainly when no relationship exists.

> Not to be confused with [Fossil SCM](https://fossil-scm.org/), the version
> control system. Different fossil. This one digs up code ancestry.

## Install

Python 3.11+ and git. No dependencies beyond the standard library.

```bash
git clone https://github.com/MiketheLemur/mike-fossil.git
cd mike-fossil
python3 -m fossil --version
```

Optional: set `GITHUB_TOKEN` (or pass `--token`) to raise GitHub API limits
from 60 to 5,000 requests/hour. Discovery runs work without a token but are
slower and can hit the limit.

## Usage

```bash
# Investigate a repo: Fossil discovers upstream candidates on its own
python3 -m fossil investigate itchyny/gojq

# Compare two repos directly (verification / restraint checks)
python3 -m fossil compare eza-community/eza ogham/exa --full-history

# ...local directories and ZIPs work too
python3 -m fossil investigate ./my-project
python3 -m fossil investigate project.zip

# Re-render a past report from its evidence bundle
python3 -m fossil report ~/.fossil/cases/<case-id>

# List past investigations
python3 -m fossil list
```

Every run creates a case directory under `~/.fossil/cases/` containing
`report.md` (the readable write-up), `evidence.json` (every number behind the
verdict, plus the thresholds used), `inputs.json`, and `log.jsonl` (the full
hypothesis → test → accept/reject trace). Findings are reproducible from the
evidence bundle.

Fossil is read-only: it clones and queries, never pushes, publishes, or
modifies anything.

## Examples

### 1. Verification — a known fork, proven with evidence

```
$ python3 -m fossil compare eza-community/eza ogham/exa --full-history
verdict: direct_descendant (confidence: high)
```

Not because it recognized the names — because `eza` shares **1,609 commits**
with `exa` (out of exa's 1,618 total), carries **406 shared rare identifiers**
(`two_times_two_nested_children`, `ignores_an_exact_filename`, …), and the
timeline reads correctly: *ogham/exa predates eza-community/eza by 3,353
days*. Along the way it surfaced a genuine detail: the license changed across
the fork (MIT → EUPL-1.2), and `eza-community/eza` isn't even flagged as a
GitHub fork — the relationship was established from evidence, not metadata.

### 2. Discovery — no ancestry supplied

```
$ python3 -m fossil investigate itchyny/gojq
finding:  acknowledged_derivation (confidence: high)
primary:  jqlang/jq
```

Fossil was told nothing about `jq`. From the repo name (`gojq` → core `jq`),
the description ("Pure Go implementation of jq"), and a README link to
`github.com/jqlang/jq`, it generated its own candidates, fetched and tested
five of them, and kept the one the evidence supported — while rejecting
`jquery/jquery`, `jquery/jquery-ui`, `blueimp/jQuery-File-Upload`, and
`exercism/jq` with reasons. The key numbers show the *kind* of relationship
honestly: 0 shared commits, 0 identical files, identifier Jaccard 0.016 —
no copied code, because a Go reimplementation of a C tool can't share code.
The verdict is "acknowledged derivation," not "copy."

### 3. Restraint — similar projects, no invented ancestry

```
$ python3 -m fossil compare sindresorhus/meow tj/commander.js --full-history
verdict: no_meaningful_relationship (confidence: medium)
```

Two JavaScript CLI-argument libraries by different authors. Same language,
overlapping problem domain — and Fossil correctly concludes nothing: 0
identical files, global code similarity 0.0, no acknowledgments. The two
shared dependencies (`tsd`, `typescript` — dev tooling) are explicitly
dismissed: *"common libraries are not ancestry."*

## How it works

A deterministic pipeline; no frameworks, no LLM in the loop:

```
ingest → fingerprint → discover candidates → compare → test hypotheses → report
```

- **Ingest**: clones (bounded depth, or `--full-history`), copies local dirs,
  unpacks ZIPs. Read-only, always.
- **Fingerprint**: file inventory + SHA-1s, token-shingle minhash sketches,
  identifier vocabularies (comments/strings stripped, keywords and generic
  words filtered), dependency manifests (`package.json`, `Cargo.toml`,
  `pyproject.toml`, `go.mod`, …), license guesses, README acknowledgment
  signals and GitHub links, git history facts.
- **Discover**: turns name cores (`gojq`→`jq`), description keywords, README
  links, and acknowledgment phrasing into GitHub search queries. Signals are
  guesses; candidates must survive comparison.
- **Compare**: byte-identical files (same path *and* moved), near-identical
  files, minhash Jaccard, identifier Jaccard, rare-identifier overlap,
  dependency overlap, license match, name similarity, shared commit SHAs,
  author overlap, timeline ordering.
- **Hypotheses**: a bounded generator–critic loop. Each candidate is a
  hypothesis; verdict rules with explicit thresholds (`THRESHOLDS` in
  `fossil/hypotheses.py`, persisted into every evidence bundle) accept or
  reject it with reasons. Weak leads are logged, not hidden.
- **Report**: deterministic Markdown from the evidence. Same evidence →
  same report, always.

Verdict types: `direct_descendant`, `acknowledged_derivation`,
`likely_shared_code`, `vendored_component`, `port_or_reimplementation`,
`no_meaningful_relationship`, `inconclusive` — each with high/medium/low
confidence and an explicit uncertainty note.

### Where AI fits (deliberately)

The architecture follows a simple rule: put model judgment only where
deterministic code can't cheaply resolve the uncertainty. Nothing in v1
needs an LLM — verdicts come from counted evidence and explicit thresholds,
which is exactly what makes the restraint test trustworthy. The genuine
future use for a model is *report narration*: `evidence.json` is designed as
the structured interface for a single LLM call that turns the evidence into
prose. Templates ship the readable report today; the bundle is ready when
narration earns its way in.

## Known limitations

- **History depth is bounded.** Default clone depth is 150 commits
  (`--full-history` for the whole thing). Ancient fork points can fall
  outside the examined range; shared-commit evidence is then unavailable and
  the verdict says so.
- **GitHub API limits.** Unauthenticated: 60 req/hour (10/min for search).
  Discovery on a tokenless run is slow and may stall — use `GITHUB_TOKEN`.
- **No code search.** v1 discovers candidates via repository search, README
  links, and acknowledgment phrasing — not GitHub code search. Vendored
  snippets with no paper trail are harder to find. (Planned.)
- **Identifier stripping is approximate.** Comment/string removal is
  regex-based and can misfire on exotic quoting; it only costs a few
  identifiers.
- **README acknowledgments are self-reports.** Fossil treats "port of X" as
  a lead and demands corroboration, but a lying README could still mislead
  it. Confidence reflects this.
- **Absence of evidence ≠ independence.** Private history, deleted repos,
  and offline influence are invisible. Reports say this explicitly.
- **Similarities are not accusations.** Shared code can mean a fork, a
  vendored dependency, a shared template, or coincidence. Fossil reports
  evidence; it does not allege plagiarism, and its license guesses are not
  legal conclusions.

## Project layout

```
fossil/
  cli.py          argument parsing, subcommands
  ingest.py       clone / copy / unzip sources (read-only)
  fingerprint.py  deterministic code fingerprinting
  github.py       read-only GitHub REST client
  compare.py      deterministic pairwise comparison
  hypotheses.py   candidate discovery + verdict rules + orchestration
  report.py       Markdown report renderer
  store.py        case directories, evidence bundles, trace logs
tests/            unit tests (stdlib unittest, no dependencies)
```

## License

MIT. See [LICENSE](LICENSE).
