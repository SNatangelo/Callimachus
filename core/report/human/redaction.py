#!/usr/bin/env python3
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Create a public, redacted derivative of a human HTML report.

The original report and its seal remain private and untouched. This tool is a
publication step, not part of report generation or verdict verification.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


SAFE_TECHNICAL_RESULTS = frozenset({"answer_received", "timeout", "protocol_invalid"})
DATA_OPEN = '<script id="cv-data" type="application/json">'
DATA_CLOSE = "</script>"
SEAL = re.compile(
    r"(?:\r?\n)?<!-- citation-verifier-human-report-seal data=[A-Za-z0-9_-]+ -->\s*\Z"
)
class RedactionError(ValueError):
    """The input is not a supported human report."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tool_version() -> str:
    try:
        version = (Path(__file__).resolve().parents[3] / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RedactionError("Callimachus VERSION is unavailable") from exc
    if not version:
        raise RedactionError("Callimachus VERSION is empty")
    return version


def _script_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (encoded.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def redact_html(original: bytes, *, timestamp: str | None = None) -> tuple[bytes, dict[str, Any]]:
    """Return the public HTML and its sidecar manifest, without writing files."""
    try:
        html = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RedactionError("report must be UTF-8") from exc
    if html.count(DATA_OPEN) != 1:
        raise RedactionError("expected exactly one cv-data report payload")
    before, remainder = html.split(DATA_OPEN, 1)
    if DATA_CLOSE not in remainder:
        raise RedactionError("missing cv-data closing tag")
    encoded, after = remainder.split(DATA_CLOSE, 1)
    seal = SEAL.search(after)
    if seal is None and "citation-verifier-human-report-seal" in after:
        raise RedactionError("malformed original report seal")
    try:
        data = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise RedactionError("invalid cv-data JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("projection"), dict):
        raise RedactionError("missing report projection")
    projection = data["projection"]
    if projection.get("schema_version") != 1:
        raise RedactionError("unsupported report projection schema")
    execution = projection.get("execution") or {}
    if not isinstance(execution, dict):
        raise RedactionError("invalid execution record")
    if (data.get("preview") or data.get("preview_failures") or
            execution.get("debug_mode") or execution.get("debug_labels")):
        raise RedactionError("camera-ready copy requires a non-preview, non-debug report")
    if "publication_redaction" in data:
        raise RedactionError("report is already a public redacted copy")
    for key in ("claims", "sources", "pairs"):
        if not isinstance(projection.get(key), list):
            raise RedactionError(f"missing report {key}")

    counts: Counter[str] = Counter()
    omitted: set[str] = set()
    redacted_sources: set[str] = set()

    def drop(record: dict[str, Any], key: str, path: str) -> None:
        if key in record:
            value = record.pop(key)
            omitted.update(s for s in _strings(value) if len(s) >= 120 and len(s.split()) >= 8)
            counts[path] += 1

    manuscript = projection.get("manuscript")
    if isinstance(manuscript, dict):
        drop(manuscript, "input_path", "manuscript.input_path")
        title_identity = manuscript.get("title_identity")
        if isinstance(title_identity, dict):
            for attempt in title_identity.get("attempts") or []:
                if isinstance(attempt, dict):
                    drop(attempt, "abstract", "manuscript.title_identity.attempts.abstract")
    for claim in projection["claims"]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim"), dict):
            raise RedactionError("invalid claim record")
        drop(claim["claim"], "context_window", "claims.claim.context_window")

    for source in projection["sources"]:
        if not isinstance(source, dict):
            raise RedactionError("invalid source record")
        previous = sum(counts.values())
        resolve = source.get("resolve")
        if isinstance(resolve, dict):
            drop(resolve, "abstract", "sources.resolve.abstract")
            for attempt in resolve.get("attempts") or []:
                if isinstance(attempt, dict):
                    drop(attempt, "abstract", "sources.resolve.attempts.abstract")
            evidence_profile = resolve.get("evidence_profile")
            if isinstance(evidence_profile, dict):
                coverage = evidence_profile.get("resolver_coverage")
                if isinstance(coverage, dict):
                    for payload in coverage.get("payloads") or []:
                        if isinstance(payload, dict) and "body" in payload:
                            body = payload["body"]
                            if (not isinstance(body, str) or
                                    _sha256(body.encode("utf-8")) != payload.get("sha256")):
                                raise RedactionError("resolver coverage payload digest mismatch")
                            drop(payload, "body", "sources.resolve.evidence_profile.resolver_coverage.payloads.body")
        for attempt in source.get("fetch_attempts", []):
            if isinstance(attempt, dict):
                drop(attempt, "trace", "sources.fetch_attempts.trace")
        if sum(counts.values()) > previous:
            reference = source.get("reference")
            if isinstance(reference, dict) and reference.get("id"):
                redacted_sources.add(str(reference["id"]))

    for pair in projection["pairs"]:
        if not isinstance(pair, dict):
            raise RedactionError("invalid pair record")
        previous = sum(counts.values())
        for request in pair.get("logical_requests", []):
            if isinstance(request, dict):
                drop(request, "payload", "pairs.logical_requests.payload")
        for candidate in pair.get("candidates", []):
            if isinstance(candidate, dict):
                for key in ("evidence", "grounded", "explanation", "outcome_fields"):
                    drop(candidate, key, f"pairs.candidates.{key}")
        for event in pair.get("candidate_events", []):
            if isinstance(event, dict):
                drop(event, "payload", "pairs.candidate_events.payload")
        for event in pair.get("dispatch_events", []):
            if isinstance(event, dict):
                payload = event.get("payload")
                technical_result = payload.get("technical_result") if isinstance(payload, dict) else None
                drop(event, "payload", "pairs.dispatch_events.payload")
                if isinstance(technical_result, str) and technical_result in SAFE_TECHNICAL_RESULTS:
                    event["payload"] = {"technical_result": technical_result}
        for rejection_type in ("jury1_rejections", "rejected_jury1_attempts", "rejected_jury2_attempts"):
            for rejection in pair.get(rejection_type, []):
                if isinstance(rejection, dict):
                    drop(rejection, "reason", f"pairs.{rejection_type}.reason")
                    for review_type in ("jury1_review", "jury2_review"):
                        review = rejection.get(review_type)
                        if isinstance(review, dict):
                            drop(review, "reason", f"pairs.{rejection_type}.{review_type}.reason")
        if sum(counts.values()) > previous and pair.get("ref_id"):
            redacted_sources.add(str(pair["ref_id"]))

    original_sha = _sha256(original)
    publication = {
        "kind": "public_redacted_derivative",
        "tool": "core.report.human.redaction",
        "tool_version": _tool_version(),
        "created_at_utc": timestamp or datetime.now(timezone.utc).isoformat(),
        "original_html_sha256": original_sha,
        "original_seal_present": seal is not None,
        "redacted_fields": dict(sorted(counts.items())),
        "redacted_source_ref_ids": sorted(redacted_sources),
        "reason": "Omitted manuscript/source prose not needed to explain verdicts",
    }
    data["publication_redaction"] = publication
    # The original projection digest is no longer valid. The provenance view
    # instead identifies the source report of this derived publication copy.
    data["render_metadata"] = {"publication_redaction": publication}
    if seal is not None:
        after = after[:seal.start()]
    if not re.search(r"<body(?:\s[^>]*)?>", before, re.IGNORECASE):
        raise RedactionError("missing report body")
    public_html = before + DATA_OPEN + _script_json(data) + DATA_CLOSE + after
    public_bytes = public_html.encode("utf-8")
    # A removed passage must not survive elsewhere in the serialized report,
    # unless the identical passage is also a retained verdict/claim excerpt.
    retained: set[str] = set()
    for claim in projection["claims"]:
        retained.update(_strings(claim.get("focus_text")))
        retained.update(_strings(claim["claim"].get("sentence")))
        retained.update(_strings(claim["claim"].get("marker_raw")))
    for pair in projection["pairs"]:
        decision = pair.get("decision")
        if isinstance(decision, dict):
            for key in ("explanation", "supported_content", "incompatible_proposition",
                        "unsupported_content", "reason", "evidence"):
                retained.update(_strings(decision.get(key)))
        verification = pair.get("verification")
        if isinstance(verification, dict):
            retained.update(_strings(verification.get("evidence")))
    for source in projection["sources"]:
        reference = source.get("reference")
        if isinstance(reference, dict):
            retained.update(_strings(reference.get("title")))
            retained.update(_strings(reference.get("raw_entry")))
    for passage in omitted:
        serialized = _script_json(passage)[1:-1]
        if (passage in public_html or serialized in public_html) and not any(
            passage in value for value in retained
        ):
            def locations(value: Any, path: str = ""):
                if isinstance(value, dict):
                    for key, item in value.items():
                        yield from locations(item, f"{path}.{key}")
                elif isinstance(value, list):
                    for item in value:
                        yield from locations(item, path + "[]")
                elif isinstance(value, str) and passage in value:
                    yield path
            raise RedactionError(
                "omitted source passage remains in public HTML "
                f"(sha256={_sha256(passage.encode('utf-8'))}, length={len(passage)}, "
                f"fields={list(locations(data))[:5]})"
            )
    manifest = {**publication, "public_html_sha256": _sha256(public_bytes)}
    return public_bytes, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="original HTML report")
    parser.add_argument("--output", type=Path, help="new public HTML path")
    parser.add_argument("--check", action="store_true", help="validate in memory without writing")
    args = parser.parse_args(argv)
    if args.check == bool(args.output):
        parser.error("specify exactly one of --output or --check")
    try:
        original = args.report.read_bytes()
        public, manifest = redact_html(original)
        if args.check:
            print(json.dumps(manifest, sort_keys=True))
            return 0
        if args.output.resolve() == args.report.resolve():
            raise RedactionError("output must differ from the original report")
        sidecar = args.output.with_suffix(args.output.suffix + ".redaction.json")
        if args.output.exists() or sidecar.exists():
            raise RedactionError("output or manifest already exists")
        with args.output.open("xb") as stream:
            stream.write(public)
        try:
            with sidecar.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        except OSError:
            args.output.unlink()
            raise
        print(f"Wrote {args.output} and {sidecar}")
        return 0
    except (OSError, RedactionError) as exc:
        print(f"redaction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
