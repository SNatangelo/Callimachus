# 05 — Tasks and Recovery

## Why tasks exist

The driver pauses only when it needs information that deterministic code cannot obtain or safely infer. Every pause is represented by a typed row in `run.sqlite`; there are no free-form task files that can be edited to bypass validation.

Task slots are:

| Slot | Purpose | Who answers it |
|---|---|---|
| `fetch` | Supply missing source text, a file, a URL, a challenge-page result, or an explicit not-found answer. | Operator or authorized agent |
| `parse_review` | Adjudicate an ambiguous Parse target. | Operator or authorized reviewer |
| `verify` | Internal, versioned claim-evidence work item. | Configured Verify runtime; not manually authored |

Task status moves through:

```text
pending → answered → applied
```

`cancelled` is explicit and remains visible. Storing an answer does not mean that it has been accepted into the next phase; resume validates and applies it.

## Inspect tasks

```bash
python run.py tasks list --run runs/<id>
python run.py tasks list --run runs/<id> --slot fetch --status pending
python run.py tasks list --run runs/<id> --status answered --status applied
python run.py tasks show --run runs/<id> --task <task-id>
```

`show` displays the exact contract, expected target, hashes, and current answer/application state. Always inspect it before answering.

## Answer a Fetch task

A simple Fetch task accepts one evidence path:

```bash
# Raw source file
python run.py tasks answer-fetch --run runs/<id> --task <task-id> \
  --file-path article.pdf

# Already extracted UTF-8 text
python run.py tasks answer-fetch --run runs/<id> --task <task-id> \
  --text-file article.txt

# Candidate URL; the pipeline still retrieves and validates it
python run.py tasks answer-fetch --run runs/<id> --task <task-id> \
  --url https://example.org/article.pdf

# Explicitly report that no source was found
python run.py tasks answer-fetch --run runs/<id> --task <task-id> \
  --not-found
```

To deliberately stop chasing every remaining ordinary Fetch task and continue
with Resolve evidence only:

```bash
python run.py tasks skip-fetch --run runs/<id>
python run.py --run runs/<id> --resume --no-fetch
```

`skip-fetch` records `found: false` for pending **source-text retrieval** tasks.
It does not mean the bibliography entry is nonexistent and does not overwrite the
separate Resolve existence/identity result. Browser-challenge tasks remain explicit.


Browser-challenge tasks can batch results by reference ID:

```bash
python run.py tasks answer-fetch --run runs/<id> --task <task-id> \
  --item-file ref-1=downloaded-1.pdf \
  --item-text-file ref-2=extracted-2.txt \
  --item-not-found ref-3
```

The answer is admitted with producer provenance. On resume, the pipeline hashes, extracts, associates, and identity-checks the supplied material. A file is not trusted merely because it was named in a task answer.

## Guided Fetch recovery (optional)

After an interactive run pauses in Fetch, the driver asks whether to open the
guided desktop workflow. If PySide6 or Playwright is missing, installation is a
separate opt-in question; declining leaves the paused run unchanged. The same
extra can be installed beforehand with:

```bash
python -m pip install -r requirements-gui.txt
```

The window lists every parsed reference and keeps source availability separate
from fabrication suspicion. A green check means validated full text, a yellow
filled marker means abstract-only, and an empty black marker means no usable
text. Suspected fabrication is shown independently in red; selecting the row
also shows the recorded reason. The contextual guidance states whether a Fetch
answer can still be supplied and what leaving the reference unresolved will do.

You can open the resolved URL or DOI in the system browser, drag or select one
local file, choose `Full text` or `Abstract`, and confirm its reference. The
assisted-browser option controls an already installed Google Chrome through
Playwright. Chrome is not bundled or downloaded. The visible browser remains
under user control so authentication or a CAPTCHA can be completed manually;
Callimachus does not automate or bypass access controls. The current HTML or a
PDF response/download can then be queued for the selected reference.

Supply only an authentic copy of the exact cited work after checking its title,
authors, and identifier. Do not substitute a similar work, edit or reconstruct
missing text, or invent source text. If the exact work cannot be verified, leave
it unresolved and use **Proceed / skip remaining**; it remains unavailable to
Verify.

The terminal prompt accepts `yes`, `no`, or `skip`. `yes` opens Guided Fetch;
`no` leaves the run paused and exits; `skip` records every remaining supported
source retrieval as an explicit `user_waived` answer and immediately resumes
the pipeline toward Verify. Unsupported non-retrieval tasks still block the
skip rather than being silently discarded.

When an agent or harness was asked to open the window but has no interactive
terminal, invoke it explicitly for the already paused run:

```bash
python run.py --run runs/<id> --resume --guided-fetch
```

If the run was created with `--agent-identity`, repeat that same option on the
launch command so execution assurance can be resolved before the window opens.

This command is allowed only for a non-autonomous Fetch pause. It opens no
fallback automation and never installs optional GUI components; install
`requirements-gui.txt` first if requested. The agent merely starts the window:
the human user still selects material, confirms the reference, and proceeds.
A run invoked with `--agent-identity`, whether authority-protected or not,
cannot accept Guided Fetch answers through that agent process: **Proceed / skip
remaining** fails before any task answer is written. Use a separately
authenticated operator channel for that deployment. A standalone run invoked
without an agent identity may record the local operator's Guided Fetch answer.

Queued material is not yet accepted evidence. On **Proceed / skip remaining**,
the workflow submits complete provenance-bearing Fetch task answers and records the other
pending retrievals as explicitly waived by the user. Resume then applies the
normal hashing, extraction, source-identity and quality gates. A failed gate
remains visible and cannot be overridden by the GUI. Non-retrieval Fetch tasks,
such as an outstanding OCR decision, must still be handled through their typed
task contract.

Generated Fetch task instructions and `fetch_manual_sources.md` operational
guidance are in English. Bibliographic titles and quotations retain the source
language and are never translated as part of task generation.

## Manual Parse review

Start a review-enabled run:

```bash
python run.py --input paper.pdf --manual-review
python run.py --input paper.pdf --manual-review \
  --manual-review-ref-number 12
```

After Parse returns exit code `10`:

```bash
python run.py tasks list --run runs/<id> \
  --slot parse_review --status pending
python run.py tasks show --run runs/<id> --task <task-id>
```

Every answer requires the displayed target hash and a reason.

### Declare that a target contains no sources

```bash
python run.py tasks answer-review --run runs/<id> --task <task-id> \
  --target-sha256 <sha256> --action no-sources \
  --reason "The note contains commentary only"
```

### Split a multi-source footnote

Each file must contain an exact source substring from the raw target. At least two source texts are required.

```bash
python run.py tasks answer-review --run runs/<id> --task <task-id> \
  --target-sha256 <sha256> --action split-sources \
  --source-text-file source-1.txt \
  --source-text-file source-2.txt \
  --reason "The note contains two independent references"
```

The driver derives ordered offsets and creates typed operational child references. It does not rewrite the original Parse row.

### Correct a bibliographic identity

Supply a corrected title, DOI, or both:

```bash
python run.py tasks answer-review --run runs/<id> --task <task-id> \
  --target-sha256 <sha256> --action correct-identity \
  --title "Correct title" --doi "10.1234/example" \
  --reason "Corrected from the printed bibliography"
```

The corrected identity is an overlay and must still pass normal Resolve and source-identity gates.

### Preserve ambiguity

```bash
python run.py tasks answer-review --run runs/<id> --task <task-id> \
  --target-sha256 <sha256> --action keep-ambiguous \
  --reason "The source boundary cannot be established"
```

`no-sources` and `keep-ambiguous` exclude the target from Resolve and Fetch. Stale hashes, duplicate application, malformed splits, or fields that do not belong to the selected action fail closed.

## Supply a folder of sources

Use the provisioning commands when the user already has source files:

```bash
python run.py provide ingest --run runs/<id> --dir supplied-sources
```

`ingest` extracts the files and automatically maps only strongly corroborated identities. Ambiguous files remain in the typed ingest-review projection rather than being guessed.

Map one file explicitly:

```bash
python run.py provide map --run runs/<id> \
  --ref <ref-id> --file supplied-sources/article.pdf
```

The normal path requires DOI, PMID, title/author/year, or another identity corroboration. `--force` records a manual mapping and should be used only with a documented identity decision. It does not change an existing `not_found` existence result.

Register text or a URL directly with explicit tier and origin:

```bash
python run.py provide record --run runs/<id> --ref <ref-id> \
  --tier fulltext --origin user --text-file article.txt
```

Find the currently registered path:

```bash
python run.py provide path --run runs/<id> --ref <ref-id>
```

## OCR recovery

Unreadable cited PDFs are parked in `sources/ocr_queue/` and recorded in the SQLite unreadable-source queue. They are not treated as empty or verified text.

The standalone OCR command is:

```bash
python run.py ocr --pdf scanned.pdf --out scanned.txt \
  --lang eng+ita --dpi 300
```

For a pipeline task, answer with the OCR output or with the source file and let the admitted Fetch/OCR path process it. Once OCR text is registered under `sources/parsed/`, the queue row is completed and the source becomes eligible for identity and Verify checks.

Unattended operation does not authorize unbounded OCR work on every cited
scan. A queued source can remain explicitly uncheckable.

## Resume safely

After any accepted answer:

```bash
python run.py --run runs/<id> --resume
```

On resume, the driver:

1. acquires the run lock;
2. validates integrity and the current schema;
3. applies admissible answered tasks;
4. reconstructs the persisted phase/runtime state;
5. advances only from the recorded phase.

Already completed work is not erased. Earlier failures and retries remain in the audit trail.

## Start over without losing lineage

Use a fresh start when the original manuscript and stable configuration should
be reused but all phases must run again:

```bash
python run.py --run runs/<parent-id> --fresh-start
```

The child receives a new timestamp/run ID and records parent provenance. It
does not mutate or resume the parent, and it does not inherit the parent's
`--no-fetch`, `--references-only`, or `--autonomous` execution modes unless
they are explicitly supplied again on the new command.

## Remediate a completed report

A completed run is immutable. To apply a manual correction, create a fresh
Parse child with the exact command prefix:

```bash
python run.py --remediate-completed runs/<parent> --run runs/<child>
```

The child preserves the parent's input and immutable configuration and records
its parent and content hashes; it does not resume or edit the completed parent.
The only remediation selectors are `--manual-review` and
`--manual-review-ref-number N`. The HTML report may show commands for routes
that could be emitted by this fresh child; they are possibilities, not active
or guaranteed tasks. Inspect the actual child with `tasks list` and
`tasks show` before answering anything.

For citation attribution, use `--manual-review`. The Parse review answer may
use `--action select-reference`, `--action select-claim`, or
`--action keep-unresolved`. The answer is bound to the displayed target hash,
reason, and candidate selection; the
original automatic finding remains visible and the applied operator decision
is labelled as an override.

Footnote source splitting is available only for typed footnote containers, not
arbitrary bibliography entries. For an unresolved ambiguous note, use
`--manual-review`; for a selected existing reference, use
`--manual-review-ref-number N` when the child should emit the corresponding
review. Answer with `split-sources`, `no-sources`, or `keep-ambiguous`. Split
texts must be exact substrings of the target, and every pending Parse review
task must be answered before Parse can advance. A selected existing reference
can require both source-split and identity review tasks.

For bibliographic identity correction, use
`--manual-review-ref-number N`, then answer `correct-identity` or
`keep-ambiguous`. The correction is an overlay and remains subject to Resolve
and source-identity gates. For not-found, mismatch, fabrication, or title
warnings, creation of the identity task happens only if the fresh child Parse
actually emits it.

When Verify emits a source-identity-attestation task, inspect its target and
either attest that exact source/reference binding or retain the uncertainty:

```bash
python run.py tasks answer-review --run runs/<id> --task <task-id> \
  --target-sha256 <sha256> --action attest-identity \
  --reason "Inspected the exact source and reference binding"

python run.py tasks answer-review --run runs/<id> --task <task-id> \
  --target-sha256 <sha256> --action keep-unverified \
  --reason "Identity could not be corroborated"

# Or skip every pending source-identity review in one explicit action.
# The affected sources remain unverified and are excluded from semantic checks.
python run.py tasks skip-identity --run runs/<id>
```

Attestation is a typed operator decision, not an override. It is unavailable
when a hard identity deny applies. `skip-identity` stores the existing typed
`keep_unverified` decision for each frozen target; it does not attest identity
or mark a source as found.

Fetch, OCR, browser, and web-research recovery routes are likewise conditional:
answer only a task that the child actually emitted. Supplied source material is
still extracted and identity-checked. Research URLs are leads that Callimachus
re-fetches and quote-validates; they are not direct evidence. `--not-found`
retains the uncertainty and does not constitute an override.

## Frozen-Fetch Verify experiments

To compare Verify behavior without repeating network-dependent phases:

```bash
# Produce a clean post-Fetch baseline
python run.py --input paper.pdf --freeze-after-fetch

# Create a diagnostic child at Verify
python run.py --run runs/<new-child> \
  --fork-frozen-fetch-verify runs/<baseline>
```

The baseline must contain Parse, Resolve, and Fetch data but no completed Verify work. The fork copies only the authorized database projections, admitted source inventory, unreadable-source state, and Fetch cache; it records inventory fingerprints and regenerates Verify tasks under current code.

This is a diagnostic/benchmark path, not a compatibility migration mechanism.

## Rerun Verify from a completed run

To compare a completed run with the LLM policy currently configured in the
environment, create a new Verify child:

```bash
python run.py --run runs/<new-child> \
  --fork-completed-verify runs/<completed-parent>
```

The parent must still be available with status `completed`, phase `done`, a
passing completion gate, and an intact registered source inventory. The child
copies the authorized Parse/Resolve/Fetch relations and source assets, then
generates fresh Verify tasks. It does not inherit the parent's LLM configuration,
Verify task answers, Verify candidates, pair state, verdicts, report, or report
journal. The parent remains read-only and the child records its parent run ID,
origin and source-inventory fingerprint.

Applied manual Parse adjudications are copied with their hash-bound task,
accepted answer, operator provenance, authenticated attached files, and
effective Parse overlay. Pending, cancelled, and unapplied review work is not
copied. The child therefore preserves the reviewed deterministic Parse result
without inheriting any prior Verify work.

In the desktop application, select the completed run in **Cronologia**, choose
**Rifai Verify…**, then select one or more of the currently configured
backend/model lanes. The selection applies only to the child process; it does
not edit `.env` or reopen the historical run.

## Never edit the database to recover a run

Direct SQLite edits bypass task provenance, compare-and-set transitions, integrity checkpoints, and report sealing. Use tasks, provisioning commands, resume, or a fresh child. If the database or source inventory has been modified outside the trusted path, treat the run as non-audit-ready.

---

Previous: [Pipeline](04-pipeline.md) · Next: [Verification and evidence](06-verification-and-evidence.md).

## Export bibliographic findings without Fetch or Verify

For an existing run that has completed Parse (including a run paused in Fetch or
Verify), export only the bibliographic evidence already recorded by Resolve:

```bash
python run.py report-bibliography --run "runs/<id>"
python run.py report-bibliography --run "runs/<id>" --suspects-only
```

The default output is a **sibling** directory, `runs/<id>-bibliography`, containing
`bibliography-report.html`, `.md`, and `.json`. Use `--output DIRECTORY` to select
another directory outside the original run. This keeps the run's artifact
inventory intact. Existing exports at that destination are replaced atomically
per file; all three carry the same snapshot hash.

This command performs **no new bibliographic lookup, full-text download, semantic
Verify, or web research**. It does not answer/cancel tasks, mark the pipeline done,
or replace `report.md` or its seal. It reads one consistent SQLite snapshot and
labels the output **BIBLIOGRAPHIC SCREENING ONLY**, not a signed full verification.
A protected agent run still requires an available authority and clean preflight;
this exporter cannot acknowledge an integrity downgrade or override a mismatch.

Suspicions are copied from the recorded `suspected_fabricated` tag, not inferred
from missing text. Transient unresolved lookups, unexamined references, weak
matches, identifier errors, and retraction flags remain separate. `--suspects-only`
filters the human-readable detail; coverage counts and every reference remain in
JSON. Zero suspicion tags is **not** proof that every reference exists. This is
an export of saved work, not a mode for starting a fresh manuscript analysis.

## Source-identity pauses and schema compatibility

A Verify pause can ask the human to attest the identity of a retrieved source.
This is **not** a request to write a semantic verdict. Inspect the task's target
SHA-256 and use `tasks answer-review` with `attest-identity` or `keep-unverified`,
a reason, and that target hash, then resume the driver.

Schema 86 admits `verify` and `parse_review` pause slots alongside `fetch` and
`research`. Schema 85 remains readable and resumable. The first new pause event
upgrades only this constraint inside the phase writer's transaction (and, for
protected runs, its integrity transition). Old events and append-only triggers
are retained. Read-only commands do not migrate the database. This is not a
general migration of schema 81 or other incompatible historical run formats.
