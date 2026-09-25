# Workflow: Citation Verifier (Antigravity adapter)

> Trigger: the user asks to verify citations, sources, references or
> bibliography of a manuscript (`.docx`/`.tex`/`.pdf`/`.md`/`.txt`) — existence, style, claim support,
> hunting for fabricated or unsupported citations.

**You do not verify citations yourself.** The deterministic script `core.run` owns the
control flow and runs the verification jury (jury1 → grounding → jury2) itself; you only
fill the **fetch** and (under `standard_web`) **research** pauses, and you never write the
report. The canonical procedure, identical for every tool, is in **`PLAYBOOK.md`**.

## Entrypoint (do this — do not improvise)
```bash
# Phase 0 config from the env (a .env): CITATION_VERIFIER_ACCURACY (default standard: full
# text, abstract fallback on paywall), CITATION_VERIFIER_MAILTO, GOOGLE_BOOKS_API_KEY.
# Axis 3 needs an explicit backend + jury2 level (nothing is auto-selected):
export CITATION_VERIFIER_VERIFY_BACKENDS=gemini_cli    # or gemini, anthropic, …
export CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium     # off|low|medium|high
python run.py --input "<manuscript>" --accuracy maximum|maximum_fallback|standard|abstract|standard_web
#   → drives parse/resolve/fetch/gaps/style/verify. It may PAUSE at FETCH through a
#     SQLite-backed task. Inspect with `python run.py tasks list` and `tasks show`; submit with
#     `python run.py tasks answer-fetch`, then:
# standard_web only: a post-verify RESEARCH task may ask your web tools for third-party
#   pages — submit with `python run.py tasks answer-research`; the driver re-fetches
#   each url and grounds the quote, then verifies it as indirect web_secondhand evidence.
python run.py --run runs/<ts> --resume       # repeat until DONE
python run.py verify --run runs/<ts>          # gate: exit 0 required
python run.py present    --run runs/<ts>           # show the report VERBATIM (never paste it)
```
Add your interpretation only BELOW the "END OF VERIFIED REPORT" divider. Missing config
stops the run unless you pass `--proceed`; style is auto-detected.

## Manual inspection (only if `core.run` cannot run)
A run is DB-native (`run.sqlite` is the system of record; only `sources/` and `report.*` are
files — no `resolve/`/`ledger/` folders), and axis 3 is run by the jury inside the driver.
The subcommands below still work for inspection (see PLAYBOOK for detail):
   1. `python run.py parse --input <file> --out /tmp/parse.json --debug /tmp/parse_debug.md`
      — then **inspect `parse_debug.md`**.
   2. `python run.py style-detect --refs /tmp/parse.json` — use if `high`, else ask the user.
   3. `python run.py resolve --ref <ref.json> --text-out /tmp/abs.txt > /tmp/resolve.json`.
   4. `python run.py gaps --run $RUN --accuracy <grade>` — STOP if `network_blocked`.
   5. `python run.py style-check --style <style> --refs /tmp/parse.json --out /tmp/style.json`.
   6. `python run.py report --run $RUN`, then `python run.py verify --run $RUN` (exit 0).
Output: **present** with `python run.py present --run $RUN` (never paste the report).

## Autonomous mode inside Antigravity
Autonomous mode (`--autonomous`) is also available here via the Gemini CLI backend — set
`CITATION_VERIFIER_VERIFY_BACKENDS=gemini_cli` (no provider is auto-selected). Set
`CITATION_VERIFIER_MODEL=gemini-2.5-flash` for a cheap, fast jury.

## Invariants
Three axes never merged · one guarded verify jury (jury1 → grounding → jury2), deterministic
report with no LLM briefing · retry only on protocol/grounding failures (clean context,
never on negative findings) · append-only ledger · degradations always visible.
