# Citation Verifier — agent (Codex adapter)

When the user asks to **verify the citations / sources / bibliography**
of a manuscript (`.docx`/`.tex`/`.pdf`/`.md`/`.txt`) (source existence, style conformity, actual
claim support, hunting for fabricated or unsupported citations), activate
this procedure.

**You do not verify citations yourself.** A deterministic script does, and it owns the
control flow. Hand off to `core.run`; it runs the verification jury (jury1 → grounding →
jury2) itself. You only fill the **fetch** and (under `standard_web`) **research** pauses,
and you never write the report. The canonical procedure is in **`PLAYBOOK.md`** (identical
for all tools).

## Audit-ready integrity authority

Every Callimachus invocation made by Codex must identify the agent explicitly:
append `--agent-identity codex` to run, resume, fork, task-answer, and report
commands. A fresh human/standalone invocation omits this option and remains
usable without an external authority; it is local, unattested, and never
audit-ready.

Agent identity attempts authority attestation. If isolation or authority checks
fail, Callimachus displays an alert and asks the human whether to continue.
Relay that question clearly; never answer it autonomously. `yes` continues
irreversibly as an unprotected, unreliable, non-audit-ready run; `no` stops.
Without a TTY the command stops: ask the human and rerun through a PTY. There
is no `--yes` bypass. Debug mode is a separate diagnostic state.

When the user requests an audit-ready or agent-resistant run, first follow
**`docs/deployment/agent-guide.md`**. Use only the administrator-installed,
root-owned authority-start and Callimachus runner launchers. Never initialise
or serve the authority from this checkout; never read or modify its key,
ledger, installed release, service unit, launchers, or protected roots. Never
request general `sudo` to repair the trusted side.

Run every documented fail-closed preflight. If the socket, ownership,
non-writability, code attestation, or content-store enrolment check fails,
relay Callimachus's question to the human and never choose for them. Do not
repair the trusted side or use the integrity debug override as a fallback. A
human-approved continuation is diagnostic and non-audit-ready. At completion,
only a clean report with `audit_ready=true` is an audit-ready result.

## Entrypoint (do this — do not improvise)
```bash
cd <skill dir>
# Phase 0 config comes from the env (a .env): CITATION_VERIFIER_ACCURACY (default standard:
# full text, abstract fallback on paywall), CITATION_VERIFIER_MAILTO, GOOGLE_BOOKS_API_KEY.
# Axis 3 needs an explicit backend + jury2 level (nothing is auto-selected):
export CITATION_VERIFIER_VERIFY_BACKENDS=codex_cli    # or openai, anthropic, …
export CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium    # off|low|medium|high
python run.py --input "<manuscript>" --agent-identity codex \
  --accuracy maximum|maximum_fallback|standard|abstract|standard_web
# → runs parse/resolve/fetch/gaps/style/verify itself. It may PAUSE at FETCH (provide a
#   missing full text) through SQLite-backed tasks. Inspect with `python run.py tasks list`
#   and `tasks show`; submit with `python run.py tasks answer-fetch ... \
#   --agent-identity codex`, then --resume.
# standard_web only: a post-verify RESEARCH task may ask your web tools for third-party
#   pages — submit them with `python run.py tasks answer-research ... \
#   --agent-identity codex`; the driver re-fetches
#   each url and grounds the quote, then verifies it as indirect web_secondhand evidence.
python run.py --run runs/<ts> --resume --agent-identity codex
# repeat until it prints DONE
```
The final `report.md` is produced by `core.report` and sealed. Then **show it with the tool,
never paste it yourself**:
```bash
python run.py verify --run runs/<ts>     # gate: exit 0 required (else fix & resume)
python run.py present    --run runs/<ts>      # prints the verified report verbatim
```
Add your interpretation only BELOW the "END OF VERIFIED REPORT" divider, as commentary.
Missing config (email / key) stops the run unless you pass `--proceed`. Style is
auto-detected. A hand-written or partial run cannot pass the gate.

## Manual inspection (only if `core.run` cannot run)
A run is DB-native: `run.sqlite` is the system of record and only `sources/` and `report.*`
are files — there are no `resolve/`/`ledger/` JSON folders, and axis 3 is run by the jury
inside the driver, not by a hand-fed Verifier loop. The subcommands below still work for
inspection (they write their own JSON with `--out`):
   - `python run.py parse --input <file> --out /tmp/parse.json --debug /tmp/parse_debug.md`
   - `python run.py style-detect --refs /tmp/parse.json` → use if `high`, else ask the user
   - `python run.py resolve --ref <ref.json> --text-out /tmp/abs.txt > /tmp/resolve.json`
   - `python run.py gaps --run runs/<ts> --accuracy <grade>` (STOP if `network_blocked`)
   - `python run.py style-check --style <style> --refs /tmp/parse.json --out /tmp/style.json`
   - `python run.py report --run runs/<ts> --agent-identity codex` then
     `python run.py verify --run runs/<ts>` (exit 0)
Deliver by **presenting** with `core.present` (never paste the report yourself).

## Autonomous mode inside Codex
Inside Codex the tool can also run fully autonomously (`--autonomous`), using the Codex
CLI itself as the LLM backend — set `CITATION_VERIFIER_VERIFY_BACKENDS=codex_cli` (no
provider is auto-selected). For a cheap, fast jury, set `CITATION_VERIFIER_MODEL=gpt-5-mini`
(or a provider-specific `*_MODEL`).

## Firm rules
Three axes never merged (exists/style/supports) · one guarded verify jury (jury1 → grounding
→ jury2), the report is deterministic with no LLM briefing · retry only on protocol/grounding
failures (never on negative findings), with clean context · append-only ledger · degradations
always shown. Detail in `PLAYBOOK.md`.
