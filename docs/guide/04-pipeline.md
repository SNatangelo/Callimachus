# 04 — Pipeline

## Exact state order

The persisted state machine advances through:

```text
parse
  → resolve
  → fetch
  → gaps
  → style
  → verify
  → web_research
  → report
  → done
```

`web_research` is a retained no-op state for prior run routing. Manual Parse review is a pause between Parse and Resolve, not a replacement for either phase.

Before the state machine starts, a preflight checks configuration, dependencies, content-store access, and integrity authority. A hard prerequisite cannot be bypassed by `--proceed`; that flag only acknowledges optional configuration gaps.

## Phase matrix

| Phase | Main responsibility | Durable evidence | Stop/pause boundary |
|---|---|---|---|
| Parse | Extract manuscript text, claims, bibliography entries, citation edges, table-only citations, coverage, and footnote provenance. | Typed Parse projections in `run.sqlite`; `parse_debug.md`. | Parser error, zero usable claims, lost citation markers, or an enabled manual-review pause. |
| Resolve | Establish source existence and identity; gather identifiers, metadata, abstracts, retraction signals, candidate links, and traces. | Resolve result, identity, evidence, link, attempt, and trace tables. | Provider failures become explicit unresolved results; they are not converted to success. |
| Fetch | Reuse or retrieve admitted source text, materialize abstracts, record every attempt, queue manual/browser/OCR work. | `source_texts`, fetch traces, source files, unreadable-source state, and Fetch tasks. | Blocking missing sources, pending Fetch tasks, or total network failure under a regime that requires text. |
| Gaps | Recompute missing-source diagnostics for the selected accuracy floor. | A recomputed view of current DB state; Fetch's `fetch_manual_sources.md` remains a separate operator artifact. | Does not perform semantic verification. A fully network-blocked run cannot proceed as if evidence existed. |
| Style | Detect or apply the selected style and check references by source type. | Findings recomputed from current DB/reference entries; the phase does not own a durable projection. | Style findings remain separate from existence and semantic support. |
| Verify | Build and execute one guarded task per cited claim-reference-scope pair with admitted local text. | Frozen policy/config, pair state, transitions, candidates, dispatch/audit records, and terminal results. | Missing or changed text, invalid identity, malformed output, grounding failure, retry exhaustion, or unresolved technical state. |
| Web research | Retired compatibility state. | No new third-party source text or Verify records. | Generic web pages cannot support a citation verdict. |
| Report | Render, seal, journal, and independently gate the deterministic DB projection; generate the human HTML companion by default after the gate passes. | `report.md`, `report.journal.md`, `report.signature_status.md`, and normally `report.html`. | Missing, stale, tampered, or incomplete canonical artifacts leave the run at Report; when enabled, an HTML-generation failure also prevents Done. |

| Done | Record operational completion after the report gate passes. | Final phase event and complete run state. | “Done” says the audit pipeline completed; it does not assert that all claims are supported. |

`--references-only` follows the alternate path Parse → Resolve → Report preview.
It uses the same human HTML renderer but writes only `report.preview.html`, with
an explicit non-audit-ready warning; it does not synthesize Fetch or Verify
records.

## Parse

Parse selects an extractor from the manuscript format, normalizes the document, separates body and bibliography, identifies citation markers, and emits claims plus links to references.

Supported parser behavior includes:

- numeric, author-year, inline DOI, and MLA-style marker schemes;
- superscript-aware extraction when the input format preserves typography;
- automatic OCR for an eligible scanned manuscript PDF;
- format-specific bibliography and table-row handling;
- orphan-marker, uncited-reference, duplicate-identifier, and coverage diagnostics;
- automatic citation-style detection when `--style` is absent.

Raw Parse facts are immutable after persistence. Replacing a Parse payload is an explicit new Parse operation that resets downstream projections; manual adjudication creates typed overlays instead of rewriting the raw record.

Parse fails closed when evidence indicates that a conversion destroyed citation markers. A flattened superscript PDF/TXT cannot be repaired by guessing which numbers were citations; provide a PDF, DOCX, HTML, or Markdown copy that retains the marker information.

## Optional manual Parse review

With `--manual-review`, the driver persists Parse first, emits hash-bound `parse_review` tasks, and pauses before Resolve. Supported actions are:

- `no-sources`;
- `split-sources`;
- `correct-identity`;
- `keep-ambiguous`.

Split children become operational references with their own identities and source membership. The raw footnote/reference text stays intact. Invalid hashes, duplicate application, non-exact source splits, and unfinished tasks block advancement.

Without manual review, unresolved ambiguous footnote containers remain excluded from downstream acquisition and are reported as such; they are not guessed.

## Resolve

Resolve works from persisted operational references. It:

- validates DOI, PMID, URL, title/author/year, and provider-specific identities;
- checks existence, metadata, abstracts, candidate links, full-text availability, and retraction signals;
- preserves every provider attempt and structured failure reason;
- avoids treating manuscript pointers such as “Id.” or “ibid.” as independent documents;
- allows a cross-reference to inherit an antecedent result only after that antecedent has been resolved;
- can repair weak metadata only when corroborated evidence justifies the new identity.

All Resolve results are persisted before retryable Fetch work begins. A timeout, 404, authentication error, or provider exception remains distinguishable in the audit trail.

## Fetch

Fetch operates only on resolved or otherwise admissible reference targets. Its main paths are:

1. reuse a corroborated source from the cross-run content store;
2. materialize an abstract already obtained by Resolve;
3. attempt open-access, repository, publisher, preprint, and provider candidates;
4. accept user-provided files or text through validated task/provisioning commands;
5. queue browser challenges or unreadable scans instead of pretending retrieval succeeded.

Normalized evidence text is stored under `sources/parsed/`. User-provided originals are copied to `sources/provided/`. Unreadable scans are parked under `sources/ocr_queue/` until OCR is completed or declined.

The phase writes an operator-facing `fetch_manual_sources.md` summary for references still missing full text. The authoritative task, attempt, and source state remains in SQLite.

For non-abstract regimes, a blocking reference with no resolution, Fetch, source, or explicit task trace cannot silently advance to Verify. If the entire run has no usable text and observed transient network failures, the driver reports a network-blocked condition.

## Gaps and style

Gaps recalculates what remains below the accuracy floor after Fetch. It distinguishes, among other states:

- abstract available but full text missing;
- full text declared available but not retrieved;
- unreadable source awaiting OCR;
- source not found;
- access challenge or paywall;
- no usable text.

Style then checks reference formatting independently. A source may exist and support a claim while still failing style; conversely, perfect style says nothing about existence or support.

## Verify

Verify creates a versioned, hash-bound work item for each cited `(claim, reference, scope)` that has locally persisted text. Cross-reference citations use the admitted antecedent text. Table-only citations remain outside semantic verification unless explicitly enabled.

For each pair, the deterministic layer:

1. validates source path, hash, scope, and bibliographic identity;
2. freezes provider, model, prompt, context, pacing, retry, and selection policy;
3. selects source context;
4. dispatches Jury1 through the configured backend;
5. validates the response schema and grounds every evidence span against local source text;
6. invokes Jury2 only when the configured policy makes it eligible;
7. persists every attempt, rejection, transition, and final terminal through compare-and-set state changes.

An identity-inadmissible full text cannot produce accepted support. A quote that is not present in the admitted source is rejected. Exhausted technical or guard budgets terminate as explicit uncertainty/exhaustion; they are not promoted to success.

See [Verification and evidence](06-verification-and-evidence.md) for the complete semantic boundary.

## Web research

The retained `web_research` state does not collect third-party web pages. A source without admitted full text, abstract, or attributed Google Books preview remains not assessable for semantic verification.

## Report, seal, and completion gate

The report is generated entirely from typed database projections. No LLM writes or composes report prose.

The report phase:

1. renders `report.md` deterministically;
2. seals the body and canonical Parse/Verify projections;
3. appends an entry to the hash-chained `report.journal.md`;
4. writes `report.signature_status.md`;
5. independently reruns the completion/authenticity gate.
6. after a successful gate, generates the sealed `report.html` companion unless
   `CITATION_VERIFIER_REPORT_HTML` explicitly disables it.

The gate fails when a report is missing, stale, tampered, has a broken journal chain, lacks required pair terminals, or disagrees with the current database projection. The phase remains `report`, allowing a valid repair and resume; it does not mark the run `done` first.

Automatic HTML output is enabled when the setting is absent or empty and can be
disabled with `0`, `false`, `no`, or `off`. Callimachus never generates an
unverified preview automatically; `report.preview.html` remains an explicit
operator action through `report-html --preview-unverified`.

## Resume, concurrency, and integrity

- Every phase transition and pause is persisted in SQLite.
- Typed tasks move through `pending → answered → applied`; cancellation is explicit.
- A run lock prevents concurrent drivers from racing the same run directory and returns exit code `3` to the second process.
- Integrity leases/checkpoints bracket phase transitions and work units. A mismatch stops the driver.
- Verify can reconstruct scheduler and dispatch state from persisted observations after a safe resume.
- Earlier failed attempts remain in the audit trail after a later retry succeeds.

These properties make resume a continuation of recorded state, not a best-effort restart from files on disk.

---

Previous: [Configuration](03-configuration.md) · Next: [Tasks and recovery](05-tasks-and-recovery.md).
