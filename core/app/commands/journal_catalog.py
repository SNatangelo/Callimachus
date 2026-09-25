# core/app/commands/journal_catalog.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Create, inspect, and atomically refresh the local journal authority catalog."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import os
from pathlib import Path
import urllib.request

from core.resolve.journal_authority import (
    CATALOG_ENV, build_catalog, catalog_metadata,
)
from core.resolve.journal_authority_sources import (
    marcxml_records, nlm_records, require_first,
)


NLM_SOURCE_URL = "https://ftp.ncbi.nih.gov/pubmed/J_Medline.txt"
TTL_ENV = "CALLIMACHUS_JOURNAL_AUTHORITY_TTL_DAYS"
AUTO_UPDATE_ENV = "CALLIMACHUS_JOURNAL_AUTHORITY_AUTO_UPDATE"
DEFAULT_TTL_DAYS = 7
MAX_NLM_BYTES = 64 * 1024 * 1024


def default_catalog_path() -> Path:
    configured = str(os.environ.get(CATALOG_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "storage" / "journal-authority" / "nlm-journals.sqlite"


def ttl_days(environ: dict[str, str] | None = None) -> int:
    raw = str((os.environ if environ is None else environ).get(TTL_ENV) or DEFAULT_TTL_DAYS).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{TTL_ENV} must be a positive integer")
    return int(raw)


def auto_update_enabled(environ: dict[str, str] | None = None) -> bool:
    raw = str((os.environ if environ is None else environ).get(AUTO_UPDATE_ENV) or "0").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{AUTO_UPDATE_ENV} must be a boolean")


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _created_at(metadata: dict) -> datetime:
    return datetime.fromisoformat(str(metadata["created_at"]).replace("Z", "+00:00"))


def _fresh(metadata: dict, *, now: datetime, ttl: int) -> bool:
    return _created_at(metadata) > now.astimezone(timezone.utc) - timedelta(days=ttl)


def _source_version(headers, source_sha256: str) -> str:
    last_modified = headers.get("Last-Modified") if headers is not None else None
    if last_modified:
        try:
            return parsedate_to_datetime(last_modified).astimezone(timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return "sha256:" + source_sha256


def update_nlm_catalog(
    path: str | os.PathLike[str], *, now: datetime | None = None,
    opener=None, source_url: str = NLM_SOURCE_URL,
) -> dict:
    """Download the public NLM list and replace the catalog only after validation."""
    now = now or datetime.now(timezone.utc)
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        source_url, headers={"User-Agent": "Callimachus journal-catalog updater"},
    )
    with opener(request, timeout=60) as response:
        status = getattr(response, "status", None)
        if type(status) is not int or status != 200:
            raise RuntimeError(f"NLM journal list returned HTTP {status}")
        chunks = []
        size = 0
        while True:
            chunk = response.read(min(1024 * 1024, MAX_NLM_BYTES + 1 - size))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise RuntimeError("NLM journal list response is not binary")
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_NLM_BYTES:
                raise RuntimeError("NLM journal list exceeds the size limit")
        body = b"".join(chunks)
        headers = getattr(response, "headers", None)
    if not body:
        raise RuntimeError("NLM journal list is empty")
    source_sha256 = hashlib.sha256(body).hexdigest()
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("NLM journal list is not UTF-8") from exc
    records = require_first(nlm_records(text))
    build_catalog(
        path, records, registry="nlm-pubmed",
        registry_version=_source_version(headers, source_sha256),
        created_at=_iso(now), source_kind="nlm-pubmed",
        source_url=source_url, source_sha256=source_sha256,
    )
    return catalog_metadata(path)


def import_issn_marcxml(
    source: str | os.PathLike[str], path: str | os.PathLike[str], *,
    registry_version: str, now: datetime | None = None,
) -> dict:
    """Import an operator-provided authorised ISSN Register MARCXML export."""
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError("ISSN MARCXML input is unavailable")
    hasher = hashlib.sha256()
    with source_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    build_catalog(
        path, require_first(marcxml_records(source_path)),
        registry="issn-register", registry_version=registry_version,
        merge_duplicate_ids=True,
        created_at=_iso(now or datetime.now(timezone.utc)),
        source_kind="issn-marcxml", source_url=source_path.as_uri(),
        source_sha256=hasher.hexdigest(),
    )
    return catalog_metadata(path)


def refresh_nlm_if_due(
    path: str | os.PathLike[str], *, force: bool = False,
    now: datetime | None = None, ttl: int | None = None, opener=None,
) -> tuple[dict, bool]:
    now = now or datetime.now(timezone.utc)
    ttl = ttl if ttl is not None else ttl_days()
    target = Path(path)
    if target.exists():
        current = catalog_metadata(target)
        if current["source_kind"] != "nlm-pubmed":
            raise RuntimeError(
                "automatic NLM refresh refuses to overwrite a non-NLM journal catalog"
            )
        if not force and _fresh(current, now=now, ttl=ttl):
            return current, False
    return update_nlm_catalog(target, now=now, opener=opener), True


def ensure_for_resolve(*, progress=print, now: datetime | None = None, opener=None) -> dict:
    """Make the free NLM catalog available before Resolve without weakening failures."""
    target = default_catalog_path()
    had_catalog = target.is_file()
    auto_update = auto_update_enabled()
    if had_catalog and not auto_update:
        os.environ[CATALOG_ENV] = str(target)
        return {"status": "available", "updated": False, "metadata": catalog_metadata(target)}
    try:
        if had_catalog:
            current = catalog_metadata(target)
            if current["source_kind"] != "nlm-pubmed":
                os.environ[CATALOG_ENV] = str(target)
                return {"status": "available", "updated": False, "metadata": current}
            metadata, updated = refresh_nlm_if_due(
                target, now=now, opener=opener,
            )
        else:
            progress("journal authority catalog missing; downloading public NLM journal list")
            metadata = update_nlm_catalog(target, now=now, opener=opener)
            updated = True
    except Exception as exc:
        if had_catalog:
            # Validate before retaining it; a corrupt file is never accepted as fallback.
            metadata = catalog_metadata(target)
            os.environ[CATALOG_ENV] = str(target)
            progress(
                f"journal authority refresh failed ({type(exc).__name__}); "
                "using the last validated snapshot"
            )
            return {"status": "stale", "updated": False, "metadata": metadata}
        os.environ.pop(CATALOG_ENV, None)
        progress(
            f"journal authority catalog unavailable ({type(exc).__name__}); "
            "authority-backed coverage checks are disabled"
        )
        return {"status": "unavailable", "updated": False, "metadata": None}
    os.environ[CATALOG_ENV] = str(target)
    if updated:
        progress(
            f"journal authority catalog updated: {metadata['record_count']} NLM identities"
        )
    return {"status": "available", "updated": updated, "metadata": metadata}


def _print_status(path: Path, metadata: dict) -> None:
    print(
        "journal authority: available; "
        f"source {metadata['source_kind']}; version {metadata['registry_version']}; "
        f"created {metadata['created_at']}; identities {metadata['record_count']}; "
        f"snapshot {metadata['snapshot_sha256']}; path {path}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the local journal-authority catalog.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    status_parser = subparsers.add_parser("status", help="Inspect the current catalog.")
    status_parser.add_argument("--catalog", type=Path, default=None)
    update_parser = subparsers.add_parser("update", help="Refresh from the public NLM journal list.")
    update_parser.add_argument("--catalog", type=Path, default=None)
    update_parser.add_argument("--ttl-days", type=int, default=None)
    update_parser.add_argument("--force", action="store_true")
    import_parser = subparsers.add_parser(
        "import-issn", help="Import an authorised ISSN Register MARCXML export.",
    )
    import_parser.add_argument("input", type=Path)
    import_parser.add_argument("--catalog", type=Path, default=None)
    import_parser.add_argument("--registry-version", required=True)
    args = parser.parse_args(argv)
    target = (args.catalog or default_catalog_path()).expanduser().resolve()
    try:
        if args.action == "status":
            if not target.is_file():
                print(f"journal authority: missing; path {target}")
                return 1
            _print_status(target, catalog_metadata(target))
            return 0
        if args.action == "update":
            if args.ttl_days is not None and args.ttl_days < 1:
                parser.error("--ttl-days must be a positive integer")
            metadata, updated = refresh_nlm_if_due(
                target, force=args.force, ttl=args.ttl_days,
            )
            _print_status(target, metadata)
            if not updated:
                print("journal authority: refresh not due")
            return 0
        metadata = import_issn_marcxml(
            args.input, target, registry_version=args.registry_version,
        )
        _print_status(target, metadata)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
