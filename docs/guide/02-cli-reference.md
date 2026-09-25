# 02 — CLI Reference

## Dispatcher

`run.py` is the single public dispatcher:

```bash
# Complete pipeline
python run.py --input <manuscript> [options]

# Specialist tool
python run.py <command> [options]
```

Without a subcommand, it loads `core.app.run`. With a subcommand, it dispatches to the owning module, which provides its own `--help`.

## Complete-pipeline options

### Input and lifecycle

| Option | Behavior |
|---|---|
| `--input PATH` | Start a new run from a manuscript. |
| `--run DIR --resume` | Resume an existing DB-native run. |
| `--run DIR --resume --guided-fetch` | Explicitly open the human-operated Guided Fetch window for an eligible paused Fetch run. This is suitable when Codex or Claude Code is running without a terminal TTY; it does not let the agent answer or attest sources. Any run invoked with `--agent-identity` requires a separately authenticated operator channel to submit Guided Fetch answers. |
| `--run DIR --status` | Show the current phase and task state without advancing. |
| `--json-only` | With `--status`, print only the machine-readable JSON snapshot. |
| `--run OLD --fresh-start` | Create a new run ID using the parent input and stable configuration, then rerun the full interactive pipeline. This is not a resume and does not inherit `--no-fetch`, `--references-only`, or `--autonomous`. |
| `--run NEW --fork-frozen-fetch-verify BASELINE` | Create a diagnostic child from a clean post-Fetch baseline. It copies Parse/Resolve/Fetch projections and regenerates Verify under current code. `NEW` must not exist. |
| `--run NEW --fork-completed-verify PARENT` | Create a Verify child from a completed run. The child reuses the parent's frozen Parse/Resolve/Fetch evidence and applied, provenance-bound Parse adjudications; it records the parent and source-inventory provenance, then runs Verify with the current environment policy. `NEW` must not exist. |
| `--freeze-after-fetch` | Stop deliberately after Fetch; useful for creating a pre-Verify baseline. |
| `--manual-review` | Add the opt-in Parse-review pause before Resolve. |
| `--manual-review-ref-number N` | Request a hash-bound review for reference `N`; repeatable and implies manual review. |
| `--agent-identity ID` | Stable opaque harness identity, 1–64 ASCII characters; requires the configured integrity authority. |

Examples:

```bash
python run.py --input paper.docx
python run.py --run runs/20260828-120000 --resume
python run.py --run runs/20260828-120000 --resume --guided-fetch
python run.py --run runs/20260828-120000 --fresh-start
python run.py --input paper.pdf --freeze-after-fetch
python run.py --run runs/experiment-new \
  --fork-frozen-fetch-verify runs/baseline-post-fetch
python run.py --run runs/verify-rerun \
  --fork-completed-verify runs/completed-parent
```

### Policy and acquisition

| Option | Values and behavior |
|---|---|
| `--autonomous` | Mark the run as unattended and shorten pause messages. The driver runs Verify with or without this flag; tasks and gates can still pause the run. |
| `--accuracy` | `maximum`, `maximum_fallback`, `standard`, `abstract`, or `standard_web`; default is `standard` unless overridden by the environment. |
| `--style` | `vancouver`, `apa7`, `chicago`, or `mla9`; detected automatically when omitted. |
| `--model ID` | Model identifier to use and record in the run policy. |
| `--verify-table-citations` | Turn citations found only in table rows into pairs to verify. By default they count toward coverage but do not become claims. |
| `--max-retries N` | Legacy driver retry budget; default `2`. Jury1 and Jury2 have separate caps. |
| `--no-fetch` | Do not pause for full-text recovery; use only text already found by Resolve. Missing sources remain explicitly unverifiable. |
| `--references-only` | Run Parse and Resolve, skip Fetch and semantic Verify, and write `report.preview.html` with the standard HTML template. The preview is explicitly unsealed and not audit-ready. On resume, the flag switches a paused pre-report run to this partial mode without consuming its pending tasks. |
| `--mailto EMAIL` | Contact sent to bibliographic APIs for polite-pool access. |
| `--http-profile` | `browser_like` or `plain`; defaults to the environment setting, then `browser_like`. |
| `--challenge-mode MODE` | Strategy for publisher challenge pages. Canonical modes include `off` and `queue`; browser and interactive modes are also registered. Use `python run.py --help` for the installed list. |
| `--ocr-lang LANG` | OCR language such as `eng` or `eng+ita`; default `eng`. |
| `--proceed` / `--ignore-missing` | Acknowledge missing optional preflight settings. It does not weaken deterministic gates. |

### Diagnostic integrity overrides

`--debug-override-artifact-integrity` is accepted only with `--debug-override-reason`. It exists for an explicitly labeled diagnostic run. It is not a normal recovery procedure and cannot make untrusted artifacts audit-ready.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Command completed; for the driver, the run reached `done`; for `verify`, the gate passed. |
| `2` | Argument, configuration, or execution error. |
| `3` | Another process already holds the run lock. |
| `10` | Action required: a typed task must be answered or applied. |
| `20` | Completion gate failed, or the run is incomplete or unauthenticated. |
| `21` | `present` refused to display a report that was not verified/authentic. |

## Subcommands

Subcommands support inspection, provisioning, and targeted operations. A normal run does not require executing them manually in phase order.

| Command | Main capability |
|---|---|
| `parse` | Extract the manuscript, claims, references, and citation edges; produce a debug view. |
| `preprocess` | Prepare source text as full text, abstract, or RAG context. |
| `authoryear resolve` | Link an ambiguous author-year marker to a selected reference. |
| `resolve` | Resolve one DB-native reference and optionally export retrieved text. |
| `provide` | Locate a registered source. Mutating `ingest`, `map`, and `record` entry points are disabled for DB-native runs; submit material through authenticated tasks. |
| `fetch` | Run targeted full-text retrieval for one reference. |
| `ocr` | OCR a PDF explicitly into a text file. |
| `verify` | Run the completion/authenticity gate, not the semantic jury. |
| `report` | Regenerate the deterministic report projection. |
| `report-html` | Generate or verify a self-contained human-readable companion for an existing run. |
| `present` | Display a report byte-for-byte after required gate and authenticity checks. |
| `preview` | Check targeted phrases through key-gated Google Books snippets. |
| `gaps` | Compute missing-source gaps for an accuracy regime. |
| `style-check` | Check one bibliography entry or a complete run against a style. |
| `style-detect` | Detect the citation style from a run. |
| `tasks` | List, inspect, and answer SQLite-backed tasks. |
| `configure` | Run GUI/headless setup, signing-key generation, and optional manual Claude Code hook-patch preparation. |
| `app` | Launch the optional five-tab Callimachus desktop application. |
| `benchmark` | Compare Verify terminals from runs that share one frozen deterministic layer. |
| `journal-catalog` | Inspect, atomically update from public NLM data, or explicitly import an authorised ISSN MARCXML snapshot. |

`configure --prepare-claude-hooks` writes manual instructions beside the selected settings file; it does not edit Claude Code settings. Its default target is `native`. Use `--claude-hook-target windows-wsl` only from WSL, with an explicit absolute path to the Windows Claude settings as visible from WSL and `--wsl-distro` (or `WSL_DISTRO_NAME`); it emits a Windows-host `wsl.exe` hook command.

### Desktop application

```bash
python run.py app [--language it|en] [--runs-root <dir>]
```

`--runs-root` selects the run directory used by the desktop History tab and by
the initial Cache tab view. A configured `CITATION_VERIFIER_STATE_DIR` remains
authoritative for shared runtime state and reusable-text storage.

The History tab's **Regenerate report** action invokes the same deterministic
operation as:

```bash
python run.py report --run <run>
```

Regenerating a report does not by itself establish completeness or authenticity.
Use the deterministic `verify` gate and then `present`, as described below,
before treating the report as audit-ready.

### Parse and preprocessing

```bash
python run.py parse --input paper.pdf --debug parse_debug.md \
  --citations auto --window 1

python run.py preprocess --text source.txt --scope rag \
  --claim "Claim to verify" --max-chars 20000 --topk 8 --out prepared.txt
```

Explicit Parse schemes are `auto`, `author-year`, `inline-doi`, `numeric`, and `mla`.

### Targeted Resolve and Fetch

```bash
python run.py resolve --run runs/<id> --ref-id <ref-id> --text-out found.txt
python run.py fetch --run runs/<id> --ref-id <ref-id>
```

### Fetch task answers and OCR sources

```bash
python run.py tasks list --run runs/<id> --slot fetch --status pending
python run.py tasks show --run runs/<id> --task <task-id>
python run.py tasks answer-fetch --run runs/<id> --task <task-id> \
  --file-path article.pdf --url <source-url>
python run.py provide path --run runs/<id> --ref <ref-id>
```

For a PDF already parked in `sources/ocr_queue/`, produce the OCR text and
answer its associated Fetch task explicitly:

```bash
python run.py ocr --pdf runs/<id>/sources/ocr_queue/<scan>.pdf \
  --out run/user_sources/<ref-id>.ocr.txt
python run.py tasks answer-fetch --run runs/<id> --task <task-id> \
  --ocr-text-file run/user_sources/<ref-id>.ocr.txt
python run.py --run runs/<id> --resume
```

`--ocr-text-file` is not a manual identity override. It binds the authenticated
text file to the task's sole pending same-reference scan by SHA-256, then applies
the normal source identity and quality gates. The scan is retired only after
successful admission with source origin `ocr`. A missing, changed, ambiguous, or
unassociated scan is rejected and remains pending.

### Gate and report

```bash
python run.py report --run runs/<id>
python run.py report-html --run runs/<id> --locale it --theme callimachus
python run.py report-html --run runs/<id> --verify
python run.py verify --run runs/<id> --write-status
python run.py verify --run runs/<id> --strict-crediting --require-signature
python run.py present --run runs/<id>
```

`--strict-crediting` additionally requires at least one semantically crediting terminal. The normal gate can pass a complete run that honestly contains only negative or unverifiable results.

The normal pipeline generates `report.html` automatically after the canonical
Markdown report passes its authenticity gate. Set
`CITATION_VERIFIER_REPORT_HTML=0` to disable this output. `report-html` reads the
same typed run projection and remains available to generate or regenerate the
companion later without changing `report.md`. Completed, sealed schema-59 runs
are also supported through an explicit read-only compatibility path; the
database and canonical Markdown artifacts are not migrated or rewritten, and
the HTML diagnostics mark modern execution assurance as unavailable. Other
historical schemas remain unsupported. To inspect an incomplete or
unverifiable run without presenting it as audit-ready, use the explicit preview
mode:

```bash
python run.py report-html --run runs/<id> --preview-unverified
```

That command writes only `report.preview.html`, with a persistent warning and no
HTML companion seal. Installed locale and theme identifiers are listed by the
external assets under `core/report/human/assets/locales/` and
`core/report/human/assets/themes/`.

### Benchmark

```bash
python run.py benchmark \
  --run runs/model-a-1 --run runs/model-a-2 --run runs/model-b-1 \
  --out benchmarks/frozen-comparison
```

Benchmark output measures terminal discipline and concordance, not accuracy against a gold set.

For the complete syntax of any installed command:

```bash
python run.py <command> --help
```

---

Previous: [Quick start](01-quickstart.md) · Next: [Configuration](03-configuration.md).

### Bibliographic screening export

```bash
python run.py report-bibliography --run "runs/<id>" [--suspects-only] [--output DIRECTORY]
```

Exports persisted Parse/Resolve evidence to a sibling directory without executing
Fetch/Verify, consuming tasks, or completing the original run. The Markdown/HTML
and JSON are explicitly scoped, unsigned bibliographic screening exports, not the
normal sealed `report.md`. Missing or transient checks are not fabricated sources.
See [Tasks and recovery](05-tasks-and-recovery.md) for scope and schema compatibility.
