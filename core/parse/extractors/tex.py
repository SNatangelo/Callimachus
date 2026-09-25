#!/usr/bin/env python3
# core/parse/extractors/tex.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""LaTeX manuscript extractor."""

from __future__ import annotations

import glob
import os
import re


EXTENSIONS = (".tex",)
_BASE_CITE_NAMES = (
    "cite", "citep", "citet", "citealp", "citealt", "citeyear", "citeyearpar",
    "parencite", "autocite", "textcite", "footcite", "shortcite",
)
CITE_CMDS = r"(?:" + "|".join(_BASE_CITE_NAMES) + r")"
_BASE_CITE_RE = r"\\(?:" + "|".join(_BASE_CITE_NAMES) + r")\b"


def _brace_group(text: str, start: int) -> str:
    """Content of the balanced {...} group whose opening brace is at ``start``."""
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return ""


def _macro_defs(src: str):
    """Yield ``(name, nargs, body)`` for each macro defined in ``src`` via
    ``\\newcommand`` / ``\\renewcommand`` / ``\\providecommand`` / ``\\def``."""
    def _skip_ws(pos: int) -> int:
        while pos < len(src) and src[pos] in " \t":
            pos += 1
        return pos

    for m in re.finditer(r"\\(?:new|renew|provide)command\*?\s*\{?\\([A-Za-z@]+)\}?", src):
        j = _skip_ws(m.end())
        nargs = 0
        if j < len(src) and src[j] == "[":            # [nargs]
            k = src.find("]", j)
            if k > 0:
                inner = src[j + 1:k].strip()
                nargs = int(inner) if inner.isdigit() else 0
                j = _skip_ws(k + 1)
        if j < len(src) and src[j] == "[":            # [default value for #1]
            k = src.find("]", j)
            if k > 0:
                j = _skip_ws(k + 1)
        body = _brace_group(src, j) if j < len(src) and src[j] == "{" else ""
        yield m.group(1), nargs, body

    for m in re.finditer(r"\\def\s*\\([A-Za-z@]+)\s*((?:#\d)*)", src):
        j = _skip_ws(m.end())
        body = _brace_group(src, j) if j < len(src) and src[j] == "{" else ""
        yield m.group(1), len(re.findall(r"#\d", m.group(2))), body


def _custom_cite_aliases(src: str) -> set[str]:
    """Names of macros the preamble (or a .sty/.cls) defines as citation commands.

    Conference styles routinely alias natbib, e.g. ``\\newcommand\\newcite{\\citet}``
    (ACL/NAACL) or ``\\def\\shortcite{\\citeyearpar}``.  Such a macro's marker is
    invisible to a fixed \\cite list, so we treat any macro whose definition body
    invokes a base citation command as a citation command too."""
    return {name for name, _nargs, body in _macro_defs(src)
            if re.search(_BASE_CITE_RE, body)}


def _gobble_macros(src: str) -> set[str]:
    """Macros that discard their argument, e.g. ``\\newcommand{\\eat}[1]{\\ignorespaces}``
    used to comment a block out.  A one-argument macro whose body never
    references ``#1`` never renders its argument, so ``\\name{...}`` is dropped."""
    return {name for name, nargs, body in _macro_defs(src)
            if nargs >= 1 and "#" not in body}


def _strip_macro_calls(body: str, names: set[str]) -> str:
    """Remove ``\\name{...}`` (balanced braces) for every macro in ``names``."""
    if not names:
        return body
    pat = re.compile(r"\\(?:" + "|".join(re.escape(n) for n in names) + r")\b[ \t]*")
    out, i = [], 0
    while True:
        m = pat.search(body, i)
        if not m:
            out.append(body[i:])
            break
        out.append(body[i:m.start()])
        j = m.end()
        if j < len(body) and body[j] == "{":
            depth, k = 0, j
            while k < len(body):
                if body[k] == "{":
                    depth += 1
                elif body[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            i = k + 1
        else:
            i = j
    return "".join(out)


def _parse_bib(path: str) -> list[tuple[str, str]]:
    """Minimal .bib parser: (key, entry composed from available fields)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    entries = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", txt):
        if m.group(1).lower() in ("comment", "preamble", "string"):
            continue
        key = m.group(2)
        start = txt.index("{", m.start())
        depth, i = 0, start
        while i < len(txt):
            if txt[i] == "{":
                depth += 1
            elif txt[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = txt[start:i + 1]
        fields = {}
        for fm in re.finditer(
            r"(\w+)\s*=\s*(?:\{((?:[^{}]|\{[^{}]*\})*)\}|\"([^\"]*)\"|(\w+))",
            block,
        ):
            val = fm.group(2) or fm.group(3) or fm.group(4) or ""
            fields[fm.group(1).lower()] = re.sub(r"[{}]", "", val).strip()
        parts = []
        for f in ("author", "title", "journal", "booktitle", "publisher", "year"):
            if fields.get(f):
                parts.append(fields[f])
        core = ". ".join(parts)
        if fields.get("volume"):
            vol = fields["volume"]
            if fields.get("number"):
                vol += f"({fields['number']})"
            if fields.get("pages"):
                vol += f":{fields['pages']}"
            core += f". {vol}"
        if fields.get("doi"):
            core += f". doi:{fields['doi']}"
        if fields.get("url"):
            core += f". {fields['url']}"
        entries.append((key, core + "."))
    return entries


def _resolve_inputs(body: str, tex_dir: str, depth: int = 0) -> str:
    if depth > 10:
        return body

    def _replace(m: re.Match) -> str:
        name = m.group(1).strip()
        if not name.endswith(".tex"):
            name += ".tex"
        sub_path = os.path.join(tex_dir, name)
        if os.path.exists(sub_path):
            with open(sub_path, encoding="utf-8", errors="replace") as sf:
                sub = sf.read()
            sub = re.sub(r"(?<!\\)%.*", "", sub)
            sub = re.sub(r"\\(?:begin|end)\{document\}", "", sub)
            return _resolve_inputs(sub, tex_dir, depth + 1)
        return ""

    return re.sub(r"\\(?:input|include)\{([^}]+)\}", _replace, body)


def _tex_text(path: str) -> tuple[str, dict]:
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    src = re.sub(r"(?<!\\)%.*", "", src)
    m = re.search(r"\\begin\{document\}(.*)\\end\{document\}", src, re.DOTALL)
    body = m.group(1) if m else src
    tex_dir = os.path.dirname(os.path.abspath(path))
    body = _resolve_inputs(body, tex_dir)
    meta = {"bib_source": None, "unknown_cite_keys": {}}

    # Custom citation macros (\newcommand\newcite{\citet}, ...) live in the
    # preamble or the loaded .sty/.cls, not the document body.  Scan them so the
    # marker regex below also catches these aliases.
    alias_src = src
    for style_path in glob.glob(os.path.join(tex_dir, "*.sty")) + \
            glob.glob(os.path.join(tex_dir, "*.cls")):
        try:
            with open(style_path, encoding="utf-8", errors="replace") as sf:
                alias_src += "\n" + re.sub(r"(?<!\\)%.*", "", sf.read())
        except OSError:
            continue
    cite_names = list(_BASE_CITE_NAMES) + sorted(
        _custom_cite_aliases(alias_src) - set(_BASE_CITE_NAMES)
    )
    cite_cmds = r"(?:" + "|".join(re.escape(n) for n in cite_names) + r")"

    # Drop argument-gobbling macros (\eat{...}) so a block commented out in the
    # source doesn't leak text — or duplicate citations — into the parse.
    body = _strip_macro_calls(body, _gobble_macros(alias_src))

    bib_entries: list[tuple[str, str]] = []
    tb = re.search(
        r"\\begin\{thebibliography\}\{[^}]*\}(.*?)\\end\{thebibliography\}",
        body,
        re.DOTALL,
    )
    if tb:
        items = re.split(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}", tb.group(1))
        for i in range(1, len(items) - 1, 2):
            entry = " ".join(items[i + 1].replace("~", " ").split())
            entry = re.sub(r"\\newblock\s*", "", entry)
            entry = re.sub(r"\\em\s*", "", entry)
            entry = re.sub(r"\\(?:textbf|textit|emph|texttt)\{([^{}]*)\}", r"\1", entry)
            bib_entries.append((items[i].strip(), entry))
        body = body.replace(tb.group(0), "")
        meta["bib_source"] = "thebibliography"
    else:
        bm = re.search(r"\\bibliography\{([^}]+)\}", body)
        if bm:
            tex_dir = os.path.dirname(os.path.abspath(path))
            found = []
            for name in bm.group(1).split(","):
                bib_p = os.path.join(tex_dir, name.strip() + ".bib")
                if os.path.exists(bib_p):
                    bib_entries.extend(_parse_bib(bib_p))
                    found.append(os.path.basename(bib_p))
                    continue
                # BibTeX-generated .bbl fallback: parse the \bibitem entries
                bbl_p = os.path.join(tex_dir, name.strip() + ".bbl")
                if os.path.exists(bbl_p):
                    with open(bbl_p, encoding="utf-8", errors="replace") as bf:
                        bbl_text = bf.read()
                    tb = re.search(
                        r"\\begin\{thebibliography\}\{[^}]*\}(.*?)\\end\{thebibliography\}",
                        bbl_text, re.DOTALL)
                    if tb:
                        items = re.split(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}", tb.group(1))
                        for i in range(1, len(items) - 1, 2):
                            entry = " ".join(items[i + 1].replace("~", " ").split())
                            entry = re.sub(r"\\newblock\s*", "", entry)
                            entry = re.sub(r"\\em\s*", "", entry)
                            entry = re.sub(r"\\(?:textbf|textit|emph|texttt)\{([^{}]*)\}", r"\1", entry)
                            bib_entries.append((items[i].strip(), entry))
                    found.append(os.path.basename(bbl_p))
            body = body.replace(bm.group(0), "")
            meta["bib_source"] = ",".join(found) if found else "bib NOT found"

    key2num = {k: i + 1 for i, (k, _) in enumerate(bib_entries)}
    counter = [len(bib_entries)]

    def cite_repl(mm):
        nums = []
        for k in [k.strip() for k in mm.group(1).split(",")]:
            if k in key2num:
                nums.append(key2num[k])
            else:
                if k not in meta["unknown_cite_keys"]:
                    counter[0] += 1
                    meta["unknown_cite_keys"][k] = counter[0]
                nums.append(meta["unknown_cite_keys"][k])
        return "[" + ",".join(map(str, nums)) + "]"

    body = re.sub(r"\\" + cite_cmds + r"\*?(?:\[[^\]]*\])*\{([^}]+)\}", cite_repl, body)

    body = re.sub(r"\\(?:begin|end)\{[^}]*\}", "\n", body)
    body = re.sub(r"\\(?:sub)*section\*?\{([^}]*)\}", r"\n\1\n", body)
    body = re.sub(r"\\(?:textbf|textit|emph|underline|texttt|textsc|mbox)\{([^{}]*)\}", r"\1", body)
    body = re.sub(r"\\label\{[^}]*\}", "", body)
    body = re.sub(r"\\(?:ref|eqref|autoref)\{[^}]*\}", "REF", body)
    body = re.sub(r"\\[\[\]()]", " ", body)
    body = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", body)
    body = body.replace("{", " ").replace("}", " ").replace("~", " ")
    body = re.sub(r"[ \t]+", " ", body)

    if bib_entries:
        out = body + "\n\nReferences\n"
        out += "\n".join(f"[{i}] {t}" for i, (_k, t) in enumerate(bib_entries, 1))
    else:
        out = body
    return out, meta


def extract(path: str, *, ocr_lang: str, ocr_notice, manuscript_ocr: bool):
    del ocr_lang, ocr_notice, manuscript_ocr
    text, meta = _tex_text(path)
    # LaTeX never conveys a citation typographically: it writes \cite{key}, which we
    # expand into a numbered marker above.  Whether a digit is raised is a question
    # about the OUTPUT, and never one we have to ask of the source.
    meta.setdefault("superscript_source", "cite-commands")
    return text, "latex", meta
