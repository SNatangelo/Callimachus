# Deployment — turning the cage on

## Minimum supported desktop systems

Desktop builds must support Windows 10 version 1809 (build 17763) or later,
macOS 14.4 or later, and GNU/Linux with glibc 2.35 or later, using Ubuntu 22.04
LTS as the qualification baseline. These are persistent user requirements. Linux
binaries must be built and tested against Ubuntu 22.04 or a demonstrably
equivalent baseline; building only on a newer distribution does not qualify this
minimum. Raising any minimum requires an explicit user request.

Qt/PySide/Shiboken 6.12 is authorized on all desktop platforms. The exact release
pins are in `packaging/release-constraints.txt`; the matching source archives and
hashes are in `packaging/release-sources.json`. Keep those files consistent when
updating a dependency. Follow the maintained
[desktop build instructions](docs/maintainers/packaging/desktop-build.md#maintainer-build-procedure),
including the macOS 14.4 binding qualification and the Windows Store CRT rules.

## Installed desktop settings

Compiled desktop releases save credentials in the current user's operating
system vault: Windows Credential Manager, macOS Keychain, or a running Linux
Secret Service. They do not fall back to plaintext when the vault is unavailable
or locked. Non-secret settings use `settings.conf` in the Callimachus user-data
directory; the bundled read-only inventory is `settings.defaults`. Old `.env`
files are neither loaded nor migrated by compiled releases. Re-enter required
credentials in the Jury chooser or Settings; old files are not automatically
deleted. Genuine process overrides retain priority, and existing explicit
signing-key-file references retain their file-based behavior. See
[Desktop packages](docs/user/en/09-desktop-packages.md#settings-and-credentials-in-20).

Settings export retains its existing optional password encryption. Choose the
encrypted export when the backup includes credentials. Source/development use
keeps the `.env` configuration and CLI setup described below. The
`python run.py configure` tool is source-only; a compiled app generates its local
report-signing key through Settings into the vault. That local key does not
replace the external integrity authority described below.

## Invocation modes

Fresh direct human CLI use omits `--agent-identity` and remains usable without
an authority as `standalone_unattested`; it is never audit-ready. Adapters pass
their stable identity (for example, `--agent-identity codex` or
`--agent-identity claude-code`) on run/resume/fork/task-answer/report commands.
Agent identity
attestation failures show an alert and ask the human. The agent relays the
question and never answers autonomously: yes irreversibly records an
unprotected, unreliable, non-audit-ready run; no stops. No TTY means stop and
ask the human, then rerun with a PTY; there is no `--yes` bypass.

## Two distinct integrity boundaries

Callimachus now has two complementary deployment layers:

1. **[Artifact integrity authority](docs/architecture/artifact-integrity.md)** — required for an audit-ready
   `parse → resolve → fetch → verify → report` run. It uses a separate OS UID,
   an external key and ledger, an immutable release, and a Unix-socket service.
   Follow the administrator runbook in
   [Administrator guide](docs/deployment/administrator-guide.md)
   and install the agent procedure from
   [Agent guide](docs/deployment/agent-guide.md).
2. **Report-HMAC and agent hooks** — the setup described below. It helps force
   completion and protects a final report signature, but it does not replace
   the artifact authority and cannot by itself produce `audit_ready=true`.

The setup GUI and `run.py configure` currently configure only layer 2. They do
not create a separate authority UID, install an immutable release, or protect
the authority key and ledger.

For the report-HMAC and hook layer below, the driver, gate, signature and hooks
only *bind* the agent once **installed correctly**. Those guarantees rest on
one thing the code cannot enforce for you:
**the signing key and the hooks must live where the agent cannot reach them.** This page is
how to set that up. (For what is and isn't guaranteed, see the README's "Making the gate
un-skippable" + the load-bearing note.)

## 0. The easy way — configuration and key generation

A single tool selectively updates `.env`, generates the signing key (chmod 600), and self-tests. Claude Code hook instructions are prepared only when explicitly requested; they are a manual patch, not an installation. A window (tkinter, no install) or headless on a server:

```bash
cd citation-verifier
python3 setup_gui.py              # opens a window: email, Google Books key, regime + the cage
# or, no display (CI / ssh):
python run.py configure --headless --mailto you@example.org --accuracy standard \
    --gen-key                     # add --google-key ... to enable the book preview tier
```

The window has a “How to get one ↗” button for the Google Books key, and the regime dropdown
explains each grade. If you explicitly select Claude Code hook instructions, the generated
manual patch places the signing key only into the **Stop hook's** environment, not the agent's
shell. The rest of this page is the same setup done by hand, plus the why.

> tkinter ships with the official Python installers (Windows/macOS); on some Linux distros it
> is a separate package (`sudo apt install python3-tk`). No display (server/ssh)? Use
> `--headless` — same logic, no window.

## 1. Create a signing key (once)

```bash
sudo mkdir -p /etc/citation-verifier
python3 -c "import secrets; print(secrets.token_hex(32))" | sudo tee /etc/citation-verifier/signing.key >/dev/null
sudo chmod 600 /etc/citation-verifier/signing.key      # readable only by the trusted user
```

The key seals every approved report (HMAC). Anyone with it can both sign and verify, so it
must **not** be readable from the agent's shell. A root-owned `chmod 600` file is the simplest
boundary; on a hosted runner use the platform's secret store instead.

## 2. Point the trusted side at the key — NOT the agent's shell

The key must be present for the **hook / CI** process and absent from the agent's interactive
environment. Prefer the file form (the agent can `env`, but not read a 600 file it doesn't own):

```bash
# In the hook / CI environment only:
export CITATION_VERIFIER_SIGNING_KEY_FILE=/etc/citation-verifier/signing.key
```

Do **not** put `CITATION_VERIFIER_SIGNING_KEY[_FILE]` in the project `.env`, in
`~/.bashrc` loaded by the agent, or anywhere the agent's `Bash` tool reads. If it leaks into
the agent's env, the HMAC becomes decorative (the agent could re-sign a fabricated run).

## 3. Wire the hooks at USER level (so the agent can't disable them)

Put the hooks in **user** settings (`~/.claude/settings.json`), not in the repo's
`.claude/settings.json` — a repo file is writable by the agent, which could remove its own
cage. Copy from `adapters/claude-code/settings.example.json`:

```jsonc
{
  "hooks": {
    "UserPromptSubmit": [ { "hooks": [ { "type": "command",
      "command": "python3 /abs/path/citation-verifier/adapters/claude-code/hooks/citation_arm.py" } ] } ],
    "Stop":             [ { "hooks": [ { "type": "command",
      "command": "python3 /abs/path/citation-verifier/adapters/claude-code/hooks/citation_verify_stop.py" } ] } ]
  }
}
```

Use **absolute paths** to the hook scripts. The `Stop` hook needs the signing key in its
environment (step 2) so it can seal an approved report; if you run Claude Code such that
hooks inherit a separate environment, set the key there.

**Required for an agent-resistant deployment — put the armed sentinel out of the agent's reach.** The arm hook writes a
sentinel (`pending.json`) that tells the Stop hook a verification was requested. By default
it lives under the project (`./.citation_verifier/`), which the agent's tools *can* write —
so a determined agent could delete it within a turn to dodge the auto-launch. To close that,
point both hooks at a directory the agent cannot write, via the **hook environment** only:

```bash
export CITATION_VERIFIER_STATE_DIR=/var/run/citation-verifier   # writable by the hook, not the agent
```

Both `citation_arm.py` and `citation_verify_stop.py` read this identically. Even without it,
the arm hook re-arms on the *next* user prompt and the Stop hook still enforces any in-flight
run — this only narrows a within-turn window.

## 4. Config (not secret) — the `.env`

The three Phase 0 knobs are *not* secrets and may live in a project `.env` (git-ignored):

```bash
cp .env.example .env        # CITATION_VERIFIER_ACCURACY / _MAILTO / GOOGLE_BOOKS_API_KEY
set -a; . ./.env; set +a
```

Keep these separate in your head from the **signing key**, which is the only value that must
be hidden from the agent.

## 5. Verify the cage is live

```bash
# A. The gate rejects an unsigned/hand-written report:
python run.py verify --run runs/<ts> --require-signature   # exit 20 if not HMAC-signed

# B. The Stop hook blocks an incomplete run: start a run, leave a pair unanswered, and end
#    the turn — Claude Code should refuse to stop and tell you to resume.

# C. Autonomous, end-to-end on your subscription (no separate API key):
python run.py --input paper.pdf --autonomous --accuracy standard --proceed
```

If A returns 0 for a report you wrote by hand, the key is reachable by the writer — recheck
step 2.

## 6. CI / external audit

In CI, give the job the key (step 2) and gate the artifact:

```bash
python run.py verify --run runs/<ts> --require-signature || exit 1
```

A report not signed by the deterministic system fails the build. That is the same check the
Stop hook applies, now enforced by infrastructure the agent never touches.

---

**What this does and does not buy you** is in the README. In short: with the key, state directory,
hook scripts and hook settings isolated from the agent as described above, the protected
deployment prevents skipping the armed verification, hand-writing or forging a report, or
ending a turn on an incomplete run. It still must *start* the process (reduced to a binary,
auto-launchable fact), and passage *relevance* remains the model's judgment.



## Security qualification of external CLI backends

The `codex_cli` adapter requests a read-only sandbox and a process timeout.
Those controls do not qualify the set of readable files. The adapter inherits
its working directory and environment and does not disable the CLI's own tools,
configuration or MCP connections. `supports_tools=False` describes Callimachus
adapter capabilities, not the tools available inside the CLI process.

Before treating this backend as isolated from local confidential files, qualify
the installed CLI version and its effective OS filesystem/network policy with
non-secret fixtures: an allowed citation file must remain readable, an excluded
fixture must not be readable, and configured tools/MCP must not expand that
scope. Keep authentication outside model-readable file access. Do not use real
secrets for qualification. Until that qualification passes, this backend is not
qualified for that protected deployment; explicitly select an existing backend
whose access scope meets the deployment requirements. Do not silently substitute
another provider. No installed CLI or host policy is certified by the source tests.

For Claude hooks, configuring `CITATION_VERIFIER_STATE_DIR` is necessary but does
not prove isolation: the agent must also be unable to alter that directory, its
ancestors, either hook script, hook settings, or the signing key. Check effective
permissions for the actual agent identity. A directory outside the project with
the same writable permissions does not close the sentinel-deletion path.


## Automatic retrieval on institutional networks

Automatic HTTP(S) source retrieval and bibliographic resolution use a public-only
connection policy. Each new connection validates the complete DNS answer and
connects to a validated numeric address without a second hostname lookup.
Redirects and cookie-session retries use the same policy. Local, private,
link-local, multicast, reserved and other nonpublic destinations are refused;
non-HTTP(S) URLs and embedded URL credentials are refused as well.

The policy keeps the original hostname for HTTP Host, TLS SNI and certificate
verification. It uses the system resolver and OS routing without choosing an
interface or bypassing a VPN. Institutional VPN access to publicly addressed
publishers therefore keeps its normal route and source-IP recognition. Native
public IPv6 and standard NAT64 addressing of public IPv4 destinations are
supported; conservative checks exclude other ambiguous transition prefixes.

On a desktop-launched analysis, an eligible private RFC1918/ULA destination or
a selected proxy requests an explicit decision in the owning desktop window.
The options are Cancel, Allow once, Remember for this session (until the desktop
closes), and Always remember. The permission binds the origin, connection host
and port, complete resolved numeric address set, and proxy/direct route. A changed
origin, endpoint or address set requires a new decision. Loopback resources,
metadata services, link-local and other prohibited destinations remain blocked;
a loopback proxy endpoint may be explicitly approved as a proxy only.

Session grants are window memory; permanent grants are user QSettings entries.
Settings > Network lists remembered grants and revokes them. Persistent grants
are saved before use. Revocation blocks reuse immediately and retains retry state
if writing the removal fails. The desktop and child exchange authenticated,
request-scoped decisions; the IPC key is removed from the child environment before
provider processes can inherit it. The run records the decision before connecting;
failing to record it denies the connection. Closing/STOP/timeout denies pending
requests. A non-desktop process without the owner channel has no implicit grant.
Decisions use dedicated append-only SQLite relations in current run schema 118,
separate from pipeline phase events. Create a fresh run for this schema; older
run databases are not migrated or deleted.

A proxy chooses the origin's final address outside the application's control;
the confirmation explains that trust delegation. The proxy is not silently
ignored and there is no automatic direct fallback. A system NO_PROXY/bypass
decision still allows a validated direct connection. Approval of network access
does not attest source identity: all existing identity/provenance gates still apply.
The separate institutional/assisted-browser and explicit local-source workflows
remain available. A real institution/VPN/provider login, native QSettings and a
packaged Qt dialog have not been exercised by the isolated source regressions.
