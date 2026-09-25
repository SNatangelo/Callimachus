#!/usr/bin/env python3
# core/fetch/fallbacks/preview.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
preview.py — Phase 2c (opt-in, key-gated): targeted verification of book passages
against Google Books *preview snippets*.

Why this tier exists
--------------------
Books rarely have an Unpaywall-style machine-readable full text, so claims against a
book may need to wait for a user-provided file. Google Books indexes the *full text* of millions of
volumes and `volumes.list?q=` performs a **full-text search inside books**, returning
`searchInfo.textSnippet` — the fragment that matches the query, ~1 sentence of the
genuine source text.

So a verbatim passage can be confirmed *as a fragment of the real book*, targeted to
the claim. This is source-attributed evidence, not a third-party discussion, but it is
strictly weaker than full text (a one-sentence window: no surrounding
context, no way to assess contradiction, possibly a different edition). Hence its
reliability is **low** and a claim that holds only here can never be ✅ green.

The honest core (read before trusting a green-looking result)
-------------------------------------------------------------
Because we search the passage itself, the returned snippet trivially contains it.
The verification value is therefore NOT "the snippet contains the passage" — that is
near-tautological — but the two gates around it:

  1. EXISTENCE: did Google Books return *any* hit for the exact phrase? A fabricated
     passage returns zero results (empirically verified: a non-existent Darwin quote
     → totalItems 0). No hit ⇒ not provisioned ⇒ passage not verified.
  2. ATTRIBUTION: is the matched volume actually the *cited* book? The same phrase
     appears in many books (a quote reused in a companion, an unrelated novel…). We
     keep a snippet ONLY if the volume corroborates against the cited reference
     (matching ISBN, or sufficient title/author token overlap via sources.corroborate).

A snippet that survives both gates is stored as a normal source `.txt` (tier `web`,
origin `googlebooks`); final grounding then validates the verdict's
passages against it. Passages found only in *other* volumes are never stored, so they
correctly fail the guard.

Key-gated and opt-in
--------------------
Active ONLY when a Google Books API key is supplied (`--key` or env
`GOOGLE_BOOKS_API_KEY`). The keyless quota is per-IP and minimal (HTTP 429 in
practice); a key raises it to a per-project quota. The key does NOT unlock more text:
snippet availability is governed by the volume's snippet-view setting, not the key.
Without a key this module is a no-op (`status="skipped"`), and the default pipeline is
unchanged — no credential is ever required.

Usage:
  GOOGLE_BOOKS_API_KEY=... python run.py preview \\
      --run runs/<ts> --ref-id r1 --passage "exact phrase" [--passage "..."]
"""
from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from core.invocation import format_run_examples

try:
    from core.resolve import sources as _sources
    from core.fetch.transport.http_headers import open_request, request_headers
    from core.fetch.transport import transport_telemetry
    from core.infra.db import RunRepository
    from core.resolve.service import _extract_book_title
    from core.verify.claim_evidence.evidence.grounding import normalize_for_matching as _normalize
except ImportError:                                    # pragma: no cover - CLI fallback
    import sources as _sources
    from http_headers import open_request, request_headers
    from db import RunRepository
    from resolve.service import _extract_book_title
    from core.verify.claim_evidence.evidence.grounding import normalize_for_matching as _normalize

TIMEOUT = 15
MAX_RESULTS = 5            # volumes inspected per passage
MAX_QUERY_PASSAGE = 200    # Google truncates long phrase queries anyway
GB_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
ENV_KEY = "GOOGLE_BOOKS_API_KEY"

# Reuse the catalog's corroboration threshold: a volume is "the cited book" if its
# metadata shares enough distinctive tokens with the bibliographic entry.
_CORROBORATE_THRESHOLD = _sources.CORROBORATE_THRESHOLD

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
#  HTTP                                                                        #
# --------------------------------------------------------------------------- #

def _get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, headers=request_headers(url=url, accept="application/json", profile="api")
    )
    with open_request(req, timeout=TIMEOUT) as r:
        return r.status, r.read()


def _search(passage: str, constraint: str, api_key: str, country: str, *,
            run_dir: str | None = None, ref_id: str | None = None,
            env_credential: bool = False) -> list:
    """Return the Google Books `items` for a phrase query, or [] on any failure."""
    phrase = passage.strip()[:MAX_QUERY_PASSAGE]
    q = f'"{phrase}"'
    if constraint:
        q += " " + constraint
    url = GB_ENDPOINT + "?" + urllib.parse.urlencode(
        {"q": q, "maxResults": str(MAX_RESULTS), "country": country, "key": api_key})
    binding = ("google_books", ENV_KEY) if env_credential else None
    # An explicit --key is deliberately not claimed as an environment key.
    with transport_telemetry.logical_request(
        run_dir=run_dir, ref_id=ref_id, url=url, profile="api",
        strategy="google_books_preview", cache_outcome="not_applicable",
    ):
        started = time.monotonic()
        scope = (
            transport_telemetry.credential_scope(
                provider=binding[0], env_name=binding[1]
            ) if binding else contextlib.nullcontext()
        )
        with scope:
            try:
                _status, body = _get(url)
            except Exception as exc:
                transport_telemetry.record_attempt(
                    attempt_kind="primary", method="GET", url=url,
                    started=started, status=getattr(exc, "code", None), error=exc,
                )
                return []
            transport_telemetry.record_attempt(
                attempt_kind="primary", method="GET", url=url,
                started=started, status=_status,
            )
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
                return payload.get("items") or []
            except Exception:
                # The physical request already succeeded and was recorded above.
                # A malformed API response is not a second network attempt.
                return []


# --------------------------------------------------------------------------- #
#  Attribution helpers                                                         #
# --------------------------------------------------------------------------- #

def _author_surname(ref: dict) -> str | None:
    """Best-effort first-author surname for an `inauthor:` constraint."""
    authors = ref.get("authors")
    if isinstance(authors, list) and authors:
        first = authors[0]
        name = first.get("family") or first.get("name") or first if isinstance(first, dict) else first
        if isinstance(name, str) and name.strip():
            return name.strip().split()[-1].strip(",.")
    raw = ref.get("raw_entry") or ""
    m = re.match(r"\s*([A-ZÀ-Ý][A-Za-zà-ÿ'’-]+)\s*,", raw)
    return m.group(1) if m else None


def _constraint(ref: dict) -> str:
    """Attribution filter to pin the search to the cited book. Author is the strongest
    discriminator; title is the fallback. Empty string if neither is usable."""
    surname = _author_surname(ref)
    if surname:
        return f"inauthor:{surname}"
    title = _extract_book_title(ref)
    if title:
        return f"intitle:{title[:60]}"
    return ""


def _isbns(vol_info: dict) -> set:
    out = set()
    for ident in vol_info.get("industryIdentifiers") or []:
        digits = re.sub(r"[^0-9Xx]", "", ident.get("identifier", ""))
        if digits:
            out.add(digits.upper())
    return out


def _attribute(ref: dict, item: dict) -> tuple[str | None, float]:
    """Is this volume the cited book? Returns (signal, score).
    Strong: matching ISBN. Otherwise: token overlap of volume metadata vs entry."""
    vi = item.get("volumeInfo", {})
    ref_isbn = ref.get("isbn")
    if ref_isbn:
        clean = re.sub(r"[^0-9Xx]", "", ref_isbn).upper()
        if clean and clean in _isbns(vi):
            return "isbn", 1.0
    meta = " ".join(filter(None, [
        vi.get("title"), vi.get("subtitle"),
        " ".join(vi.get("authors") or []), vi.get("description"),
        " ".join(vi.get("publisher", "") if isinstance(vi.get("publisher"), str) else []),
    ]))
    return _sources.corroborate(ref, meta)


def _clean_snippet(raw: str) -> str:
    """Strip the <b> highlight tags and unescape entities from a textSnippet.

    Tags are removed with the EMPTY string, not a space: Google highlights whole
    tokens (`acknowledged</b>, that`), so a space would inject `acknowledged , that`
    and break the verbatim match that final grounding later performs against this text."""
    return _WS.sub(" ", html.unescape(_TAG.sub("", raw or ""))).strip()


# --------------------------------------------------------------------------- #
#  Public API                                                                  #
# --------------------------------------------------------------------------- #

def provision_preview(
    ref: dict,
    run_dir: str,
    passages: list[str],
    *,
    api_key: str | None = None,
    country: str = "US",
    store: bool = True,
) -> dict:
    """Confirm `passages` as fragments of the cited book via Google Books snippets.

    Returns a status dict:
      status: skipped     — no API key (opt-in tier inactive)
              no_passages — nothing to search
              not_found   — searched, no passage attributable to the cited book
              stored      — at least one attributed snippet stored as a source .txt
      passages: [{passage, found, attributed, signal, score, snippet}]
      stored_as, match_signal, match_score, matched_title — when status=stored
    """
    env_key = (os.environ.get(ENV_KEY) or "").strip()
    explicit_key = (api_key or "").strip()
    key_from_env = not explicit_key
    api_key = explicit_key or env_key
    if not api_key:
        return {"status": "skipped", "via": "googlebooks",
                "reason": f"no {ENV_KEY}: preview-snippet tier is opt-in (key-gated)"}
    passages = [p for p in (passages or []) if p and p.strip()]
    if not passages:
        return {"status": "no_passages", "via": "googlebooks",
                "reason": "no candidate passages to confirm"}

    constraint = _constraint(ref)
    results, kept_snippets = [], []
    best_signal, best_score, matched_title = None, 0.0, None

    for passage in passages:
        items = _search(
            passage, constraint, api_key, country, run_dir=run_dir,
            ref_id=ref.get("id"), env_credential=key_from_env,
        )
        rec = {"passage": passage, "found": bool(items),
               "attributed": False, "signal": None, "score": 0.0, "snippet": None}
        for item in items:
            signal, score = _attribute(ref, item)
            is_book = signal == "isbn" or (signal and score >= _CORROBORATE_THRESHOLD)
            if not is_book:
                continue
            snippet = _clean_snippet(item.get("searchInfo", {}).get("textSnippet", ""))
            # The snippet must actually carry the passage (study-guide hits about the
            # book describe it without quoting it — those must not count).
            if not snippet or not _contains(snippet, passage):
                continue
            rec.update(attributed=True, signal=signal, score=round(score, 3),
                       snippet=snippet)
            kept_snippets.append(snippet)
            if score >= best_score:
                best_signal, best_score = signal, score
                matched_title = item.get("volumeInfo", {}).get("title")
            break
        results.append(rec)

    if not kept_snippets:
        return {"status": "not_found", "via": "googlebooks", "passages": results,
                "reason": "no passage confirmed in a Google Books copy of the cited book"}

    out = {"status": "stored", "via": "googlebooks", "passages": results,
           "match_signal": best_signal, "match_score": round(best_score, 3),
           "matched_title": matched_title}
    if store:
        text = _build_source_text(ref, matched_title, kept_snippets)
        entry = _sources.store_text(
            run_dir, ref, "web", "googlebooks", text,
            source_ref=f"googlebooks:{matched_title}" if matched_title else "googlebooks",
            mapping=best_signal if best_signal == "isbn" else "tokens",
            signal=best_signal, score=round(best_score, 3))
        out["stored_as"] = entry["stored_as"]
    return out


def _contains(snippet: str, passage: str) -> bool:
    """True iff deterministic report matching finds the passage in the snippet.

    Uses the final boundary's deterministic normalizer so that a stored result is
    mechanically reproducible. Google Books OCR text often drops or shifts punctuation
    ('good fortune, must' → 'good fortune must'), so long passages spanning such
    boundaries will NOT match. At this tier the Verifier should quote short,
    distinctive phrases (which the ~1-sentence snippet window favours anyway)."""
    np = _normalize(passage)
    return bool(np) and np in _normalize(snippet)


def _build_source_text(ref: dict, matched_title: str | None, snippets: list[str]) -> str:
    """The stored .txt: a labelled bundle of preview fragments. Honest header so a
    human reading sources/ knows these are short snippets, not the full book."""
    header = (
        "# GOOGLE BOOKS PREVIEW SNIPPETS — fragments of the cited book's full text,\n"
        "# retrieved by targeted phrase search. NOT the full text: each snippet is a\n"
        "# ~1-sentence window with no surrounding context. Reliability: LOW.\n"
        f"# matched volume: {matched_title or '(title not reported)'}\n"
    )
    body = "\n\n".join(f"[snippet {i}] {s}" for i, s in enumerate(snippets, 1))
    return header + "\n" + body + "\n"


def _reference_from_run(run_dir: str, ref_id: str) -> dict:
    repo = RunRepository.open_readonly(run_dir)
    try:
        matches = [
            ref for ref in repo.effective_parse_payload().get("references", [])
            if ref.get("id") == ref_id
        ]
    finally:
        repo.close()
    if len(matches) != 1:
        raise SystemExit(f"reference {ref_id!r} is not present exactly once in the run")
    return matches[0]


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=format_run_examples(__doc__),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run directory (runs/<ts>)")
    ap.add_argument("--ref-id", required=True, help="reference id from the run database")
    ap.add_argument("--passage", action="append", default=[],
                    help="a verbatim passage to confirm (repeatable)")
    ap.add_argument("--key", help=f"Google Books API key (or env {ENV_KEY})")
    ap.add_argument("--country", default="US", help="country for the volumes API")
    ap.add_argument("--no-store", action="store_true",
                    help="probe only: do not write a source .txt")
    args = ap.parse_args()

    ref = _reference_from_run(args.run, args.ref_id)
    passages = list(args.passage)

    result = provision_preview(ref, args.run, passages,
                               api_key=args.key,
                               country=args.country, store=not args.no_store)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "stored" else 1)


if __name__ == "__main__":
    main()
