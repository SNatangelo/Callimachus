#!/usr/bin/env python3
# core/app/commands/present.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
present.py — the deterministic system shows the report; the model never does.

The report is the authoritative artifact. If the LLM retypes or "summarizes" it, that prose
is no longer the verified output — it is the model talking, exactly what this tool exists to
prevent. So the report reaches the user through THIS command's stdout, not through
the assistant's message. The flow is:

  1. verify the run actually passed the gate (and the signature, when a key is configured);
     refuse to present anything that did not — there is no "show it anyway".
  2. print the verified report between clear banners. Escape terminal control characters
     in the display only, labelling that view; leave report.md and its signature unchanged.
  3. print a divider. ONLY BELOW it may the model add its interpretation, clearly labelled
     as commentary — never mixed into, nor passed off as, the verified report.

Usage:
  python run.py present --run runs/<ts>
Exit 0 = presented; 21 = refused (run not verified/authentic).
"""
from __future__ import annotations

import argparse
import os
import sys

from core.invocation import format_run_examples, run_command

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from core.verify import verify_run as _verify_run
    from core.infra.integrity import signing as _signing
except ImportError:  # direct execution
    import verify_run as _verify_run
    import signing as _signing

REFUSED = 21
_BAR = "=" * 72


def present(run_dir) -> tuple[bool, str]:
    """Return (ok, text). text is either the framed verified report or a refusal."""
    report_path = os.path.join(run_dir, "report.md")
    # A key in the environment means we are in a trusted/audit context: demand the HMAC.
    result = _verify_run.verify(run_dir, require_signature=_signing.key_present())
    if not result["ok"]:
        lines = [
            _BAR,
            "REPORT WITHHELD - the run did not pass verification.",
            _BAR,
            "This report is NOT shown because it is incomplete or not authentic:",
        ]
        lines += [f"  x {f}" for f in result["failures"]]
        lines.append("")
        lines.append(f"Finish the run ({run_command('--resume')}) - do not paste or "
                     "summarise an unverified report.")
        return False, "\n".join(lines)

    if not os.path.exists(report_path):
        return False, f"REPORT WITHHELD - report.md not found; run {run_command('report')}."

    with open(report_path, encoding="utf-8") as f:
        body = f.read()

    info = result["info"]
    sig = info.get("report_seal_alg", "?")
    verified = info.get("pairs_with_text_verified", "?")
    no_text = info.get("pairs_no_text", "?")
    created = info.get("report_created_at", "?")
    head = [
        _BAR,
        "VERIFIED CITATION REPORT - shown verbatim by the deterministic system",
        f"run: {os.path.basename(os.path.normpath(run_dir))} | seal: {sig} | "
        f"pairs verified: {verified} | without text: {no_text} | snapshot: {created}",
        "Everything between these banners is the signed, code-produced report.",
        _BAR,
        "",
    ]
    if _terminal_safe(body) != body:
        head[1] = "VERIFIED CITATION REPORT - terminal-safe view by the deterministic system"
        head[3] = "Control characters are escaped in this view; the verified report.md is unchanged."
    foot = [
        "",
        _BAR,
        "END OF VERIFIED REPORT.",
        "Anything the assistant writes BELOW this line is its own interpretation -",
        "commentary, not the verified report, and not validated by the guards.",
        _BAR,
    ]
    return True, "\n".join(head) + body.rstrip("\n") + "\n" + "\n".join(foot)



def _terminal_safe(text: str) -> str:
    """Escape terminal controls without modifying the verified artifact."""
    bidi_controls = {0x061c, 0x200e, 0x200f, *range(0x202a, 0x202f), *range(0x2066, 0x206a)}
    parts = []
    for char in text:
        code = ord(char)
        if (code < 32 and char not in "\n\t") or 0x7f <= code <= 0x9f or code in bidi_controls:
            parts.append(f"\\x{code:02x}" if code <= 0xff else f"\\u{code:04x}")
        else:
            parts.append(char)
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser(description=format_run_examples(__doc__),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run directory (runs/<ts>)")
    args = ap.parse_args()
    ok, text = present(args.run)
    safe_text = _terminal_safe(text)
    if safe_text != text:
        print("Terminal-safe view: controls escaped; the original artifact is unchanged.")
    print(safe_text)
    sys.exit(0 if ok else REFUSED)


if __name__ == "__main__":
    main()
