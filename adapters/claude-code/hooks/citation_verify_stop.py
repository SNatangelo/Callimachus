#!/usr/bin/env python3
# adapters/claude-code/hooks/citation_verify_stop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
citation_verify_stop.py — Claude Code Stop hook. Two jobs, both from OUTSIDE the agent:

  A. Completion: a run that exists must pass the gate before the turn can end. Gate red →
     block (the agent must `run.py --resume`); gate green → sign the report and allow.
  B. Coverage of the "never launched it" case: if citation_arm.py armed a verification task
     (sentinel from the USER's words) and there is no signed, passing run for that
     manuscript, the hook itself LAUNCHES the autonomous runner — so even "press start"
     leaves the orchestrator's hands. If it cannot (no manuscript / no `claude` CLI), it
     blocks with instructions.

Wire alongside citation_arm.py in .claude/settings.json (see settings.example.json). Provide
the signing secret to the HOOK environment only (CITATION_VERIFIER_SIGNING_KEY[_FILE]). The
hook always exits 0; the decision travels as JSON on stdout. stdlib-only.
"""
import glob
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #

def _pkg_dir(cwd):
    """Repo root that contains core/ and run.py (from which the CLI runs)."""
    for base in (cwd, os.path.join(cwd, "citation-verifier")):
        if os.path.isfile(os.path.join(base, "run.py")):
            return base
    return None


def _cli(pkg, args):
    p = subprocess.run([sys.executable, os.path.join(pkg, "run.py"), *args], cwd=pkg,
                       capture_output=True, text=True)
    return p.returncode, p.stdout or "", p.stderr or ""


def _run_command(pkg, *args):
    """Render the runner command for the OS that executes this hook."""
    argv = [sys.executable, os.path.join(pkg, "run.py"), *args]
    if os.name != "nt":
        return shlex.join(argv)
    rendered = " ".join(_windows_argument(value) for value in argv)
    if subprocess.list2cmdline([sys.executable]).startswith('"'):
        rendered = "& " + rendered
    return rendered


def _windows_argument(value):
    rendered = subprocess.list2cmdline([value])
    if ("<" in value or ">" in value) and rendered == value:
        return f'"{value}"'
    return rendered


def _verify(pkg, run_rel, require_sig=False):
    args = ["verify", "--run", run_rel]
    if require_sig:
        args.append("--require-signature")
    try:
        rc, out, err = _cli(pkg, args)
    except Exception as exc:
        return {"ok": False, "failures": ["verify invocation failed: " + str(exc)], "info": {}}
    try:
        result = json.loads(out)
    except Exception:
        return {"ok": False, "failures": [err.strip() or "verify_run emitted invalid JSON"], "info": {}}
    if not isinstance(result, dict):
        return {"ok": False, "failures": ["verify_run emitted non-object JSON"], "info": {}}
    if rc != 0 or type(result.get("ok")) is not bool:
        return {"ok": False, "failures": result.get("failures") or
                [err.strip() or "verify_run returned an invalid result"], "info": result.get("info", {})}
    return result


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _runs(pkg):
    # A run dir is DB-native: SQLite (run.sqlite) is the system of record, no parse.json.
    return [d for d in glob.glob(os.path.join(pkg, "runs", "*"))
            if os.path.isfile(os.path.join(d, "run.sqlite"))]


def _manuscript_sha(run):
    # Read the manuscript sha from run.sqlite (run.input_sha256), read-only so a mere
    # detection pass never migrates or writes the user's run DB. Best-effort: any error
    # (missing/locked/malformed DB) returns None rather than crashing the Stop event.
    try:
        con = sqlite3.connect(f"file:{os.path.join(run, 'run.sqlite')}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT input_sha256 FROM run LIMIT 1").fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except Exception:
        return None


def _passing_run_for_sha(pkg, msha):
    """Newest run whose manuscript matches msha and whose gate passes (signed)."""
    cands = [r for r in _runs(pkg) if _manuscript_sha(r) == msha]
    for run in sorted(cands, key=os.path.getmtime, reverse=True):
        rel = os.path.relpath(run, pkg)
        if _verify(pkg, rel, require_sig=True).get("ok"):
            return rel
    return None


def _allow():
    print(json.dumps({}))
    sys.exit(0)


def _block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def _state_dir(cwd):
    """Where the armed sentinel lives. Defaults under the project (agent-writable), but a
    deployment can point CITATION_VERIFIER_STATE_DIR at a dir the agent's tools cannot
    write — so the agent cannot delete the sentinel to dodge Job B. citation_arm.py must
    resolve this identically."""
    return (os.environ.get("CITATION_VERIFIER_STATE_DIR")
            or os.path.join(cwd, ".citation_verifier"))


def _sentinel_path(cwd):
    return os.path.join(_state_dir(cwd), "pending.json")


def _clear_sentinel(cwd):
    try:
        os.remove(_sentinel_path(cwd))
    except OSError:
        pass


def _sign_and_allow(pkg, rel, cwd):
    """Gate green: apply the deterministic signature, confirm, then allow."""
    _cli(pkg, ["report", "--run", rel])
    if _verify(pkg, rel, require_sig=True).get("ok"):
        _clear_sentinel(cwd)
        _allow()
    _block("Citation-verifier could not sign run " + rel + " (signing key misconfigured "
           "in the hook environment). Fix CITATION_VERIFIER_SIGNING_KEY[_FILE], then retry.")


# --------------------------------------------------------------------------- #
#  main                                                                        #
# --------------------------------------------------------------------------- #

def _main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _block("Citation-verifier hook received invalid JSON input.")
    if not isinstance(payload, dict):
        _block("Citation-verifier hook input must be a JSON object.")
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str) or not cwd:
        _block("Citation-verifier hook input has an invalid working directory.")
    pkg = _pkg_dir(cwd)
    sentinel = None
    sp = _sentinel_path(cwd)
    if os.path.isfile(sp):
        try:
            with open(sp, encoding="utf-8") as f:
                sentinel = json.load(f)
        except Exception:
            _block("Citation-verifier pending sentinel is unreadable; verification is blocked.")

    # ---- Job B: a verification task was armed from the user's request ----
    if sentinel is not None:
        if not isinstance(sentinel, dict):
            _block("Citation-verifier pending sentinel has an invalid shape; verification is blocked.")
        manuscript = sentinel.get("manuscript")
        if manuscript is not None and (not isinstance(manuscript, str) or not manuscript):
            _block("Citation-verifier pending sentinel has an invalid manuscript; verification is blocked.")
        if manuscript is None:
            _block("A citation-verification task was requested but no manuscript file was "
                   "identified. Provide the manuscript file and run `" +
                   _run_command(pkg or cwd, "--input", "<manuscript>", "--autonomous") +
                   "` before ending the turn.")
        if not pkg:
            _block("A citation-verification task was requested but core/ is not reachable "
                   "from the working dir. cd into the citation-verifier skill dir and run "
                   "the command from that directory.")
        if manuscript and os.path.isfile(manuscript):
            msha = _sha256(manuscript)
            rel = _passing_run_for_sha(pkg, msha)
            if rel:
                _sign_and_allow(pkg, rel, cwd)          # already done → finalise
            if shutil.which("claude"):                  # the hook presses start itself
                _cli(pkg, ["--input", manuscript, "--autonomous",
                           "--accuracy", "standard", "--proceed"])
                rel = _passing_run_for_sha(pkg, msha)
                if rel:
                    _sign_and_allow(pkg, rel, cwd)
                _block("Auto-launch of the verifier for " + os.path.basename(manuscript) +
                       " did not produce a signed run. Run it manually: `" +
                       _run_command(pkg, "--input", manuscript, "--autonomous") +
                       "` and check your `claude` login.")
            _block("This is a citation-verification task with no signed run yet, and no "
                   "`claude` CLI to auto-run it. Execute `" +
                   _run_command(pkg, "--input", manuscript, "--autonomous") +
                   "` (or fill the interactive slots), then end.")
        else:
            _block("A citation-verification task was requested but no signed run exists. "
                   "Provide the manuscript file and run `" +
                   _run_command(pkg or cwd, "--input", "<manuscript>", "--autonomous") +
                   "` before ending the turn.")

    # ---- Job A: no armed task — just enforce completeness of an in-flight run ----
    runs = _runs(pkg) if pkg else []
    if not runs:
        _allow()                                        # skill not used → no-op
    run = max(runs, key=os.path.getmtime)
    rel = os.path.relpath(run, pkg)
    result = _verify(pkg, rel)
    if not result.get("ok"):
        fails = "; ".join(result.get("failures", [])) or "the run is incomplete"
        _block("Citation-verifier GATE FAILED for run " + rel + ": " + fails +
               ". You may NOT finish. Resume: `" +
               _run_command(pkg, "--run", rel, "--resume") +
               "`. Do not hand-write the report; only core.report may produce it.")
    _sign_and_allow(pkg, rel, cwd)


def main():
    try:
        _main()
    except Exception as exc:
        _block("Citation-verifier hook error: " + str(exc))


if __name__ == "__main__":
    main()
