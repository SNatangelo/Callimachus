# core/report/human/cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""CLI for producing an optional, retroactive human HTML report companion."""

from __future__ import annotations

import argparse
import json

from core.infra.integrity import signing as _signing
from core.infra.integrity.execution_assurance import resolve_existing
from core.infra.integrity.gate import IntegrityGateError
from core.report.io import load_run_projection
from core.verify import verify_run

from .projection import build_human_report_projection
from .render import render_human_report
from .sealing import verify_html_report, write_html_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic HTML companion for an existing report run."
    )
    parser.add_argument("--run", required=True, help="existing run directory")
    parser.add_argument("--locale", help="installed external locale identifier")
    parser.add_argument("--theme", help="installed external theme identifier")
    parser.add_argument("--agent-identity", help="stable opaque harness identity")
    parser.add_argument(
        "--preview-unverified", action="store_true",
        help="write only report.preview.html with an explicit unverified watermark",
    )
    parser.add_argument("--verify", action="store_true", help="verify an existing report.html without writing")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.verify:
        if args.preview_unverified or args.locale or args.theme:
            raise SystemExit("--verify cannot be combined with rendering options")
        result = verify_html_report(args.run, require_signature=_signing.key_present())
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 1
    try:
        resolved = resolve_existing(
            args.run,
            args.agent_identity,
            mirror_audit_records=False,
            allow_local_downgrade=False,
        )
    except IntegrityGateError as exc:
        raise SystemExit(f"integrity assurance stopped HTML report: {exc}") from exc
    except RuntimeError as exc:
        raise SystemExit(f"HTML report generation stopped: {exc}") from exc
    try:
        gate_result = verify_run.verify(
            args.run, require_signature=_signing.key_present()
        )
        if not gate_result["ok"] and not args.preview_unverified:
            raise SystemExit("HTML report refused: " + "; ".join(gate_result["failures"]))
        run_projection = load_run_projection(args.run)
        projection = build_human_report_projection(run_projection)
        rendered = render_human_report(
            projection,
            locale=args.locale,
            theme=args.theme,
            preview=args.preview_unverified,
            preview_failures=tuple(gate_result["failures"]),
        )
        path = write_html_report(
            args.run,
            rendered=rendered,
            preview=args.preview_unverified,
            gate=resolved.gate,
        )
    except (IntegrityGateError, OSError, ValueError) as exc:
        raise SystemExit(f"HTML report generation stopped: {exc}") from exc
    print(json.dumps({
        "ok": True,
        "path": path,
        "preview": bool(args.preview_unverified),
        "locale": rendered.locale,
        "theme": rendered.theme,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
