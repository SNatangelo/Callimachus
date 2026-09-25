# 01 — Quick Start

## Prerequisites

The base pipeline requires:

- Python 3.9 or later;
- outbound network access for resolvers and source retrieval;
- at least one configured LLM backend for Verify;
- local space for `runs/<id>/run.sqlite` and normalized source text.

OCR and RAG are optional components with separate dependencies.

## Install

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Install optional components only when needed:

```bash
# Scanned PDFs
python -m pip install -r requirements-ocr.txt

# Extractive RAG for Verify context
python -m pip install -r requirements-rag.txt

# Callimachus desktop application and guided Fetch
python -m pip install -r requirements-gui.txt
```

OCRmyPDF with Tesseract is recommended when the required platform packages are
available. The `requirements-ocr.txt` stack remains the portable,
cross-platform fallback and works without system OCR binaries. Callimachus uses
the first installed backend in a fixed order: OCRmyPDF, Tesseract with Poppler,
then RapidOCR with PDFium. RAG requires the `bm25s` version pinned in
`requirements-rag.txt`. If it is missing or has the wrong version, Verify stops
instead of silently changing context strategy.

The guided Fetch extra installs PySide6 and the Playwright control library.
Google Chrome remains a separate, external prerequisite: Callimachus never
bundles or downloads a browser. When an interactive Fetch pauses, the driver
offers the guided workflow. If its Python components are absent, it asks for a
second explicit confirmation before installing `requirements-gui.txt`.

Launch the full desktop application with:

```bash
python run.py app
```

To keep desktop runs under a different directory, pass an explicit run root:

```bash
python run.py app --runs-root <dir>
```

The History tab and the initial Cache tab view use that selected run root. If
`CITATION_VERIFIER_STATE_DIR` is configured, it remains authoritative for
shared runtime state and reusable-text storage.

Its tabs cover new analyses, previous runs, reusable text cache, effective
configuration, and this local guide. Runs still execute through the existing
CLI driver in a child process; the SQLite run database remains the system of
record. Removing an item from the Cache tab disables cross-run reuse while
preserving user originals and evidence already materialized in runs. A cached
abstract does not block a later full-text attempt when full text is possible.
On Analysis, **Finish without manual review** leaves automatic checks active,
waives pending manual Fetch retrieval, and keeps sources needing a manual
identity decision unverified. It disables interactive browser challenges for
that run. The choice and negative decisions remain in the
run audit trail. The final HTML report opens automatically and can be reopened
from Analysis. **Resume latest** applies only to an unfinished latest run.
From the previous-runs tab, **Run Verify again…** (or **Continue with Fetch and
Verify** for a references-only report) creates a new child from a
completed run and lets you select exact backend/model lanes from the current
configuration. Parse, Resolve and Fetch evidence, including applied
provenance-bound Parse reviews, is reused with recorded provenance; the
historical run and its verdicts remain unchanged.

## Minimum configuration

Copy the template and edit the local copy:

```bash
cp .env.example .env
```

The driver loads `.env` or `.env.local` automatically. Variables already present in the process environment take precedence.

Keep these two setup references open while configuring the first run:

- [Keys and credentials](10-keys-and-credentials.md) gives exact key-creation
  steps and official provider links.
- [Environment reference](11-environment-reference.md) defines the accepted
  values, defaults, and sensitivity of every label in the template.

They are numbered as reference chapters, but they are part of initial setup.

At minimum, select a backend and an explicit Jury2 level before Verify can run. A model ID is also required by model-driven HTTP backends and is recommended for a reproducible CLI-backed run:

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=claude_cli
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
CITATION_VERIFIER_MODEL=<model-id>
```

`claude_cli`, `codex_cli`, and `gemini_cli` reuse the login of the corresponding installed client. A credentialed HTTP backend also requires its credential, for example:

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=anthropic
ANTHROPIC_API_KEY=<secret>
CITATION_VERIFIER_MODEL=<model-id>
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
```

FreeToken is a credentialless local HTTP backend. Start the separately
installed server first, then configure its server root and an explicit model:

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=freetoken
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
FREETOKEN_HOST=http://127.0.0.1:1919
FREETOKEN_MODEL=<served-model-id>
```

An empty `FREETOKEN_HOST` uses `http://127.0.0.1:1919`; the value is the
server root, without `/v1`. Callimachus does not install or start FreeToken and
does not discover the served model automatically. Follow the
[FreeToken setup instructions](10-keys-and-credentials.md#run-a-local-freetoken-server-without-an-api-key)
to launch it and inspect `/v1/models`.

No backend is selected automatically. `CITATION_VERIFIER_VERIFY_JURY2_LEVEL` deliberately has no default: choose `off`, `low`, `medium`, or `high` explicitly.

The configuration tool can prepare `.env` and, when requested, deployment signing and hooks:

```bash
python run.py configure
python run.py configure --headless --mailto you@example.org \
  --accuracy standard --env-path .env
```

For an existing `.env`, the configuration tool applies an atomic selective patch only to values explicitly supplied; with no values it is a no-op. It does not add signing-key paths to `.env`.

## Run the first manuscript

The Python driver executes the configured jury calls itself; no agent is
required:

```bash
python run.py --input paper.pdf --accuracy standard
```

To check only bibliographic existence and identity, without Fetch or an LLM
backend, start an explicitly partial run:

```bash
python run.py --input paper.pdf --references-only
```

This path stops after Resolve and writes `report.preview.html` with the standard
report interface and an unverified-preview warning. It does not create a sealed
`report.md` or claim-evidence verdicts.

Accepted manuscript formats are DOCX, LaTeX, PDF, Markdown, plain text, and HTML/XHTML. If `--style` is omitted, the style is detected after Parse.

`--autonomous` is optional. It marks the run as unattended and shortens pause
messages. The driver runs Verify with or without this flag, and tasks or gates
can still pause the run.
The startup preflight may ask for a contact email and an optional Google Books key. The email is sent to bibliographic services for polite-pool access but is not included in the report. To acknowledge missing optional settings in a non-interactive environment:

```bash
python run.py --input paper.pdf --accuracy standard --proceed
```

`--proceed` does not replace a Verify backend, a required dependency, or an integrity check.
If a normal interactive run reaches Verify without
`CITATION_VERIFIER_VERIFY_BACKENDS`, Callimachus asks for a backend name (or CSV
list) for that run, or offers the same reference-check-only preview.

## Pause and resume

The driver returns exit code `10` when external input is required. Use the run directory printed by the driver:

```bash
python run.py tasks list --run runs/<id> --status pending
python run.py tasks show --run runs/<id> --task <task-id>
```

At an interactive Fetch pause, accept the guided-mode prompt to inspect every
reference, open its resolved link, and queue a lawful local file or a manually
captured browser page. See [guided Fetch recovery](05-tasks-and-recovery.md#guided-fetch-recovery-optional).

After answering the task with the appropriate command, resume:

```bash
python run.py --run runs/<id> --resume
```

Repeat until the driver prints `DONE` or reports a failed gate. Do not edit `run.sqlite` directly.

## Check and present the result

Final artifacts are stored in the run directory:

```text
runs/<id>/report.md
runs/<id>/report.journal.md
runs/<id>/report.signature_status.md
```

Run the completion gate explicitly:

```bash
python run.py verify --run runs/<id>
```

For an audit that requires an external HMAC signature:

```bash
python run.py verify --run runs/<id> --require-signature
```

`present` prints the report byte-for-byte only after the required gate and authenticity checks pass:

```bash
python run.py present --run runs/<id>
```

## Fast checks before a complete run

Run Parse alone to inspect input quality and citation coverage:

```bash
python run.py parse --input paper.pdf --debug parse_debug.md
```

Inspect an existing run without advancing it:

```bash
python run.py --run runs/<id> --status
python run.py --run runs/<id> --status --json-only
```

---

Index: [Complete guide](README.md) · Next: [CLI reference](02-cli-reference.md).
