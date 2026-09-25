# Agent guide for protected Callimachus runs

This guide is for an agent that runs Callimachus in a deployment configured
with the isolated artifact-integrity authority. It covers the agent's
preflight, run, resume, task-answer, and completion steps. It does not install
or configure the authority; an administrator must complete that work first.

For the system design, see [Artifact integrity architecture](../architecture/artifact-integrity.md).
For service installation and recovery, see the [administrator guide](administrator-guide.md).

## What the agent can claim

Callimachus can run without an authority for ordinary standalone use. A fresh
invocation without `--agent-identity` is recorded as `standalone_unattested`
and cannot produce `audit_ready=true`. An agent must supply its stable harness
identity, such as `codex` or `claude-code`, on every run, resume, fork, task
answer, and explicit report command that accepts that option.

The identity is a label for the invoking harness. It does not select a
verification model or provider. It must be 1–64 ASCII characters: the first
character is a letter or digit; later characters may also include `.`, `_`,
`:`, or `-`. Use the exact identity configured by the administrator for this
agent account. Keep it the same for the lifetime of a run; a resume cannot
change or omit the persisted identity.

An audit-ready report requires all of the following:

- the deterministic report gate passes;
- run integrity is `clean`;
- content-store integrity is `clean`;
- execution assurance is `agent_attested`;
- debug mode is off.

These checks establish the run's integrity and provenance state. They do not,
by themselves, prove that every source or citation is correct; the report's
verification findings still need to be read on their evidence.

## Administrator handoff

Before starting, obtain these non-secret values from the administrator:

- the immutable installed release root and its Python interpreter;
- the authority service unit path, if the deployment uses systemd;
- the exact authority-start launcher and runner launcher;
- the runner account name;
- the integrity Unix-socket path;
- the public authority-state path, for a non-writability check;
- the authority-key path, for a non-readability check only;
- the task inspection and answer commands permitted for this deployment;
- the exact agent identity mapped to this account.

The handoff must not include the authority key, ledger access, general root
access, or permission to edit the release, service, launchers, authority root,
or protected run and content-store roots. If any required value is missing,
ask the administrator to complete the handoff before proceeding.

The examples below show a Linux/systemd deployment. Replace every example
path and account with the exact values from the handoff. The commands assume
GNU `stat`, a Unix socket, and a site-approved `sudo` rule for the two
launchers. Never copy an example path into a deployment without checking it.

```bash
# Example values only; use the administrator's values.
RELEASE_ROOT=/opt/callimachus
RELEASE_PYTHON=/opt/callimachus/.venv/bin/python
SERVICE_UNIT=/etc/systemd/system/callimachus-integrity.service
AUTHORITY_START=/usr/local/sbin/callimachus-integrity-start
RUNNER_LAUNCHER=/usr/local/sbin/callimachus-runner
RUNNER_USER=callimachus-runner
INTEGRITY_SOCKET=/run/callimachus/integrity.sock
AUTHORITY_STATE=/var/lib/callimachus-authority/state
AUTHORITY_KEY=/var/lib/callimachus-authority/key
AGENT_ID=codex
```

Do not take these values from a repository file that the agent can edit.

## Preflight and authority start

Run the checks as the agent account, before launching the pipeline. A failed
check stops the audit-ready path; do not repair permissions or substitute a
different launcher.

```bash
test -d "$RELEASE_ROOT" && test ! -w "$RELEASE_ROOT"
test -f "$SERVICE_UNIT" && test ! -w "$SERVICE_UNIT"
test "$(stat -c %U "$SERVICE_UNIT")" = root
test -x "$AUTHORITY_START"
test "$(stat -c %U "$AUTHORITY_START")" = root
test ! -w "$AUTHORITY_START"
test -x "$RUNNER_LAUNCHER"
test "$(stat -c %U "$RUNNER_LAUNCHER")" = root
test ! -w "$RUNNER_LAUNCHER"
test ! -w "$AUTHORITY_STATE"
test ! -r "$AUTHORITY_KEY"

export CITATION_VERIFIER_INTEGRITY_SOCKET="$INTEGRITY_SOCKET"
sudo -n "$AUTHORITY_START"
test -S "$INTEGRITY_SOCKET"
```

The start command must be the exact narrow launcher supplied by the
administrator. A zero exit from these local checks establishes only that the
checked prerequisites passed; the first protected Callimachus operation must
still receive a healthy authority attestation. If the start command is not
allowed or the socket does not appear, ask the administrator to start or
repair the service. Do not run `core.infra.integrity.service init` or `serve`
from a checkout, and do not ask for unrestricted `sudo`.

## Start and resume

Run only the installed runner. Do not invoke `python run.py` from a writable
checkout for an audit-ready request. The input must be readable by the runner
account. Select a configured accuracy value and use the stable identity from
the handoff:

```bash
sudo -n -u "$RUNNER_USER" "$RUNNER_LAUNCHER" \
  --input "$MANUSCRIPT_PATH" --accuracy standard \
  --agent-identity "$AGENT_ID"
```

Record the absolute run path printed by Callimachus as `RUN_PATH`. Resume that
same run through the runner and retain the identity:

```bash
sudo -n -u "$RUNNER_USER" "$RUNNER_LAUNCHER" \
  --run "$RUN_PATH" --resume --agent-identity "$AGENT_ID"
```

Do not omit the identity when resuming. A run created by a standalone human
invocation cannot later be relabelled as an attested agent run. Derived child
runs are separate executions and do not change their parent; if a source run
was unattested, Callimachus requires fresh human acknowledgement for the
agent-derived child.

## Paused tasks

Use the installed release's task inspector, not a checkout, to list or inspect
tasks. These read-only commands do not accept an agent identity:

```bash
cd "$RELEASE_ROOT"
"$RELEASE_PYTHON" run.py tasks list --run "$RUN_PATH"
"$RELEASE_PYTHON" run.py tasks show --run "$RUN_PATH" --task "$TASK_ID"
```

For a healthy attested run, submit only answers allowed by the administrator.
The authority admits the answer and records the producer identity from the
calling Unix account. For example, if the task asks for a retrieved source
file:

```bash
"$RELEASE_PYTHON" run.py tasks answer-fetch \
  --run "$RUN_PATH" --task "$TASK_ID" --file-path "$SOURCE_FILE" \
  --agent-identity "$AGENT_ID"
```

For a Research task, use the source-based finding format accepted by the CLI:

```bash
"$RELEASE_PYTHON" run.py tasks answer-research \
  --run "$RUN_PATH" --task "$TASK_ID" \
  --finding "$URL|$STANCE|$QUOTE_OR_QUOTE_FILE" \
  --agent-identity "$AGENT_ID"
```

Use material actually retrieved from the cited source. Do not invent a source
or treat an agent identity flag as proof of source authorship. If an answer
command reports that the trusted runner is required, rerun that exact task
through the administrator-approved runner launcher, preserving the same
identity:

```bash
sudo -n -u "$RUNNER_USER" "$RUNNER_LAUNCHER" tasks answer-fetch \
  --run "$RUN_PATH" --task "$TASK_ID" --file-path "$SOURCE_FILE" \
  --agent-identity "$AGENT_ID"
```

Use `answer-research` in the same way for a Research task. Never grant the
agent direct write access to `run.sqlite`. Source-identity attestations and
guided Fetch answers require a separately authenticated human; an agent must
not submit them as if it were that human.

## Human decisions and diagnostic runs

If identity attestation or an integrity check fails, Callimachus presents a
human-only choice to continue unprotected or stop. Relay the alert and choice
to the human without selecting an answer. If there is no interactive terminal,
stop and ask the human; continue only after they decide, by rerunning through
an interactive terminal so Callimachus can record that decision.

An acknowledged unprotected run is permanently not audit-ready. The report
shows `agent_unprotected_acknowledged` and a warning. This is separate from a
debug override. Do not use `--debug-override-artifact-integrity` to bypass a
missing authority, bad ownership, overlapping UIDs, changed release code, or
failed attestation. A debug override requires an operator reason and a trusted
authority write; it is diagnostic and never audit-ready.

If any preflight or authority check fails, do not fix the trusted side. Ask an
administrator to repair the service, socket, ownership, UID mapping, release,
content-store enrolment, or protected paths. The human may choose a diagnostic
run only through Callimachus's own prompt.

## Completion

Read the final deterministic report. For an audit-ready result, it must pass
the report gate and show:

```text
Audit readiness: yes
run integrity clean
content-store integrity clean
Execution assurance: agent_attested
```

If any value differs, present the run as diagnostic or incomplete and explain
the reported state. Do not rewrite, summarise as verified, or reseal a report
that fails the gate.
