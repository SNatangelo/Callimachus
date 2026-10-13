#!/usr/bin/env python3
# tools/probe_authoryear_refs.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Prototype splitter for non-numbered author-year bibliographies.

This is intentionally separate from core.parse_manuscript while we tune the
heuristics against real PDFs.
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.parse.extract import extract_text
from core.parse.parse_manuscript import _split_body_bibliography

YEAR = r"(?:19|20)\d{2}"


def clean_line(line: str) -> str:
    line = " ".join((line or "").split())
    line = re.sub(r"\s+([,.;:])", r"\1", line)
    line = re.sub(r"\(\s+", "(", line)
    line = re.sub(r"\s+\)", ")", line)
    # PDF extraction may spell a URL with spaces between every character.
    line = re.sub(r"h\s*t\s*t\s*p\s*s?\s*:\s*/\s*/", "https://", line, flags=re.I)
    line = re.sub(r"w\s*w\s*w\s*\.", "www.", line, flags=re.I)
    line = re.sub(r"\b(Publisher's Note|Publisher’s Note)\b.*$", "", line).strip()
    return line.strip()


def is_noise(line: str) -> bool:
    if not line:
        return True
    low = line.lower()
    if low in {"retracted article", "1 3"}:
        return True
    if re.fullmatch(r"\d{3,5}", line):
        return True
    if low.startswith("publisher's note"):
        return True
    if low == "institutional affiliations.":
        return True
    if low.startswith("springer nature or its licensor"):
        return True
    if low in {"authors and affiliations"}:
        return True
    return False


def is_reference_list_end(line: str) -> bool:
    low = line.lower()
    return (
        low.startswith("publisher's note")
        or low == "authors and affiliations"
        or low.startswith("springer nature or its licensor")
    )


def looks_ref_start(line: str) -> bool:
    """APA-ish start: author/organization followed by a year near the front."""
    if is_noise(line):
        return False
    if not re.search(rf"\({YEAR}[a-z]?\)|\b{YEAR}[a-z]?\b", line[:180]):
        return False
    if re.match(r"^[A-Z][A-Za-zÀ-ÿ'’.-]+(?:\s+[A-Z]\.)?,", line):
        return True
    if re.match(r"^[A-Z][A-Za-zÀ-ÿ'’.-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’.-]+){1,3},\s*[A-Z]\.", line):
        return True
    if re.match(r"^[A-Z][A-Za-zÀ-ÿ'’.-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’.-]+){0,4}\s*\(", line):
        return True
    if re.match(
        r"^[A-Z][A-Za-zÀ-ÿ'’.-]+(?:\s+(?:of|and|the|for|in|on|[A-Z][A-Za-zÀ-ÿ'’.-]+)){1,7}"
        rf"\.?\s*\({YEAR}[a-z]?\)",
        line,
    ):
        return True
    if re.match(r"^[A-Z][A-Za-zÀ-ÿ'’.-]+(?:\s+&\s+|\s+and\s+)[A-Z]", line):
        return True
    if re.match(r"^[A-Z][A-Za-zÀ-ÿ'’.-]+(?:,\s*[A-Z]\.)+(?:,\s*&\s*[A-Z]|,\s*[A-Z])", line):
        return True
    return False


def merge_hyphen_wraps(text: str) -> str:
    return re.sub(r"([A-Za-zÀ-ÿ])-\s+([A-Za-zÀ-ÿ])", r"\1\2", text)


def split_authoryear_refs(biblio: str) -> list[str]:
    refs: list[str] = []
    buf: list[str] = []
    for raw_line in biblio.splitlines():
        line = clean_line(raw_line)
        if is_reference_list_end(line):
            break
        if is_noise(line):
            continue
        if looks_ref_start(line) and buf:
            refs.append(merge_hyphen_wraps(" ".join(buf)))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        refs.append(merge_hyphen_wraps(" ".join(buf)))
    return [r for r in refs if re.search(rf"\b{YEAR}\b", r)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    args = ap.parse_args()

    text, fmt, meta = extract_text(args.pdf)
    body, biblio, cut, _end = _split_body_bibliography(text)
    refs = split_authoryear_refs(biblio)
    result = {
        "format": fmt,
        "pdf_method": meta.get("pdf_method"),
        "cut": cut,
        "n_refs": len(refs),
        "refs": refs,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
