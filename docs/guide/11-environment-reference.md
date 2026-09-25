# 11 — Environment reference

This chapter explains every assignment label in the current `.env.example`. It distinguishes the value shown in the template from the default applied when a variable is absent. That distinction matters: copying the template sets `CITATION_VERIFIER_HTTP_PROFILE=plain`, while the runtime default is `browser_like` when the variable is unset.

For instructions on obtaining credentials, see [Keys and credentials](10-keys-and-credentials.md). For higher-level policy choices and advanced variables that are recognized by the runtime but are not part of the public template, see [Configuration](03-configuration.md).

## How the file is loaded

Copy the template, then edit the local copy:

```bash
cp .env.example .env
```

The normal driver loads the first applicable `.env` or `.env.local` without overwriting variables already present in the process environment. Practical precedence is:

1. an explicit CLI option, for settings that expose one;
2. a value already exported in the process environment;
3. the value loaded from `.env` or `.env.local`;
4. the runtime default, if the setting has one.

An empty assignment such as `CORE_API_KEY=` means “unset” unless the table says otherwise. Do not add quotes around ordinary values merely to make them look like strings. Comma-separated values are trimmed, must be non-empty and unique where the Verify policy consumes them, and retain their order.

The “Secret?” column describes the value itself. A model name, URL, email address, command, or filesystem path may still be sensitive operational metadata even when it is not a cryptographic secret.

## 1. Core run settings

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `CITATION_VERIFIER_ACCURACY` | Evidence/retrieval regime: `maximum`, `maximum_fallback`, `standard`, `abstract`, or `standard_web`. `standard_web` is a retained alias of `standard`. | `standard`. Optional. | No |
| `CITATION_VERIFIER_MAILTO` | Contact email used for polite-pool identity and rate-limit etiquette with bibliographic services such as Crossref, Europe PMC, and Unpaywall. It is sent to those services but is not written into the final report. | Empty. Optional, although some services may throttle keyless/contactless traffic more aggressively. | No; personal data |
| `CITATION_VERIFIER_OCR_LANG` | OCR language code or `+`-joined Tesseract language set, for example `eng` or `eng+ita`. The selected OCR backend must have those language assets installed. | `eng`. Optional. | No |
| `CITATION_VERIFIER_OCR_AUTO` | Enables automatic OCR when a cited PDF has too little extracted text. `1`, `true`, `yes`, and `on` enable it; any other explicit value disables it. | Enabled. The template pins `1`. | No |
| `CITATION_VERIFIER_OCR_AUTO_MAX_PAGES` | Maximum page count eligible for automatic OCR. It is parsed as an integer, clamped to a minimum of `1`, and falls back to the default when invalid. This limit does not restrict an explicit/manual OCR task. | `50`. | No |
| `CITATION_VERIFIER_OCR_WORKERS` | Process-wide OCR worker count. It is parsed as an integer, clamped to a minimum of `1`, and falls back to the default when invalid. The serial default avoids backend-dependent concurrent-inference failures; raise it only after validating the selected backend. | `1`. | No |
| `CITATION_VERIFIER_VERIFY_TABLE_CITATIONS` | Includes citations found only in tables as ordinary claims. `1`, `true`, `yes`, and `on` enable it; every other value disables it. | Disabled. The template pins `0`. | No |

See [Configuration — Accuracy regimes](03-configuration.md#accuracy-regimes) for the full evidence semantics of each accuracy value.

## 2. Final Verify runtime

Verify is intentionally fail-closed. At least the backend list, Jury2 policy, a usable model list, and any credentials required by the selected backends must resolve before dispatch.

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `CITATION_VERIFIER_VERIFY_BACKENDS` | Ordered, unique CSV of registered backends: `anthropic`, `openai`, `gemini`, `openrouter`, `ollama`, `freetoken`, `host`, `claude_cli`, `codex_cli`, `gemini_cli`, `openai_compatible`, `glm`, `mistral`, `opencode`, or `typesafe`. The order participates in the frozen provider policy. | No default. Required for Verify; empty or unknown values fail configuration. | No |
| `CITATION_VERIFIER_VERIFY_JURY2_LEVEL` | Second-judge policy: `off`, `low`, `medium`, or `high`. `off` disables Jury2; the other levels progressively tighten what can be accepted after Jury2 disagreement or retry exhaustion. | No default. Required even when the chosen value is `off`. | No |
| `CITATION_VERIFIER_MODEL` | Ordered, unique CSV of model identifiers used by backends that have no provider-specific model value. It is also the fallback for `GEMINI_MODEL`, `OPENAI_MODEL`, `FREETOKEN_MODEL`, `ZHIPUAI_MODEL`, `MISTRAL_MODEL`, and `OPENCODE_MODEL`. | No default. A non-empty model list is required for every selected backend that does not resolve a provider-specific list. The current policy also requires it for CLI and `host` backends. | No |
| `CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT` | Positive integer cap across all Jury1/Jury2 provider calls. This is aggregate concurrency, not a per-provider worker count. | `4`. | No |
| `CITATION_VERIFIER_VERIFY_MAX_TOKENS` | Positive integer output-token budget per call, or the literal `none` to omit the client-side limit. `none` is valid only when every dispatched backend supports an omitted limit. | `4000`. | No |
| `CITATION_VERIFIER_REASONING` | Provider reasoning mode: `auto`, `on`, or `off`. Backend support differs; the transport applies only supported controls. | `auto`. | No |
| `CITATION_VERIFIER_REASONING_EFFORT` | Requested effort: `low`, `medium`, `high`, `max`, or `xhigh`. It is meaningful only for a backend that exposes a compatible reasoning control. | `medium`. | No |
| `CITATION_VERIFIER_VERIFY_MAX_CANDIDATE_CYCLES` | Positive integer bound on provider/credential/model candidate cycles for a claim-evidence pair. | `5`. | No |
| `CITATION_VERIFIER_VERIFY_JURY1_MAX_TECHNICAL_ATTEMPTS` | Positive integer cap on technical Jury1 transport attempts. Semantic disagreement is not silently converted into a transport retry. | `3`. | No |
| `CITATION_VERIFIER_VERIFY_JURY2_MAX_TECHNICAL_ATTEMPTS` | Positive integer cap on technical Jury2 transport attempts. | `3`. | No |
| `CITATION_VERIFIER_VERIFY_JURY1_ONLY` | Optional CSV of exact configured `provider:model` selectors. A listed lane may serve Jury1 but is excluded from Jury2. | Empty; all configured lanes are eligible unless restricted. Unknown selectors, duplicates, or conflicts fail configuration. | No |
| `CITATION_VERIFIER_VERIFY_JURY2_ONLY` | Optional CSV of exact configured `provider:model` selectors. A listed lane may serve Jury2 but is excluded from Jury1. | Empty; all configured lanes are eligible unless restricted. It must not overlap `CITATION_VERIFIER_VERIFY_JURY1_ONLY`. | No |

A TypeSafe lane eligible for Jury1 requires
`CITATION_VERIFIER_VERIFY_CONTEXT_MODE=extractive_rag` and a positive
`CITATION_VERIFIER_MAX_SOURCE_CHARS`. Otherwise Verify stops before dispatch
with those settings in the console message. A TypeSafe lane restricted through
`CITATION_VERIFIER_VERIFY_JURY2_ONLY` does not impose that Jury1 requirement.
TypeSafe Jury1 remains configurable, including its confidence floor, but is not
currently recommended: the controlled Attention canary at the default `0.8`
ended 57 of 58 pairs `jury1_provider_uncertain`. When another Jury1 lane is
configured, prefer restricting Jev to Jury2, for example
`CITATION_VERIFIER_VERIFY_JURY2_ONLY=typesafe:jev-1.13.0`. Do not lower the
floor solely to force verdict coverage; calibrate an override on a documented
representative labeled set.

## 3. LLM provider credentials, endpoints, and models

API-key variables consumed by the declarative Verify policy may contain an ordered, unique CSV of credentials. A single credential is easier to operate and audit. Raw keys are held in memory; persisted policy state uses credential aliases and fingerprints rather than the secret value.

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `OPENAI_API_KEY` | Credential for the `openai` backend. It is also reused as the credential field for `openai_compatible`, in which case it must be issued by the operator of `OPENAI_BASE_URL`. | Empty. Required when either `openai` or `openai_compatible` is selected. | Yes |
| `ANTHROPIC_API_KEY` | Primary credential for the `anthropic` backend. | Empty. Required by the current declarative Verify policy when `anthropic` is selected. | Yes |
| `ANTHROPIC_AUTH_TOKEN` | Fallback token read by the Anthropic HTTP transport after `ANTHROPIC_API_KEY`; useful for some compatible gateways. | Empty. It does **not** satisfy the current declarative Verify policy by itself, which identifies the lane through `ANTHROPIC_API_KEY`. | Yes |
| `ANTHROPIC_BASE_URL` | Base URL for an Anthropic-compatible endpoint. Supply the API base, without the final `/messages`; the transport appends it. | Empty selects the official Anthropic Messages endpoint. | No; endpoint metadata |
| `GEMINI_API_KEY` | Credential for the direct `gemini` backend. | Empty. Required when `gemini` is selected. | Yes |
| `GEMINI_MODEL` | Ordered, unique CSV of Gemini model identifiers. | Empty falls back to `CITATION_VERIFIER_MODEL`. | No |
| `OPENROUTER_API_KEY` | Credential for the `openrouter` backend. | Empty. Required when `openrouter` is selected. | Yes |
| `OLLAMA_API_KEY` | Ollama Cloud credential. The current declarative policy also uses this field to identify an `ollama` lane for a local server. | Empty. Currently required whenever `ollama` is selected, even if `OLLAMA_HOST` names an unauthenticated local endpoint. Use a dedicated local-only placeholder for such a server. | Yes for cloud; local placeholder is not a real secret |
| `OLLAMA_HOST` | Ollama server URL or host. Include `http://` or `https://` to make the transport explicit. | With an API key and no host, the transport selects `https://ollama.com`. Without a key it would select `http://127.0.0.1:11434`, although the current Verify policy rejects the missing key first. | No; endpoint metadata |
| `FREETOKEN_HOST` | Server root for the credentialless local `freetoken` backend. Supply the root without `/v1`; the transport appends `/v1/chat/completions`. A missing URL scheme is interpreted as `http://`. | Empty selects `http://127.0.0.1:1919`. The external server must already be running. | No; local endpoint metadata |
| `FREETOKEN_MODEL` | Ordered, unique CSV of model identifiers served by FreeToken. | Empty falls back to `CITATION_VERIFIER_MODEL`; if both are empty, configuration fails before dispatch. Callimachus does not choose a model from `/v1/models`. | No |
| `OPENAI_BASE_URL` | Endpoint for `openai_compatible`. A non-official value also prevents the direct `openai` backend from claiming the same configuration. | Empty or an official OpenAI URL is compatible with the direct `openai` backend. Set an explicit third-party base URL for `openai_compatible`. | No; endpoint metadata |
| `OPENAI_MODEL` | Ordered, unique CSV of model identifiers specifically for `openai_compatible`. It is not the direct `openai` model variable. | Empty falls back to `CITATION_VERIFIER_MODEL`. | No |
| `ZHIPUAI_API_KEY` | Credential for the `glm`/ZhipuAI backend. | Empty. Required when `glm` is selected. | Yes |
| `ZHIPUAI_MODEL` | Ordered, unique CSV of GLM model identifiers. | Empty falls back to `CITATION_VERIFIER_MODEL`. | No |
| `MISTRAL_API_KEY` | Credential for the direct `mistral` backend. | Empty. Required when `mistral` is selected. | Yes |
| `MISTRAL_MODEL` | Ordered, unique CSV of Mistral model identifiers. | Empty falls back to `CITATION_VERIFIER_MODEL`. | No |
| `OPENCODE_API_KEY` | Credential for the OpenCode Zen `opencode` backend. | Empty. Required when `opencode` is selected. | Yes |
| `OPENCODE_MODEL` | Ordered, unique CSV of OpenCode model identifiers. | Empty falls back to `CITATION_VERIFIER_MODEL`. | No |
| `TYPESAFE_API_KEY` | Credential for the TypeSafe SystemOne backend. | Empty. Required when `typesafe` is selected. | Yes |
| `TYPESAFE_MODEL` | Pinned Jev model identifier in versioned form, for example `jev-1.13.0`. | Required when `typesafe` is selected; unversioned identifiers fail configuration. | No |
| `TYPESAFE_MIN_CONFIDENCE` | Jev auto-action floor in `(0,1]`. A lower structurally valid Jury2 Choice answer is persisted but cannot issue a verdict: it records no yes/no vote and requeues Jury1 for another bounded candidate cycle. For Jury1 Noul evidence, the same floor applies to each span's `max(p, 1-p)` certainty. It is not a probability that the citation is correct. | `0.8` in the template; required when `typesafe` is selected. The value and policy ID are frozen into the run. Override only from a documented labeled-set calibration. | No |
| `CITATION_VERIFIER_LLM_HOST_COMMAND` | Shell-style command for the `host` bridge. The bridge sends a JSON request on standard input and expects its response on standard output. Do not embed a long-lived secret in the command text. | Empty. Required when `host` is selected. The current policy also requires `CITATION_VERIFIER_MODEL`. | Potentially sensitive |
| `CITATION_VERIFIER_OPENROUTER_SITE_URL` | Optional application/site URL sent as OpenRouter attribution metadata. | Empty; no attribution URL header. | No |
| `CITATION_VERIFIER_OPENROUTER_APP_NAME` | Optional application title sent as OpenRouter attribution metadata. | Empty; no application-title header. | No |

The `freetoken` backend sends no API-key authorization header. Callimachus does
not install or start the external server and does not query `/v1/models`
automatically; use the [FreeToken setup instructions](10-keys-and-credentials.md#run-a-local-freetoken-server-without-an-api-key)
to start it and select a model explicitly.

For account creation links and minimal examples, see [Create an LLM provider API key](10-keys-and-credentials.md#create-an-llm-provider-api-key).

## 4. Final Verify tuning

These values are validated and frozen into the run's execution policy before provider dispatch. Changing the environment later does not rewrite the already-frozen policy of an existing run.

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `CITATION_VERIFIER_VERIFY_CONTEXT_MODE` | Context selection: `auto`, `full_text`, or `extractive_rag`. `full_text` admits the available source text directly; `extractive_rag` performs deterministic in-document selection; `auto` depends on the profile. | `auto`. | No |
| `CITATION_VERIFIER_CONTEXT_PROFILE` | Context profile: `large`, `medium`, or `small`. In `auto`, `large` uses full context while `medium` and `small` require a bounded extractive-RAG configuration. | `large`. | No |
| `CITATION_VERIFIER_MAX_SOURCE_CHARS` | Positive integer character budget for admitted Verify context. Surrounding whitespace, zero, negatives, and non-integers are invalid. | Empty is allowed for `full_text` and `auto` + `large`. Required for `extractive_rag` and for `auto` + `medium`/`small`. | No |
| `CITATION_VERIFIER_VERIFY_PACING_INTERVAL_MS` | Non-negative integer delay, in milliseconds, between aggregate dispatch starts. | `0`, meaning no configured delay. | No |
| `CITATION_VERIFIER_VERIFY_PACING_BY_MODEL_MS` | Optional CSV mapping in the exact form `provider:model=milliseconds`. Selectors must name configured lanes; values are non-negative integers. | Empty; no per-model override. | No |
| `CITATION_VERIFIER_VERIFY_SELECTION_SEED` | Non-empty explicit seed for deterministic provider/credential/model selection. It becomes part of the frozen policy identity. | Empty derives a seed from the run ID, Verify contract, and policy material. | No |
| `CITATION_VERIFIER_VERIFY_COOLDOWN_SECONDS` | Positive integer baseline cooldown after a rate-limit response. | `5`. | No |
| `CITATION_VERIFIER_VERIFY_COOLDOWN_OVERRIDES` | Optional CSV mapping `provider:model=seconds`. Selectors must exist and seconds must be positive integers. | Empty; every lane uses the baseline. | No |
| `CITATION_VERIFIER_VERIFY_COOLDOWN_MULTIPLIER` | Positive integer multiplier used when extending local cooldown after repeated throttling. | `2`. | No |
| `CITATION_VERIFIER_VERIFY_COOLDOWN_MAX_SECONDS` | Positive integer ceiling for locally calculated cooldown. An authoritative provider `Retry-After` remains a lower bound and is not truncated to this local ceiling. | `300`. | No |
| `CITATION_VERIFIER_VERIFY_COOLDOWN_STABLE_SUCCESSES` | Positive integer number of successful calls required for cooldown state to stabilize. | `2`. | No |

## 5. Content and resolver API keys

These credentials are optional for the overall pipeline. Missing credentials disable or reduce only the matching provider path; they never authorize Callimachus to turn missing evidence into a successful result.

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `GOOGLE_BOOKS_API_KEY` | Google Cloud API key for Books API requests. Keyless volume metadata requests can still work, but the key enables identified quota and the opt-in preview-snippet tier used by Callimachus. | Empty. Optional; the key-gated preview tier is unavailable. | Yes |
| `CORE_API_KEY` | CORE API credential used for open-access discovery and API full-text retrieval. | Empty. The CORE credentialed provider path reports itself unavailable and other resolvers continue. | Yes |
| `ZENODO_ACCESS_TOKEN` | Optional Zenodo access token used for the conservative publication-copy fallback after version-of-record and open-access lookup. | Empty disables authenticated Zenodo retrieval. | Yes |
| `ELSEVIER_API_KEY` | Elsevier Developer API key for DOI/PII Article API retrieval. Actual full-text entitlement can still depend on the account, institution, use case, and subscription. | Empty. Elsevier API retrieval is unavailable. | Yes |
| `NCBI_API_KEY` | NCBI E-utilities API key. `ENTREZ_API_KEY` is a supported alias when `NCBI_API_KEY` is blank. | Empty. E-utilities use the configured 3/s shared rate; a nonblank key raises it to 10/s and is attached only to E-utilities URLs. | Yes |
| `ENTREZ_API_KEY` | Supported alias for the NCBI E-utilities key. `NCBI_API_KEY` takes precedence when both are nonblank. | Empty. Used only when `NCBI_API_KEY` is blank. | Yes |
| `OPENALEX_API_KEY` | Optional OpenAlex API key used by authenticated Resolve and Fetch requests. | Empty. OpenAlex uses its keyless path where supported. | Yes |
| `SEMANTIC_SCHOLAR_API_KEY` | Optional Semantic Scholar Academic Graph API key used in authenticated resolver requests. | Empty. The provider can use its unauthenticated service path where available, subject to shared rate limits. | Yes |
| `LENS_API_KEY` | Lens Scholarly API access token used by the Lens resolver. | Empty. Lens title-search fallback is unavailable. | Yes |
| `TDM_API_TOKEN` | Wiley text-and-data-mining UUID token, sent only in the Wiley API request header for a catalog-routed DOI. | Empty disables the Wiley TDM candidate. Entitlement can still require an authorized public IP. | Yes |
| `SPRINGER_NATURE_META_API_KEY` | Springer Nature Meta API key for DOI-exact metadata and abstract enrichment. | Empty disables the Meta adapter. Its result can fill missing fields but cannot replace the selected Resolve identity. | Yes |
| `SPRINGER_NATURE_OPEN_ACCESS_API_KEY` | Springer Nature Open Access API key for DOI-exact JATS full text. | Empty disables the Open Access adapter. A response must contain the exact DOI and a substantive body before normal Fetch validation. | Yes |
| `BIBLIO_GLUTTON_URL` | Base URL of an optional local/self-hosted biblio-glutton service, for example `http://localhost:8080`. It is a local metadata accelerator only; Callimachus still validates identity and does not use a public-demo fallback. | Empty disables the adapter. | No; local endpoint metadata |
| `COURTLISTENER_API_TOKEN` | CourtListener HTTP token used to resolve US case-law reporter citations. Supply only the token value; the adapter constructs the authentication header. | Empty. CourtListener case-law resolution is skipped. | Yes |
| `TAVILY_API_KEY` | Tavily credential used only by the Tavily deterministic web-search backend. | Empty disables that backend. | Yes |
| `MOJEEK_API_KEY` | Mojeek Search API credential used only by the Mojeek deterministic web-search backend. | Empty disables that backend. | Yes |

Creation instructions and official registration links are in [Create optional resolver and content keys](10-keys-and-credentials.md#create-optional-resolver-and-content-keys).

## 6. HTTP and Fetch settings

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `CITATION_VERIFIER_USER_AGENT` | Complete outbound User-Agent override. Use a truthful application identifier and contact channel; do not impersonate another product. | Empty selects the built-in browser-like User-Agent for publisher-facing requests and `CitationVerifier/1.0` for API-style requests. | No |
| `CITATION_VERIFIER_HTTP_PROFILE` | HTTP request profile: `plain` or `browser_like`. An unknown value falls back to `browser_like`. | Runtime default when unset: `browser_like`. The checked-in template explicitly sets `plain`, so copying it pins the plain profile. | No |
| `CITATION_VERIFIER_FETCH_CHALLENGE_MODE` | Publisher challenge handling. Current canonical values are `off`, `queue`, `interactive_challenge`, `interactive_closed`, `interactive_fallback`, and `interactive_fallback_closed`. Current aliases include `browser_challenge`, `interactive`, `playwright`, `interactive_browser`, `interactive_access`, `interactive_browser_closed`, `interactive_recovery`, `browser_fallback`, `interactive_recovery_closed`, and `browser_fallback_closed`. Interactive modes require their browser dependencies and operator workflow. | `off`. Unknown or unloadable selections resolve to `off`. | No |
| `CITATION_VERIFIER_GUIDED_SEARCH_URL` | Search-engine URL used by both Guided Fetch browser controls only when no concrete resolved or parsed source URL exists. It must be a credential-free HTTP(S) URL with a hostname and contain exactly the `{query}` placeholder; the query value is percent-encoded. Unknown placeholders, conversions, format specifications, or invalid output fail Guided Fetch launch/navigation visibly. | `https://www.google.com/search?q={query}`. | No |
| `CITATION_VERIFIER_GUIDED_SEARCH_QUERY` | Template for a non-DOI Guided Fetch search query. It must contain one or more of `{raw_entry}`, `{title}`, `{doi}`, `{pmid}`, `{isbn}`, `{year}`, `{ay_surname}`, or `{source_type}`; whitespace is normalized. A recovered Resolve DOI or parsed DOI always searches for that DOI alone instead. | `{raw_entry}`. | No |
| `CITATION_VERIFIER_FETCH_PROVIDERS` | `auto`, blank, or a comma-separated subset of known Fetch provider names. A non-`auto` list filters the configured Fetch provider set; it does not create arbitrary modules. | Blank or `auto` uses the provider configuration's default enabled set. | No |
| `CITATION_VERIFIER_FETCH_PROVIDER_ORDER` | Optional comma-separated precedence list for configured Fetch providers. It reorders known providers within the selected set; provider capability, availability, and per-host policy still apply. | Empty uses the order from `core/resolve/providers.json` and the built-in provider configuration. | No |
| `CITATION_VERIFIER_OA_ALTERNATES` | Controls the second acquisition round through alternate open-access candidates. Only `0`, `false`, or `no` disables it; an empty or any other value enables it. | Enabled. The template pins `1`. | No |
| `CITATION_VERIFIER_WAYBACK` | Enables acquisition through the Internet Archive's Wayback Machine. `0`, `false`, `no`, or `off` disables it; an empty or any other value enables it. | Enabled. The template pins `1`. | No |
| `CITATION_VERIFIER_PERMA` | Enables acquisition through Perma.cc. `0`, `false`, `no`, or `off` disables it; an empty or any other value enables it. | Enabled. The template pins `1`. | No |
| `CITATION_VERIFIER_FETCH_WORKERS` | Integer source-fetch worker count. Valid integers are clamped to `1..16`; invalid or empty values use the default. This does not bypass per-host limits. | `4`. | No |
| `CITATION_VERIFIER_RESOLVE_WORKERS` | Integer reference-resolution worker override. Valid integers are clamped to `1..32`. With no override, the driver starts from Fetch concurrency and can scale with bibliography size, normally up to 16. | Derived dynamically; `4` for a small run under default settings. | No |
| `CITATION_VERIFIER_FETCH_PDF_TIMEOUT` | Per-PDF download timeout in seconds. Integer values are clamped to `5..300`; invalid or empty values use the default. | `45` seconds. | No |
| `CITATION_VERIFIER_FETCH_BUDGET_S` | Per-reference Fetch wall-clock budget, parsed as seconds. A positive number sets the budget; `0` means unlimited. Non-positive numbers also resolve to unlimited, but `0` is the documented form. Invalid text falls back to the default. | `120` seconds when empty or invalid. | No |

Fetch concurrency never weakens provider throttles, host cooldowns, provenance capture, or retry classification.

## 7. Report output and signing

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `CITATION_VERIFIER_REPORT_HTML` | Controls automatic generation of the sealed, self-contained `report.html` after the canonical report gate passes. Missing or empty values and `1`, `true`, `yes`, or `on` enable it; `0`, `false`, `no`, or `off` disable it. Other values fail the Report phase explicitly. Disabling generation does not delete an existing companion. | Enabled. The template pins `1`. | No |
| `CITATION_VERIFIER_SIGNING_KEY_FILE` | Filesystem path to a non-empty HMAC key file. The file form has precedence over the inline `CITATION_VERIFIER_SIGNING_KEY`. The trusted process must be able to read it; the constrained process must not. | Empty means no external key. Callimachus falls back to a plain SHA-256 content seal unless the gate requires HMAC, in which case verification fails closed. | The path is sensitive metadata; the file contents are secret |

`CITATION_VERIFIER_SIGNING_KEY_FILE` appears in `.env.example` so its role is discoverable, but the value must not be placed in an agent-readable project `.env`. Generate and deploy the key by following [Generate the report-signing key](10-keys-and-credentials.md#generate-the-report-signing-key).

## 8. Diagnostics

All three diagnostic toggles recognize `1`, `true`, `yes`, or `on`, case-insensitively, as enabled. Empty values, `0`, and other strings are disabled.

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `CITATION_VERIFIER_DEBUG_RUN` | Forces debug event capture and records the environment-trigger label. Debugging does not relax integrity checks or verdict semantics. | Disabled. | No |
| `CITATION_VERIFIER_PERF` | Enables the thread-safe timing collector, persists its typed summary in `run.sqlite`, and prints the phase timing table at run completion. | Disabled; hot-path calls remain near-no-op. | No |
| `CITATION_VERIFIER_HTTP_MEMO` | Enables in-process memoization for identical read-only HTTP GET requests during one run. It is not a persistent cross-run cache and does not memoize mutations. | Disabled. | No |

## 9. Bibliographic registries

These local catalogs make journal identity and resolver coverage explicit. They
contain no credentials and cannot turn a failed, partial, or stale request into
negative evidence.

| Variable | Meaning and accepted values | Effective default or requirement | Secret? |
|---|---|---|---|
| `CALLIMACHUS_JOURNAL_AUTHORITY_DB` | Path to the atomically replaceable local journal-authority SQLite catalog used to map registered journal names and abbreviations to ISSNs. An invalid or ambiguous catalog fails closed. | Empty selects `storage/journal-authority/nlm-journals.sqlite` when it exists. | No; local path |
| `CALLIMACHUS_JOURNAL_AUTHORITY_AUTO_UPDATE` | Boolean. Before Resolve, create or refresh an NLM-backed catalog when its snapshot reaches the configured TTL. A non-NLM catalog is never overwritten. | `0` (manual updates only). | No |
| `CALLIMACHUS_JOURNAL_AUTHORITY_TTL_DAYS` | Positive integer lifetime for an NLM journal-authority snapshot before an enabled automatic refresh is due. | `7`. | No |
| `CALLIMACHUS_RESOLVER_COVERAGE_DB` | Path to the append-only local SQLite catalog of dated, resolver-specific journal coverage observations. Each run copies the evidence it used into its own `run.sqlite`. | Empty selects `storage/resolver-coverage.sqlite`. | No; local path |
| `CALLIMACHUS_RESOLVER_COVERAGE_TTL_DAYS` | Positive integer lifetime, in days, for resolver journal-coverage observations. At expiry, automatic Resolve refreshes the observation before it can strengthen suspicion. | `30`. | No |

Previous: [Keys and credentials](10-keys-and-credentials.md) · Index: [Complete guide](README.md).
