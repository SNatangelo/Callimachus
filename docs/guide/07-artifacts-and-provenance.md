# 07 — Artifacts and Provenance

## Authoritative run layout

A run may contain the following files. Each artifact appears only after its owning phase has run:

```text
runs/<id>/
├── run.sqlite
├── parse_debug.md                    # after Parse
├── fetch_manual_sources.md           # when Fetch reports missing full text
├── sources/
│   ├── parsed/
│   │   └── <ref>_<tier>_<origin>.txt
│   ├── provided/
│   │   └── <ref>_<original-name>
│   └── ocr_queue/
│       └── <ref>_..._.unreadable.pdf
├── report.md                         # after Report
├── report.journal.md                 # after Report
├── report.signature_status.md        # after Report
├── report.html                       # verified human companion; default output
└── report.preview.html               # optional, explicitly unverified preview
```

Some runs also contain internal Fetch response cache files. Their presence does not replace a registered source-text row or source hash.

`run.sqlite` is the system of record. Current runs do not use `resolve/*.json`, `ledger/*.json`, or editable task files as authority.

## What is stored in SQLite

The repository persists typed relational projections for:

- run identity, parent/child lineage, phase, selected accuracy/style, and execution assurance;
- manuscript hash and immutable Parse facts;
- claims, references, citation edges, table-only citations, and footnote provenance;
- manual Parse review tasks, answers, applications, overlays, and operational references;
- Resolve results, identity evidence, links, attempts, provider traces, and retraction metadata;
- source manifests, source text, hashes, unreadable/OCR state, and Fetch attempts;
- generic typed tasks and their producer provenance;
- frozen Verify configuration and policy hashes;
- logical requests, physical dispatches, candidates, rejected candidates, transitions, and terminal results;
- report inputs, completion summaries, performance snapshots, and integrity checkpoints.

The current schema is validated whenever the repository is opened. A JSON payload is not trusted in place of the typed projection.

## Source-file lifecycle

### `sources/parsed/`

This directory contains normalized UTF-8 text that can be admitted to grounding. File names encode reference number, tier, and origin. The database binds each file to its hash, identity, evidence scope, and provenance.

Sources can originate from a user, a resolver/fetch provider, OCR, Google Books, or a browser session. Equal text with a different identity or provenance is not automatically interchangeable.

### `sources/provided/`

Raw originals supplied by the user are retained as audit copies. Their extracted text is stored separately in `sources/parsed/`.

Raw network downloads are not retained by default; normalized text and the complete acquisition trace are retained. This distinction prevents a downloaded binary from being mistaken for a user-provided original.

### `sources/ocr_queue/`

An unreadable scanned PDF is parked here with a typed queue row. Until OCR succeeds, it is not a text-bearing source. After OCR text is admitted under `sources/parsed/`, the queue row is marked complete and the parked scan follows the implemented OCR lifecycle.

## Human-readable intermediate artifacts

`parse_debug.md` is a view of Parse output for inspection. It helps diagnose claim count, citation markers, bibliography boundaries, coverage, and extraction metadata. Editing it does not edit Parse facts.

`fetch_manual_sources.md` summarizes references still missing full text, observed access failures, candidate URLs, abstracts, and required manual action. It is an operator aid; SQLite task/source state remains authoritative.

Gap diagnostics and style findings are recomputed from current DB/reference state; the Gaps and Style phases do not own durable projections. Re-running a view does not override the underlying run state.

## Provenance chain

### Remediation and report provenance

Remediation never deletes the automatic finding. Reports distinguish a manual override,
retained uncertainty, operator-supplied evidence, a research lead,
and an identity attestation. A completed-child report records the parent run
ID plus SHA-256 hashes of the parent input, report, journal, and source
inventory. Configuration is inherited and validated separately; it is not
stored as a completed-child configuration hash. Use the typed task and
provenance projections to inspect these decisions; never edit SQLite directly.

An operator-provided file, OCR result, browser result, or fetch answer remains
evidence only after normal extraction, association, and identity checks. A
research answer is recorded as a lead and must be re-fetched and quote-validated.
An identity attestation is a separate typed fact and must not be presented as a
semantic or deterministic override.

For a source to affect a semantic terminal, the audit chain links:

```text
manuscript hash
  → raw/effective reference identity
  → Resolve evidence and attempts
  → Fetch/provisioning answer and producer
  → normalized source path + SHA-256
  → admitted evidence scope
  → Verify request/prompt/policy fingerprint
  → grounded source spans
  → terminal transition
  → deterministic report projection
```

Manual intervention adds provenance; it does not erase earlier evidence. Examples include:

- a manual identity correction remains an overlay over immutable Parse data;
- a forced source mapping records that the association was manual;
- a research URL is re-fetched and its quote is checked before admission;
- a successful retry leaves earlier technical and guard failures intact;
- a fresh or frozen-Fetch child records its parent and source-inventory fingerprint.

## Report artifacts

### `report.md`

The report is a deterministic rendering of current typed projections. No LLM writes its prose or chooses which rows to omit.

Read it from summary to evidence:

| Section | What it tells you |
|---|---|
| **0. Run Health** | Whether execution, integrity, and completion checks passed, with diagnostic counters. |
| **1. Triage — problems first** | Which failures, warnings, and unstable outcomes need attention. |
| **2. Coverage** | Claim and reference counts, unresolved or risky references, missing text, and claim-verification coverage. |
| **3. Per-claim detail** | Each claim, its linked references, source-existence and style findings, admitted evidence scope, and semantic outcome. |
| **4. Per-source detail** | Each bibliography entry, resolution, provenance, acquired text or evidence, and style findings. |
| **5. Provenance** | Parser and runtime metadata, immutable dispatch counts, and journal policy. |

Read sections 0–2 first for the overall decision. Use the per-claim and
per-source sections to investigate a specific result, then use Provenance to
audit how it was produced.

### `report.journal.md`

Each rendered report is appended as a hash-linked snapshot. The completion gate verifies both the current body and the journal chain. Replacing or editing an old entry breaks authenticity.

### `report.html` and `report.preview.html`

`report.html` is a deterministic projection of the typed run data, generated by
default after the canonical report gate passes. It is self-contained for
offline use and does not parse `report.md` for facts. Its
companion seal binds the exact Markdown report, the human-report projection, the
selected locale and theme, every embedded presentation asset, and the final HTML
body. Generate or regenerate it for a completed current-schema run with:

```bash
python run.py report-html --run runs/<id> --locale it --theme callimachus
python run.py report-html --run runs/<id> --verify
```

The same command has a narrowly scoped, read-only adapter for completed and
sealed schema-59 runs. It validates their historical report contract and marks
modern execution assurance as unavailable; it never migrates the database or
rewrites `report.md`/`report.journal.md`. No other historical schema is implied
to be compatible.

`report.md` remains canonical. If its authenticity gate does not pass, normal
HTML generation fails closed. Set `CITATION_VERIFIER_REPORT_HTML=0` to skip
automatic generation; the retroactive command remains available. Disabling the
setting does not delete an existing companion. `--preview-unverified` instead
writes only
`report.preview.html`; the file carries a visible warning and deliberately has no
companion seal.

The overview keeps bibliographic risk separate from incomplete technical work.
A positively contradicted reference is shown as **Reference refuted**. A fresh,
completed same-resolver check in which the resolver both indexes the declared
journal and finds no compatible article at the cited coordinates is shown as
**Very likely fabricated**. Either result makes the overall report red, even
when it is the only such source. Completed independent searches without that
closed-world article check are shown separately in amber as **Elevated
bibliographic suspicion**; this is a serious unresolved problem, not a
fabrication diagnosis. The Sources view can filter each class and retains the
evidence-derived explanation.

The HTML companion omits the manuscript's absolute input path and redacts local
absolute paths from all other projected values. Public HTTP(S) source URLs and
run-relative paths remain visible. Known public resolver route templates are
rendered as named lookups rather than being mistaken for local paths. Remediation
commands use the checkout-relative virtual-environment interpreter so they stay
readable without exposing a machine-specific path. The **Method and
configuration** view also
reports, for current-schema runs, only the names of recognized non-LLM
credential environment variables that were present when the run was initially
created. It never
projects their values. For each credential actually attached to an outbound
Resolve, Fetch, or Search request, it reports physical-call counts, HTTP 2xx,
401, 403, 429, other HTTP responses, and network failures, broken down by
Resolve, Fetch, and Search. Keyless requests and
cache hits are not attributed to a credential. A Google Books Preview key passed
directly with `--key` is likewise not misreported as an environment credential;
its value is never persisted.

A credential first supplied during a later resume can therefore appear as used
without appearing in the initial-presence list. This distinction preserves the
technical remediation history rather than rewriting the original environment
snapshot.

An HTTP 2xx count means that the provider accepted the transport request; it
does not by itself prove that a reference was resolved, that useful text was
returned, or that evidence passed identity and admission gates. When every
observed call for one credential returned only 401/403, the report warns that
the key may be expired, revoked, unauthorized, or missing the required
entitlement. It does not claim which of those causes applies. LLM provider
dispatch telemetry remains in the existing model/runtime cards.

Report text and presentation tokens are external assets:

- `core/report/human/assets/locales/*.json` contains UI text and translation metadata;
- `core/report/human/assets/themes/*.json` contains validated design tokens;
- `core/report/human/assets/manifest.json` selects defaults and defines the token contract.

Adding or modifying a conforming asset requires no Python or JavaScript change.
Because asset hashes are sealed, a previously generated HTML companion must be
regenerated after its locale, theme, template, CSS, or client code changes.

### `report.signature_status.md`

This file records the result of report sealing and gate checks. `python run.py verify --write-status` refreshes it from the current run; it is not an operator approval field.

## Seal versus external signature

| Mechanism | Detects/proves | Limitation |
|---|---|---|
| SHA-256 content seal | The report and canonical projections have not changed since sealing. | Anyone who controls both content and seal generation could regenerate it. |
| HMAC signature | A holder of the external signing key approved the exact sealed content. | Strong only when the key is isolated from the agent/process being constrained. |

Use `CITATION_VERIFIER_SIGNING_KEY_FILE` in a Stop hook or CI environment that the agent cannot read. `--require-signature` makes absence or invalidity a hard gate failure.

## Integrity authority and checkpoints

The driver can bind execution to an integrity authority. It records execution assurance, enrolls the run, checks the content store, and places integrity checkpoints around phase transitions and work units.

The following are not transparent changes:

- direct edits to `run.sqlite`;
- replacing a registered source file while keeping its path;
- rewriting `report.md` or its journal;
- changing a task answer after admission;
- copying only part of a live run directory.

An integrity override is diagnostic, requires a reason, and labels the run. It does not retroactively establish trust.

## Audit commands

Use the public interfaces for a read-only audit:

```bash
# Phase and task snapshot
python run.py --run runs/<id> --status --json-only

# Typed task history
python run.py tasks list --run runs/<id>
python run.py tasks show --run runs/<id> --task <task-id>

# Completion and authenticity
python run.py verify --run runs/<id> --write-status
python run.py verify --run runs/<id> --require-signature

# Present only an accepted report
python run.py present --run runs/<id>

# Generate or verify the optional human HTML companion
python run.py report-html --run runs/<id> --locale it
python run.py report-html --run runs/<id> --verify
```

For archival copying, stop all writers first and retain the complete run directory, including the database and source tree. Do not assemble an audit copy from selected report files alone.

## Completion and semantic quality are different

The report gate establishes operational completeness and internal authenticity. It intentionally permits honest outcomes such as no text, unsupported claims, contradictions, or uncertain/exhausted verification.

A complete run is auditable because those outcomes are visible. Concealing them would be a gate failure; their existence is not.

---

Previous: [Verification and evidence](06-verification-and-evidence.md) · Next: [Capabilities and limits](08-capabilities-and-limits.md).
