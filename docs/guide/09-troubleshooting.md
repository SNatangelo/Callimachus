# 09 — Troubleshooting

Start with a non-mutating snapshot:

```bash
python run.py --run runs/<id> --status
python run.py --run runs/<id> --status --json-only
```

Then use the symptom-specific path below. Do not edit `run.sqlite`, source files, or report seals to make an error disappear.

## The command cannot import a dependency

Typical errors include `ModuleNotFoundError` for Brotli, PDF libraries, OCR, or RAG.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

For OCR or RAG:

```bash
python -m pip install -r requirements-ocr.txt
python -m pip install -r requirements-rag.txt
```

Confirm that `python run.py --help` is executed with the same interpreter that has the dependencies installed.

## Startup stops before creating a run

The preflight reports missing optional settings, dependencies, content-store access, or integrity authority.

1. Read every preflight item; some are optional and some are hard requirements.
2. Run `python run.py configure` or edit `.env`.
3. Use `--proceed` only to acknowledge missing optional values such as email or Google Books access.
4. Do not expect `--proceed` to bypass Verify policy, RAG dependencies, schema validation, or integrity.

## Verify says that no backend is configured

Set both required policy variables:

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=<registered-backend>
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=<off|low|medium|high>
```

Then configure the required client/login or API key and model for that backend. Use [Keys and credentials](10-keys-and-credentials.md) for creation instructions and [Configuration](03-configuration.md) for the backend matrix.

Unknown providers, duplicate CSV values, missing keys, empty model lists, or selectors that leave no eligible Jury1/Jury2 lane are hard configuration errors.

## The driver exits with code 10

This is a controlled pause, not a crash.

```bash
python run.py tasks list --run runs/<id> --status pending
python run.py tasks show --run runs/<id> --task <task-id>
```

Answer the exact task type, then resume:

```bash
python run.py --run runs/<id> --resume
```

See [Tasks and recovery](05-tasks-and-recovery.md) for Fetch, research, and Parse-review examples.

## The driver exits with code 3

Another process holds the run lock. Do not delete the lock immediately.

1. Check whether another driver is still running for the same directory.
2. Let that process finish or stop it cleanly.
3. Re-run `--status`, then `--resume`.

Removing a lock while a live writer exists can create competing state transitions and invalidate the run.

## Parse reports lost citation markers

The input likely lost superscript or structural marker information during conversion.

- Prefer the original PDF with text-layer glyph metadata or the DOCX.
- For HTML/Markdown, retain `<sup>...</sup>`, `^...^`, or Unicode raised digits.
- Inspect `parse_debug.md` and citation coverage.
- Do not guess flattened numbers back into citations.

`CITATION_VERIFIER_ALLOW_LOST_MARKERS=1` is an explicit escape hatch for a knowingly incomplete diagnostic parse. It does not recover lost citations and should not be used to claim complete coverage.

## Parse finds zero claims or references

Run Parse alone and inspect its debug view:

```bash
python run.py parse --input paper.pdf --debug parse_debug.md
```

Check that:

- the file has extractable text;
- the bibliography heading/structure is present;
- the citation scheme is detected correctly;
- the manuscript actually has in-text citation markers;
- OCR is available for a scanned PDF.

Do not weaken a zero-claim or coverage gate merely to advance the pipeline.

## Resolve leaves many references unresolved

Common causes are missing identifiers, provider/network failure, rate limiting, or weak bibliography text.

- Set `CITATION_VERIFIER_MAILTO` for polite-pool services.
- Check outbound DNS/TLS/HTTP access.
- Configure optional provider keys where appropriate.
- Inspect Resolve attempts rather than treating all `unresolved` results as `not_found`.
- Use manual Parse identity correction only when the bibliography itself is wrong and the correction is known.
- Supply a source through `provide` when you lawfully have the file.

## Fetch reports a network-blocked run

When no usable text was retrieved and at least one transient network failure occurred, the pipeline refuses to perform evidence-free verification.

1. Restore network access or wait for the provider outage/rate limit.
2. Resume the same run so prior attempts remain visible.
3. Alternatively, supply known sources through Fetch tasks or `provide`.

If some sources are usable, the run may continue while unresolved references remain explicitly listed as gaps.

## Full text is missing

Inspect pending Fetch tasks and `fetch_manual_sources.md`:

```bash
python run.py tasks list --run runs/<id> --slot fetch --status pending
```

Possible actions are:

- provide a file or text;
- provide a candidate URL for validated retrieval;
- ingest a folder and map a strongly corroborated file;
- process an OCR-queued scan;
- answer `not-found` honestly;
- select a regime that authorizes abstract-level evidence, if that matches the intended policy.

Do not use `--no-fetch` to make missing evidence look complete. It only suppresses the recovery pause.

## A publisher returns a challenge page

Automated Fetch records the challenge instead of storing the HTML shell as article text.

- Use `--challenge-mode queue` to create a browser task.
- Use an installed interactive/browser mode only when the environment and access policy allow it.
- Submit the resulting file/text through the typed task command.
- Do not bypass access controls or record a login page as evidence.

## OCR fails or produces unusable text

1. Install `requirements-ocr.txt` or a supported system OCR backend.
2. Select the correct language, such as `--ocr-lang eng+ita`.
3. Inspect the output before mapping it to a source.
4. If identity or text quality is inadequate, leave the source unreadable/unverified.

OCR cannot restore text or superscript distinctions that are not visible in the scan.

## RAG configuration fails

`extractive_rag`, and `auto` with medium/small profiles, require:

- the pinned dependency from `requirements-rag.txt`;
- a positive `CITATION_VERIFIER_MAX_SOURCE_CHARS`;
- a valid context mode/profile combination.

Fix the configuration and resume. The runtime intentionally does not fall back to arbitrary truncation.

## LLM requests fail with 401, 429, timeout, or transport errors

- `401`/credential errors: verify the selected backend's key or CLI login. Invalid credentials remain recorded.
- `429`: reduce `CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT`, add pacing, and review cooldown settings.
- timeouts: review `CITATION_VERIFIER_LLM_TIMEOUT` and tools timeout, but do not set unbounded values casually.
- unavailable model/lane: verify model IDs and Jury role selectors.

The runtime retries only within frozen technical caps. Exhaustion becomes an explicit terminal instead of switching provider policy silently.

## The report gate exits with code 20

First inspect state and tasks:

```bash
python run.py --run runs/<id> --status
python run.py tasks list --run runs/<id> --status pending
```

Then resume incomplete work. If all phases are complete but the report projection is stale, regenerate and gate it through supported commands:

```bash
python run.py report --run runs/<id>
python run.py verify --run runs/<id> --write-status
```

Typical hard failures are:

- source-bearing pairs without typed terminals;
- report body that does not match current DB projections;
- a broken or rewritten report journal;
- missing or invalid evidence grounding;
- a stale source hash/path;
- missing required HMAC signature;
- artifact-integrity failure.

Do not hand-edit the report or journal to repair the gate.

## `present` exits with code 21

`present` refuses to print an unverified or unauthenticated report. Run the completion gate and fix its reported cause. If signature is required, ensure the signing-key file is available to the trusted hook/CI process, then regenerate/gate through that authority.

## The HMAC signature is missing

Check that `CITATION_VERIFIER_SIGNING_KEY_FILE` points to a readable key in the trusted verification environment. The constrained agent should not have access to that key.

```bash
python run.py verify --run runs/<id> --require-signature --write-status
```

An ordinary content seal is not a substitute when the audit policy requires HMAC.

## Artifact integrity fails

An admitted database row, source file, task answer, or report artifact no longer matches its trusted checkpoint.

- Stop all writers.
- Identify and restore the exact original artifact if a trusted copy exists.
- Otherwise create a fresh run or an authorized child from a valid baseline.
- Use a debug override only for diagnosis and always provide a reason.

A diagnostic override labels the run; it does not make modified evidence trustworthy.

## A frozen-Fetch fork is rejected

The baseline must:

- exist and contain `run.sqlite`;
- be at a valid post-Fetch point;
- contain Parse/Resolve/source evidence;
- contain no completed/non-pending Verify work or pair state;
- include every applied manual Parse adjudication and required source asset.

The target directory must not exist. Create a new clean baseline with `--freeze-after-fetch` instead of forcing an incompatible fork.

## An older run database no longer opens

Callimachus is pre-release and does not promise general historical run-schema
compatibility. Preserve the old run for audit, then create a new run under the
current schema when current execution is required. The sole reporting exception
is the explicit read-only `report-html` adapter for a completed, sealed schema-59
run; it does not enable execution, migration, or compatibility with other schema
versions.

## Still unresolved

Capture the smallest evidence set before escalating:

```bash
python run.py --run runs/<id> --status --json-only
python run.py tasks list --run runs/<id>
python run.py verify --run runs/<id> --write-status
```

Report the exact command, exit code, phase, task ID where relevant, and the first deterministic failure. Do not attach secrets, the signing key, or an entire run database unless the receiving party is authorized for its contents.

---

Previous: [Capabilities and limits](08-capabilities-and-limits.md) · Next: [Keys and credentials](10-keys-and-credentials.md) · Index: [Complete guide](README.md).
