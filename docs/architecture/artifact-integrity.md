# Artifact integrity architecture in Callimachus

Callimachus uses an isolated integrity authority to detect changes to run data
and admitted source artifacts across the citation-verification pipeline. The
authority keeps the signing key and authoritative event ledger outside the
agent-writable workspace. It records trusted checkpoints as the run advances
and reports whether current artifacts still match those checkpoints.

This mechanism protects provenance and mutation history. It does not prove
that a source is true, that a citation supports a claim, or that a model's
verification judgment is correct. Those questions remain part of the
evidence-based verification process.

## Trust boundaries

The system has three distinct roles:

- **Agent account:** may invoke Callimachus and inspect permitted run data. It
  must not be able to edit the installed release, authority state, signing key,
  protected run root, or content-store root.
- **Trusted runner:** executes the installed pipeline under an administrator-
  controlled account. It receives only the authority needed for the official
  Callimachus process to perform its run-scoped mutations.
- **Integrity authority:** runs under a separate operating-system identity.
  It owns the key and ledger, listens on a Unix socket, authenticates peer
  credentials, and signs checkpoint and violation events.

The authority rejects configurations where its UID overlaps a trusted-writer
UID or a configured agent UID. The service also checks the installed worker
code at startup and activation. An allowlisted writer UID may activate an
official Callimachus process, but the allowlist alone does not grant raw write
authority: the service issues an in-memory capability bound to the process,
its start identity, the installed code digest, and one run directory. The
capability is required for protected mutation and is not stored in the run,
environment, or command line.

These controls depend on operating-system isolation. A hostile process sharing
the trusted runner's UID may be able to impersonate or inspect it where the
operating system permits. Production audit-ready deployment therefore keeps
the agent, runner, and authority in separate UID or container trust domains
and denies the agent general root access or write access to trusted launchers.

## What is checkpointed

A run checkpoint covers a canonical logical projection of the run database
and an inventory of files in the protected run directory. The projection
includes persisted input identity, configuration, pipeline results,
task-answer provenance, and report-boundary data. Source files are inventoried
by path, byte length, and SHA-256 digest. Symlinked or unstable artifacts are
rejected.

The database is not authenticated as raw SQLite bytes. Page order, journals,
and vacuum history are storage details, so the authority hashes a stable
projection of table schemas and rows instead. Volatile session data and the
checkpoint mirror itself are excluded from that projection.

The shared content store has its own global checkpoint chain. It is separate
from each run chain because an admitted shared-object update should not
invalidate unrelated runs. During operations that may read or update shared
content, Callimachus holds the required run and content-store leases and
records the relevant changes at their checkpoint boundaries.

The authority ledger is the source of truth. `run.sqlite` contains a relational
mirror of checkpoints and audit events so that the run can be inspected and
reported, but a local mirror cannot establish integrity by itself. On each
protected check, the service validates the current manifest against its latest
signed checkpoint and the application checks the mirror against the service's
history.

## Checkpoint and mutation flow

Before protected start or resume mutations, task-answer admission, Verify, and
Report work, Callimachus checks the run and, when relevant, the shared content
store against the authority's latest checkpoints. A mismatch stops the
protected operation before domain mutation. If the authority or its history
cannot be verified, the operation fails closed.

The official pipeline obtains a process-bound, run-scoped mutation capability
before writing protected state. It records successful transitions, pauses,
and admitted external inputs as new signed checkpoints linked to the prior
checkpoint. Task answers enter through controlled admission: the authority
binds the submitted data to the peer identity configured for the caller's
Unix UID. The `--agent-identity` option is the stable harness label stored on
the run; it is not a substitute for the authority's OS-level caller identity.

The run database mirrors signed checkpoints and reports for auditability, but
the authority remains authoritative when the two disagree. A local database
edit cannot create a valid checkpoint or replace the external ledger.

## Integrity and execution states

Integrity state and execution assurance answer different questions.

Run and content-store integrity use these states:

- **`clean`** — the current artifact manifest matches the latest trusted
  checkpoint.
- **`violated`** — one or more current artifacts differ from that checkpoint.
- **`debug_overridden`** — after a detected mismatch, the authority recorded a
  signed diagnostic override and established a diagnostic baseline. This
  history cannot be erased by a later clean checkpoint.
- **`unverifiable`** — the required authority or trusted history is missing,
  invalid, or unavailable.

Execution assurance records how a run was invoked:

- **`standalone_unattested`** — a fresh direct invocation without an agent
  identity. It is usable for standalone work, but not audit-ready.
- **`agent_attested`** — an agent-identified run with a healthy isolated
  authority.
- **`agent_unprotected_acknowledged`** — an agent invocation continued after
  the human explicitly acknowledged an attestation or integrity failure. The
  state records the reason and time and is irreversible for that run.

If agent attestation is unavailable, Callimachus presents a human-only choice
to continue unprotected or stop. An agent must relay that choice and cannot
answer it. Without an interactive terminal, the command stops. An acknowledged
unprotected run is not a debug override and cannot be audit-ready.

The dedicated `--debug-override-artifact-integrity` option is diagnostic. It
requires a non-empty operator reason. For a detected mismatch, the authority
must sign and persist the violation, differences, override, and resulting
checkpoint before execution can continue. The option cannot bypass an
unavailable authority or failed authority write. Debug mode is non-audit-ready
even when no mismatch was found; when a mismatch was overridden, the integrity
state also records `debug_overridden`.

## Report readiness

The deterministic report gate sets `audit_ready=true` only when run integrity
and content-store integrity are both clean, debug mode is off, and execution
assurance is `agent_attested`. Otherwise the report records the relevant
diagnostic state and cannot be presented as an audit-ready citation-verification
result.

This report status describes the integrity and provenance controls that
Callimachus applied. It does not replace review of the report's source evidence
or verdicts. A separate report HMAC, if configured, protects a completed
report; it does not provide checkpoints for earlier pipeline artifacts and
cannot replace the integrity authority.

## Deployment requirements

Audit-ready operation requires an immutable installed release, a separately
owned authority key and ledger, a protected run root, a protected content-store
root, and a Unix socket accessible to the configured clients. The authority
UID, trusted runner UID, and agent UIDs must not overlap. The agent must not be
able to alter the service, launchers, authority state, or trusted source code.

The [agent guide](../deployment/agent-guide.md) describes the invocation and
completion procedure. The [administrator guide](../deployment/administrator-guide.md)
describes installing and operating the isolated authority.
