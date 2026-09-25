#!/usr/bin/env python3
# core/parse/preprocess.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
preprocess.py — Preprocessor (spec §6), deterministic.

Prepares the text the Verifier will see, according to the tier (scope). Records
provenance: removed sections, truncation, sha of the preparation, and for RAG the
retrieved chunks. The model remains confined to its slot: here we choose only WHAT
it sees, and every choice is a recorded fact.

Scope:
  fulltext_complete : text as-is (truncated if it exceeds --max-chars).
  fulltext_trimmed  : removes terminator headings (Supplementary, Funding,
                      Acknowledgements, Conflicts, Data availability, References...),
                      records removed_sections.
  abstract_only     : text as-is (already an abstract).
  rag               : for low-context models. Splits full text into chunks,
                      lexical ranking (tf-idf) against the claim, keeps top-k in
                      original order, records retrieved_chunks. Reads the real source
                      but only in part -> medium reliability (strong positives, weaker
                      negatives: may not have seen the relevant passage).

Final grounding still validates passages against the FULL SOURCE TEXT, not against the
preparation: so a quote from RAG chunks remains verifiable.

Usage:
  python run.py preprocess --text sources/parsed/<n>_fulltext_user.txt --scope rag \
     --claim "<claim sentence>" --out /tmp/<ref_id>.rag.txt
"""
import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter

TERMINATORS = [
    "references", "bibliography", "bibliografia", "supplementary",
    "supplementary material", "acknowledgements", "acknowledgments",
    "acknowledgement", "author contributions", "funding", "conflicts of interest",
    "conflict of interest", "competing interests", "data availability",
    "declaration of competing interest", "ringraziamenti", "finanziamenti",
]

STOP = set("""a an the of to in and or for with on at by from as is are was were be been
being this that these those it its their his her our your we you they i not no than then
which who whom whose what when where how why all any both each few more most other some
such only own same so too very can will just di e il la lo gli le un una dei del della che
non per con su tra fra come piu' meno dove quando perche'""".split())

WORD = re.compile(r"[A-Za-zÀ-ÿ0-9]+")


def normalize_sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def tokens(s: str):
    return [w.lower() for w in WORD.findall(s) if w.lower() not in STOP and len(w) > 1]


def trim(text: str):
    """Cuts everything from the FIRST exact-line terminator heading onwards.

    The match requires that the ENTIRE line is the heading (not a word inside a
    sentence), so the §11 risk (the word 'references' mid-sentence) does not apply.
    Records all terminator headings that fall in the removed tail.
    """
    lines = text.split("\n")
    cut = None
    for i, line in enumerate(lines):
        s = line.strip().lower().rstrip(":.")
        if s in TERMINATORS:
            cut = i
            break
    if cut is None:
        return text, []
    removed = [l.strip() for l in lines[cut:]
               if l.strip().lower().rstrip(":.") in TERMINATORS]
    return "\n".join(lines[:cut]), removed


def chunkify(text: str, target_sentences: int = 4):
    """Splits into chunks: first by paragraphs; long paragraphs into sentence windows."""
    chunks = []
    for para in [p for p in text.split("\n") if p.strip()]:
        sents = re.split(r"(?<=[.!?])\s+", para.strip())
        if len(sents) <= target_sentences:
            chunks.append(para.strip())
        else:
            for i in range(0, len(sents), target_sentences):
                chunks.append(" ".join(sents[i:i + target_sentences]))
    return chunks or ([text.strip()] if text.strip() else [])


def rag_select(text: str, claim: str, topk: int):
    chunks = chunkify(text)
    n = len(chunks)
    if n <= topk:
        return text, [{"index": i, "score": None} for i in range(n)]
    # document frequency per term
    df = Counter()
    chunk_toks = []
    for ch in chunks:
        ts = tokens(ch)
        chunk_toks.append(ts)
        for t in set(ts):
            df[t] += 1
    idf = {t: math.log(1 + n / (1 + df[t])) for t in df}
    q = set(tokens(claim))
    if not q:
        # claim yields no meaningful tokens after stop-word filtering (empty or
        # all stop-words): rank by document order rather than hiding the issue
        # as all-zero scores. Flag in provenance so the orchestrator can see it.
        keep = list(range(min(topk, n)))
        prepared = "\n\n".join(chunks[i] for i in keep)
        retrieved = [{"index": i, "score": None, "note": "empty_query"} for i in keep]
        return prepared, retrieved
    scored = []
    for i, ts in enumerate(chunk_toks):
        tf = Counter(ts)
        score = sum(tf[t] * idf.get(t, 0.0) for t in q)
        scored.append((score, i))
    top = sorted(scored, key=lambda x: (-x[0], x[1]))[:topk]
    keep = sorted(i for _, i in top)
    prepared = "\n\n".join(chunks[i] for i in keep)
    retrieved = [{"index": i, "score": round(next(s for s, j in top if j == i), 3)} for i in keep]
    return prepared, retrieved


def preprocess(text: str, scope: str, claim: str, max_chars: int, topk: int):
    removed = []
    retrieved = None
    if scope == "fulltext_trimmed":
        text, removed = trim(text)
    elif scope == "rag":
        if not claim:
            raise SystemExit("scope=rag requires --claim")
        text, retrieved = rag_select(text, claim, topk)
    # abstract_only / fulltext_complete: text unchanged

    truncated_at = None
    if max_chars and len(text) > max_chars:
        truncated_at = max_chars
        text = text[:max_chars]

    prov = {
        "scope": scope,
        "removed_sections": removed,
        "truncated_at_chars": truncated_at,
        "retrieved_chunks": retrieved,
        "prepared_sha256": normalize_sha(text),
        "char_count": len(text),
    }
    return text, prov


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="source text (sources/parsed/<n>_<tier>_<origin>.txt)")
    ap.add_argument("--scope", required=True,
                    choices=["fulltext_complete", "fulltext_trimmed", "abstract_only", "rag"])
    ap.add_argument("--claim", help="claim sentence (required for scope=rag)")
    ap.add_argument("--max-chars", type=int, default=0, help="0 = no truncation")
    ap.add_argument("--topk", type=int, default=6, help="chunks kept by RAG")
    ap.add_argument("--out", required=True, help="where to write the prepared text")
    args = ap.parse_args()

    text = open(args.text, encoding="utf-8", errors="replace").read()
    prepared, prov = preprocess(text, args.scope, args.claim, args.max_chars, args.topk)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(prepared)
    print(json.dumps({"prepared_text": args.out, "provenance": prov}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
