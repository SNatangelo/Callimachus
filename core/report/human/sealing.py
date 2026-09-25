# core/report/human/sealing.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Sealing and verification for the optional human HTML report companion."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from typing import Any, Mapping

from core.infra.integrity import signing as _signing
from core.report.io import load_run_projection
from core.report.sealing import seal_payload
from core.verify import verify_run

from .projection import build_human_report_projection
from .render import RenderedHumanReport, render_human_report


SEAL_SCHEMA = "citation-verifier.human-report-seal.v1"
RENDERER_VERSION = "core.report.human.v1"
_COMMENT_RE = re.compile(
    r"\n<!-- citation-verifier-human-report-seal data=(?P<data>[A-Za-z0-9_-]+) -->\n\Z"
)
_REQUIRED_META = frozenset({
    "schema", "format", "renderer", "report_md_sha256", "projection_sha256",
    "locale", "theme", "asset_sha256", "body_sha256", "alg", "sig", "content",
})
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _seal_fields(rendered: RenderedHumanReport, report_md_sha256: str) -> dict[str, Any]:
    return {
        "schema": SEAL_SCHEMA,
        "format": "html",
        "renderer": RENDERER_VERSION,
        "report_md_sha256": report_md_sha256,
        "projection_sha256": rendered.projection_sha256,
        "locale": rendered.locale,
        "theme": rendered.theme,
        "asset_sha256": dict(sorted(rendered.asset_sha256.items())),
    }


def _validated_meta(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _REQUIRED_META:
        raise ValueError("human HTML seal has an invalid schema")
    for field in ("schema", "format", "renderer", "locale", "theme", "alg", "sig", "content"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError("human HTML seal has an invalid scalar field")
    for field in ("report_md_sha256", "projection_sha256", "body_sha256", "content"):
        if not isinstance(value[field], str) or not _HASH_RE.fullmatch(value[field]):
            raise ValueError("human HTML seal has an invalid digest")
    assets = value["asset_sha256"]
    if not isinstance(assets, dict) or not assets:
        raise ValueError("human HTML seal has an invalid asset digest map")
    if any(not isinstance(key, str) or not key or not isinstance(digest, str)
           or not _HASH_RE.fullmatch(digest) for key, digest in assets.items()):
        raise ValueError("human HTML seal has an invalid asset digest map")
    return value


def strip_html_seal(html: str) -> str:
    """Return the exact unsealed HTML body, rejecting malformed trailing seals."""
    if not isinstance(html, str):
        raise ValueError("human HTML report is not text")
    matches = list(_COMMENT_RE.finditer(html))
    if len(matches) != 1:
        raise ValueError("human HTML report requires exactly one final seal")
    return html[:matches[0].start()]


def parse_html_seal(html: str) -> dict[str, Any]:
    """Decode the final, reversible companion seal without accepting loose syntax."""
    if not isinstance(html, str):
        raise ValueError("human HTML report is not text")
    matches = list(_COMMENT_RE.finditer(html))
    if len(matches) != 1:
        raise ValueError("human HTML report requires exactly one final seal")
    encoded = matches[0].group("data")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("human HTML report seal cannot be decoded") from exc
    return _validated_meta(value)


def seal_html_body(rendered: RenderedHumanReport, report_md_bytes: bytes) -> str:
    """Append a signed, machine-readable seal to a verified HTML body."""
    if not isinstance(report_md_bytes, bytes):
        raise ValueError("report.md bytes are required for human HTML sealing")
    fields = _seal_fields(rendered, _sha256_bytes(report_md_bytes))
    body_sha256 = _sha256_bytes(rendered.body.encode("utf-8"))
    payload = seal_payload(fields, body_sha256)
    signature = _signing.sign(payload)
    meta = {
        **fields,
        "body_sha256": body_sha256,
        "alg": signature["alg"],
        "sig": signature["sig"],
        "content": _sha256_bytes(payload),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{rendered.body}\n<!-- citation-verifier-human-report-seal data={encoded} -->\n"


def _restore_output(path: str, previous: bytes | None) -> None:
    if previous is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    else:
        with open(path, "wb") as stream:
            stream.write(previous)


def _write_output(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".report-html-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_html_report(
    run_dir: str,
    *,
    rendered: RenderedHumanReport,
    preview: bool = False,
    gate: Any = None,
) -> str:
    """Write a sealed verified report or explicitly unsealed preview inside a lease."""
    if preview is not rendered.preview:
        raise ValueError("human HTML output mode does not match the rendered report")
    root = os.path.abspath(run_dir)
    if not os.path.isdir(root):
        raise ValueError("run directory does not exist")
    name = "report.preview.html" if preview else "report.html"
    path = os.path.join(root, name)
    if os.path.commonpath((root, os.path.abspath(path))) != root:
        raise ValueError("human HTML output escapes the run directory")
    if os.path.exists(path):
        with open(path, "rb") as stream:
            previous = stream.read()
    else:
        previous = None
    lease = None
    try:
        if gate is not None:
            lease = gate.begin_pipeline_transition(
                root, checkpoint_kind="report", mutates_content_store=False
            )
        if preview:
            content = rendered.body
        else:
            with open(os.path.join(root, "report.md"), "rb") as stream:
                content = seal_html_body(rendered, stream.read())
        _write_output(path, content)
        if gate is not None:
            gate.commit_pipeline_transition(root, lease)
        return path
    except BaseException as exc:
        try:
            _restore_output(path, previous)
        finally:
            if gate is not None and lease is not None:
                gate.abort_pipeline_transition(
                    root, lease, reason=f"human HTML generation raised {type(exc).__name__}"
                )
        raise


def verify_html_report(run_dir: str, *, require_signature: bool = False) -> dict[str, Any]:
    """Verify the optional HTML companion against its Markdown report and live run data."""
    root = os.path.abspath(run_dir)
    base = verify_run.verify(root, require_signature=require_signature)
    failures = list(base.get("failures") or ())
    path = os.path.join(root, "report.html")
    if not os.path.exists(path):
        failures.append("report.html missing")
        return {"ok": False, "failures": failures, "base_gate": base}
    try:
        with open(path, encoding="utf-8", newline="") as stream:
            html = stream.read()
        body = strip_html_seal(html)
        meta = parse_html_seal(html)
        if meta["schema"] != SEAL_SCHEMA or meta["format"] != "html" or meta["renderer"] != RENDERER_VERSION:
            failures.append("report.html seal declares an unsupported format")
        with open(os.path.join(root, "report.md"), "rb") as stream:
            report_md_bytes = stream.read()
        if meta["report_md_sha256"] != _sha256_bytes(report_md_bytes):
            failures.append("report.html binding does not match current report.md")
        run_projection = load_run_projection(root)
        projection = build_human_report_projection(run_projection)
        rendered = render_human_report(projection, locale=meta["locale"], theme=meta["theme"])
        if meta["projection_sha256"] != rendered.projection_sha256:
            failures.append("report.html binding does not match current run projection")
        if dict(meta["asset_sha256"]) != dict(rendered.asset_sha256):
            failures.append("report.html binding does not match current presentation assets")
        if body != rendered.body or meta["body_sha256"] != _sha256_bytes(body.encode("utf-8")):
            failures.append("report.html body does not match the deterministic renderer")
        fields = {
            "schema": meta["schema"], "format": meta["format"], "renderer": meta["renderer"],
            "report_md_sha256": meta["report_md_sha256"],
            "projection_sha256": meta["projection_sha256"], "locale": meta["locale"],
            "theme": meta["theme"], "asset_sha256": dict(sorted(meta["asset_sha256"].items())),
        }
        payload = seal_payload(fields, meta["body_sha256"])
        if meta["content"] != _sha256_bytes(payload):
            failures.append("report.html content seal is invalid")
        if require_signature and meta["alg"] != "hmac-sha256":
            failures.append("report.html carries only a weak sha256 seal, not the HMAC of the deterministic system")
        elif not _signing.verify(payload, meta["alg"], meta["sig"]):
            failures.append("report.html signature is invalid")
    except (OSError, ValueError) as exc:
        failures.append(f"report.html verification failed: {exc}")
    return {"ok": not failures, "failures": failures, "base_gate": base}
