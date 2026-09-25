# core/fetch/extraction/cran_archive.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Strict extraction of the one audited CRAN source archive layout."""

from __future__ import annotations

import io
import tarfile


MAX_COMPRESSED_BYTES = 25 * 1024 * 1024
MAX_MEMBERS = 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024

_ROOT = "vegan"
_DESCRIPTION = "vegan/DESCRIPTION"
_INTRO_PDF = "vegan/inst/doc/intro-vegan.pdf"


class CranArchiveError(ValueError):
    """A deterministic, reportable CRAN archive rejection."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _safe_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if (
        not name
        or "\x00" in name
        or name.startswith("/")
        or any(part == ".." for part in name.split("/"))
        or not (name == _ROOT or name.startswith(f"{_ROOT}/"))
    ):
        raise CranArchiveError("cran_archive_unsafe_member", "CRAN archive contains an unsafe member path")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise CranArchiveError("cran_archive_unsafe_member", "CRAN archive contains a non-regular member")
    if not (member.isfile() or member.isdir()):
        raise CranArchiveError("cran_archive_unsafe_member", "CRAN archive contains an unsupported member type")
    if name == _ROOT and not member.isdir():
        raise CranArchiveError("cran_archive_unsafe_member", "CRAN archive root must be a directory")


def _description_fields(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CranArchiveError("cran_archive_metadata_mismatch", "CRAN DESCRIPTION is not UTF-8") from exc
    fields: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line[:1].isspace() and current is not None:
            fields[current] += " " + line.strip()
            continue
        if ":" not in line:
            current = None
            continue
        key, value = line.split(":", 1)
        current = key.strip()
        fields[current] = value.strip()
    return fields


def extract_vegan_2_5_3_archive(body: bytes) -> tuple[bytes, dict[str, str]]:
    """Return the allowlisted PDF only after exact package metadata checks.

    This deliberately does not unpack to disk.  It accepts no archive layout
    besides the recorded vegan 2.5-3 source package and exposes no arbitrary
    member selection to callers.
    """
    if len(body) > MAX_COMPRESSED_BYTES:
        raise CranArchiveError("cran_archive_limit_exceeded", "CRAN archive exceeds compressed-size limit")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise CranArchiveError("cran_archive_metadata_mismatch", "CRAN response is not a readable gzip tar archive") from exc

    description = None
    intro_pdf = None
    total = 0
    try:
        for count, member in enumerate(archive, start=1):
            if count > MAX_MEMBERS:
                raise CranArchiveError("cran_archive_limit_exceeded", "CRAN archive exceeds member-count limit")
            _safe_member(member)
            if member.size > MAX_MEMBER_BYTES:
                raise CranArchiveError("cran_archive_limit_exceeded", "CRAN archive member exceeds size limit")
            total += member.size
            if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise CranArchiveError("cran_archive_limit_exceeded", "CRAN archive exceeds total-size limit")
            if member.name not in {_DESCRIPTION, _INTRO_PDF}:
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise CranArchiveError("cran_archive_required_member_missing", "CRAN archive required member is not readable")
            payload = stream.read()
            if member.name == _DESCRIPTION:
                description = payload
            else:
                intro_pdf = payload
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise CranArchiveError("cran_archive_metadata_mismatch", "CRAN archive could not be read safely") from exc
    finally:
        archive.close()

    if description is None or intro_pdf is None:
        raise CranArchiveError("cran_archive_required_member_missing", "CRAN archive lacks required DESCRIPTION or intro PDF")
    fields = _description_fields(description)
    if (
        fields.get("Package") != "vegan"
        or fields.get("Version") != "2.5-3"
        or fields.get("Title") != "Community Ecology Package"
        or fields.get("Date") != "2018-10-24"
        or not fields.get("Author", "").startswith("Jari Oksanen")
    ):
        raise CranArchiveError("cran_archive_metadata_mismatch", "CRAN DESCRIPTION does not identify vegan 2.5-3 by Oksanen in 2018")
    if not intro_pdf.startswith(b"%PDF"):
        raise CranArchiveError("cran_archive_member_not_pdf", "CRAN intro documentation lacks PDF magic")
    return intro_pdf, fields
