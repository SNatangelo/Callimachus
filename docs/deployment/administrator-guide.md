# Administrator guide: deploy the integrity authority

Callimachus uses an external artifact-integrity authority to protect run
artifacts and the shared content store. A report can declare `audit_ready=true`
only when both integrity subjects are clean. This guide describes how a trusted
host administrator provisions that authority. It is separate from optional
report-HMAC and Claude hook setup in [`DEPLOYMENT.md`](../../DEPLOYMENT.md).

Provision the service outside an agent session. An agent with unrestricted
`sudo` or root access, write access to the installed release or systemd unit,
or credentials for the authority account can replace the trust boundary and
cannot produce an audit-ready assurance. For agent invocation after setup, see
the [agent guide](agent-guide.md); for the assurance model, see the
[artifact-integrity overview](../architecture/artifact-integrity.md).

The examples target Linux with systemd, Unix sockets, `SO_PEERCRED`, and
`/proc`. WSL2 is suitable only when systemd is enabled and every protected path
is on the Linux filesystem. Keep the installed release, authority, key, ledger,
protected runs, and content store off `/mnt/c`, OneDrive, and other
agent-writable mounted workspaces.

## Security boundary

The boundary is created by the operating system, not by an agent prompt.

- A trusted administrator installs one reviewed Callimachus release at a
  root-owned, agent-read-only path such as `/opt/callimachus`.
- A dedicated `callimachus-authority` UID runs the authority and owns its key
  and ledger.
- A distinct `callimachus-runner` UID runs the official `run.py` entry point.
- Codex, Claude Code, and human operators use other UIDs. Each agent UID is
  mapped explicitly by the authority.
- Agents may connect to the socket and may invoke narrowly scoped, root-owned
  launchers. They cannot write the release, service unit, launchers, authority
  root, key, or ledger.

An agent with unrestricted `sudo`, root access, write access to the installed
release or systemd unit, or credentials for the authority UID remains able to
replace the trust boundary. Such a session cannot produce an audit-ready
assurance, regardless of what its instructions say. Remove that authority or
perform provisioning from a separate administrator session before relying on
the result.

Starting a preinstalled root-owned service is safe to delegate. Installing or
rewriting that service is not.

## Deployment identities and paths

Use site-specific names if required, but preserve the separation:

| Purpose | Example | Writable by an agent? |
| --- | --- | --- |
| Reviewed release | `/opt/callimachus` | No |
| Authority UID | `callimachus-authority` | No impersonation |
| Runner UID | `callimachus-runner` | Only through a fixed launcher |
| Authority state and ledger | `/var/lib/callimachus-authority/state` | No |
| Authority key | `/var/lib/callimachus-authority/key` | No read or write |
| Protected runs | `/var/lib/callimachus-runner/runs` | Runner only |
| Content-store state | `/var/lib/callimachus-runner/state` | Runner only |
| Unix socket | `/run/callimachus/integrity.sock` | Connect only |

If Codex and Claude Code share one Unix UID, `SO_PEERCRED` cannot distinguish
them. Map that UID to one honest shared identity, or run the tools under
separate UIDs/containers when per-agent attribution matters.

## One-time administrator provisioning

Run this section as a trusted host administrator, outside the agent session.
Replace every angle-bracket placeholder before installing the service unit.
Use decimal UIDs (for example, the output of `id -u <AGENT_USER>`), and choose
the stable authority and agent identities used on this host. Review the
release and paths before provisioning.

### 1. Install immutable code and identities

Install a reviewed release in `/opt/callimachus`, including its virtual
environment, as root. The installed files and every parent directory must be
non-writable by runner and agent UIDs.

```bash
sudo groupadd --system callimachus-ipc
sudo useradd --system --user-group --home-dir /var/lib/callimachus-authority \
  --shell /usr/sbin/nologin callimachus-authority
sudo useradd --system --user-group --home-dir /var/lib/callimachus-runner \
  --shell /usr/sbin/nologin callimachus-runner

sudo chown -R root:root /opt/callimachus
sudo chmod -R go-w /opt/callimachus
```

Add every UID that must connect to the socket to `callimachus-ipc`. Do not add
those UIDs to either service account's private group.

```bash
sudo usermod -aG callimachus-ipc callimachus-authority
sudo usermod -aG callimachus-ipc callimachus-runner
sudo usermod -aG callimachus-ipc <AGENT_USER>
```

Create roots with no group or world write permission. The setgid bit preserves
the read/traverse group on child run files; the authority service receives only
the filesystem capability needed to inspect and restore these roots.

```bash
sudo install -d -o callimachus-authority -g callimachus-authority -m 0700 \
  /var/lib/callimachus-authority \
  /var/lib/callimachus-authority/state
sudo install -d -o callimachus-runner -g callimachus-ipc -m 2750 \
  /var/lib/callimachus-runner \
  /var/lib/callimachus-runner/runs \
  /var/lib/callimachus-runner/state
```

The authority still needs to persist its narrow admission/checkpoint and
recovery updates in runner-owned state. Do not grant that access with a
writable group or POSIX ACL: Callimachus deliberately rejects a protected root
whose group permission class is writable. The systemd unit below instead gives
only the authority process `CAP_DAC_OVERRIDE`/`CAP_DAC_READ_SEARCH`, bounded by
`ProtectSystem=strict` and the explicit `ReadWritePaths`. The runner remains
the root owner, `callimachus-ipc` remains read/traverse-only, and the agent UID
receives no database write access. A non-systemd deployment needs an equivalent
root-managed capability and filesystem sandbox; otherwise it is not an
isolated, audit-ready deployment.

### 2. Initialise the authority key once

`init` never overwrites an existing key. Run it as the authority UID from the
immutable release:

```bash
sudo -u callimachus-authority /bin/sh -c '
  cd /opt/callimachus
  exec ./.venv/bin/python -m core.infra.integrity.service init \
    --authority-root /var/lib/callimachus-authority/state \
    --key-file /var/lib/callimachus-authority/key
'
```

Required postconditions:

```bash
sudo stat -c '%U %G %a %n' \
  /var/lib/callimachus-authority/state \
  /var/lib/callimachus-authority/key
# expected owner: callimachus-authority; modes: 700 and 600
```

Never expose the key through `.env`, an agent shell, a command argument, a
repository secret file, or a launcher environment.

### 3. Bootstrap the fresh content store

The authority enrolment operation intentionally requires an existing, valid
content-store database. Before the first authority start, create a fresh store
as the runner UID using the installed release and its public preflight API:

```bash
sudo -u callimachus-runner /bin/sh -c '
  cd /opt/callimachus
  export CITATION_VERIFIER_STATE_DIR=/var/lib/callimachus-runner/state
  exec ./.venv/bin/python -c \
    "from core.fetch.storage import content_store; print(content_store.preflight())"
'
```

Confirm that
`/var/lib/callimachus-runner/state/content_store/content.sqlite` exists and is
owned by the runner. Do not seed it from a legacy or agent-writable store.

### 4. Install the systemd service

Create `/etc/systemd/system/callimachus-integrity.service` as root. Replace all
angle-bracket placeholders before installation. Repeat `--peer-identity` for
each distinct agent UID.

```ini
[Unit]
Description=Callimachus artifact-integrity authority
After=local-fs.target

[Service]
Type=simple
User=callimachus-authority
Group=callimachus-ipc
UMask=0027
RuntimeDirectory=callimachus
RuntimeDirectoryMode=0750
WorkingDirectory=/opt/callimachus
ExecStart=/opt/callimachus/.venv/bin/python -m core.infra.integrity.service serve \
  --socket /run/callimachus/integrity.sock \
  --authority-root /var/lib/callimachus-authority/state \
  --key-file /var/lib/callimachus-authority/key \
  --protected-root /var/lib/callimachus-runner/runs \
  --content-store-root /var/lib/callimachus-runner/state/content_store \
  --authority-id <HOST_STABLE_AUTHORITY_ID> \
  --trusted-writer-uid <CALLIMACHUS_RUNNER_UID> \
  --peer-identity <CODEX_UID>=agent:codex \
  --peer-identity <CLAUDE_UID>=agent:claude-code
Restart=on-failure
RestartSec=2

# The authority must read and restore runner-owned protected artifacts without
# making those roots group-writable.
CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH
AmbientCapabilities=CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH
NoNewPrivileges=yes

ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
ReadOnlyPaths=/opt/callimachus
ReadWritePaths=/var/lib/callimachus-authority
ReadWritePaths=/var/lib/callimachus-runner
ReadWritePaths=/run/callimachus

[Install]
WantedBy=multi-user.target
```

The service intentionally fails before binding the socket when its own UID
matches a trusted-writer or peer-identity UID.

Validate and start it from the administrator session:

```bash
sudo systemd-analyze verify \
  /etc/systemd/system/callimachus-integrity.service
sudo systemctl daemon-reload
sudo systemctl enable --now callimachus-integrity.service
sudo systemctl --no-pager --full status callimachus-integrity.service
```

### 5. Install immutable launchers

The agent may start the already installed authority, but it must not receive a
general service-management or root shell capability. Install this root-owned
launcher as `/usr/local/sbin/callimachus-integrity-start`:

```sh
#!/bin/sh
set -eu
exec /bin/systemctl start callimachus-integrity.service
```

Install a second root-owned launcher as
`/usr/local/sbin/callimachus-runner`:

```sh
#!/bin/sh
set -eu
umask 027
unset ENV BASH_ENV CDPATH PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONINSPECT
unset PYTHONBREAKPOINT LD_PRELOAD
export PYTHONDONTWRITEBYTECODE=1
export CITATION_VERIFIER_INTEGRITY_SOCKET=/run/callimachus/integrity.sock
export CITATION_VERIFIER_STATE_DIR=/var/lib/callimachus-runner/state
cd /var/lib/callimachus-runner
exec /opt/callimachus/.venv/bin/python /opt/callimachus/run.py "$@"
```

Both files and `/usr/local/sbin` must be root-owned and non-writable by agents.
Grant only the exact start and runner commands through sudoers or PolicyKit.
For example, after validation with `visudo`:

```bash
sudo chown root:root \
  /usr/local/sbin/callimachus-integrity-start \
  /usr/local/sbin/callimachus-runner
sudo chmod 0755 \
  /usr/local/sbin/callimachus-integrity-start \
  /usr/local/sbin/callimachus-runner
```

```sudoers
<AGENT_USER> ALL=(root) NOPASSWD: /usr/local/sbin/callimachus-integrity-start
<AGENT_USER> ALL=(callimachus-runner) NOPASSWD: /usr/local/sbin/callimachus-runner *
```

Do not grant `systemctl`, a Python interpreter, a shell, file-copy tools,
`sudoedit`, `daemon-reload`, or arbitrary commands. The runner launcher quotes
all arguments and executes only the immutable `run.py`.

### 6. Enrol the content store

After the first service start, initialise the selected content store through
the official administrative entry point under the trusted runner UID:

```bash
sudo -u callimachus-runner /bin/sh -c '
  cd /opt/callimachus
  export CITATION_VERIFIER_INTEGRITY_SOCKET=/run/callimachus/integrity.sock
  export CITATION_VERIFIER_STATE_DIR=/var/lib/callimachus-runner/state
  exec ./.venv/bin/python \
    -m core.infra.integrity.service enroll-content-store \
    --socket /run/callimachus/integrity.sock
'
```

Do not enrol a legacy or agent-writable content store.

## Fail-closed acceptance checks

Run these checks as the actual agent UID, not as root:

```bash
test -d /opt/callimachus && test ! -w /opt/callimachus
test -f /etc/systemd/system/callimachus-integrity.service
test ! -w /etc/systemd/system/callimachus-integrity.service
test -x /usr/local/sbin/callimachus-integrity-start
test "$(stat -c %U /usr/local/sbin/callimachus-integrity-start)" = root
test ! -w /usr/local/sbin/callimachus-integrity-start
test -x /usr/local/sbin/callimachus-runner
test "$(stat -c %U /usr/local/sbin/callimachus-runner)" = root
test ! -w /usr/local/sbin/callimachus-runner
test ! -r /var/lib/callimachus-authority/key
test ! -w /var/lib/callimachus-authority/state
sudo -n /usr/local/sbin/callimachus-integrity-start
test -S /run/callimachus/integrity.sock
```

Also confirm the agent has no general sudo/root route. A failed check is a
deployment failure: do not start an audit-ready run and do not use the debug
override to continue.

For a smoke run, invoke only the immutable runner:

```bash
sudo -n -u callimachus-runner \
  /usr/local/sbin/callimachus-runner \
  --input <MANUSCRIPT_READABLE_BY_RUNNER> --accuracy standard \
  --agent-identity <STABLE_AGENT_IDENTITY>
```

Complete and present the run through the same installed release. The final
report must show both integrity subjects as `clean` and `audit_ready=true`.
`unverifiable`, `violated`, or `debug_overridden` is never an audit-ready
success.

## Upgrades and recovery

An agent must not restart the authority against a modified working checkout.
For an upgrade, the administrator installs a newly reviewed immutable release,
updates the root-owned unit, preserves the existing authority key and ledger,
validates the service, and only then exposes the launcher. The `init` command
does not replace an existing key; do not generate a replacement key in place
or delete the ledger to clear a failure. If key or ledger state is damaged,
stop audit-ready runs and restore a consistent protected backup.

Runs, keys, and ledgers created before UID isolation or under an agent-writable
release cannot become trustworthy retroactively. Preserve them as diagnostic
records or regenerate the run under the isolated deployment.
