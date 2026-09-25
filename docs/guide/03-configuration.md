# 03 — Configuration

## Resolution order

For settings exposed as CLI flags, the practical precedence is:

1. explicit CLI option;
2. variable already present in the process environment;
3. value loaded from `.env` or `.env.local`;
4. a documented default, when one exists.

The driver loads the first local dotenv file it finds without overwriting existing environment variables. Keep the project-level `.env` as the normal location. `.env` is local and git-ignored; `.env.example` is the versioned public template. The [Environment reference](11-environment-reference.md) explains every label individually.

## Accuracy regimes

`--accuracy` or `CITATION_VERIFIER_ACCURACY` controls the weakest evidence tier that may be considered and whether the system keeps chasing full text.

| Regime | Full-text behavior | Fallback behavior |
|---|---|---|
| `maximum` | Requires full text for semantic verification. | Does not settle for an abstract. Missing full text remains unverified. |
| `maximum_fallback` | Still requires full text whenever fuller text is known to exist. | May inspect an abstract provisionally when full-text existence is unknown; the result remains inconclusive and the gap stays open. |
| `standard` | Prefers and attempts full text. | May use an abstract when full text is unavailable, with scope and limitations preserved. This is the default. |
| `abstract` | Does not chase full text. | Treats title and abstract as the authorized final source scope. |
| `standard_web` | Alias of `standard`, retained for existing configuration. | Third-party web pages are not admitted as citation evidence. |

`--no-fetch` is separate from the regime: it tells the driver not to pause for full-text recovery. It does not upgrade the reliability of whatever text is already available.

`--references-only` is a different, explicit run mode. It executes Parse and
Resolve only, does not require an LLM backend, and produces an unsealed
`report.preview.html` that clearly lacks Fetch and semantic Verify results. A
normal interactive run with no selected Verify backend offers this mode before
pausing; entering a registered backend name instead stores that selection for
the current run. Without interactive input, the run pauses cleanly before
starting Verify and prints both recovery choices; a blank backend setting is
never passed to the LLM runtime.

## Core run settings

| CLI / environment | Purpose | Default |
|---|---|---|
| `--accuracy` / `CITATION_VERIFIER_ACCURACY` | Evidence and retrieval regime. | `standard` |
| `--style` | Citation style: `vancouver`, `apa7`, `chicago`, or `mla9`. | Auto-detected |
| `--mailto` / `CITATION_VERIFIER_MAILTO` | Polite-pool contact for Crossref, Europe PMC, Unpaywall, and related services. | Unset |
| `GOOGLE_BOOKS_API_KEY` | Enables targeted Google Books preview-snippet checks and improves book API quota. | Feature disabled |
| `--ocr-lang` / `CITATION_VERIFIER_OCR_LANG` | OCR language set, for example `eng+ita`. | `eng` |
| `--http-profile` / `CITATION_VERIFIER_HTTP_PROFILE` | HTTP behavior: `browser_like` or `plain`. | `browser_like` in the driver |
| `--challenge-mode` / `CITATION_VERIFIER_FETCH_CHALLENGE_MODE` | Publisher challenge handling. | `off` |
| `--verify-table-citations` / `CITATION_VERIFIER_VERIFY_TABLE_CITATIONS=1` | Include table-only citation rows as claims. | Off |

The email is sent to third-party services when requests are made. It is not written into the final report.

## Journal authority and resolver coverage

Create the free NLM-backed local journal-authority catalog with:

```bash
python run.py journal-catalog update
python run.py journal-catalog status
```

The default path is `storage/journal-authority/nlm-journals.sqlite`. Set
`CALLIMACHUS_JOURNAL_AUTHORITY_AUTO_UPDATE=1` to let Resolve refresh this
snapshot after `CALLIMACHUS_JOURNAL_AUTHORITY_TTL_DAYS` (default seven days).
Updates are atomic; a failed refresh retains the last validated catalog.

An operator who has an authorised ISSN Register MARCXML export can replace the
authority snapshot explicitly without enabling any ISSN network access:

```bash
python run.py journal-catalog import-issn export.xml \
  --registry-version 2026-09
```

Callimachus never overwrites an ISSN-backed catalog with NLM data. Resolve then
refreshes missing or expired resolver-coverage observations automatically; the
default coverage catalog is `storage/resolver-coverage.sqlite` and its default
TTL is 30 days.

To force a manual refresh for the distinct journals cited by an existing run:

```bash
python -m core.resolve.resolver_coverage \
  --run-db runs/<run-id>/run.sqlite \
  --journal-authority-db storage/journal-authority/nlm-journals.sqlite \
  --catalog-db storage/resolver-coverage.sqlite
```

Repeat `--resolver <name>` to restrict the refresh to registered coverage
adapters. The command reports distinct eligible journals and actual resolver
queries; it fails instead of silently succeeding when the authority catalog or
run database is unavailable. Every run copies the exact dated observations and
response payloads it used into its own database.

## Required Verify policy

The claim-evidence runtime refuses to start without both:

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=<comma-separated registered backends>
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=<off|low|medium|high>
```

The explicit requirement prevents environment-dependent provider selection and an accidental change in second-judge policy.

### Registered backends

The current runtime registry exposes these backend names:

| Backend | Main credential/command | Model setting | Notes |
|---|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | `CITATION_VERIFIER_MODEL` | Direct Anthropic API; optional `ANTHROPIC_BASE_URL`. The transport can read `ANTHROPIC_AUTH_TOKEN`, but a token alone does not satisfy the current Verify policy. |
| `openai` | `OPENAI_API_KEY` | `CITATION_VERIFIER_MODEL` | Direct OpenAI API. |
| `gemini` | `GEMINI_API_KEY` | `GEMINI_MODEL`, then generic model fallback | Direct Gemini API. |
| `openrouter` | `OPENROUTER_API_KEY` | `CITATION_VERIFIER_MODEL` | Optional site URL and app-name headers. |
| `ollama` | `OLLAMA_API_KEY` is currently required by the declarative policy | `CITATION_VERIFIER_MODEL` | Endpoint selected by `OLLAMA_HOST`; a local server may use a dedicated local-only placeholder. |
| `freetoken` | Running FreeToken server; no API key | `FREETOKEN_MODEL`, then generic model fallback | Credentialless local HTTP backend. `FREETOKEN_HOST` is the server root; empty uses `http://127.0.0.1:1919`. Callimachus does not install, start, or discover a model from the server. |
| `host` | `CITATION_VERIFIER_LLM_HOST_COMMAND` | `CITATION_VERIFIER_MODEL` under the current policy | Calls an explicitly configured host command. |
| `claude_cli` | Existing `claude` login | `CITATION_VERIFIER_MODEL` under the current policy | Credentialless subprocess backend. |
| `codex_cli` | Existing `codex` login | `CITATION_VERIFIER_MODEL` under the current policy | Credentialless subprocess backend. |
| `gemini_cli` | Existing Gemini CLI login | `CITATION_VERIFIER_MODEL` under the current policy | Credentialless subprocess backend. |
| `openai_compatible` | `OPENAI_API_KEY` and `OPENAI_BASE_URL` | `OPENAI_MODEL`, then generic model fallback | OpenAI-compatible endpoints such as vLLM or third-party APIs. |
| `glm` | `ZHIPUAI_API_KEY` | `ZHIPUAI_MODEL`, then generic model fallback | GLM/ZhipuAI endpoint. |
| `mistral` | `MISTRAL_API_KEY` | `MISTRAL_MODEL`, then generic model fallback | Mistral API. |
| `opencode` | `OPENCODE_API_KEY` | `OPENCODE_MODEL`, then generic model fallback | OpenCode API. |

Backend availability still depends on the installed client, endpoint, credentials, and network. Naming a backend is a policy choice, not proof that it can currently dispatch.

See [Keys and credentials](10-keys-and-credentials.md) for exact creation and storage instructions. The [Environment reference](11-environment-reference.md) records current policy limitations as well as intended transport behavior.

`CITATION_VERIFIER_VERIFY_BACKENDS` is an ordered, unique CSV list. Provider keys and model fields may also contain CSV values where supported; the resolved lanes, credential fingerprints, model selection, and policy hash are frozen in the run before dispatch.

Use provider/model selectors to restrict jury roles:

```dotenv
CITATION_VERIFIER_VERIFY_JURY1_ONLY=anthropic:model-a
CITATION_VERIFIER_VERIFY_JURY2_ONLY=openai:model-b
```

Selectors must name configured `provider:model` pairs. An invalid or empty eligible lane fails configuration rather than falling back silently.

## Jury and dispatch controls

| Variable | Allowed/default behavior |
|---|---|
| `CITATION_VERIFIER_VERIFY_JURY2_LEVEL` | Required: `off`, `low`, `medium`, or `high`. |
| `CITATION_VERIFIER_VERIFY_MAX_TOKENS` | Positive integer; default `4000`. `none` is accepted only by backends that support omitted client limits. |
| `CITATION_VERIFIER_REASONING` | `auto`, `on`, or `off`; default `auto`. |
| `CITATION_VERIFIER_REASONING_EFFORT` | `low`, `medium`, `high`, `max`, or `xhigh`; default `medium`. |
| `CITATION_VERIFIER_VERIFY_MAX_CANDIDATE_CYCLES` | Positive integer; default `5`. |
| `CITATION_VERIFIER_VERIFY_JURY1_MAX_TECHNICAL_ATTEMPTS` | Positive integer; default `3`. |
| `CITATION_VERIFIER_VERIFY_JURY2_MAX_TECHNICAL_ATTEMPTS` | Positive integer; default `3`. |
| `CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT` | Global in-flight limit; default `4`. |
| `CITATION_VERIFIER_VERIFY_PACING_INTERVAL_MS` | Delay between dispatch starts; default `0`. |
| `CITATION_VERIFIER_VERIFY_PACING_BY_MODEL_MS` | Per-`provider:model` pacing overrides. |
| `CITATION_VERIFIER_VERIFY_SELECTION_SEED` | Explicit deterministic selection seed; otherwise derived from run and frozen policy. |

Rate-limit cooldown settings are also frozen:

- `CITATION_VERIFIER_VERIFY_COOLDOWN_SECONDS` — default `5`;
- `CITATION_VERIFIER_VERIFY_COOLDOWN_MULTIPLIER` — default `2`;
- `CITATION_VERIFIER_VERIFY_COOLDOWN_MAX_SECONDS` — default `300`;
- `CITATION_VERIFIER_VERIFY_COOLDOWN_STABLE_SUCCESSES` — default `2`;
- `CITATION_VERIFIER_VERIFY_COOLDOWN_OVERRIDES` — per-model overrides.

## Source-context policy

Jury1 reads a deterministic context selected before dispatch:

| Variable | Values |
|---|---|
| `CITATION_VERIFIER_VERIFY_CONTEXT_MODE` | `auto`, `full_text`, `extractive_rag` |
| `CITATION_VERIFIER_CONTEXT_PROFILE` | `large`, `medium`, `small` |
| `CITATION_VERIFIER_MAX_SOURCE_CHARS` | Positive, frozen character budget when RAG is required |

`full_text` uses the admitted text directly. `extractive_rag` ranks in-document chunks deterministically. In `auto`, the `large` profile uses full context; `medium` and `small` can select extractive RAG and therefore require a positive source-character budget. Missing RAG dependencies are a hard configuration error, not a fallback to truncation.

## Resolve and Fetch configuration

Common controls are:

| Variable | Purpose |
|---|---|
| `CITATION_VERIFIER_FETCH_PROVIDERS` | Enable/disable the configured fetch-provider set. |
| `CITATION_VERIFIER_FETCH_PROVIDER_ORDER` | Override fetch-provider priority. |
| `CITATION_VERIFIER_FETCH_PROVIDER_WORKERS` | Provider-registry concurrency. |
| `CITATION_VERIFIER_RESOLVE_WORKERS` | Reference-resolution concurrency. |
| `CITATION_VERIFIER_FETCH_WORKERS` | Source-fetch concurrency; driver default `4`. |
| `CITATION_VERIFIER_FETCH_PDF_TIMEOUT` | Per-PDF timeout; default `45` seconds, clamped by the implementation. |
| `CITATION_VERIFIER_FETCH_BUDGET_S` | Per-reference fetch wall-clock budget; `0` means unlimited. |
| `CITATION_VERIFIER_FETCH_HOST_CONCURRENCY` | Per-host concurrency control. |
| `CITATION_VERIFIER_FETCH_HOST_MIN_INTERVAL` | Minimum interval between requests to one host. |
| `CITATION_VERIFIER_USER_AGENT` | Explicit outbound User-Agent. |
| `CITATION_VERIFIER_ACCEPT_LANGUAGE` | HTTP language preference. |
| `CITATION_VERIFIER_OA_ALTERNATES` | Enable or disable alternate open-access candidates. |
| `CITATION_VERIFIER_WAYBACK` / `CITATION_VERIFIER_PERMA` | Archival acquisition paths; enabled by default and disabled explicitly with `0`, `false`, `no`, or `off`. |

Optional resolver/content credentials include `CORE_API_KEY`, `ELSEVIER_API_KEY`, `TDM_API_TOKEN`, `SPRINGER_NATURE_META_API_KEY`, `SPRINGER_NATURE_OPEN_ACCESS_API_KEY`, `OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `LENS_API_KEY`, `NCBI_API_KEY` (or the supported `ENTREZ_API_KEY` alias), `COURTLISTENER_API_TOKEN`, and `GOOGLE_BOOKS_API_KEY`. An NCBI key is attached only to E-utilities requests and raises their configured shared rate from 3/s to 10/s. Wiley and Springer Nature calls require a DOI or official host routed by the versioned `core/resolve/providers/publisher_routes.json` catalog; routing selects an adapter but never proves document identity. `BIBLIO_GLUTTON_URL` instead names an optional local/self-hosted metadata accelerator; it is not a credential and has no public-demo fallback. Its output remains subject to normal identity validation before remote Crossref search can be skipped. Their absence disables or rate-limits the corresponding path; it does not authorize fabricated metadata.

The exact provider registry and rates are controlled by `core/resolve/providers.json` and `core/resolve/provider_config.py`.

## OCR controls

| Variable | Behavior |
|---|---|
| `CITATION_VERIFIER_OCR_LANG` | OCR language set; default `eng`. |
| `CITATION_VERIFIER_OCR_AUTO` | Automatic OCR for low-text PDFs; enabled by default. |
| `CITATION_VERIFIER_OCR_AUTO_MAX_PAGES` | Maximum page count for automatic OCR. |
| `CITATION_VERIFIER_OCR_WORKERS` | Process-wide OCR concurrency; default `1`. Increase it only after validating concurrent inference with the selected backend. |

OCRmyPDF with Tesseract is recommended where platform packages are available.
For a pure-pip installation across Windows, macOS, and Linux, install
`requirements-ocr.txt`; RapidOCR with PDFium then remains the portable fallback.
Backend selection is automatic and deterministic: OCRmyPDF, Tesseract with
Poppler, then RapidOCR with PDFium.

Manuscript OCR and cited-source OCR have different policies. A scanned manuscript may be OCRed during Parse; an unreadable cited source is queued and remains traceable. Precomputed text for an associated queued scan is submitted with `tasks answer-fetch --ocr-text-file`, which binds it to the pending PDF before storing it with OCR provenance.

## Signing and integrity

| Variable | Purpose |
|---|---|
| `CITATION_VERIFIER_SIGNING_KEY_FILE` | Path to the external HMAC signing key. Preferred deployment form. |
| `CITATION_VERIFIER_SIGNING_KEY` | Inline key; less isolated and not recommended for an agent-visible shell. |
| `CITATION_VERIFIER_STATE_DIR` | Runtime state shared with integrity/LLM infrastructure. |
| `CITATION_VERIFIER_INTEGRITY_SOCKET` | Integrity-authority connection where configured. |

The signing key should exist only in the hook or CI environment, not in an agent-readable `.env`. Without external key isolation, a SHA-256 content seal still detects accidental changes, but it does not prove approval by an external authority. Follow [Generate the report-signing key](10-keys-and-credentials.md#generate-the-report-signing-key) for fresh, existing-install, and protected deployment workflows.

See `DEPLOYMENT.md` and `docs/deployment/` for the signing and hook threat model.

## Diagnostics

- `CITATION_VERIFIER_DEBUG_RUN=1` enables debug event capture.
- `CITATION_VERIFIER_PERF=1` persists phase timing in `run.sqlite` and prints a timing table.
- `CITATION_VERIFIER_HTTP_MEMO=1` deduplicates identical read-only GET requests within one run.
- `CITATION_VERIFIER_LLM_TIMEOUT` and `CITATION_VERIFIER_LLM_TOOLS_TIMEOUT` bound transport calls.

Diagnostic integrity overrides require an explicit reason and label the run. They are not a production configuration.

---

Previous: [CLI reference](02-cli-reference.md) · Next: [Pipeline](04-pipeline.md).
