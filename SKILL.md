---
name: citation-verifier
description: >-
  Verifies the citations of a manuscript (.docx, LaTeX/.tex, .pdf, .md, .txt, .html):
  for each source it checks that it EXISTS (DOI/PubMed/web, incl. retraction), that it is
  cited in the correct STYLE (Vancouver, APA 7, Chicago, MLA 9) and that it truly
  SUPPORTS the claim that cites it, with verbatim passages validated
  deterministically. Use when the user asks to verify/check citations, sources,
  bibliographic references, bibliography, citation check, source fact-checking,
  or to find fabricated/hallucinated or unsupported citations in a
  paper/manuscript/article.
allowed-tools:
  - Bash
  - Read
  - Write
  - WebFetch
  - WebSearch
---

# Citation Verifier (skill)

**You do not verify citations yourself.** A deterministic script does — and it must be the
one in control, not you. Do not read the sources to judge them, do not decide which are
valid, do not "spot-check" a sample, and never write `report.md`. Your job is to **hand off
to the runner** and relay its sealed result. This is non-negotiable: a verdict you produce
by reading is exactly the failure this tool exists to prevent.

## Step 1 — hand off (do this first, always)

There are two ways to run the deterministic verifier; **prefer the autonomous runner**,
which IS the script driving the model — you are not in the loop at all:

```bash
cd <skill dir>
# The script drives end-to-end and runs the verification jury itself (jury1 → grounding →
# jury2) — you never judge a pair. Name a backend FIRST: `claude_cli` reuses the user's
# login (NO separate API key, NO extra bill); an HTTP backend (anthropic/openai/gemini/…)
# uses its own key. The jury2 level is mandatory. The runner produces a SIGNED report itself.
export CITATION_VERIFIER_VERIFY_BACKENDS=claude_cli    # or anthropic, openai, gemini, …
export CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium     # off|low|medium|high
python run.py --input "<manuscript>" --autonomous \
       --agent-identity claude-code \
       --accuracy maximum|maximum_fallback|standard|abstract|standard_web \
       [--model <id>] [--mailto <email>]
```

If the autonomous runner cannot run here (no `claude` CLI and no API key), tell the user
plainly: **"The citation verifier is installed. Run it yourself with the command above,"**
and/or fall back to Step 2 (you fill the FETCH/RESEARCH pauses, but the driver still runs
the verification jury and owns the flow). Either way you are a launcher, not the judge.

Phase 0 (collect first, for either path): accuracy grade; an optional contact email for the
polite pool (it is SENT to Crossref/Europe PMC/Unpaywall, never in the report); optionally a
Google Books API key for the opt-in book preview tier. These three live in the environment —
`CITATION_VERIFIER_ACCURACY` (default `standard`: full text, abstract fallback on paywall),
`CITATION_VERIFIER_MAILTO`, `GOOGLE_BOOKS_API_KEY` — and `core.run` reads them automatically;
offer to write a git-ignored `.env`. If an optional value is missing the runner **stops
before starting** and asks; pass `--proceed` to ignore and continue. Do NOT ask for the style
(auto-detected). The runner prints per-pair progress as it verifies.

## Step 2 — driving it by hand (you do NOT judge pairs)

The driver runs the verification jury (jury1 → grounding → jury2) **itself** — you never
submit a Verify verdict by hand. Configure a backend (Step 1) and run
`python run.py`; it parses, resolves, fetches, checks style, verifies, then reports and
seals. It only **pauses** where you can supply something code cannot: **FETCH** (a full
text retrieval missed) and, under `--accuracy standard_web`, **RESEARCH** (web hints for a
last-resort indirect check). Answer only through the SQLite-backed `run.py tasks` CLI, then
`--resume`. Do not improvise
an alternative procedure, do not write `report.md` yourself, do not "spot-check" a sample.

```bash
cd <skill dir>
export CITATION_VERIFIER_VERIFY_BACKENDS=claude_cli   # required
export CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium    # required
python run.py --input "<manuscript>" \
       --agent-identity claude-code \
       --accuracy maximum|maximum_fallback|standard|abstract|standard_web \
       --mailto <email> --model <your-model-id>
#   → the driver parses, resolves, then may PAUSE:
#     "ACTION REQUIRED · slot: FETCH"

# For each FETCH task: retrieve the source's FULL TEXT, then inspect and answer it through:
# python run.py tasks list --run runs/<ts> --slot fetch
# python run.py tasks show --run runs/<ts> --task <task_id>
# python run.py tasks answer-fetch --run runs/<ts> --task <task_id> \
#        --file-path <text-file> --agent-identity claude-code
# Use --not-found only if you truly cannot find it. NEVER invent text.
python run.py --run runs/<ts> --resume --agent-identity claude-code
# ingests texts, verifies (jury), continues
# ... under standard_web a RESEARCH pause may follow (see below) ... resume each time ...
# → the run produces report.md and runs the gate. EXIT 0 = done, 20 = gate failed.
```

`core.report` produces and seals the final `report.md`; `core.verify_run` then **fails** if
any (claim, source) pair with available text has no guarded verdict, or if the report was
not produced by the pipeline. A hand-written or partial run cannot pass the gate. If
`core.run` exits 3 on parsing (PDF best-effort failed), extract the text yourself and
re-run with a `.txt`. If the gate fails (exit 20), it prints exactly what is missing — go
back, `--resume`. Do not deliver a report the gate rejected.

**`standard_web` only — the RESEARCH slot.** After verify, when a source has no full text
and its abstract was missing or its verdict was inconclusive, the driver pauses on
a pending SQLite task in the RESEARCH slot. Use your web tools to find third-party pages
discussing that source, then submit each copied quote with:

```bash
python run.py tasks answer-research --run runs/<ts> --task <task_id> \
  --agent-identity claude-code --finding 'URL|STANCE|QUOTE_OR_QUOTE_FILE'
```

These are only **hints**: on `--resume` the driver
independently re-fetches every `url` and rejects any quote it cannot find on the page, then
verifies the surviving pages as INDIRECT `web_secondhand` evidence (never green). If you
have no web tool, just `--resume` — the driver falls back to a deterministic web search.

## Step 3 — show results: the report is presented by the tool, not by you

**Never paste, quote, or summarise the report as if it were the report.** To show it, run:

```bash
python run.py present --run runs/<ts>
```

`core.present` verifies the gate (and the signature) and prints the report **verbatim**
between banners; it refuses if the run is not authentic. The user sees the verified report
through that command's output, not through your prose. Only **after** it — below the "END OF
VERIFIED REPORT" divider — may you add **your interpretation**, clearly labelled as your own
commentary (e.g. "My reading of the above:"). Your commentary never restates findings as if
verified, and never replaces the presented report.

## Agent invocation and consent

Claude Code agent invocations always pass `--agent-identity claude-code` on run,
resume, fork, task-answer, and report commands. Fresh direct human use omits the flag and remains
available without an authority, but is standalone/unattested and never
audit-ready. For an agent-identified invocation, an authority or isolation
failure produces a prominent alert and a yes/no question for the human. Relay it; never choose on
the user's behalf. Yes records an irreversible unprotected-agent state and the
result is unreliable and non-audit-ready; no stops. Without a TTY, stop and
ask the human, then rerun through a PTY; no `--yes` bypass exists. Debug is
distinct from agent protection.

## Enforcement: you cannot end the turn on an unverified run

For an audit-ready or agent-resistant run, the Stop hook and report-signing key
are not sufficient. Follow **`docs/deployment/agent-guide.md`** and use only
the administrator-installed immutable authority and runner launchers. Never
run authority `init`/`serve` from an agent-writable checkout, inspect or alter
the authority key/ledger, edit the installed release or service, or request
general `sudo`. On a failed authority preflight, relay Callimachus's question
to the human and never answer it yourself. A human-approved continuation and a
debug override are distinct diagnostic states; neither is a deployment
fallback nor audit-ready. Only a clean final projection with
`audit_ready=true` may be presented as audit-ready.

A **Stop hook** (`adapters/claude-code/hooks/citation_verify_stop.py`, wired in
`.claude/settings.json`) runs `core.verify_run` when you try to finish. If the gate is red
it **blocks** the stop and hands you back the exact reason — you must `core.run --resume`
and complete every pair before you can end. When the gate is green, the hook (the trusted
deterministic context) **signs** the report with an HMAC key and lets you stop.

The signing key (`CITATION_VERIFIER_SIGNING_KEY` / `_KEY_FILE`) lives ONLY in the hook's
environment, never in your shell. So you cannot forge the deterministic system's signature:
a report you hand-write or re-seal yourself carries at most a plain `sha256` seal, which
`core.verify_run --require-signature` (CI / audit) rejects. Do not look for, print, or use
that key — by design you do not have it.

## Manual mode (only if the driver cannot run)

The driver wraps the same scripts documented in **`PLAYBOOK.md`**. If you must run them by
hand, you still MUST finish with
`python run.py report --run <run> --agent-identity claude-code` and
`python run.py verify --run <run>` (exit 0) — that gate is non-negotiable.

## Quick start (legacy manual phases — see PLAYBOOK)

1. Obtain the manuscript path (`.docx`, `.tex`, `.pdf`, `.md`, `.txt`, `.html`). Then, in Phase 0:
   ask the **accuracy grade** (`--accuracy maximum|standard|abstract|standard_web`, default
   `standard`); ask for a contact **email** for the polite pool (explain why — it is
   *sent* to Crossref/Europe PMC/Unpaywall, not kept local, but never in the report; the
   user may decline); and optionally ask for a **Google Books API key** (env
   `GOOGLE_BOOKS_API_KEY`) to enable the opt-in `preview_snippet` tier for books. If the
   user wants to reuse a secret, **offer to write a git-ignored local `.env`** (never
   commit secrets; on the hosted env use the environment's secret config instead). See
   PLAYBOOK Phase 0. **Do not ask for the style** — it is auto-detected after parsing
   (`core.style.detect`); ask only if the guess is weak.
   For LaTeX the `\cite{key}` are converted into numeric markers (bibliography from
   `thebibliography` or adjacent `.bib`). For PDF the extraction is best-effort: if
   the script fails (exit 3), extract the text YOURSELF (you can read PDFs) and pass
   back a `.txt`.
2. **Prefer the driver.** A run is now **DB-native** — SQLite (`run.sqlite`) is the system
   of record; only `sources/{parsed,provided,ocr_queue}/` and the final `report.*` are files
   on disk. There is no `parse.json`/`resolve/*.json`/`style.json`/`ledger/verdicts.jsonl`
   scaffolding to assemble by hand, and axis 3 is run by the jury inside the driver, not by
   a hand-fed Verifier loop. The standalone subcommands below still exist for **inspection**
   (they write their own JSON when you pass `--out`), but the canonical path is
   `python run.py --input <manuscript> --agent-identity claude-code` (Steps 1–2):

```bash
cd <skill dir>
# Inspect parsing + the debug view (CHECK: claims vs markers, biblio cut):
python run.py parse --input "<manuscript>" --out /tmp/parse.json \
       --debug /tmp/parse_debug.md --window 1
# Auto-detect the style (ask the user only if confidence != high):
python run.py style-detect --refs /tmp/parse.json --out /tmp/style_detect.json
# Check the style of a parsed bibliography (dispatch by source_type):
python run.py style-check --style <style> --refs /tmp/parse.json --out /tmp/style.json
# Inspect an existing run's gaps / ledger / report:
python run.py gaps   --run runs/<ts> --accuracy standard   # maximum|maximum_fallback|standard|abstract|standard_web
python run.py ledger accepted --file runs/<ts>/run.sqlite
python run.py report --run runs/<ts> --agent-identity claude-code
```

3. Deliver `report.md` (and `report.summary.json` if needed for CI).

## Firm rules (see PLAYBOOK for detail)

- **Three axes never merged**: exists? / correct style? / supports? — do not conflate them.
- **Axis 3 limit**: grounding proves that the passages **exist**; the **relevance**
  to the claim remains a model judgment, made inspectable (claim and passages side by side).
  It reduces fabricated/unsupported citations; it **does not guarantee** the absence of
  relevance errors.
- **One guarded verify slot**: jury1 (outcome + verbatim evidence) → grounding (every
  passage must exist in the source) → optional jury2 (re-judge on the evidence alone).
  The report is a deterministic projection — there is no LLM briefing. It is code, not
  your narrative.
- **Retry** only on protocol/grounding failures, with **clean session/context**;
  NEVER on negative findings (`not_found`, `contradicts`, `off_topic`).
- **Append-only**: every attempt is a new ledger row; do not overwrite.
- **Degradations** (no full text → abstract, truncation, unresolved source,
  exhausted retry) are data to show, never to hide.

## Setup
`python3 >= 3.9`, stdlib only for the core. To read PDFs or web pages use your
tools (`WebFetch`) and save the text in `sources/<ref_id>.txt`.
