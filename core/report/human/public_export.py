# core/report/human/public_export.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Export a verified human HTML report as a public redacted derivative."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Callable

from core.infra.db.repository import RunRepository
from core.infra.integrity import signing
from core.invocation import run_command

from .redaction import redact_html
from .projection import HumanReportProjection
from .render import render_human_report
from .sealing import (
    RENDERER_VERSION,
    SEAL_SCHEMA,
    parse_html_seal,
    seal_payload,
    strip_html_seal,
    verify_html_report,
)


_CV_DATA_OPEN = '<script id="cv-data" type="application/json">'
_CV_DATA_CLOSE = "</script>"


def _verify_historical_artifact(
    original: bytes,
    *,
    check_cancelled: Callable[[], None],
    allow_unverified_signature: bool = False,
) -> dict[str, str]:
    """Authenticate the standalone report seal and require our canonical renderer."""
    check_cancelled()
    try:
        html = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("historical report must be UTF-8") from exc

    body = strip_html_seal(html)
    meta = parse_html_seal(html)
    if (
        meta["schema"] != SEAL_SCHEMA
        or meta["format"] != "html"
        or meta["renderer"] != RENDERER_VERSION
    ):
        raise ValueError("historical report seal declares an unsupported format or renderer")

    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if meta["body_sha256"] != body_sha256:
        raise ValueError("historical report body does not match its seal")
    fields = {
        "schema": meta["schema"],
        "format": meta["format"],
        "renderer": meta["renderer"],
        "report_md_sha256": meta["report_md_sha256"],
        "projection_sha256": meta["projection_sha256"],
        "locale": meta["locale"],
        "theme": meta["theme"],
        "asset_sha256": dict(sorted(meta["asset_sha256"].items())),
    }
    payload = seal_payload(fields, body_sha256)
    if meta["content"] != hashlib.sha256(payload).hexdigest():
        raise ValueError("historical report content seal is invalid")

    if meta["alg"] == "hmac-sha256":
        if allow_unverified_signature:
            # The human Desktop export may redact a historical artifact without
            # requesting access to the old installation's private signing key.
            authentication = "hmac_not_authenticated"
        else:
            key = signing.load_key()
            if not key:
                raise ValueError("historical report HMAC cannot be authenticated without a signing key")
            if not signing.verify(payload, meta["alg"], meta["sig"], key=key):
                raise ValueError("historical report HMAC signature is invalid")
            authentication = "hmac_authenticated"
    elif meta["alg"] == "sha256":
        # An unkeyed checksum proves content consistency only, never authorship.
        if not signing.verify(payload, meta["alg"], meta["sig"], key=""):
            raise ValueError("historical report checksum is invalid")
        authentication = "checksum_only_unauthenticated"
    else:
        raise ValueError("historical report uses an unsupported seal algorithm")

    check_cancelled()
    if body.count(_CV_DATA_OPEN) != 1:
        raise ValueError("historical report must contain exactly one cv-data payload")
    _, remainder = body.split(_CV_DATA_OPEN, 1)
    if _CV_DATA_CLOSE not in remainder:
        raise ValueError("historical report cv-data payload is incomplete")
    encoded_projection, _ = remainder.split(_CV_DATA_CLOSE, 1)
    try:
        data = json.loads(encoded_projection)
    except json.JSONDecodeError as exc:
        raise ValueError("historical report cv-data payload is invalid") from exc
    if not isinstance(data, dict) or not isinstance(data.get("projection"), dict):
        raise ValueError("historical report projection is missing")

    projection = HumanReportProjection(data["projection"])
    rendered = render_human_report(
        projection,
        locale=meta["locale"],
        theme=meta["theme"],
    )
    check_cancelled()
    if (
        rendered.body != body
        or rendered.projection_sha256 != meta["projection_sha256"]
        or dict(rendered.asset_sha256) != dict(meta["asset_sha256"])
    ):
        raise ValueError("historical report does not match the supported deterministic renderer")
    return {"algorithm": meta["alg"], "authentication": authentication}


def _add_historical_notice(
    public: bytes,
    manifest: dict,
    *,
    source_seal: dict[str, str],
) -> tuple[bytes, dict]:
    """Add a visible artifact-only qualification and record its verification limits."""
    authentication_notice = {
        "hmac_authenticated": "The source HMAC was authenticated with this installation's signing key.",
        "hmac_not_authenticated": "The source signature was not authenticated. Content consistency was checked; authorship and authenticity were not verified.",
        "checksum_only_unauthenticated": "The source SHA-256 checksum matched; it does not authenticate the report's author.",
    }[source_seal["authentication"]]
    notice = (
        '<aside class="cv-artifact-only-notice" role="note" '
        'style="border:2px solid #8a4b00;padding:0.75rem;margin:0.75rem 0">'
        "<strong>Historical artifact-only export.</strong> "
        f"{authentication_notice} "
        "The current run database, completion status, semantic Verify mode, "
        "and report.md binding were not checked."
        "</aside>\n"
    )
    try:
        html = public.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("redacted historical report must be UTF-8") from exc
    body_open = "<body>\n"
    if html.count(body_open) != 1:
        raise ValueError("redacted historical report has an unsupported body structure")
    public = html.replace(body_open, body_open + notice, 1).encode("utf-8")
    manifest = {
        **manifest,
        "historical_export": {
            "kind": "historical_artifact_only",
            "source_html_seal": dict(source_seal),
            "current_run_database_verified": False,
            "completion_status_verified": False,
            "verification_mode_verified": False,
            "report_markdown_binding_verified": False,
        },
        "public_html_sha256": hashlib.sha256(public).hexdigest(),
    }
    return public, manifest


class PublicExportCancelled(RuntimeError):
    """The public HTML export was cooperatively cancelled."""


def export_public_html(
    run_dir: str,
    output: str,
    *,
    cancel_check: Callable[[], bool] | None = None,
    overwrite: bool = False,
    historical: bool = False,
    allow_unverified_signature: bool = False,
) -> str:
    """Write a redacted derivative of a sealed report.

    By default, require a complete semantic Verify run and verify the report
    against its live database. ``historical=True`` is an explicit artifact-only
    path for a supported standalone report; its output says that current run
    state and completion were not verified. A human Desktop caller may opt into
    ``allow_unverified_signature`` for historical artifacts: no signing key is
    read and the output explicitly records that authenticity was not verified.
    Content/renderer checks and redaction still apply. Existing files are rejected unless
    ``overwrite`` is explicitly enabled. Overwrite stages both files and
    attempts to restore the prior pair if replacement is cancelled or fails.
    ``cancel_check`` lets a background caller request cooperative cancellation
    during source verification, redaction, and output writes.
    """
    def check_cancelled() -> None:
        if cancel_check is not None and cancel_check():
            raise PublicExportCancelled("public HTML export cancelled")

    if type(historical) is not bool:
        raise ValueError("historical export mode must be a boolean")
    if type(allow_unverified_signature) is not bool or (allow_unverified_signature and not historical):
        raise ValueError("unverified signatures are only allowed for historical artifact-only exports")
    check_cancelled()
    source = Path(run_dir) / "report.html"
    historical_seal = None
    if historical:
        original = source.read_bytes()
        historical_seal = _verify_historical_artifact(
            original,
            check_cancelled=check_cancelled,
            allow_unverified_signature=allow_unverified_signature,
        )
        if source.read_bytes() != original:
            raise ValueError("report.html changed while its standalone seal was being verified")
    else:
        repo = RunRepository.open_readonly(run_dir)
        try:
            run = repo.get_run()
            if run.status != "completed" or run.phase != "done":
                raise ValueError("public export requires a completed run at phase done")
            if repo.get_run_setting("references_only", None) is not False:
                raise ValueError("public export requires semantic Verify, not references-only mode")
        finally:
            repo.close()

        check_cancelled()
        original = source.read_bytes()
        check_cancelled()
        gate = verify_html_report(
            run_dir,
            require_signature=signing.key_present(),
            cancel_check=check_cancelled,
        )
        if not gate["ok"]:
            raise ValueError("public export refused: " + "; ".join(gate["failures"]))
        if source.read_bytes() != original:
            raise ValueError("report.html changed while its seal was being verified")

    check_cancelled()
    destination = Path(output)
    if destination.resolve() == source.resolve():
        raise ValueError("output must differ from the original report")
    if destination.resolve().is_relative_to(Path(run_dir).resolve()):
        raise ValueError("output must be outside the run directory")
    public, manifest = redact_html(original, cancel_check=check_cancelled)
    if historical_seal is not None:
        public, manifest = _add_historical_notice(
            public,
            manifest,
            source_seal=historical_seal,
        )
    check_cancelled()
    sidecar = destination.with_suffix(destination.suffix + ".redaction.json")
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    if not overwrite:
        if destination.exists() or sidecar.exists():
            raise ValueError("output or manifest already exists")
        output_created = False
        sidecar_created = False
        try:
            with destination.open("xb") as stream:
                output_created = True
                for offset in range(0, len(public), 256 * 1024):
                    check_cancelled()
                    stream.write(public[offset:offset + 256 * 1024])
            check_cancelled()
            with sidecar.open("x", encoding="utf-8") as stream:
                sidecar_created = True
                stream.write(manifest_json)
            check_cancelled()
        except BaseException as exc:
            cleanup_errors = []
            if sidecar_created:
                try:
                    sidecar.unlink()
                except OSError as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if output_created:
                try:
                    destination.unlink()
                except OSError as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise OSError(
                    f"could not remove an incomplete public export: {cleanup_errors[0]}"
                ) from exc
            raise
        return str(destination)

    if destination.is_symlink() or sidecar.is_symlink():
        raise ValueError("output and manifest must not be symlinks")

    temporary_paths: list[Path] = []
    backups: dict[Path, Path] = {}
    installed: set[Path] = set()

    def temporary_path(suffix: str) -> Path:
        fd, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=suffix, dir=destination.parent
        )
        path = Path(name)
        temporary_paths.append(path)
        os.close(fd)
        return path

    try:
        staged_html = temporary_path(".tmp")
        with staged_html.open("wb") as stream:
            for offset in range(0, len(public), 256 * 1024):
                check_cancelled()
                stream.write(public[offset:offset + 256 * 1024])
            stream.flush()
            os.fsync(stream.fileno())

        staged_sidecar = temporary_path(".tmp")
        with staged_sidecar.open("w", encoding="utf-8") as stream:
            stream.write(manifest_json)
            stream.flush()
            os.fsync(stream.fileno())

        existing_target = next(
            (target for target in (destination, sidecar) if target.exists()), None
        )
        for target, staged in (
            (destination, staged_html), (sidecar, staged_sidecar)
        ):
            mode_source = target if target.exists() else existing_target
            if mode_source is not None:
                os.chmod(staged, stat.S_IMODE(mode_source.stat().st_mode))

        for target in (destination, sidecar):
            if target.exists():
                backup = temporary_path(".bak")
                shutil.copy2(target, backup)
                backups[target] = backup

        check_cancelled()
        os.replace(staged_html, destination)
        installed.add(destination)
        check_cancelled()
        os.replace(staged_sidecar, sidecar)
        installed.add(sidecar)
        check_cancelled()
    except BaseException as exc:
        rollback_errors = []
        for target in (sidecar, destination):
            if target not in installed:
                continue
            backup = backups.get(target)
            try:
                if backup is None:
                    target.unlink()
                else:
                    os.replace(backup, target)
                    temporary_paths.remove(backup)
            except OSError as rollback_error:
                rollback_errors.append((target, backup, rollback_error))
        if rollback_errors:
            for path in temporary_paths:
                if path in backups.values() and any(
                    backup == path for _, backup, _ in rollback_errors
                ):
                    continue
                try:
                    path.unlink()
                except OSError:
                    pass
            target, backup, rollback_error = rollback_errors[0]
            raise OSError(
                "could not restore the previous public export at "
                f"{target}: {rollback_error}"
                + (f"; backup retained at {backup}" if backup is not None else "")
            ) from exc
        cleanup_errors = []
        for path in temporary_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise OSError(
                f"could not clean up an incomplete public export: {cleanup_errors[0]}"
            ) from exc
        raise
    for path in temporary_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return str(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=run_command("report-export"),
        description="Export a public redacted copy of a completed Verify report.",
    )
    parser.add_argument("--run", required=True, help="completed Verify run directory")
    parser.add_argument("--output", required=True, help="new public HTML path")
    args = parser.parse_args(argv)
    try:
        path = export_public_html(args.run, args.output)
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        parser.exit(1, f"report export refused: {exc}\n")
    print(f"Wrote {path} and {path}.redaction.json")
    return 0
