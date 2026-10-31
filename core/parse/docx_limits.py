# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Bound DOCX directory allocation and the XML actually used by the parser."""
from __future__ import annotations

import zipfile


MAX_DIRECTORY_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_XML_TOTAL_BYTES = 64 * 1024 * 1024
XML_PARTS = ("word/document.xml", "word/numbering.xml", "word/styles.xml")


def check_directory(path: str) -> None:
    # ZipFile builds the complete central-directory index in its constructor.
    # The stdlib's bounded EOCD reader also handles ZIP64; inspect that record
    # first, before allocating an index for an untrusted document.
    with open(path, "rb") as stream:
        end = zipfile._EndRecData(stream)
    if end is None:
        raise zipfile.BadZipFile("DOCX has no ZIP directory")
    if end[zipfile._ECD_ENTRIES_TOTAL] > MAX_MEMBERS:
        raise ValueError("DOCX contains too many archive entries")
    if end[zipfile._ECD_SIZE] > MAX_DIRECTORY_BYTES:
        raise ValueError("DOCX archive directory exceeds the size limit")


def check_xml_parts(archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > MAX_MEMBERS:
        raise ValueError("DOCX contains too many archive entries")
    selected = [info for info in entries if info.filename in XML_PARTS]
    if len({info.filename for info in selected}) != len(selected):
        raise ValueError("DOCX contains duplicate text XML entries")
    if any(info.file_size > MAX_XML_BYTES for info in selected):
        raise ValueError("DOCX text XML exceeds the size limit (32 MiB per part)")
    if sum(info.file_size for info in selected) > MAX_XML_TOTAL_BYTES:
        raise ValueError("DOCX text XML exceeds the combined size limit (64 MiB)")


def read_xml(archive: zipfile.ZipFile, name: str) -> bytes:
    with archive.open(name) as stream:
        body = stream.read(MAX_XML_BYTES + 1)
    if len(body) > MAX_XML_BYTES:
        raise ValueError("DOCX text XML exceeds the size limit (32 MiB per part)")
    return body
