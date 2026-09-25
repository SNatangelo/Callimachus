# Deployment — turning the cage on

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

**Optional — put the armed sentinel out of the agent's reach.** The arm hook writes a
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

**What this does and does not buy you** is in the README. In short: with the key isolated and
the hooks at user level, an agent cannot skip verification, hand-write or forge a report, or
end its turn on an incomplete run. It still must *start* the process (reduced to a binary,
auto-launchable fact), and passage *relevance* remains the model's judgment.
