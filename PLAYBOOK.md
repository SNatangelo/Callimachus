# Citation Verifier — PLAYBOOK (orchestration procedure)

This document is the **portable heart** of the skill. Claude Code, Codex and
Antigravity read it identically; only the adapter that points to it changes. The only
non-deterministic step is the axis-3 **verification jury**, which the driver runs itself;
everything else is code.

> **Preferred entrypoint: `python run.py`.** It executes this whole procedure for
> you. It runs the verification jury (jury1 → grounding → jury2) itself and only **pauses**
> where a human can supply something code cannot: **fetch** (provide a missing full text)
> and, under `standard_web`, **research** (web hints) — it exposes SQLite-backed tasks you
> answer through `python run.py tasks`, then
> `--resume`. It owns the control flow so the pipeline cannot be skipped, and finishes by
> sealing the report and running the gate `python run.py verify` (exit 0/20). The phases
> below are the reference for what the driver does; you still MUST end with `core.report` +
> `core.verify_run` passing. See SKILL.md.

## Guiding principle (non-negotiable)

The only non-deterministic operation is the axis-3 verification jury; every other step is
a script that produces an inspectable output. Degradations (full text missing, abstract
used, truncation, retry exhausted, source unresolved) are **recorded data**, never
silently absorbed.

Three axes, never merged, for every source:
1. **Does it exist?** → `core/resolve/` (deterministic + WebFetch for the web)
2. **Cited in the correct style?** → `core/style/check.py` (deterministic, by type)
3. **Does it support the claim?** → the claim-evidence jury (`core/verify/claim_evidence/`),
   whose quoted passages are validated by the deterministic **grounding** check
   (`core/verify/claim_evidence/evidence/grounding.py`)

> **What axis 3 really guarantees.** Grounding proves that the cited passages
> **exist** in the source; the **relevance** of the passage to the claim remains a model
> judgment, made inspectable because the report prints claim and passages side by side.
> The tool **reduces** fabricated and unsupported citations; it **does not guarantee
> the absence of relevance errors**.

## Verification reliability tier (axis 3)

Every verdict is produced at a **tier**, recorded in the `scope` field and translated
into `reliability`. A source automatically falls to the highest tier for which text is
available; the user authorizes only *how far down* it can go via the **accuracy grade**
(`--accuracy`, Phase 0): `maximum`→`fulltext` only · `standard`/`abstract`→down to
`abstract` · `standard_web`→down to `web_secondhand` (web search as a LAST resort,
**after** full text and abstract both failed).

| tier (`scope`) | what the jury sees | `reliability` |
|---|---|---|
| `fulltext_complete` / `fulltext_trimmed` | full text of the source | **high** |
| `rag` | **partial** full text: top-k chunks retrieved, for low-context models | medium |
| `abstract_only` | abstract only | medium |
| `preview_snippet` | **fragments** of the source: ~1-sentence Google Books preview snippets, targeted to the claim (opt-in, key-gated) | **low** |
| `web_secondhand` | **not** the source: third-party pages that cite/describe it | **low (indirect)** |

`rag` reads the real source but only partially: a `supports` with a verified passage is
solid, but an `off_topic`/`partial` is weaker (the passage might not be in the
retrieved chunks). Use it when the full text exceeds the model's context, **instead** of
truncating blindly.

`abstract_only`: the report **reinterprets** the outcome deterministically (no model
prompt) using `fulltext_exists` from the resolver. An `off_topic`/`partial` seen only
on the abstract of a source whose **full text exists** (e.g. paywalled, not read) is
**inconclusive**, not a hard negative: the data may be in the unread text. If instead
the **full text does not exist** (conference abstract), the abstract *is* the source and
the outcome is **conclusive**. The model just judges; the code weighs the verdict knowing
*what that text was*.

`preview_snippet` (opt-in, **key-gated** — `core/fetch/fallbacks/preview.py`) targets *books*. Google
Books full-text-searches the claim phrase and returns ~1-sentence snippets of the
genuine source text. A snippet is kept only if (1) it exists at all — a fabricated
passage returns zero hits — and (2) the matched volume **corroborates** as the cited
book (ISBN or title/author token overlap), guarding against the same phrase reused in
other volumes. The kept snippets are stored like any source (`tier=web`,
`origin=googlebooks`) and the ordinary grounding check validates against them. Two hard
limits, both empirically confirmed: snippets are **OCR-noisy** (dropped/shifted
punctuation), so quote **short, distinctive** phrases — long passages spanning a
comma the OCR lost will not match; and it is a one-sentence window, so contradiction
cannot be assessed. Active only when `GOOGLE_BOOKS_API_KEY` is set (the key raises the
quota; it does **not** unlock more text); without it the tier is a silent no-op and the
default pipeline is unchanged. A claim that holds only here can never be ✅ green.

`web_secondhand` is the last tier, opt-in. Grounding there validates passages
against the **third-party web pages** (not against the source), and the report writes
"indirect verification". A claim that holds only at this tier can never be ✅ green.

## The verification jury (the only non-deterministic point), guarded

Axis 3 runs inside a single `verify` phase driven by the claim-evidence runtime
(`core/verify/claim_evidence/`, contract `verify-claim-evidence-v4`). Two LLM judges
bracket a deterministic guard:

| Stage | What it does | Guard / gate |
|---|---|---|
| **jury1** (`contracts/jury1.py`) | judges the exact claim using its manuscript context only for interpretation; `outcome` + verbatim `evidence` come from the selected source context | **grounding** (`evidence/grounding.py`): every quoted passage must exist in the source, else the verdict is rejected and retried |
| **jury2** (`contracts/jury2.py`, opt-in) | re-judges the claim against **only** the extracted evidence — one boolean `evidence_justifies_outcome` | gated by `CITATION_VERIFIER_VERIFY_JURY2_LEVEL` (off/low/medium/high, **required**) |

Multi-source claims are verified independently per (claim, source) pair like any other —
there is **no** separate Interpreter LLM. The final report is a deterministic projection
of the typed run ledger — there is **no** Orchestrator/briefing LLM.

**Retry**: only on protocol/grounding failures (malformed JSON, hallucinated passage).
NEVER on *negative findings* (`not_found`, `contradicts`, `off_topic`): those are findings,
and retrying them to "make them pass" is dishonest. Requests, dispatch attempts,
candidates, and state transitions are stored in typed append-only relations.

## Layout of a run

A run is **DB-native**: `run.sqlite` is the system of record — parse output, resolve
results, the verdict ledger and the task queue all live there, not as JSON files. Only the
`sources/` tree and the final `report.*` are files on disk. (Manifest / ingest-review /
unreadable payloads are rows in the DB, read through the repository, not standalone JSON.)

```
runs/<timestamp>/
  run.sqlite                          # system of record: parse, resolve, ledger, tasks
  parse_debug.md                      # human-readable parse debug view (§11)
  sources/parsed/<n>_<tier>_<origin>.txt  # normalized text of EVERY source — what the
                                      #   jury and grounding read. origin: user | crossref |
                                      #   europepmc | unpaywall | webfetch | websearch |
                                      #   googlebooks | ocr | manual
  sources/provided/<n>_<name>         # raw originals SUPPLIED BY THE USER (audit copy;
                                      #   downloads are NOT kept raw, only their parsed text)
  sources/ocr_queue/<n>_..._.unreadable.pdf  # unreadable scans awaiting opt-in OCR;
                                      #   DELETED once OCR'd into parsed/
  report.md                          # deterministic human-readable report
  report.signature_status.md          # HMAC/seal status (written by the gate)
```

## Procedure

### Phase 0 — setup
- Ask for/receive: manuscript path (`.docx` | `.tex` | `.pdf` | `.md` | `.txt` | `.html`),
  opt. `--window` (default 1), `max_retries` (default 2). **Do NOT ask for the style
  here** — it is auto-detected after parsing (Phase 1, step "style").
- **Accuracy grade (`--accuracy`, ASK at the start).** Present the grades and let
  the user choose; default `standard`. The grade sets the floor tier *and* whether the
  full text is chased (see table). The decisive property: at **`maximum`** the verdict
  **never** degrades to the abstract — a missing full text leaves the source *unverified*,
  it is never silently settled on a weaker text.

  | grade | floor (`min_tier`) | abstract behaviour | web | chases full text |
  |---|---|---|---|---|
  | `maximum` | `fulltext` | **never** → else source unverified | no | required |
  | `maximum_fallback` | `fulltext` | **never when a full text is known to exist**; when full-text *existence is unknown* the abstract is read as a **provisional** basis (flagged, never green) while the gap stays open | no | required |
  | `standard` *(default)* | `abstract` | fallback; a negative on the abstract of a source whose full text exists is **inconclusive** | no | yes |
  | `abstract` | `abstract` | abstract+title **accepted as final** (full text not chased) | no | no |
  | `standard_web` | `web_secondhand` | same as `standard`, **plus** a post-verify web research LAST resort when full text is missing **and** the abstract is missing OR its verdict is inconclusive (`off_topic`/`partial`/unstable). Delegated to the agent's web tools first (autonomous: per-backend web tool), deterministic web search as fallback; low reliability, indirect, additive to any abstract verdict | last resort | yes |

  `maximum_fallback` sits between `maximum` and `standard`: it keeps `maximum`'s refusal
  to settle for the abstract **whenever a fuller text is known to exist** (`fulltext_exists
  == True`), but — instead of leaving a source mute when we cannot even tell whether a
  fuller text exists (`fulltext_exists == "unknown"`) — it verifies the abstract
  **provisionally**. Such a verdict is flagged `provisional` in the SQLite-backed gap summary, the report
  keeps it inconclusive (an abstract-only verdict is never green unless `fulltext_exists ==
  False`), and the source **stays in the gap list** so the user can still provide the full
  text to make it conclusive. `standard_web` is the only grade that authorises
  `web_secondhand`; it must be an explicit choice. It chases full text and abstract
  first, and only falls to a
  **deterministic** web search (`core/verify/websearch.py`, no model tools required, so it works
  under any LLM and in the autonomous runner) when nothing else is reachable.
- **Contact email (mailto).** *After* the manuscript is provided, ask the user for a
  contact email, **explaining why**: Crossref/Europe PMC/Unpaywall grant higher rate
  limits to the "polite pool", so without it more sources come back `unresolved` (429).
  Be honest about where it goes: it **is sent** in the request header to those third-party
  services (that is the point) — it is **not** kept "local" — but it never enters the
  report. The user may **decline and proceed** — record that the run ran without it
  (expect more rate-limiting). Read from `--mailto` / `CITATION_VERIFIER_MAILTO`.
- **Google Books API key (optional, key-gated).** Ask whether the user wants to enable
  the `preview_snippet` tier for books (Phase 2c) — short passages confirmed against
  Google Books preview fragments — and lift the keyless quota on the book existence
  check. Explain it is optional; to create a key, [Google Cloud Console → APIs &
  Services → Credentials, enable the *Books API*](https://console.cloud.google.com/apis/library/books.googleapis.com).
  Read from env `GOOGLE_BOOKS_API_KEY`. The key is used only in requests to Google,
  never written to a run artifact or the report. No key → the tier is a silent no-op.
- **Verify RAG dependency (optional).** Install `pip install -r requirements-rag.txt` only
  when `CITATION_VERIFIER_VERIFY_CONTEXT_MODE=extractive_rag`, or an `auto` medium/small
  profile, can select extractive RAG. Set a positive `CITATION_VERIFIER_MAX_SOURCE_CHARS`.
  Verify checks the exact pinned `bm25s` version and stops before the jury task when it is
  absent; there is no silent context fallback.
- **Offer to persist secrets safely (so the user does not make a mess).** If the user
  hands you an email or an API key and wants to reuse it, **offer to write a local
  `.env`** (and ensure `.gitignore` ignores `.env`/`*.env`, with a committed
  `.env.example` template) so the secret stays out of git. Load it before a run with
  `set -a; . ./.env; set +a`. ⚠️ A `.env` is **per-machine and does not survive** an
  ephemeral cloud session (it is git-ignored, so a fresh clone won't have it): for the
  hosted environment the durable place is the environment's **secret / env-var
  configuration**, injected at every session start. Never commit a real secret.
- The driver creates `runs/<timestamp>/` with `run.sqlite` (the system of record) and the
  `sources/{parsed,provided,ocr_queue}/` tree on demand — there are no `resolve/`/`ledger/`
  JSON folders. (When running a subcommand standalone for inspection, write its `--out`
  wherever you like, e.g. `/tmp`.)

### Phase 1 — parsing (deterministic)
```
python run.py parse --input <file> --debug runs/<ts>/parse_debug.md \
       --window 1 [--citations auto|numeric|author-year]
```
- `--citations auto` (default) detects the scheme: **numeric** (`[n]`/`(n)`/superscript/
  `\cite`) or **author-year** (`(Smith 2021)`, `Smith et al. (2021)`).
- **No verifiable claims (exit code 4).** If the document has **0 in-context citations**
  (i.e. it is a bare bibliography / reference list, not a manuscript with citations in
  context), `parse_manuscript` writes the parse outputs and then **stops with exit 4**.
  CitationVerifier checks whether cited sources *support the statements that cite them*;
  it does not audit bare reference lists (other tools do that). Report this to the user and
  stop — do not run the rest of the pipeline on nothing.
- **Open `parse_debug.md` and check** the number of claims vs number of markers and the
  bibliography cut (§11). If the numbers look wrong, flag it: do not proceed as if nothing
  happened.
- **Author-year**: the debug lists the **ambiguous** citations (same surname+year,
  multiple entries) and **orphan** ones. The ambiguous ones **are not guessed**: present
  them to the user with the claim and all candidate sources; when they choose, link
  deterministically:
  ```
  python run.py authoryear resolve --run runs/<ts> \
         --marker "(Lee 2018)" --ref <chosen-ref_number>
  ```
  Unresolved ambiguous ones remain flagged in the report (sec. 4b), verification suspended.

- **Style (auto-detect, do not assume).** The system recognises the style itself from the
  parsed references; it asks the user only when the guess is weak.
  ```
  python run.py style-detect --run runs/<ts>
  ```
  - `decision="use"` (confidence `high`) → use `suggested`, but **announce** it
    ("detected style: APA 7 — proceeding").
  - `decision="ask"` (confidence `medium`/`low`) → present the `available_styles`
    (read live from the style modules, so new styles appear automatically) with the
    ranked guess on top, and let the user **force** one. Never silently pick a
    low-confidence style: an unverified style is a hidden degradation.
  - The chosen style feeds Phase 3 (`--style`).

### Manual Parse adjudication (opt-in)

Use this only when the deterministic parser reports a footnote with no
recoverable source, multiple sources, or a reference whose identity needs a
human correction. It is an explicit, DB-native review step; do not edit
`run.sqlite` directly.

Start a new run with the review pause enabled:

```bash
python run.py --input <paper> --manual-review
python run.py --input <paper> --manual-review --manual-review-ref-number N
```

The complete Parse/style phase runs first, then pauses immediately before
Resolve with exit code 10, even when there are zero review tasks. List and
inspect pending work with:

```bash
python run.py tasks list --run <run> --slot parse_review --status pending
python run.py tasks show --run <run> --task <task-id>
```

`show` displays the raw target and its `target_sha256`. If a target is selected
during the pause, resume the driver with the selector. It creates the identity
task and, when that reference is the parent of a footnote, also creates the
source-boundary task. Explicit selection is how an operator requests review of
a whole-note source that the deterministic parser otherwise preserves
one-to-one. The driver then pauses again:

```bash
python run.py --run <run> --resume --manual-review-ref-number N
```

If the run has already passed Parse, start a new `--manual-review` run.
Answer every task with its target hash and a reason. For `split-sources`, each
UTF-8 file must contain an exact source substring from the raw note; provide
at least two files and no offsets—the driver derives ordered offsets.

```bash
python run.py tasks answer-review --run <run> --task <task-id> --action no-sources \
  --target-sha256 <sha256> --reason "No source is recoverable from this note"
python run.py tasks answer-review --run <run> --task <task-id> --action split-sources \
  --source-text-file source-1.txt --source-text-file source-2.txt \
  --target-sha256 <sha256> --reason "The note contains two references"
python run.py tasks answer-review --run <run> --task <task-id> --action correct-identity \
  --title "Correct title" --doi "10.1234/example" \
  --target-sha256 <sha256> --reason "Corrected from the printed bibliography"
python run.py tasks answer-review --run <run> --task <task-id> --action keep-ambiguous \
  --target-sha256 <sha256> --reason "The source boundary cannot be determined"
```

Apply answered reviews and continue the pipeline with `python run.py --run
<run> --resume`. `no_sources` and `keep_ambiguous` leave the item out of
Resolve and Fetch. `split_sources` creates complete, ordered child sources.
Identity correction is an overlay: the raw parse remains immutable and the
corrected identity still passes normal deterministic gates. Stale hashes,
duplicate applications, malformed answers, and non-exact split substrings
fail closed. The report separates these outcomes from HTTP failures. Without
`--manual-review`, ambiguous notes remain excluded and are reported, but the
run does not pause.

### Phase 1b — ingesting provided sources (if any)

**Two modes, different ordering** (this is deliberate):
- **Sources provided by the user** → association is a **batch up-front step**: decode every
  file and map it to its reference number FIRST (this Phase 1b), THEN verify in reference
  order (Phase 4). You cannot stream per-reference because the files arrive as an unordered
  pile that must be matched to references before anything else.
- **No sources provided** → forced **per-reference streaming** (Phase 2): for each reference
  resolve → fetch → decode → verify → next. Order is intrinsic (one reference at a time).

If the user provides a **folder** with the full texts (any format:
PDF/txt/docx/tex/md), map them to the references BEFORE searching online:
```
python run.py provide ingest --run runs/<ts> --dir <folder>
```
- **Automatic** matching only if strongly corroborated (DOI/PMID in the file, or nearly
  all the tokens of the entry) → manifest with `mapping=deterministic`.
- The **ambiguous** ones go to the typed ingest-review rows in `run.sqlite`, **not guessed**. For each
  one read the file, propose which reference it belongs to and **confirm**:
  ```
  python run.py provide map --run runs/<ts> \
         --ref <ref_id> --file <path>
  ```
  The code corroborates (DOI / author+year / title in the file); without corroboration the
  mapping is rejected (use `--force` only if you are certain).
- **Unreadable provided files** (scanned, no text layer): NOT a crash. `ingest` keeps the
  file and lists it under `unreadable` in the typed ingest-review projection; `map` keeps it **associated
  to the reference you named** and returns `reason=unreadable_pdf` (no crash). Both push it
  onto the typed OCR queue in `run.sqlite`. **Association needs text**, so an
  unreadable file cannot be matched until OCR'd: the order is **park → (ask) OCR →
  associate**. See "OCR queue" below.

### Phase 2 — axis 1 (existence) + OA full-text retrieval (streaming, per reference)

**Design principle**: process one reference at a time. Each reference completes its
full pipeline — resolve → fetch → record — before moving to the next. Intermediate
`.txt` files are written to disk before the manifest entry is registered, so a crash
loses at most the current reference; no previously-completed work is redone (idempotent).

#### Phase 2a — resolve (existence check)
```
python run.py resolve --run runs/<ts> --ref-id <ref_id> \
       --text-out /tmp/abs_<ref_id>.txt [--mailto you@uni.edu]
```
(`resolve` prints a transient protocol result on stdout for inspection; do not persist it.
The authoritative result is written to `run.sqlite`, not a `resolve/` folder.)
**Polite pool (recommended):** pass an email with `--mailto` or `CITATION_VERIFIER_MAILTO`
— Crossref/Europe PMC give more lenient limits and you reduce `rate_limited`. The email
goes only in the header to those services, never in the report.

**Optional `GOOGLE_BOOKS_API_KEY`:** unlocks the opt-in `preview_snippet` tier for books
(Phase 2c) and lifts the keyless per-IP quota on the Google Books existence check (HTTP
429 otherwise). The key is read from the environment, used only in the request to Google,
and never written to a run artifact or the report.

**Resolver routing** (in order, stops at first conclusive result):
1. **DOI → Crossref** — `not_found` is fabrication (unique ID absent).
2. **PMID/DOI/title → Europe PMC** (skipped for `source_type="book"`).
   PMID absent in EPMC → confirmed on PubMed: `absent` = `not_found`, `exists` = `resolved`.
3. **ISBN or `source_type="book"` → OpenLibrary + Google Books (two-catalog corroboration)**
   - Neither catalog is exhaustive, so existence is checked against **both** before concluding:
     - found in either → `resolved` (`existence_corroboration="corroborated"`).
     - **well-formed ISBN absent from BOTH** → `not_found` (fabrication: a unique code no catalog knows).
     - **title-only absent from BOTH** → `unverified` + `existence_corroboration="searched_not_found"`
       (LOUD orange warning, **never red** — catalogs are not exhaustive).
     - nothing to search on (no ISBN, no usable title) → `unverified` + `existence_corroboration="none"`.
   - `work_type="book"`, `fulltext_exists="unknown"` (books rarely have machine-readable full text).

Resolve status meanings:
- `status=not_found` → **finding** (possible fabrication): ONLY a DOI (Crossref 404), PMID (absent on PubMed), or ISBN (well-formed, absent on **both** OpenLibrary and Google Books) that definitively does not exist. A title-only search with no results is **never** `not_found`.
- `status=identifier_mismatch` → **finding**: the DOI/PMID resolves but to a work with a clearly different title. Wrong or copied DOI.
- `status=unverified` → no strong identifier, or title-only search empty, or ISBN malformed. **NOT fabrication**. See `existence_corroboration` to tell apart *"couldn't check"* from *"checked everywhere, found nothing"*.
- `existence_corroboration=searched_not_found` → **orange warning**: book searched in OpenLibrary **and** Google Books, found in neither. Not auto-red (catalogs aren't exhaustive) but high suspicion → verify manually, or escalate with an opt-in web search (level 3, performed by the base LLM agent's native web tools, **not** an external API).
- `status=unresolved` → rate-limited (429) or network/HTTP error: **transient, NOT fabrication**, retryable.
- `title_flag=warn` → the DOI resolves but the returned title only partially matches the cited entry. Check manually (yellow signal, not a finding).
- `fulltext_exists=false` → resolver confirmed this is abstract-only (e.g. conference paper): the abstract IS the ceiling; do not request the full text.
- `retracted=true` → **red finding**: it exists but is RETRACTED; verified anyway, never green.

#### Phase 2b — OA full-text fetch (articles with DOI + open access)
Immediately after resolve, **for articles** where `oa_status in {open, unknown}` and `fulltext_exists is not False`:
```
python run.py fetch --run runs/<ts> --ref-id <ref_id> [--mailto you@uni.edu]
```
- Queries the **Unpaywall API** (`api.unpaywall.org/v2/{doi}?email={email}`) for the best OA PDF URL.
- Downloads the PDF, extracts text via `core/fetch/extraction/pdf.py` (pdfminer.six with column detection;
  falls back to pdftotext → stdlib extractor).
- Passes a quality gate (≥500 chars, ≥35% alpha).
- Calls `sources.store_text()` → writes `<n>_fulltext_unpaywall.txt` and updates manifest.
- **Idempotent**: if fulltext already in manifest for this ref_id, returns `already_stored` immediately.
- Return status:
  - `stored` → text available; proceed directly to Phase 4 for this reference.
  - `not_found` → no OA URL on Unpaywall; fall through to Phase 2.5.
  - `skipped` → no DOI, `fulltext_exists=False`, or `oa_status=paywalled`.
  - `quality_error` → PDF downloaded but **unreadable** (no text layer — typically an old
    scan without OCR). The downloaded PDF is **kept** in
    `sources/ocr_queue/<n>_fulltext_unpaywall.unreadable.pdf` (so a restart does not
    re-download it). The source is **parked**: it stays a gap and is flagged
    `unreadable_pdf`. See "OCR (opt-in)" below.

##### OCR queue for unreadable PDFs
A scanned PDF without a text layer is **not** discarded and does **not** fail the run — it is
**parked** in `sources/ocr_queue/` and pushed onto a single deterministic queue,
the typed unreadable-source queue in `run.sqlite` (status `pending|done`), fed by
source acquisition and controlled-input paths:
- automatic `fetch` (downloaded PDF, unreadable) — kept and **associated** to its reference;
- an authenticated Fetch answer containing an unreadable PDF — kept and **associated**;
- startup ingestion of an unreadable user source — kept **unassociated** when its identity cannot yet be established.

`gaps.py` and the report aggregate this queue. Eligible associated scans are OCRed
automatically when `CITATION_VERIFIER_OCR_AUTO=1`; an operator can instead run the
same declared OCR backend explicitly after authorising the work. Keep the output in
the workspace:
```
python run.py ocr --pdf runs/<ts>/<kept_as> \
       --out run/user_sources/<ref_id>.ocr.txt [--lang eng|eng+ita] [--dpi 300]
# <kept_as> is taken verbatim from the queue, e.g. sources/ocr_queue/3_fulltext_unpaywall.unreadable.pdf
```
- Backends (first available wins): `ocrmypdf` or `pdftoppm`+`tesseract` (system, faster),
  then the **bundled** `rapidocr`+`pypdfium2` (pure-pip, models included, **no system
  binaries** — `pip install -r requirements-ocr.txt`). This bundled stack is what ships,
  so OCR works out of the box. If no backend is available, `ocr.py` fails loudly with
  install instructions (never a silent empty result).
- Submit precomputed OCR text only through the authenticated Fetch task associated
  with that queued scan:
  ```
  python run.py tasks answer-fetch --run runs/<ts> --task <fetch-task-id> \
         --ocr-text-file run/user_sources/<ref_id>.ocr.txt
  python run.py --run runs/<ts> --resume
  ```
  The answer gate copies and hashes the `.ocr.txt`, and binds it to the exact pending
  same-reference PDF by the scan's SHA-256. A missing, ambiguous, or changed scan is
  rejected. Only after the OCR text passes the normal identity and quality gates is it
  stored with origin `ocr`; the parsed text lands in `sources/parsed/`, the parked PDF
  is deleted, and its typed queue row becomes `ocr_status=done`.

  This authenticated command requires a queue row already associated with the Fetch
  task's reference. An unassociated parked file remains pending; do not bypass task
  authentication with the disabled direct `provide map` mutation path.

#### Phase 2c — books and remaining sources: manual or web

**The inventory of what is still missing.** At the end of the automatic fetch the
driver writes `fetch_manual_sources.md` in the run dir: one entry per reference
that has no stored full text, each with the resolver status, declared OA vs
observed access, clickable DOI, candidate URLs, the attempts made, and the
reason. This is the list an operator reads to decide what to supply by hand — you
do not have to reconstruct it from the database.

Each entry carries a `manual_action_required` flag:
- **`yes`** — an external full text is expected but not yet stored *and* no
  terminal task answer exists. These are the ones worth supplying.
- **`no`** — nothing to supply, for one of two reasons: the fetch task already has
  a terminal answer, **or** the reference is by-design non-fetchable — a
  `manuscript_pointer` (a pointer to the manuscript's OWN sections, no external
  document exists) or a work-scoped availability negative with no contradictory
  candidate. A reference URL or a non-DOI full-text link is a contradictory
  candidate and keeps the entry actionable (`yes`). See
  `_no_external_fulltext_expected` in `core/app/phases/fetch.py`.

**Supplying a source and having it accepted.** For a DB-native run, answer the
per-reference fetch task with the `tasks` CLI, then `--resume`:
```
python -m core.app.commands.tasks list --run runs/<ts> --slot fetch --status pending
python -m core.app.commands.tasks answer-fetch --run runs/<ts> --task <task_id> \
       --file-path /path/to/source.pdf --url <source-url>   # or --text-file, or --not-found
# For OCR text derived from the exact PDF already parked for this task:
python -m core.app.commands.tasks answer-fetch --run runs/<ts> --task <task_id> \
       --ocr-text-file run/user_sources/<ref_id>.ocr.txt
python run.py --run runs/<ts> --resume
```

##### Deterministic resume of candidates interrupted by the deadline

When the time budget expires before a planned candidate can be examined,
Callimachus preserves its typed, immutable record. Each `--resume` may claim
**at most one** skipped candidate. Before sending the request it verifies that
the reference, resolver result, and execution settings still match the frozen
context. It then uses the normal download, parsing, identity-validation, and
storage path without regenerating candidates or starting additional discovery,
provider, Perma, or Wayback work.

If the resumed attempt completes and materializes full text, the old ordinary
`fetch` task no longer blocks the phase. The task is not deleted, applied, or
rewritten; it remains in the ledger as an audit record of the original request.
Browser-challenge and OCR tasks still require their normal explicit answers.

The `Audit retry frozen` section of `fetch_manual_sources.md` reports cases that
cannot be resolved automatically:

- `frozen_retry_claimed_without_completion`: the candidate was claimed, but a
  process interruption prevented Callimachus from recording completion with
  certainty. It is never retried automatically, avoiding a second unauditable
  request.
- `frozen_retry_invalidated:<reason>`: the candidate is no longer safe to use,
  for example because its context changed or its record cannot be validated.

For either state, inspect the task with `tasks show`, review attempts and any
source already present in the run, then close the task through `answer-fetch`
with the correct document or `--not-found`. Do not issue the same URL manually
merely to unblock the ledger: record any recovered source through the normal
task ingress.

A file/text supplied this way is **force-accepted**: `--resume` ingests it via
`_register_run_source_text(force=True)`, which bypasses the 0.60 corroboration
gate that would otherwise reject a non-matching auto-fetch. It is stored with
`identity_status=externally_corroborated_text`, `mapping=manual`,
`supplied_by=user`; the corroboration `match_signal`/`match_score` are still
computed and recorded (inspect them in `source_texts`) but do **not** block. The
burden of supplying the *correct* document is therefore on the operator — the
pipeline records provenance, it does not re-verify identity for a hand-supplied
source.

For books (`work_type="book"`) or sources where fetch returned `skipped`/`not_found`:
- **Record abstract** from resolver metadata if available:
  ```
  python run.py provide record --run runs/<ts> --ref <ref_id> \
         --tier abstract --origin crossref --text-file /tmp/abs_<ref_id>.txt --url <doi-url>
  ```
- **Recover the book's text when possible.** Books have no Unpaywall-style auto-fetch, but a
  full text can still arrive: user-provided file (PDF → **OCR** if scanned, same opt-in queue),
  an OA/preview URL via WebFetch, etc. Record it with `--tier fulltext --origin {ocr|webfetch|user}`.
  **How that text is then processed depends on the underlying model** (this is the only
  model-aware branch, and it is *declared* in the report):
  - **Strong model with enough context** → treat the book full text like any full paper:
    `fulltext_complete`/`fulltext_trimmed`, with all the usual consequences (OCR provenance,
    truncation recorded). High reliability.
  - **Weak / small-context model** → do **not** dump the whole book: use the `rag` tier
    (`preprocess --scope rag --claim ... --topk N`, top-k chunks) or heuristics, and
    **SIGNAL it** — `rag` reliability is medium and an `off_topic`/`partial` there is
    weaker (the passage may sit outside the retrieved chunks). The report must say the
    book was read partially.
- **Book claims with no full text — preview snippets** (opt-in, key-gated): if
  `GOOGLE_BOOKS_API_KEY` is set, confirm short verbatim passages as fragments of the
  cited book before falling back to the web. Pass the candidate passages the jury
  wants to ground:
  ```
  GOOGLE_BOOKS_API_KEY=... python run.py preview --run runs/<ts> \
         --ref-id <ref_id> --passage "short distinctive phrase" [--passage ...]
  ```
  On success it stores a `tier=web origin=googlebooks` source of the kept snippets; the
  jury then judges with `scope=preview_snippet` and grounding validates as
  usual. Low reliability (one-sentence OCR window); never green. No key → no-op.
- **Claims with no full text and a missing/inconclusive abstract**: at accuracy grade
  `standard_web` the driver runs a post-verify **web_research** phase (`phase_web_research`).
  It pauses on the RESEARCH slot and asks the agent's web tools (autonomous: a per-backend
  web tool) for third-party pages, taking back only **untrusted
  hints** `{url, stance, quote}`. The driver then **re-fetches every url itself and rejects
  any quote not found on the page** (grounding) — same existence/quote discipline as
  for cited sources — and records the survivors with `--tier web --origin web_research`. If
  the agent finds nothing (or no web tool exists), it falls back to a **deterministic** web
  search (`core/verify/websearch.py`, `--origin websearch`). The jury then judges the retrieved
  text (`scope=web_secondhand`): low-reliability indirect verification, never green, and
  **additive** to any abstract verdict already in the ledger. The researchers are a
  **plug-and-play registry** (`core/verify/researchers/`, auto-discovered like `core/fetch/fallbacks/fetch_modes/`):
  each module exposes `NAME`/`PRIORITY`/`available(st)`/`produce(st, ref, claims, answer)`
  and is slotted into the priority-ordered fallback chain. Ships with `agent_findings`
  (validates the agent's hints, priority 10) and `deterministic` (plain web search, priority
  90); drop in another `.py` to add a researcher without touching `run.py`.
- **No text** → the source stays without a verdict (Phase 2.5), never silently substituted.

**Always record what you retrieve online** with its `origin` to keep provenance inspectable.

### Phase 2.5 — gap list, provisioning, restart
```
python run.py gaps --run runs/<ts> --accuracy <maximum|standard|abstract|standard_web>
```
Pass the Phase 0 `--accuracy` grade: it sets the floor tier and whether the full text is
chased. The SQLite-backed gap summary records `accuracy`, `min_tier`, and
`chase_fulltext`.

- **Network-blocked guard (hard stop).** If the gap summary has `network_blocked=true`
  (nothing usable retrieved — no full text, no abstract, no web — **and** ≥1 reference
  failed with a transient network error), **STOP** and report the network outage: with
  no text there is nothing to verify. If instead **at least one** source has usable text,
  **proceed** on what is available and flag the remaining `unresolved` references as
  *not verified* (they appear under `unverified` / `gaps`, never silently substituted).

Availability is read from the **source manifest projection in SQLite**. Procedure:
1. **Verify first all** sources that already have a `fulltext` in that projection (Phase 4).
2. The gap summary lists the missing ones, with the reason and the reachable tier. Present
   the user with **a consolidated list** and ask for the missing full texts. When they
   arrive, record them (`run.py provide map` for files, `record` for the web) and **re-run
   `run.py gaps`**: verification **restarts** only on the now-available pairs — the ledger is
   append-only, nothing already done is redone.
3. For what **remains**, offer the **downgrade** within `min_tier`: `abstract` if
   available, otherwise — if authorized — `web_secondhand` (`--tier web --origin
   websearch`, record the `evidence_urls`). It is the weakest tier, indirect.
- A `not_found` source for which the user provides the text anyway: map with `--force`
  (provenance `mapping=manual`), but the existence axis **stays `not_found`**, it does not
  flip. The three axes remain separate.

### Phase 3 — axis 2 (style), by type
```
python run.py style-check --style <style> --run runs/<ts>
```
The dispatcher detects the `source_type` of each entry and applies the style module.

### Phase 4 — axis 3 (support): the verification jury
For every `citation` with a non-null `ref_id` and available source text, the driver runs
the claim-evidence jury itself (`core/app/phases/verify.py` → `ClaimEvidenceRuntime`) — you
do **not** hand-run a Verifier prompt or a `passage-guard` command:

1. The runtime selects the source context (`CITATION_VERIFIER_VERIFY_CONTEXT_MODE`). In
   `auto`, `large` uses the complete source, `medium` switches to extractive RAG only above
   `CITATION_VERIFIER_MAX_SOURCE_CHARS`, and `small` uses extractive RAG. The frozen ranges,
   hashes, algorithm, and retrieval configuration are recorded. RAG requires the optional
   `requirements-rag.txt`; a missing or wrong pinned dependency stops before jury dispatch.
2. **jury1** judges the pair — one at a time, isolated: it sees only the claim (+ window)
   and the selected source text, and returns an `outcome` plus the verbatim `evidence`
   passages that justify it.
3. **grounding** (`core/verify/claim_evidence/evidence/grounding.py`) string-matches every
   `evidence` passage against the **complete** source text; any passage not found rejects
   the verdict, which is retried with clean context (protocol failures are recorded, not
   lost, so "Run health" counts them).
4. **jury2** (when `CITATION_VERIFIER_VERIFY_JURY2_LEVEL` ≠ `off`) re-judges the claim
   against only the grounded evidence and answers `evidence_justifies_outcome`; on a
   refutal the pair is re-verified, accepted-with-flag, or marked `uncertain` per the level.

Every request, dispatch attempt, admissible candidate, and terminal transition is stored
in typed SQLite relations; a pair whose budget is exhausted surfaces as
`exhausted`/`uncertain`.

### Phases 5 & 6 — removed (no Interpreter, no Orchestrator)
There is no separate multi-source "Interpreter" slot and no LLM "Orchestrator" briefing.
Multi-source claims are verified independently per (claim, source) pair; the report is a
fully deterministic projection of the typed run ledger (`core/report/`), so **no LLM
writes any part of it**.

### Phase 7 — report (deterministic)
```
python run.py report --run runs/<ts>
```
Produces `report.md` (a fully deterministic projection — **no briefing**) and
The report command emits its machine-readable CI summary on stdout and stores the authoritative summary in typed SQLite rows (`ci_pass=false` if there are `not_found`, `identifier_mismatch`, retracted sources, or ❌ claims; `unverified` does NOT fail CI).

### Phase 8 — benchmarking (optional, opt-in)
Compare the tool's behaviour **across LLMs** and **across runs of the same LLM** — no
gold set needed, because the deterministic guards already record objective signals
(guard pass, hallucinated passages, protocol violations, accepted outcome) per verdict.

**Fairness rule (do this or the comparison is meaningless):** freeze the deterministic
layer and run it ONCE (parse + resolve + fetched `sources/`), then run **only the
verification jury** (jury1 → grounding → jury2) per model against that same frozen fixture
— same pairs, same `scope` per pair, same retry budget. Give
each run its own `runs/<ts_model>/` (so `run_id` differs) while reusing the frozen
fixture; repeat a model ≥2 times to measure its run-to-run stability. Otherwise network
variability in resolve/fetch contaminates the result.

```
python run.py benchmark --run runs/<ts_modelA_1> --run runs/<ts_modelA_2> \
       --run runs/<ts_modelB_1> --out runs/benchmark
```
Writes `benchmark.md`:
- **intrinsic per model** — guard pass@1, accepted/unstable rate, hallucinated-passage
  rate, protocol-violation rate, near-miss rate, attempts/cell, outcome mix;
- **within-model concordance** — agreement across separate runs of the SAME model
  (stability: unanimity, mean pairwise agreement, Cohen's κ);
- **between-model concordance** — agreement across different models;
- **discordant pairs** — the (claim, ref) where models/runs disagree: the actionable
  output (review these; they are also the natural candidates for a future gold set).

Read these as **discipline + concordance**, NOT accuracy: there is no ground truth, so a
cautious model (e.g. quoting conservatively) can score well on the guard. The accepted
**outcome mix is a finding, not a score** — a model is not "better" for saying "supports"
more often.

---

## Prompt — jury1 (one pair, one judgment)

> The driver sends this itself; the **authoritative** prompt and JSON schema live in
> `core/verify/claim_evidence/contracts/jury1.py` (with `jury2.py` for the second judge).
> The sketch below is a reference for what jury1 does — treat the contract module as truth
> if they ever diverge.

```
You are verifying whether a source text supports a specific statement.

STATEMENT (the sentence to verify):
{{claim_sentence}}

CONTEXT (for understanding only; judge the statement, not the context):
{{context_window}}

SOURCE TEXT:
{{prepared_text}}

Judge ONLY against the source text. Do not use external knowledge. Do not assume
that other sources exist.

Respond with a single JSON object and nothing else:
{
  "outcome": "supports" | "partial" | "contradicts" | "related" | "off_topic" | "non_decidable",
  "evidence": ["<verbatim passages from the source text that ground the outcome>"],
  "supported_part": "<only if partial: which part of the claim is supported>",
  "explanation": "<one sentence at most>"
}

Rules:
- Cite in "evidence" ALL the verbatim passages that ground the outcome. If the
  claim reports specific data, they can be multiple sentences/cells, even non-contiguous.
- Copy VERBATIM from the source text: no paraphrase, no rounded numbers.
  If you cannot cite a real passage, the outcome CANNOT be "supports".
- "supports": the text directly states or implies the statement.
- "partial": it supports part of it; indicate which in supported_part.
- "contradicts": the text states the opposite or an incompatible value.
- "related": the text is on-topic but does not settle the statement.
- "off_topic": the text does not address the statement (empty evidence).
- "non_decidable": cannot be judged from this text; jury1 records a structured `reason`
  (e.g. attribution / material_limit / no_consensus / retrieval_limit).
```

### jury1 variant — `web_secondhand` tier (indirect verification)

To be used ONLY when the source could not be read and this tier was reached.
The text passed (`{{web_excerpts}}`) are **third-party** pages that cite/describe
the source, not the source. The verdict's `scope` = `web_secondhand`, and it populates
`evidence_urls`.

```
You could NOT read the original source. Below are excerpts from OTHER web pages
that cite or describe it. Assess whether they indicate that the source supports
the statement. This is INDIRECT and less reliable evidence.

STATEMENT: {{claim_sentence}}
THIRD-PARTY EXCERPTS (with URLs): {{web_excerpts}}

Same JSON as jury1, but:
- "evidence" = verbatim passages FROM THE THIRD-PARTY EXCERPTS (not from the source);
- in "explanation" write that this is an indirect assessment;
- do not declare "supports" if the excerpts do not clearly state the content
  of the source with respect to the claim.
```

### jury1 variant — `preview_snippet` tier (book preview fragments)

To be used ONLY for books reached at this opt-in, key-gated tier. The text passed are
**short Google Books preview snippets** — genuine fragments of the cited book, but a
~1-sentence window with no surrounding context. The verdict's `scope` =
`preview_snippet`.

```
You could read only short PREVIEW SNIPPETS of the cited book (fragments of its real
text, ~1 sentence each, no surrounding context). Assess whether they support the
statement.

STATEMENT: {{claim_sentence}}
BOOK PREVIEW SNIPPETS: {{preview_snippets}}

Same JSON as jury1, but:
- "evidence" = SHORT, distinctive verbatim phrases present in the snippets
  (a few words; do not span punctuation-heavy boundaries — the snippets are OCR'd);
- do NOT declare "contradicts": a one-sentence window cannot establish contradiction;
- in "explanation" write that this is a preview-fragment assessment.
```

## Prompt — jury2 (isolated second judge, opt-in)

> Also driver-sent; authoritative prompt in `core/verify/claim_evidence/contracts/jury2.py`.
> jury2 sees **only** the claim and the passages jury1 already extracted (no full source
> text) and answers a single boolean — `evidence_justifies_outcome` — with a short reason.
> It never re-reads the source and never edits jury1's evidence; its verdict feeds the
> level policy (`CITATION_VERIFIER_VERIFY_JURY2_LEVEL`) described above.

## Removed prompts — Interpreter (Slot 2) and Orchestrator (Slot 3)

These two LLM slots **do not exist**. Multi-source citations are reported from their
independent typed pair results, and the report is a deterministic projection of the run
database with **no LLM-authored briefing**.

## Scope (non-goals of this version)
- Citations: **numeric** (`[n] (n)`, superscript, `\cite{key}` LaTeX) and
  **author-year** (`(Smith 2021)`, `Smith et al. (2021)`) via deterministic
  converter. The ambiguous ones (same surname+year) are not guessed: the user
  confirms them. In-text author-year detection is heuristic (see debug view).
- Formats: `.docx`, `.tex` (thebibliography or adjacent `.bib`), `.pdf`
  (pdfminer.six with two-column detection; falls back to pdftotext → stdlib;
  quality gate: if all fail, the agent extracts the text and passes back a `.txt`),
  `.md`, `.txt`, `.html`/`.htm`/`.xhtml` (publisher HTML pages are parsed with
  citation-marker preservation — `<sup>` tags kept, numeric bibliography anchors
  `<a href="#ref-n">` rebracketed to `[n]`; paywalled landing/abstract-only HTML
  source is refused at the full-text tier). One manuscript per run. A bibliography-only
  document (0 claims) is refused with exit 4 — out of scope by design.
- Source types resolved: **articles** (DOI → Crossref, PMID → EuropePMC/PubMed,
  OA full text → Unpaywall → pdf.py), **books** (ISBN/title → OpenLibrary **+**
  Google Books for existence corroboration; claim verification at the opt-in,
  key-gated `preview_snippet` tier — short passages confirmed against Google Books
  preview fragments, `core/fetch/fallbacks/preview.py` — and/or `web_secondhand` via the base agent's
  web tools), **webpages** (existence recorded; agent fetches via WebFetch).
- **Scanned PDFs**: OCR is available (`core/fetch/extraction/ocr.py`) but **opt-in, per source, on
  user request** — never automatic. Unreadable downloads are parked, not dropped.
  A bundled pure-pip OCR backend (`rapidocr`+`pypdfium2`, models included, no system
  binaries) ships via `requirements-ocr.txt`; system backends are used if present.
- The deeper web-search level (level 3: book existence corroboration + `web_secondhand`
  claim support) is done by the **base LLM agent's native web tools** (WebSearch/WebFetch),
  not an external search API. On a harness without web tools, that tier is simply unavailable.
- No chunking for small models: it truncates and records the truncation.
- Dependencies: stdlib only, plus `pdfminer.six` (PDF extraction). OCR backends
  (`ocrmypdf` or `tesseract`+`poppler`) are optional system tools, needed only for
  scanned PDFs. Install with: `pip install -r requirements.txt`
