#!/usr/bin/env python3
# tools/apply_license_headers.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Check or add Callimachus SPDX headers to safely commentable tracked files."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path, PurePosixPath
import subprocess


COPYRIGHT = "Copyright (C) 2026 Stefano Natangelo"
SPDX = "SPDX-License-Identifier: AGPL-3.0-only"
HASH_EXTENSIONS = {".py", ".pyi", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties"}
SLASH_EXTENSIONS = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx"}
BLOCK_EXTENSIONS = {".css", ".scss", ".sass"}
XML_EXTENSIONS = {".html", ".htm", ".xml", ".svg", ".xhtml"}
PERCENT_EXTENSIONS = {".tex", ".sty", ".cls"}
SQL_EXTENSIONS = {".sql"}
HASH_DOTFILES = {
    ".editorconfig",
    ".env",
    ".env.example",
    ".gitignore",
    ".graphifyignore",
    ".pre-commit-config.yaml",
    ".worktreeinclude",
}
HASH_NAMES = {"makefile", "dockerfile", "containerfile", "requirements.txt"}


def comment_style(path: PurePosixPath) -> str | None:
    """Return a safe comment style, or None for a file we must not touch."""
    name = path.name
    lower_name = name.lower()
    suffix = path.suffix.lower()
    parts = tuple(part.lower() for part in path.parts)
    if suffix == ".md" or lower_name == "license" or lower_name.startswith("license."):
        return None
    if ("tests" in parts and "fixtures" in parts) or ("examples" in parts and "report" in parts):
        return None
    if lower_name in HASH_DOTFILES or lower_name in HASH_NAMES or lower_name.startswith("requirements") and suffix == ".txt":
        return "hash"
    if len(parts) >= 2 and parts[0] == "core" and name.endswith("_example.py.txt"):
        return "hash"
    if suffix in HASH_EXTENSIONS:
        return "hash"
    if suffix in SLASH_EXTENSIONS:
        return "slash"
    if suffix in BLOCK_EXTENSIONS:
        return "block"
    if suffix in XML_EXTENSIONS:
        return "xml"
    if suffix in PERCENT_EXTENSIONS:
        return "percent"
    if suffix in SQL_EXTENSIONS:
        return "sql"
    return None


def render_header(path: str, style: str, newline: str) -> str:
    prefixes = {"hash": "# ", "slash": "// ", "block": "/* ", "xml": "<!-- ", "percent": "% ", "sql": "-- "}
    suffixes = {"hash": "", "slash": "", "block": " */", "xml": " -->", "percent": "", "sql": ""}
    return "".join(f"{prefixes[style]}{line}{suffixes[style]}{newline}" for line in (path, COPYRIGHT, SPDX))


def _preamble_end(text: str) -> int:
    lines = text.splitlines(keepends=True)
    end = 0
    index = 0
    if lines and lines[0].startswith("#!"):
        end += len(lines[0]); index = 1
    if index < len(lines) and ("coding=" in lines[index] or "coding:" in lines[index]):
        end += len(lines[index]); index += 1
    while index < len(lines) and lines[index].lower().startswith(("# syntax=", "# escape=", "# check=")):
        end += len(lines[index]); index += 1
    if index < len(lines) and lines[index].lstrip().lower().startswith("@charset "):
        end += len(lines[index]); index += 1
    if index < len(lines) and lines[index].lstrip().startswith("<?xml"):
        end += len(lines[index]); index += 1
    if index < len(lines) and lines[index].lstrip().lower().startswith("<!doctype"):
        end += len(lines[index])
    return end


def _managed_header_end(text: str, start: int) -> int | None:
    lines = text[start:].splitlines(keepends=True)
    if len(lines) < 3:
        return None
    normalized = []
    for line in lines[:3]:
        value = line.strip()
        for prefix, suffix in (("#", ""), ("//", ""), ("%", ""), ("--", ""), ("/*", "*/"), ("<!--", "-->")):
            if value.startswith(prefix) and (not suffix or value.endswith(suffix)):
                value = value[len(prefix):]
                if suffix:
                    value = value[:-len(suffix)]
                break
        normalized.append(value.strip())
    if normalized[1:] == [COPYRIGHT, SPDX]:
        return start + sum(len(line) for line in lines[:3])
    return None


def transform(data: bytes, path: str, style: str) -> bytes:
    """Return data with one canonical header, retaining BOM, preamble and EOL style."""
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    body = data[len(bom):]
    text = body.decode("utf-8")
    newline = "\r\n" if b"\r\n" in body else "\n"
    start = _preamble_end(text)
    managed_end = _managed_header_end(text, start)
    header = render_header(path, style, newline)
    if managed_end is not None:
        text = text[:start] + header + text[managed_end:]
    elif text[start:].startswith(header):
        return data
    else:
        text = text[:start] + header + text[start:]
    return bom + text.encode("utf-8")


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, stdout=subprocess.PIPE)
    return sorted(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report missing or stale headers (default)")
    mode.add_argument("--write", action="store_true", help="write canonical headers to eligible tracked files")
    args = parser.parse_args(argv)
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip())
    counts: Counter[str] = Counter()
    changes: list[tuple[str, bytes]] = []
    for relative in tracked_files(root):
        style = comment_style(PurePosixPath(relative))
        if style is None:
            counts["unsafe"] += 1
            continue
        target = root / relative
        data = target.read_bytes()
        if b"\0" in data:
            counts["binary"] += 1
            continue
        try:
            updated = transform(data, relative, style)
        except UnicodeDecodeError:
            counts["non_utf8"] += 1
            continue
        if updated == data:
            counts["ok"] += 1
        else:
            changes.append((relative, updated)); counts["needs_header"] += 1
    for relative, updated in changes:
        if args.write:
            (root / relative).write_bytes(updated)
            print(f"updated {relative}")
        else:
            print(f"needs-header {relative}")
    print("skipped " + " ".join(f"{reason}={counts[reason]}" for reason in ("unsafe", "binary", "non_utf8")))
    print(f"eligible ok={counts['ok']} needs_header={counts['needs_header']}")
    return 1 if changes and not args.write else 0


if __name__ == "__main__":
    raise SystemExit(main())
