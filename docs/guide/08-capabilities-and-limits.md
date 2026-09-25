# 08 — Capabilities and Limits

## Capability summary

Callimachus can:

- parse one manuscript into claims, bibliography entries, and citation edges;
- resolve bibliographic identity and existence across scholarly, book, legal, clinical-trial, repository, and institutional-report sources;
- fetch and normalize primary-source text through provider and open-access paths;
- accept user-provided sources with identity and provenance controls;
- queue challenge pages and scanned PDFs instead of losing them;
- verify each cited claim-source pair with guarded LLM judgments;
- check citation style independently;
- produce a deterministic, sealed, independently gated report;
- resume from typed persisted state;
- compare LLM terminal behavior on a frozen deterministic fixture.

## Manuscript formats

The active Parse CLI accepts:

| Family | Extensions/examples | Important behavior |
|---|---|---|
| Word | `.docx` | Preserves explicit superscript runs. |
| LaTeX | `.tex` | Handles LaTeX bibliography and table structures. |
| PDF | `.pdf` | Best-effort layout extraction; reads superscript glyph flags when a text layer exists; can OCR eligible scans. |
| Markdown | `.md`, `.markdown` | Supports explicit superscript forms and Markdown tables. |
| Plain text | `.txt` | Works when citation-marker information survived conversion. |
| HTML | `.html`, `.htm`, `.xhtml` | Preserves `<sup>` markers and structural elements when present. |

The extractor registry is authoritative. Disabled extractors remove their extensions from the supported set.

## Citation schemes and styles

Explicit Parse schemes are:

- `numeric`;
- `author-year`;
- `inline-doi`;
- `mla`;
- `auto`, which scores and selects an enabled scheme.

Numeric handling includes bracketed, parenthesized, LaTeX, and preserved superscript markers. Author-year conversion is deterministic but ambiguous markers may require a manual link.

Style checking supports:

- Vancouver;
- APA 7;
- Chicago;
- MLA 9.

Source-type-specific style findings are independent of identity and semantic support.

## Parse quality checks

Parse records and reports:

- claim/citation/reference counts;
- orphan markers and uncited bibliography entries;
- duplicate DOI/PMID identities under different reference numbers;
- bibliography-boundary diagnostics;
- citation coverage;
- table-only citation rows;
- footnote/container provenance;
- extraction and OCR metadata.

Collapsed citation coverage plus missing typographic markers can trigger `CitationMarkersLost`. This is a refusal to infer information destroyed before parsing.

## Resolve and Fetch coverage

The provider registry currently includes adapters for:

- scholarly indexes and metadata: Crossref metadata, OpenAlex, DataCite, Semantic Scholar, Lens, CORE, Europe PMC;
- preprints and venues: arXiv, bioRxiv, SSRN, OpenReview, ACL, ACM, AAAI, NeurIPS, JMLR, PMLR, TAC, CVF;
- open-access/content paths: Unpaywall, Elsevier, repositories, curated copies, scholarly archives;
- books: OpenLibrary/Google Books logic and targeted curated providers;
- clinical trials: ClinicalTrials.gov, CTIS, and ISRCTN;
- legal and public records: CourtListener and the UN Digital Library;
- institutional and grey literature: configured institutional sources and official-report adapters.

Provider availability, ordering, credentials, and rate limits are runtime configuration. A listed adapter is not a guarantee that a remote service is reachable or that a particular work is available.

Resolve can use DOI, PMID, ISBN, URL, title/author/year, exact legal reporter citation, clinical-trial identifiers, and provider-specific identifiers. Weak title search does not by itself justify a high-confidence identity.

Fetch can use direct/provider candidates, open-access locations, repositories, cached corroborated content, user files, browser-challenge answers, and OCR. Every attempt and failure reason is recorded.

## Evidence and verification capabilities

The semantic runtime supports:

- six explicit outcomes: `supports`, `partial`, `contradicts`, `related`, `off_topic`, and `non_decidable`;
- multiple evidence passages for one source;
- exact local grounding for every evidentiary passage;
- optional isolated-passage Jury2 enforcement;
- multiple providers, models, and credentials under a frozen deterministic scheduler;
- bounded technical retries, rate-limit pacing, and cooldown;
- four persisted scopes (`fulltext_complete`, `abstract_only`, `abstract_fallback`, and `preview_snippet`) plus frozen full-text/RAG context modes;
- persistent resume without erasing earlier attempts;
- separate treatment of cross-references and multi-source markers.

## LLM backends

The runtime currently registers:

```text
anthropic
openai
gemini
openrouter
ollama
freetoken
host
claude_cli
codex_cli
gemini_cli
openai_compatible
glm
mistral
opencode
```

Backends are explicit. There is no silent “best available” selection. A backend must satisfy its endpoint, credential/client, and model requirements. The `freetoken` backend is credentialless, but its external local server must be running and its model must be selected explicitly.

## Operational capabilities

- New run, status inspection, typed pause/resume, and fresh-start lineage.
- Manual Parse adjudication without mutating raw Parse evidence.
- Batch ingestion and strong automatic matching of user source folders.
- Targeted Resolve, Fetch, OCR, style, report, and preview tools.
- Post-Fetch freezing and provenance-safe Verify child runs.
- Completion gate with optional strict-crediting and required-HMAC modes.
- Deterministic report presentation that refuses unauthenticated output.
- Cross-run/model benchmarking of terminal concordance and stability.

## Extension points

The project is organized around registries rather than orchestration special cases:

| Extension | Location / contract |
|---|---|
| Manuscript extractor | `core/parse/extractors/` — extensions plus `extract(...)`. |
| Citation scheme | `core/parse/citation_schemes/` — detection and claim/citation emission. |
| Format behavior | `core/parse/format_handlers/` — bibliography, sentence, reference, and table hooks. |
| Resolve/Fetch provider | `core/resolve/providers/` plus `core/resolve/providers.json`. |
| Fetch challenge mode | `core/fetch/fallbacks/fetch_modes/`. |
| Web researcher | `core/verify/researchers/`. |
| LLM backend | `core/verify/backends/` registry. |
| Citation style | `core/style/`. |

Adding a module is not sufficient unless it satisfies the owning registry contract and tests. The pipeline should not need provider- or format-specific branches.

## Explicit limits and non-capabilities

### One manuscript per run

A run represents one manuscript and one immutable manuscript hash. A different manuscript requires a new run.

### PDF and OCR are best effort

PDF layout extraction can fail on columns, scans, or damaged text layers. OCR can recover visible glyphs but cannot restore semantic/typographic information that a prior conversion destroyed. OCR output still passes normal Parse/source-quality checks.

### No paywall or access-control bypass

Callimachus can discover candidate links, use configured APIs, queue browser challenges, and accept a user-provided lawful copy. It does not bypass authentication, paywalls, CAPTCHAs, or access controls.

### No invented evidence or identity

An unresolved citation remains unresolved. A weak title match stays weak. User/agent input is validated and provenance-bearing; it cannot deterministically override an identity mismatch without an explicit, traceable review path.

### Abstract and web evidence are not full text

Every result retains its scope. Google Books preview text is limited source evidence recorded under `preview_snippet`. Generic third-party web pages are not citation evidence; `standard_web` is retained as an alias of `standard`.

### Grounding is not semantic truth

Grounding proves that a quoted passage exists in the admitted source. Jury2 reduces context-borrowing risk, but neither stage creates a gold label. Human review remains appropriate for high-stakes or contested claims.

### Table rows are excluded by default

A table label is often not a grammatical assertion. Table-only citations count toward coverage but are not verified unless `--verify-table-citations` is selected.

### Completion is not positive support

A run can complete with contradictions, unsupported claims, no-text sources, or uncertainty, provided those states are honestly and completely recorded. Use `--strict-crediting` for a stronger operational requirement, not as a semantic-accuracy metric.

### Benchmarking is not accuracy measurement

Without a labeled corpus, benchmark output measures within-model stability, between-model agreement, outcome mix, and protocol/grounding discipline. It must not be described as accuracy.

### Remote capability is environment-dependent

API keys, client logins, network policy, rate limits, provider changes, and source availability affect runtime success. Deterministic failure recording is guaranteed; remote availability is not.

### Report signatures depend on key isolation

An HMAC is meaningful only when the constrained agent cannot read the signing key. A key exposed in the same shell proves integrity of bytes, not independent authorization.

---

Previous: [Artifacts and provenance](07-artifacts-and-provenance.md) · Next: [Troubleshooting](09-troubleshooting.md).
