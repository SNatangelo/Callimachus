#!/usr/bin/env python3
# core/resolve/sources.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
sources.py — management of retrieved sources: provenance manifest, file-to-reference
matcher with corroboration, normalisation to .txt.

Principle: the mapping "this file IS reference [n]" is not a model's act of faith.
Either it is deterministic (the file contains the DOI/PMID, or almost all tokens from
the bibliographic entry), or the model proposes and THIS code corroborates
(author+year / title present in the text). If not corroborated, no mapping:
it is flagged. Everything saved is recorded in the manifest as a PROVENANCE FACT,
never as a narrative.

Layout (in runs/<ts>/sources/):
  provided/<...>                     raw originals SUPPLIED BY THE USER (audit copy;
                                     the run stays self-contained). Downloads are NOT
                                     kept here — only their parsed text is saved.
  parsed/<ref_number>_<tier>_<origin>.txt
                                     normalised text of EVERY source (provided, fetched,
                                     web, OCR output). This is what preprocess/guard read.
  ocr_queue/<...>.unreadable.pdf     unreadable PDFs (scans) parked awaiting opt-in OCR;
                                     DELETED once OCR'd into parsed/ (see resolve_unreadable).
  (metadata lives in the run database; no JSON sidecar files are written)

tier   = fulltext | abstract | web        (reliability tier)
origin = user | crossref | europepmc | pubmed | unpaywall | openalex | core | arxiv | webfetch | websearch | googlebooks | manual
"""
import hashlib
import os
import re
import shutil
import tempfile
import threading
import unicodedata
import urllib.parse
from collections.abc import Iterator, Set

try:
    from core.fetch.storage import content_store
    from core.parse.manuscript_identity import bounded_front_matter
    from . import provider_config
    from core.infra.db import RunRepository
except ImportError:  # direct execution
    import content_store
    from parse.manuscript_identity import bounded_front_matter
    from resolve import provider_config
    from db import RunRepository

TIERS = ["fulltext", "abstract", "web"]          # from most to least reliable
TIER_RANK = {t: i for i, t in enumerate(reversed(TIERS))}  # fulltext ranks highest
_BASE_ORIGINS = frozenset({
    "user", "crossref", "europepmc", "pubmed", "webfetch", "websearch", "web_research",
    "manual", "unpaywall", "openlibrary", "openalex", "core", "arxiv", "ssrn", "ocr",
    "googlebooks", "internet_archive_item", "browser_session", "semantic_scholar", "elsevier", "acl", "jmlr", "neurips", "cvf",
    "pmlr", "curated_copies", "openai_reports",
})


def _provider_origins() -> set[str]:
    try:
        from . import providers
    except ImportError:  # direct execution fallback
        from resolve import providers
    origins = set()
    for module in providers.resolve_capable().values():
        origin = providers.origin_name(module)
        if origin:
            origins.add(origin)
    return origins


class _OriginRegistry(Set[str]):
    def _values(self) -> frozenset[str]:
        return _BASE_ORIGINS | frozenset(_provider_origins())

    def __contains__(self, value: object) -> bool:
        return value in self._values()

    def __iter__(self) -> Iterator[str]:
        return iter(self._values())

    def __len__(self) -> int:
        return len(self._values())


ORIGINS = _OriginRegistry()

# Subfolders under sources/ — the run keeps raw originals, parsed text and the OCR
# queue physically separate, so what a tool reads (parsed/) is never confused with
# raw inputs (provided/) or scans still awaiting OCR (ocr_queue/).
PARSED_SUBDIR = "parsed"
PROVIDED_SUBDIR = "provided"
OCR_SUBDIR = "ocr_queue"
_BOOTSTRAP_LOCK = threading.Lock()

# Thresholds (token-overlap entry<->text): strict auto-map, looser corroboration.
AUTO_THRESHOLD = 0.85
CORROBORATE_THRESHOLD = 0.60
# A preprint's DOI differs from the cited (published) DOI, so the strong identifier
# match is unavailable: a token match alone could latch onto a DIFFERENT paper with a
# similar title. For a non-record version we therefore demand a HIGHER title-token
# overlap AND, when known, the first-author surname.
PREPRINT_TITLE_THRESHOLD = 0.75
DOCUMENT_IDENTITY_TITLE_THRESHOLD = 0.75
DOCUMENT_IDENTITY_HEAD_CHARS = 6000
# PDF text extractors sometimes interleave title columns, running heads, or author
# blocks.  This is deliberately stricter than the ordinary head-title gate: it is
# only an escape hatch for an otherwise well identified front page, never a lower
# global title threshold.
DOCUMENT_IDENTITY_SPLIT_TITLE_OVERLAP = 0.85
DOCUMENT_IDENTITY_SPLIT_TITLE_ORDERED_RATIO = 0.85
DOCUMENT_IDENTITY_SPLIT_TITLE_MAX_SPAN_RATIO = 6.0

# Content version of a retrieved full text. Identity ("is this the same work?") and
# version ("preprint or version of record?") are SEPARATE axes — like the three axes
# never merged. Identity is answered by corroborate(); the version comes from provider
# metadata (OpenAlex/Unpaywall `version`) or a known preprint host. A non-record version
# is usable only PROVISIONALLY — never green — because peer review may have changed the
# very passage a claim cites.
PUBLISHED_VERSION = "published"
NON_RECORD_VERSIONS = ("preprint", "accepted_manuscript")
CONTENT_VERSIONS = (PUBLISHED_VERSION,) + NON_RECORD_VERSIONS
OFFICIALLY_SURFACED_COPY = "officially_surfaced_copy"
PROVENANCE_RELATIONS = (OFFICIALLY_SURFACED_COPY,)

# OpenAlex / Unpaywall report a per-location `version` field.
_VERSION_LABELS = {
    "submittedversion": "preprint",
    "acceptedversion": "accepted_manuscript",
    "publishedversion": PUBLISHED_VERSION,
}


def _url_host(url: str | None) -> str:
    if not url:
        return ""
    host = (urllib.parse.urlparse(url).netloc or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":", 1)[0]


_PREPRINT_PATTERN_CACHE: tuple[tuple[str, ...], tuple[re.Pattern[str], ...]] | None = None


def _preprint_markers() -> dict[str, tuple[str, ...]]:
    return provider_config.preprint_markers()


def _preprint_doi_patterns() -> tuple[re.Pattern[str], ...]:
    global _PREPRINT_PATTERN_CACHE
    raw_patterns = tuple(_preprint_markers().get("doi_patterns") or ())
    if _PREPRINT_PATTERN_CACHE is None or _PREPRINT_PATTERN_CACHE[0] != raw_patterns:
        _PREPRINT_PATTERN_CACHE = (
            raw_patterns,
            tuple(re.compile(pattern, re.I) for pattern in raw_patterns),
        )
    return _PREPRINT_PATTERN_CACHE[1]


def host_is_preprint(url: str | None) -> bool:
    host = _url_host(url)
    host_suffixes = _preprint_markers().get("host_suffixes") or ()
    return bool(host) and any(host == suffix or host.endswith(f".{suffix}") for suffix in host_suffixes)


def normalize_content_version(version: str | None) -> str | None:
    """Map a provider `version` string to a content_version, or None if unknown."""
    if not version:
        return None
    key = re.sub(r"[\s_-]+", "", str(version).strip().lower())
    return _VERSION_LABELS.get(key)


def preprint_identity_ok(ref: dict, text: str) -> bool:
    """Stricter identity gate for a non-record version (preprint / accepted manuscript).

    Identity ("same work?") stays separate from version: here we re-check identity at a
    higher bar because the cited identifier cannot vouch for a different-DOI preprint.
    Requires a high overlap of the cited entry's distinctive tokens (title preferred,
    else the raw entry) AND, when a first-author surname is known, its presence in the
    text."""
    ref_tokens = _tokens(ref.get("title") or "") or _tokens(ref.get("raw_entry") or "")
    if not ref_tokens:
        return False
    overlap = len(ref_tokens & _tokens(text)) / len(ref_tokens)
    if overlap < PREPRINT_TITLE_THRESHOLD:
        return False
    surname = str(ref.get("ay_surname") or "").strip().lower()
    if len(surname) >= 3 and surname not in _norm(text):
        return False
    return True


def ref_is_preprint(ref: dict, resolve_result: dict | None = None) -> bool:
    """True when the CITED source is itself a preprint (so reading the preprint is
    faithful to the citation, not a degraded stand-in for a version of record).
    Driven by the cited DOI shape and, when available, the resolver's work type."""
    rr = resolve_result or {}
    if str(rr.get("type") or "").strip().lower() in ("posted-content", "preprint"):
        return True
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(ref.get("doi") or "").strip(),
                 flags=re.IGNORECASE).strip()
    return any(pattern.match(doi) for pattern in _preprint_doi_patterns())


def content_version_for(url: str | None, version: str | None = None) -> str:
    """Resolve the content version of a retrieved text: an already-normalised value is
    kept as-is; otherwise raw provider metadata (`submittedVersion`...) is mapped, then
    a known preprint host, otherwise the version of record (published)."""
    normalized = version if version in CONTENT_VERSIONS else normalize_content_version(version)
    # PMC's article/JATS routes are authoritative full-text copies.  Candidate
    # providers can attach weaker "preprint" metadata to the same URL; do not
    # let that downgrade an exact PMC article.  Preserve manuscript evidence.
    if normalized == "preprint" and _is_authoritative_pmc_fulltext_url(url):
        return PUBLISHED_VERSION
    return normalized or (
        "preprint" if host_is_preprint(url) else PUBLISHED_VERSION
    )


def _is_authoritative_pmc_fulltext_url(url: str | None) -> bool:
    parsed = urllib.parse.urlparse(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"pmc.ncbi.nlm.nih.gov", "europepmc.org"}:
        return False
    # Covers HTML, PDF, and JATS/XML descendants while requiring an exact PMC
    # article identifier rather than a search/result or citation URL.
    return bool(re.search(r"/(?:articles|article)/(PMC\d+)(?:/|$)", parsed.path, re.I))

_STOP = set("""the and for with from that this study journal vol volume pp page pages
doi http https www org com pubmed pmid abstract et al eds editor edition press
springer elsevier wiley nature science lancet bmj jama cell proc natl acad sci
new york london received accepted published available online article review""".split())
_WORD = re.compile(r"[a-zA-Zà-ÿ]{3,}")

# A locator is not a title, wherever the question is asked; matching's own title
# guard reads this same pattern rather than keeping a second copy of the rule.
LOCATOR_TITLE_RE = re.compile(r"^(?:https?://|www\.|doi:|10\.\d{4,9}/)", re.IGNORECASE)

# Whether a title exists at all is a different question from which of its words are
# distinctive, so this deliberately does NOT use _STOP.  That list carries journal
# names — science, nature, cell, lancet — because they are noise when scanning a
# document's text; judged by it, "The skewness of science" is a titleless reference,
# which is how a real title came to be checked against nothing.
_TITLE_FUNCTION_WORDS = frozenset(
    "the and for with from that this into over under about".split())


def probe_title_is_usable(title: str | None) -> bool:
    """Whether there is enough of a title here to check a document against."""
    text = str(title or "").strip()
    if not text or LOCATOR_TITLE_RE.match(text):
        return False
    words = [word.lower() for word in _WORD.findall(text)]
    return len([word for word in words if word not in _TITLE_FUNCTION_WORDS]) >= 2


def _norm(s: str) -> str:
    text = unicodedata.normalize("NFC", s or "").lower()
    text = text.replace("\u00ad", "")
    # Rejoin words split by PDF/line-break hyphenation artifacts such as
    # "convolu- tional" or "im- age" before token and phrase matching.
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "", text)
    return text


def _tokens(s: str) -> set:
    return {w for w in _WORD.findall(_norm(s)) if w not in _STOP}


def _venue_aliases_from_ref(ref: dict) -> set[str]:
    raw = str(ref.get("raw_entry") or "")
    aliases: set[str] = set()
    for match in re.findall(r"\(([A-Za-z][A-Za-z0-9&./ -]{2,})\)", raw):
        alias = " ".join(match.split()).strip(" ,.;")
        if alias:
            aliases.add(_norm(alias))
    for match in re.finditer(r"\bIn\s+([^.;]+)", raw, flags=re.IGNORECASE):
        phrase = match.group(1)
        phrase = re.split(
            r",\s*(?:pages?|pp\.?|editors?|eds?\.?)\b",
            phrase,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        phrase = re.split(r",\s*\d{4}\b", phrase, maxsplit=1)[0]
        phrase = phrase.strip(" ,.;")
        if len(_tokens(phrase)) >= 2:
            aliases.add(_norm(phrase))
    return {alias for alias in aliases if alias}


def infer_provenance_relation(
    ref: dict,
    text: str,
    *,
    content_version: str | None = None,
) -> str | None:
    """Infer when a non-record copy is surfaced as an official citation-path copy."""
    if content_version not in NON_RECORD_VERSIONS or not text:
        return None
    aliases = _venue_aliases_from_ref(ref)
    if not aliases:
        return None
    front = _norm(text[:4000])
    if not any(marker in front for marker in (
        "published as",
        "published in",
        "appeared in",
        "appears in",
        "accepted at",
        "accepted for",
        "to appear in",
        "conference paper at",
        "workshop paper at",
        "poster at",
    )):
        return None
    cited_year = str(ref.get("year") or "").strip()
    if cited_year and cited_year not in front:
        return None
    if any(alias in front for alias in aliases):
        return OFFICIALLY_SURFACED_COPY
    return None


def non_record_identity_note(
    content_version: str,
    *,
    provenance_relation: str | None = None,
) -> str:
    label = "preprint" if content_version == "preprint" else "accepted manuscript"
    if provenance_relation == OFFICIALLY_SURFACED_COPY:
        return (
            f"retrieved text is the {label} publicly surfaced as an official "
            "citation-path copy; it is not the formal version of record, but it "
            "is not treated as a mismatched fallback"
        )
    return (
        f"retrieved text is the {label}, NOT the version of record; "
        "peer review may have changed the cited passage - verdict is provisional"
    )


def _phrase_words(s: str) -> list[str]:
    return re.findall(r"[a-zA-Zà-ÿ0-9]+", _norm(s))


def _phrase_text(s: str) -> str:
    return " ".join(_phrase_words(s))


def _title_key(text: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


_CITATION_TITLE_TOKEN_RE = re.compile(
    r"[^\W_]+(?:[-\u2010-\u2015\u2212][^\W_]+)*", re.UNICODE
)
_INLINE_MARKUP_RE = re.compile(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?|<[^>]+>")


def _citation_title_token_key(token: str) -> str:
    """Return a closed comparison key for one citation title token."""
    normalized = unicodedata.normalize("NFKD", token).casefold()
    return "".join(
        char for char in normalized
        if char.isalnum() and unicodedata.category(char) != "Mn"
    )


def _citation_title_tokens(text: str, *, preserve_offsets: bool = False) -> list[tuple[str, int, int]]:
    """Tokenize a possible citation title without borrowing candidate wording.

    Inline markup is masked, rather than removed, when offsets are needed so a
    matching span can be returned verbatim from the citation's raw entry.
    """
    value = str(text or "")
    if preserve_offsets:
        value = _INLINE_MARKUP_RE.sub(lambda match: " " * len(match.group(0)), value)
    else:
        value = _INLINE_MARKUP_RE.sub(" ", value)
    return [
        (key, match.start(), match.end())
        for match in _CITATION_TITLE_TOKEN_RE.finditer(value)
        if (key := _citation_title_token_key(match.group(0)))
    ]


def _citation_owned_title_span(raw_entry: str | None, candidate_title: str | None) -> str | None:
    """Return an exact normalized candidate-title span printed by this citation.

    A candidate may locate words in the citation, but cannot supply words absent
    from it.  Matching is contiguous on lexical tokens, so it cannot match a
    substring inside a larger word.  Hyphen glyphs within a word are normalized
    away (``Second-harmonic`` and ``Secondharmonic``), while whitespace remains a
    word boundary.
    """
    candidate_tokens = [key for key, _, _ in _citation_title_tokens(candidate_title or "")]
    raw_tokens = _citation_title_tokens(raw_entry or "", preserve_offsets=True)
    if not candidate_tokens or len(candidate_tokens) > len(raw_tokens):
        return None
    for start in range(len(raw_tokens) - len(candidate_tokens) + 1):
        window = raw_tokens[start:start + len(candidate_tokens)]
        if [key for key, _, _ in window] == candidate_tokens:
            raw = str(raw_entry or "")
            return raw[window[0][1]:window[-1][2]]
    return None


def _has_spaced_hyphen_artifact(text: str | None) -> bool:
    return bool(re.search(r"(?<=\w)-\s+(?=\w)", str(text or "")))


def _strip_leading_parenthesized_page_range(text: str) -> str | None:
    """Return the citation title after a parser-displaced page range."""
    match = re.fullmatch(
        r"\s*\(\s*(\d{1,5})\s*[-–—]\s*(\d{1,5})\s*\)\s*(.+?)\s*",
        str(text or ""),
    )
    if not match:
        return None
    first_page, last_page = int(match.group(1)), int(match.group(2))
    title = match.group(3).strip()
    if first_page <= 0 or last_page < first_page or len(_tokens(title)) < 3:
        return None
    return title


def _expected_probe_title_info(
    ref: dict,
    resolve_result: dict | None = None,
    candidate_context: dict | None = None,
) -> tuple[str, str]:
    parsed = str((ref or {}).get("title") or "").strip()
    matched = str(((resolve_result or {}).get("matched_title")) or "").strip()
    candidate = str(((candidate_context or {}).get("title")) or "").strip()
    parsed_tokens = len(_tokens(parsed))
    matched_tokens = len(_tokens(matched))
    raw_entry_key = _title_key((ref or {}).get("raw_entry"))
    matched_key = _title_key(matched)
    parsed_key = _title_key(parsed)
    try:
        from . import matching as _matching
    except ImportError:  # direct execution fallback
        from resolve import matching as _matching
    recovered = _matching.quoted_title((ref or {}).get("raw_entry"))
    recovered_key = _title_key(recovered)
    repaired_page_range_title = (
        _strip_leading_parenthesized_page_range(parsed)
        if parsed and matched
        else None
    )
    if (
        recovered
        and probe_title_is_usable(recovered)
        and recovered_key
        and parsed_key
        and recovered_key in parsed_key
    ):
        return recovered, "citation_quoted"
    # A clean resolver title is citation-derived (not circular) when both the
    # complete title and the parser's retained venue fragment are printed in
    # the citation's raw entry.  Requiring that retained fragment avoids
    # borrowing a resolver title when parsing yielded no title at all.
    if (
        parsed_key
        and parsed_key in raw_entry_key
        and matched_tokens >= 3
        and matched_key
        and matched_key in raw_entry_key
        and repaired_page_range_title is None
    ):
        return matched, "citation_raw_entry"
    if candidate:
        parsed_key = _title_key(parsed)
        candidate_key = _title_key(candidate)
        # Candidate records may decorate the real title with a venue/badge prefix
        # (for example "NIPS - Grammar as a foreign language").  A substantial
        # parsed title fully contained in that decoration remains the cleaner
        # expected document title, just as it does for matched resolver titles.
        if (
            parsed_tokens >= 3
            and parsed_key
            and candidate_key
            and parsed_key != candidate_key
            and parsed_key in candidate_key
        ):
            return parsed, "citation"
        candidate_span = _citation_owned_title_span(
            (ref or {}).get("raw_entry"), candidate
        )
        if candidate_span:
            return candidate_span, "citation_raw_entry"
    if parsed and matched:
        parsed_key = _title_key(parsed)
        matched_key = _title_key(matched)
        repaired = repaired_page_range_title
        repaired_tokens = _tokens(repaired) if repaired else set()
        matched_token_set = _tokens(matched)
        # Some extracted reference lists displace a journal page range to the
        # beginning of an otherwise intact title.  Remove only that closed
        # syntactic shape, and only when the primary resolution independently
        # corroborates almost all of its distinctive title tokens.  The
        # resulting yardstick still comes from the citation, not the resolver.
        if (
            repaired
            and parsed_key != matched_key
            and len(matched_token_set) >= 3
            and len(repaired_tokens & matched_token_set)
            / max(len(matched_token_set), 1)
            >= 0.9
        ):
            return repaired, "citation"
        if (
            parsed_tokens >= 3
            and parsed_key
            and matched_key
            and parsed_key == matched_key
            and _has_spaced_hyphen_artifact(parsed)
        ):
            return matched, "primary_resolution"
        # Resolver titles can include venue/badge noise while still containing the
        # real paper title. Prefer the cleaner parsed title when it is already a
        # substantial title and fully contained in the resolver title.
        if parsed_tokens >= 3 and parsed_key and matched_key and parsed_key in matched_key:
            return parsed, "citation"
        # If the parsed title is too short or truncated, the resolver's validated
        # title may supply the missing specificity -- but only where the citation
        # itself agrees with it.  A truncated "Introduction to the conll-" is
        # contained in "Introduction to the CoNLL-2003 shared task", so the
        # citation has voted and the longer form is the same work said in full.
        # Where it does not agree, borrowing the title asks the document whether
        # it is the work the resolver went looking for, which it will be whenever
        # the resolver was wrong -- see the comment on the removed fallback below.
        if (
            parsed_tokens < 3
            and matched_tokens >= 3
            and parsed_key
            and matched_key
            and parsed_key in matched_key
        ):
            return matched, "primary_resolution"
    if parsed and parsed_tokens >= 3:
        return parsed, "citation"
    # The parsed title is unusable, but the entry itself may print its title
    # verbatim in quotes -- 97 of the 119 corpus references left without one had it
    # printed in full, right there in their own text.  Recovering it here is not the
    # substitution removed below: it is the citation's own words, so the citation
    # still votes, and the document is held against what the manuscript actually
    # cited rather than against what we went looking for.
    if recovered and probe_title_is_usable(recovered):
        return recovered, "citation_quoted"
    # No fallback to the resolver's own title when the citation supplies none.
    # That check confirmed a document by comparing it against the very record the
    # resolver had chosen: circular, and it answered "yes" precisely when the
    # resolver had picked the wrong work.  Returning the (unusable) parsed title
    # sends the probe down its inconclusive path instead, which is the honest
    # answer -- an identity that could not be established, not one established by
    # our own guess.  The cost is real and accepted: a citation whose title we
    # failed to parse is no longer confirmable this way.
    return parsed, "citation"


def _expected_probe_title(
    ref: dict,
    resolve_result: dict | None = None,
    candidate_context: dict | None = None,
) -> str:
    """Return only the selected expected title."""
    return _expected_probe_title_info(ref, resolve_result, candidate_context)[0]


_OBSERVED_RESOLVED_TITLE_VARIANT_WORDS = frozenset({"a", "by", "in"})


def _resolved_title_is_citation_function_word_variant(
    citation_title: str, resolved_title: str
) -> bool:
    """Allow only the observed non-substantive citation/resolver title variants."""
    citation_words = _phrase_words(citation_title)
    resolved_words = _phrase_words(resolved_title)
    if len(citation_words) < 3 or len(resolved_words) < 3:
        return False
    if citation_words == resolved_words:
        return False
    citation_substantive = [
        word for word in citation_words if word not in _OBSERVED_RESOLVED_TITLE_VARIANT_WORDS
    ]
    resolved_substantive = [
        word for word in resolved_words if word not in _OBSERVED_RESOLVED_TITLE_VARIANT_WORDS
    ]
    return citation_substantive == resolved_substantive


def _ordered_title_window(words: list[str], text: str) -> tuple[int, int | None]:
    """Greedy ordered token match within *text*.

    Returns (matched_count, span_token_count). span_token_count is the size of the
    smallest greedy window that covered the ordered match; None when not all expected
    words were matched in order.
    """
    if not words:
        return 0, None
    got = _phrase_words(text)
    def equivalent(expected: str, observed: str) -> bool:
        if expected == observed:
            return True
        if min(len(expected), len(observed)) < 4:
            return False
        return bool(
            expected in {observed + "s", observed + "es"}
            or observed in {expected + "s", expected + "es"}
        )

    got_pos = 0
    expected_pos = 0
    matched = 0
    first_pos = last_pos = None
    while expected_pos < len(words):
        expected = words[expected_pos]
        while got_pos < len(got):
            end_pos = got_pos
            token_matches = equivalent(expected, got[got_pos])
            expected_words_matched = 1
            if (
                not token_matches
                and got_pos + 1 < len(got)
                and expected == got[got_pos] + got[got_pos + 1]
            ):
                token_matches = True
                end_pos = got_pos + 1
            elif (
                not token_matches
                and expected_pos + 1 < len(words)
                and expected + words[expected_pos + 1] == got[got_pos]
            ):
                token_matches = True
                expected_words_matched = 2
            if token_matches:
                if first_pos is None:
                    first_pos = got_pos
                last_pos = end_pos
                matched += expected_words_matched
                expected_pos += expected_words_matched
                got_pos = end_pos + 1
                break
            got_pos += 1
        else:
            break
    if matched != len(words) or first_pos is None or last_pos is None:
        return matched, None
    return matched, last_pos - first_pos + 1


def _first_author_surname(ref: dict) -> str | None:
    surname = str(ref.get("ay_surname") or "").strip().lower()
    if surname:
        return surname
    raw_entry = ref.get("raw_entry") or ""
    if not raw_entry:
        return None
    clean = re.sub(r"[{}]", "", raw_entry)
    clean = re.sub(r"\\[a-zA-Z]+\s*\{\}", "", clean)
    clean = re.sub(r"\\[a-zA-Z]+", "", clean)
    title = str(ref.get("title") or "").strip()
    if title:
        title_pattern = re.escape(title)
        title_pattern = re.sub(r"\\\s+", r"\\s+", title_pattern)
        m_title = re.search(title_pattern, clean, flags=re.IGNORECASE)
        if m_title and m_title.start() > 0:
            clean = clean[:m_title.start()].strip(" .")
    m = re.match(r"\s*([A-Z][^\W\d_][\w'’.-]+)\s+[A-Z]{2,4}\b", clean)
    if m:
        return m.group(1).lower()
    m = re.match(r"\s*([A-Z][^\W\d_][\w'’.-]+),\s*[A-Z]", clean)
    if m:
        return m.group(1).lower()
    m = re.match(r"\s*[A-Z][^\W\d_][\w'’.-]+(?:\s+[A-Z]\.)+\s+([A-Z][^\W\d_][\w'’.-]+)", clean)
    if m:
        return m.group(1).lower()
    first_author = re.split(r"\s+(?:and|&)\s+|,|;", clean, maxsplit=1)[0].strip()
    tokens = re.findall(r"[A-Z][^\W\d_][\w'’.-]*", first_author)
    if len(tokens) >= 2:
        if re.fullmatch(r"[A-Z]{2,4}", tokens[1]):
            return tokens[0].lower()
        return tokens[-1].lower()
    return None


def _title_is_isolated_in_head(expected_title: str, head_text: str) -> bool:
    title_phrase = _phrase_text(expected_title)
    if not title_phrase:
        return False
    for raw_line in (head_text or "").splitlines()[:80]:
        if _phrase_text(raw_line) == title_phrase:
            return True
    return False


def _front_matter_before_abstract(head_text: str) -> str:
    match = re.search(r"\babstract\b", head_text or "", flags=re.IGNORECASE)
    if match:
        return head_text[:match.start()]
    return (head_text or "")[:3000]


def _layout_split_context_signals(
    ref: dict,
    front_matter_text: str,
    candidate_context: dict | None,
) -> tuple[str, ...]:
    """Independent front-page signals for a layout-split title.

    A title whose words are spread across PDF extraction columns is safe to accept
    only when a second identity cue ties that front page to the citation.  Candidate
    context is useful here only when it is already marked as a canonical source (or
    supplies the cited year), so an arbitrary deduplicated URL cannot relax the gate.
    """
    front = _norm(front_matter_text)
    signals: list[str] = []
    cited_year = str(ref.get("year") or "").strip()
    if cited_year and re.search(rf"(?<!\d){re.escape(cited_year)}(?!\d)", front):
        signals.append("year")
    if any(alias in front for alias in _venue_aliases_from_ref(ref)):
        signals.append("venue")

    context = candidate_context or {}
    if not context.get("context_conflict"):
        candidate_title = str(context.get("title") or "")
        expected_title = str(ref.get("title") or "")
        title_tokens = _tokens(candidate_title)
        expected_tokens = _tokens(expected_title)
        candidate_title_matches = bool(
            title_tokens
            and expected_tokens
            and len(title_tokens & expected_tokens)
            / max(min(len(title_tokens), len(expected_tokens)), 1)
            >= DOCUMENT_IDENTITY_SPLIT_TITLE_OVERLAP
        )
        context_year = str(context.get("year") or "").strip()
        if candidate_title_matches and cited_year and context_year == cited_year:
            signals.append("candidate_context")
        if candidate_title_matches and context.get("canonical_host") is True:
            signals.append("canonical_host")
    return tuple(signals)


def document_identity_probe(
    ref: dict,
    text: str,
    resolve_result: dict | None = None,
    candidate_context: dict | None = None,
) -> dict:
    """Check whether the retrieved document looks like the cited work itself.

    This complements global corroboration by requiring the expected title/author
    signals to appear in the document front matter, not only somewhere later such
    as a bibliography or related-work section.
    """
    expected_title, expected_title_source = _expected_probe_title_info(
        ref, resolve_result, candidate_context
    )
    # PDF extraction can preserve compatibility glyphs (for example ``ﬁ``).
    # Keep that normalization confined to the identity comparison; callers retain
    # the extracted text and general resolver normalization unchanged.
    expected_title = unicodedata.normalize("NFKC", expected_title)
    text = unicodedata.normalize("NFKC", text or "")
    citation_title = unicodedata.normalize("NFKC", str(ref.get("title") or ""))
    resolved_title = unicodedata.normalize(
        "NFKC", str((resolve_result or {}).get("matched_title") or "")
    )
    context = candidate_context or {}
    context_title = unicodedata.normalize("NFKC", str(context.get("title") or ""))
    if (
        expected_title_source.startswith("citation")
        and context.get("canonical_host") is True
        and _title_key(context_title) == _title_key(resolved_title)
        and not context.get("context_conflict")
        and not context.get("identity_conflict")
        and not (resolve_result or {}).get("identity_conflict")
        and not (resolve_result or {}).get("metadata_conflict")
        and _resolved_title_is_citation_function_word_variant(citation_title, resolved_title)
    ):
        expected_title = resolved_title
        expected_title_source = "citation_resolved_function_word_variant"
    # Superscript extraction can split an alphanumeric title token, e.g.
    # ``MobileNetV⟦SUP:2⟧``.  Reattach only an immediately adjacent numeric
    # marker; ordinary citation superscripts remain separate evidence noise.
    text = re.sub(r"(?<=\w)⟦SUP:(\d+)⟧", r"\1", text)
    title_tokens = _tokens(expected_title)
    if not probe_title_is_usable(expected_title):
        # Nothing to check the document against.  This used to answer "confirmed",
        # which is confirmation by absence of evidence — the wrong direction for a
        # check whose job is to catch a document that is not the cited work.  The
        # honest answer is that identity could not be established, and the callers
        # already handle that: an exact arXiv/ACL/curated id, an explicitly cited
        # URL, or external corroboration each still carry the source through.
        return {
            "ok": False,
            "decision": "inconclusive",
            "reason_code": "no_expected_title_for_front_matter_probe",
            "reason": "no usable expected title to probe the document front matter with",
            "expected_title_source": expected_title_source,
            "title_overlap": None,
            "title_position": None,
            "author_ok": None,
        }

    head_text = (text or "")[:DOCUMENT_IDENTITY_HEAD_CHARS]
    front_matter_text = _front_matter_before_abstract(head_text)
    head_tokens = _tokens(head_text)
    title_overlap = len(title_tokens & head_tokens) / len(title_tokens) if title_tokens else 0.0
    title_phrase = _phrase_text(expected_title)
    front_matter_phrase = _phrase_text(front_matter_text)
    full_phrase = _phrase_text(text)
    title_position = full_phrase.find(title_phrase) if title_phrase else -1
    title_in_head = bool(title_phrase) and title_phrase in front_matter_phrase
    references_positions = [
        pos for pos in (full_phrase.find(" references "), full_phrase.find(" bibliography "))
        if pos >= 0
    ]
    references_position = min(references_positions) if references_positions else -1
    title_only_in_bibliography = (
        title_position >= 0
        and references_position >= 0
        and title_position > references_position
        and not title_in_head
    )
    ordered_title_words = _phrase_words(expected_title)
    ordered_title_match, ordered_title_span = _ordered_title_window(ordered_title_words, head_text)
    ordered_title_ratio = (
        ordered_title_match / len(ordered_title_words) if ordered_title_words else 0.0
    )
    short_title = len(title_tokens) < 3
    surname = _first_author_surname(ref)
    author_ok = None
    author_ok_front = None
    if surname:
        surname_phrase = _phrase_text(surname)
        author_ok = surname_phrase in _phrase_text(head_text)
        author_ok_front = surname_phrase in _phrase_text(front_matter_text)
    dense_title_in_head = bool(
        ordered_title_words
        and ordered_title_match == len(ordered_title_words)
        and ordered_title_span is not None
        and ordered_title_span / max(len(ordered_title_words), 1) <= 2.5
    )
    span_ratio = (
        ordered_title_span / max(len(ordered_title_words), 1)
        if ordered_title_span is not None
        else None
    )
    near_complete_title_with_missing_glyph = (
        author_ok is True
        and 0.85 <= title_overlap < 1.0
        and 0.85 <= ordered_title_ratio < 1.0
    )
    compact_title_with_author = (
        author_ok is True
        and title_overlap >= 0.95
        and ordered_title_match == len(ordered_title_words)
        and span_ratio is not None
        and span_ratio <= 6.0
    )
    relaxed_title_in_head = near_complete_title_with_missing_glyph or compact_title_with_author
    layout_split_signals = _layout_split_context_signals(
        ref, front_matter_text, candidate_context
    )
    layout_split_title_with_context = bool(
        author_ok_front is True
        and title_overlap >= DOCUMENT_IDENTITY_SPLIT_TITLE_OVERLAP
        and ordered_title_ratio >= DOCUMENT_IDENTITY_SPLIT_TITLE_ORDERED_RATIO
        and span_ratio is not None
        and span_ratio <= DOCUMENT_IDENTITY_SPLIT_TITLE_MAX_SPAN_RATIO
        and any(signal in {"year", "venue"} for signal in layout_split_signals)
    )

    if title_only_in_bibliography and not layout_split_title_with_context:
        return {
            "ok": False,
            "decision": "rejected",
            "reason_code": "expected_title_only_in_bibliography",
            "reason": "expected title not found in front matter; found only in document bibliography",
            "expected_title_source": expected_title_source,
            "title_overlap": round(title_overlap, 3),
            "title_position": title_position if title_position >= 0 else None,
            "title_ordered_ratio": round(ordered_title_ratio, 3),
            "title_span_ratio": (
                round(ordered_title_span / max(len(ordered_title_words), 1), 3)
                if ordered_title_span is not None
                else None
            ),
            "author_ok": author_ok,
        }

    if title_overlap < DOCUMENT_IDENTITY_TITLE_THRESHOLD and not title_in_head and not dense_title_in_head:
        return {
            "ok": False,
            "decision": "inconclusive",
            "reason_code": "front_matter_unreadable",
            "reason": "expected title not found in document front matter",
            "expected_title_source": expected_title_source,
            "title_overlap": round(title_overlap, 3),
            "title_position": title_position if title_position >= 0 else None,
            "title_ordered_ratio": round(ordered_title_ratio, 3),
            "title_span_ratio": (
                round(ordered_title_span / max(len(ordered_title_words), 1), 3)
                if ordered_title_span is not None
                else None
            ),
            "author_ok": author_ok,
        }

    if (
        not title_in_head
        and not dense_title_in_head
        and not relaxed_title_in_head
        and not layout_split_title_with_context
    ):
        return {
            "ok": False,
            "decision": "inconclusive",
            "reason_code": "title_tokens_dispersed",
            "reason": "expected title tokens were too dispersed in document front matter",
            "expected_title_source": expected_title_source,
            "title_overlap": round(title_overlap, 3),
            "title_position": title_position if title_position >= 0 else None,
            "title_ordered_ratio": round(ordered_title_ratio, 3),
            "title_span_ratio": (
                round(ordered_title_span / max(len(ordered_title_words), 1), 3)
                if ordered_title_span is not None
                else None
            ),
            "author_ok": author_ok,
        }

    if surname:
        if short_title and (
            not author_ok_front
            or not _title_is_isolated_in_head(expected_title, head_text)
        ):
            return {
                "ok": False,
                "decision": "inconclusive",
                "reason_code": "short_title_not_isolated",
                "reason": (
                    "short expected title did not have an isolated title and cited first-author "
                    "match in document front matter"
                ),
                "expected_title_source": expected_title_source,
                "title_overlap": round(title_overlap, 3),
                "title_position": title_position if title_position >= 0 else None,
                "title_ordered_ratio": round(ordered_title_ratio, 3),
                "title_span_ratio": (
                    round(ordered_title_span / max(len(ordered_title_words), 1), 3)
                    if ordered_title_span is not None
                    else None
                ),
                "author_ok": bool(author_ok_front),
            }
        if not author_ok and title_overlap < 0.95 and not title_in_head:
            return {
                "ok": False,
                "decision": "inconclusive",
                "reason_code": "front_matter_author_unreadable",
                "reason": "expected first-author surname not found in document front matter",
                "expected_title_source": expected_title_source,
                "title_overlap": round(title_overlap, 3),
                "title_position": title_position if title_position >= 0 else None,
                "title_ordered_ratio": round(ordered_title_ratio, 3),
                "title_span_ratio": (
                    round(ordered_title_span / max(len(ordered_title_words), 1), 3)
                    if ordered_title_span is not None
                    else None
                ),
                "author_ok": False,
            }

    return {
        "ok": True,
        "decision": "confirmed",
        "reason_code": (
            "identity_confirmed_layout_split_front_matter"
            if layout_split_title_with_context else "identity_confirmed_front_matter"
        ),
        "reason": (
            "layout_split_front_matter_identity_confirmed"
            if layout_split_title_with_context else "front_matter_identity_confirmed"
        ),
        "expected_title_source": expected_title_source,
        "title_overlap": round(title_overlap, 3),
        "title_position": title_position if title_position >= 0 else None,
        "title_ordered_ratio": round(ordered_title_ratio, 3),
        "title_span_ratio": (
            round(ordered_title_span / max(len(ordered_title_words), 1), 3)
            if ordered_title_span is not None
            else None
        ),
        "author_ok": author_ok,
        "relaxed_front_matter_match": relaxed_title_in_head,
        "layout_split_front_matter_match": layout_split_title_with_context,
        "layout_split_context_signals": list(layout_split_signals),
    }


def manuscript_title_identity_probe(expected_title: str, text: str) -> dict:
    """Corroborate an identifier-resolved title in manuscript front matter.

    Unlike ``document_identity_probe``, this boundary does not pretend that a
    resolver title came from a citation.  Its caller must already have resolved
    an identifier extracted from the uploaded manuscript itself.  The check is
    deliberately strict: the complete title must occur before the abstract,
    either as a phrase or as one compact ordered layout sequence.
    """
    expected_title = unicodedata.normalize("NFKC", str(expected_title or ""))
    text = unicodedata.normalize("NFKC", text or "")
    if not probe_title_is_usable(expected_title):
        return {
            "ok": False,
            "decision": "inconclusive",
            "reason_code": "no_resolved_title_for_manuscript_probe",
            "reason": "no usable identifier-resolved title to probe",
        }
    front_matter = bounded_front_matter(text[:DOCUMENT_IDENTITY_HEAD_CHARS])
    expected_phrase = _phrase_text(expected_title)
    front_phrase = _phrase_text(front_matter)
    if expected_phrase and expected_phrase in front_phrase:
        return {
            "ok": True,
            "decision": "confirmed",
            "reason_code": "manuscript_title_confirmed_front_matter",
            "reason": "identifier-resolved title found in manuscript front matter",
        }
    words = _phrase_words(expected_title)
    ordered_match, ordered_span = _ordered_title_window(words, front_matter)
    span_ratio = ordered_span / max(len(words), 1) if ordered_span is not None else None
    if (
        words
        and ordered_match == len(words)
        and span_ratio is not None
        and span_ratio <= 2.5
    ):
        return {
            "ok": True,
            "decision": "confirmed",
            "reason_code": "manuscript_title_confirmed_compact_layout",
            "reason": "identifier-resolved title found as a compact front-matter layout sequence",
        }
    return {
        "ok": False,
        "decision": "inconclusive",
        "reason_code": "resolved_title_not_in_manuscript_front_matter",
        "reason": "identifier-resolved title not found in manuscript front matter",
    }


def corroborate(ref: dict, text: str, resolve_result: dict | None = None,
                detail: dict | None = None):
    """(signal, score in [0,1]). Strong signals: DOI/PMID present in the text.
    Otherwise: overlap of distinctive tokens from the bibliographic entry with the text.
    When resolve_result is provided, its discovered DOI is also checked — prevents
    accepting a wrong file fetched via a resolver-discovered DOI.

    Pass a dict as *detail* to learn WHICH check answered, and what the resolution's
    status was at that moment.  ``signal`` alone cannot distinguish the citation's
    own DOI from one the resolver discovered, and the manifest's stored signal is a
    snapshot from storage time while a resolve status is the final one — so reading
    the two together after the fact does not reconstruct this.  Purely descriptive:
    nothing here reads *detail* back.
    """
    def _seen(branch: str):
        if detail is not None:
            detail["branch"] = branch
            detail["resolve_status"] = (resolve_result or {}).get("status")

    t = _norm(text)
    doi = (ref.get("doi") or "").lower()
    if doi and doi in t:
        _seen("cited_doi")
        return "doi", 1.0
    # Also check the DOI discovered by the resolver (not in the original ref entry).
    # If we fetched via this DOI, the downloaded text should contain it.
    #
    # Gated on the resolver having accepted the record, exactly as the matched title
    # below is. Both answer "is this the document the resolver pointed at?", which is
    # only the same question as "is this the cited work?" while the resolver's own
    # answer stands. Once it has refused the record, the citation's question has to be
    # answered from the citation's own tokens, which is what the fall-through does.
    if resolve_result:
        rr_doi = _norm(resolve_result.get("doi") or "")
        if rr_doi and rr_doi in t and resolve_result.get("status") == "resolved":
            _seen("resolver_doi")
            return "doi", 1.0
        # The matched title stands in for the citation only if the resolver
        # accepted the match. When it returned "unverified" it is saying it does
        # not believe this record is the cited work, and borrowing the title
        # anyway turns that refusal into a perfect score: a law-review note for
        # "An In-Depth Analysis of the Mirai Botnet" whose parsed title had
        # decayed to the venue fragment "on Software Sec" drew a JOSS paper about
        # a Python simulation library, and the JOSS PDF was then stored as its
        # full text at match_score 1.0 - the two sharing only an author surname.
        # A low existence_confidence is NOT disqualifying: a book resolved by
        # title carries it routinely, and Goodfellow's "Deep learning" corroborates
        # correctly through this path.
        rr_title = resolve_result.get("matched_title") or ""
        if rr_title and resolve_result.get("status") == "resolved":
            rr_title_tokens = _tokens(rr_title)
            text_tokens_temp = _tokens(text)
            if rr_title_tokens and len(rr_title_tokens & text_tokens_temp) / len(rr_title_tokens) >= 0.80:
                _seen("resolver_title")
                return "title", 1.0
    pmid = ref.get("pmid")
    if pmid and re.search(rf"\b{re.escape(str(pmid))}\b", t):
        _seen("cited_pmid")
        return "pmid", 1.0
    ref_tokens = _tokens(ref.get("raw_entry", "")) | _tokens(ref.get("title") or "")
    if not ref_tokens:
        _seen("none")
        return None, 0.0
    text_tokens = _tokens(text)
    overlap = len(ref_tokens & text_tokens) / len(ref_tokens)
    _seen("cited_tokens")
    return "tokens", round(overlap, 3)


_DOI_URL_HOSTS = frozenset({"doi.org", "www.doi.org", "dx.doi.org", "www.dx.doi.org"})
_DOI_SHAPE_RE = re.compile(r"10\.\d{4,9}/\S+", re.IGNORECASE)


def _doi_url_value(value: object) -> str | None:
    """Return a normalised DOI only when ``value`` is a DOI resolver URL."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or (parsed.hostname or "").lower() not in _DOI_URL_HOSTS:
        return None
    candidate = urllib.parse.unquote(parsed.path.lstrip("/"))
    match = _DOI_SHAPE_RE.fullmatch(candidate)
    return match.group(0).rstrip(".,;)").lower() if match else None


def weak_metadata_abstract_invalidated(
    ref: dict,
    resolve_result: dict,
    fetch_attempts: list[dict] | tuple[dict, ...],
) -> bool:
    """Whether a resolver abstract is unusable after its exact DOI is rejected.

    This is deliberately narrower than a generic fetch failure: it protects
    explicit identifiers and preserves the raw resolver payload for audit.
    """
    if not resolve_result.get("abstract"):
        return False
    if (
        resolve_result.get("via") != "crossref_metadata"
        or resolve_result.get("resolution_basis") != "metadata_search"
    ):
        return False
    if any(str(ref.get(key) or "").strip() for key in ("doi", "pmid", "isbn", "url")):
        return False

    candidate_dois = set()
    for role, links in (
        ("primary", resolve_result.get("fulltext_links") or []),
        ("auxiliary", resolve_result.get("auxiliary_fulltext_links") or []),
    ):
        for link in links:
            if not isinstance(link, dict):
                continue
            discovered_via = str(link.get("discovered_via") or "").lower()
            if discovered_via and discovered_via not in {
                "crossref",
                "crossref_metadata",
            }:
                continue
            if role == "auxiliary" and not discovered_via:
                continue
            doi = _doi_url_value(link.get("url"))
            if doi:
                candidate_dois.add(doi)
    if not candidate_dois:
        return False

    attempts = [attempt for attempt in fetch_attempts if isinstance(attempt, dict)]
    for index, attempt in enumerate(attempts):
        attempted_doi = _doi_url_value(attempt.get("url") or attempt.get("final_url"))
        if attempted_doi not in candidate_dois:
            continue
        if (
            attempt.get("method") == "metadata"
            and attempt.get("origin") == "crossref"
            and attempt.get("outcome") == "identity_mismatch"
            and attempt.get("reason") == "landing page identifier conflict"
        ):
            return not any(later.get("outcome") == "stored" for later in attempts[index + 1:])
    return False


# ---------------- manifest ----------------

def parsed_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "sources", PARSED_SUBDIR)


def provided_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "sources", PROVIDED_SUBDIR)


def ocr_queue_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "sources", OCR_SUBDIR)


def _repo_open(run_dir: str):
    # Only an absent database permits sources' standalone bootstrap path.
    # Existing databases whose open fails must remain visible to the caller so
    # provenance is never written to a replacement database.
    if not os.path.exists(os.path.join(run_dir, "run.sqlite")):
        return None
    return RunRepository.open(run_dir)


def _repo_require(run_dir: str):
    repo = _repo_open(run_dir)
    if repo is not None:
        return repo
    with _BOOTSTRAP_LOCK:
        repo = _repo_open(run_dir)
        if repo is not None:
            return repo
        os.makedirs(run_dir, exist_ok=True)
        run_id = "bootstrap-" + hashlib.sha256(
            os.path.abspath(run_dir).encode("utf-8")
        ).hexdigest()[:12]
        return RunRepository.create(
            run_dir,
            run_id=run_id,
            input_path="",
            input_sha256="",
            accuracy="",
            style=None,
            model_id=None,
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="sources-bootstrap",
            run_origin="bootstrap",
        )


def _ensure_reference(repo, ref: dict | None):
    if not ref or not ref.get("id"):
        return
    with repo._connection_lock:
        ref_id = str(ref["id"])
        if repo._conn.execute(
            "SELECT 1 FROM operational_references WHERE ref_id=?", (ref_id,)
        ).fetchone() is not None:
            return
        existing = repo.get_reference(ref_id)
        ref_number = existing.ref_number if existing is not None else ref.get("ref_number")
        if ref_number is None:
            ref_number = int(repo._conn.execute(
                "SELECT COALESCE(MAX(ref_number),0)+1 FROM operational_references"
            ).fetchone()[0])
        with repo._conn:
            if existing is None:
                repo._conn.execute(
                """
                INSERT INTO reference_entries(
                  ref_id, ref_number, raw_entry, title, doi, pmid, isbn, url, year,
                  source_type, source_kind, indexability, source_type_confidence
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref_id,
                    int(ref_number),
                    str(ref.get("raw_entry") or ""),
                    ref.get("title"),
                    ref.get("doi"),
                    ref.get("pmid"),
                    ref.get("isbn"),
                    ref.get("url"),
                    ref.get("year"),
                    ref.get("source_type"),
                    ref.get("source_kind"),
                    ref.get("indexability"),
                    ref.get("source_type_confidence"),
                ),
            )
            repo._conn.execute(
                "INSERT INTO operational_references VALUES(?,?,?,?,?,?,?)",
                (ref_id, int(ref_number), "raw_parse", ref_id, None, None, _now()),
            )


def _source_text_id(entry: dict) -> str:
    key = f"{entry.get('ref_id')}|{entry.get('tier')}|{entry.get('origin')}|{entry.get('stored_as')}"
    return "src-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

def load_manifest(run_dir: str) -> dict:
    repo = _repo_require(run_dir)
    try:
        return repo.source_manifest_payload()
    finally:
        repo.close()


def save_manifest(run_dir: str, man: dict):
    repo = _repo_require(run_dir)
    try:
        for entry in man.get("entries", []):
            repo.store_source_text(
                source_text_id=_source_text_id(entry),
                ref_id=entry["ref_id"],
                identity_key=f"path:{entry['stored_as']}",
                tier=entry["tier"],
                origin=entry["origin"],
                stored_path=os.path.join("sources", entry["stored_as"]).replace("\\", "/"),
                sha256=entry["sha256"],
                char_count=int(entry.get("char_count") or 0),
                source_ref=entry.get("source_ref"),
                mapping=entry.get("mapping"),
                match_signal=entry.get("match_signal"),
                match_score=entry.get("match_score"),
                identity_status=entry.get("identity_status"),
                identity_note=entry.get("identity_note"),
                content_version=entry.get("content_version"),
                provenance_relation=entry.get("provenance_relation"),
                extraction_flags=entry.get("extraction_flags"),
                extraction_method=entry.get("extraction_method"),
            )
    finally:
        repo.close()


def best_for(man: dict, ref_id: str):
    """The cleanest entry at the highest available tier for a reference."""
    cands = [e for e in man.get("entries", []) if e.get("ref_id") == ref_id]
    if not cands:
        return None
    return max(
        cands,
        key=lambda e: (
            TIER_RANK.get(e.get("tier"), -1),
            not bool(e.get("extraction_flags")),
        ),
    )


def delete_text(
    run_dir: str,
    ref_id: str,
    *,
    tier: str | None = None,
    origin: str | None = None,
    source_ref: str | None = None,
) -> list[dict]:
    repo = _repo_require(run_dir)
    try:
        deleted = repo.delete_source_texts(
            ref_id,
            tier=tier,
            origin=origin,
            source_ref=source_ref,
        )
    finally:
        repo.close()
    removed = []
    for row in deleted:
        stored_path = str(row.stored_path or "").replace("/", os.sep)
        abs_path = os.path.join(run_dir, stored_path)
        try:
            if abs_path and os.path.exists(abs_path):
                os.unlink(abs_path)
        except OSError:
            pass
        removed.append({
            "ref_id": row.ref_id,
            "tier": row.tier,
            "origin": row.origin,
            "stored_path": row.stored_path,
            "source_ref": row.source_ref,
        })
    return removed


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _stored_structure_flags(tier: str, text: str, supplied: list[str] | None) -> list[str]:
    """Compute flags on the exact text persisted for verification."""
    if tier != "fulltext":
        return sorted(set(supplied or []))
    try:
        from core.fetch.extraction.pdf import structure_flags
        return structure_flags(text)
    except (ImportError, ModuleNotFoundError):
        return sorted(set(supplied or []))


def reconcile_materialized_texts(run_dir: str) -> int:
    """Finish only assets that have a durable, typed materialization intent."""
    repo = _repo_open(run_dir)
    if repo is None:
        return 0
    try:
        return repo.reconcile_source_text_materializations()
    finally:
        repo.close()


def _after_materialization_replace() -> None:
    """Narrow fault-injection seam for the publish-to-registration boundary."""
    return None


def store_text(run_dir, ref, tier, origin, text, *, source_ref=None,
               mapping="manual", signal=None, score=None,
               identity_status=None, identity_note=None,
               content_version=None, provenance_relation=None, supplied_by=None,
               supplied_via=None, file_format=None,
               library_item_id=None, extraction_flags=None,
               extraction_method=None) -> dict:
    """Writes the normalised .txt under sources/parsed/ and records/updates the entry
    in the manifest. Returns the entry. Dedup by (ref_id, tier, origin): last write wins.

    stored_as is kept relative to sources/ (e.g. 'parsed/3_fulltext_user.txt'), so the
    long-standing readers that do join(run_dir, 'sources', stored_as) resolve unchanged."""
    assert tier in TIERS, tier
    assert origin in ORIGINS, origin
    pdir = parsed_dir(run_dir)
    refnum = ref.get("ref_number")
    base = f"{refnum}_{tier}_{origin}.txt" if refnum is not None \
        else f"{_safe(ref['id'])}_{tier}_{origin}.txt"
    stored_as = f"{PARSED_SUBDIR}/{base}"
    stored_flags = _stored_structure_flags(tier, text, extraction_flags)
    entry = {
        "ref_id": ref["id"], "ref_number": refnum,
        "tier": tier, "origin": origin, "stored_as": stored_as,
        "source_ref": source_ref,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "char_count": len(text),
        "mapping": mapping, "match_signal": signal, "match_score": score,
        "recorded_at": _now(),
    }
    if identity_status:
        entry["identity_status"] = identity_status
    if identity_note:
        entry["identity_note"] = identity_note
    if provenance_relation in PROVENANCE_RELATIONS:
        entry["provenance_relation"] = provenance_relation
    if supplied_by:
        entry["supplied_by"] = supplied_by
    if supplied_via:
        entry["supplied_via"] = supplied_via
    if file_format:
        entry["file_format"] = file_format
    if stored_flags:
        entry["extraction_flags"] = stored_flags
    if extraction_method:
        entry["extraction_method"] = extraction_method
    # A non-record version (preprint / accepted manuscript) is recorded as data, never
    # silently absorbed: the entry carries the version and, absent a caller note, an
    # explicit warning that this is NOT the version of record.
    if content_version in NON_RECORD_VERSIONS:
        entry["content_version"] = content_version
        if "identity_note" not in entry:
            entry["identity_note"] = non_record_identity_note(
                content_version,
                provenance_relation=entry.get("provenance_relation"),
            )
    stored_path = os.path.join("sources", stored_as).replace("\\", "/")
    repo = _repo_require(run_dir)
    try:
        _ensure_reference(repo, ref)
        intent_id = repo.prepare_source_text_materialization(
            source_text_id=_source_text_id(entry),
            ref_id=ref["id"],
            identity_key=f"path:{stored_as}",
            tier=tier,
            origin=origin,
            stored_path=stored_path,
            sha256=entry["sha256"],
            char_count=entry["char_count"],
            source_ref=source_ref,
            mapping=mapping,
            match_signal=signal,
            match_score=score,
            identity_status=identity_status,
            identity_note=entry.get("identity_note"),
            content_version=entry.get("content_version"),
            provenance_relation=entry.get("provenance_relation"),
            supplied_by=entry.get("supplied_by"),
            supplied_via=entry.get("supplied_via"),
            file_format=entry.get("file_format"),
            extraction_flags=entry.get("extraction_flags"),
            extraction_method=entry.get("extraction_method"),
        )
    finally:
        repo.close()
    os.makedirs(pdir, exist_ok=True)
    final_path = os.path.join(pdir, base)
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=f".{base}.", suffix=".tmp", dir=pdir)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        temp_path = None
        _after_materialization_replace()
        repo = _repo_require(run_dir)
        try:
            outcome = repo.finalize_source_text_materialization(intent_id)
        finally:
            repo.close()
        if outcome != "registered":
            raise RuntimeError(
                f"source text materialization did not register: {outcome}"
            )
    except Exception:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise
    content_store.record_parsed_text(
        run_dir,
        ref,
        tier,
        origin,
        text,
        source_ref=source_ref,
        mapping=mapping,
        signal=signal,
        score=score,
        identity_status=identity_status,
        identity_note=entry.get("identity_note"),
        content_version=entry.get("content_version"),
        provenance_relation=entry.get("provenance_relation"),
        supplied_by=supplied_by,
        supplied_via=supplied_via,
        file_format=file_format,
        library_item_id=library_item_id,
        extraction_method=entry.get("extraction_method"),
        extraction_flags=entry.get("extraction_flags"),
    )
    return entry


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------- unreadable-PDF queue (OCR opt-in) ----------------
#
# A PDF that yields no usable text (typically a scan with no OCR layer) is NOT
# discarded and does NOT crash the run. It is KEPT on disk and recorded here, in a
# single deterministic queue that gaps.py / report aggregate so the agent can offer
# OCR (opt-in) at the end. Provenance stays honest: who provided it, why it failed.
#
# Crucially, a file fed by the USER cannot be associated to a reference until it has
# text (corroboration matches identifiers/tokens against the content). So for
# provided files the flow is: park -> (ask) OCR -> now there is text -> associate.

def load_unreadable(run_dir: str) -> dict:
    repo = _repo_require(run_dir)
    try:
        return repo.unreadable_payload()
    finally:
        repo.close()


def save_unreadable(run_dir: str, data: dict):
    repo = _repo_require(run_dir)
    try:
        for entry in data.get("entries", []):
            repo.park_unreadable_source(
                ref_id=entry.get("ref_id"),
                ref_number=entry.get("ref_number"),
                kept_path=entry.get("kept_as"),
                source_ref=entry.get("source_ref"),
                origin=entry.get("origin") or "",
                reason=entry.get("reason") or "",
            )
    finally:
        repo.close()


def park_unreadable(
    run_dir,
    ref,
    src_path=None,
    *,
    origin,
    reason,
    move=False,
    src_bytes: bytes | None = None,
    src_name: str | None = None,
) -> str | None:
    """Keep an unreadable PDF in sources/ocr_queue/ and record it in the OCR queue.

    ref may be None (e.g. a provided file we could not read, hence not yet
    associable). move=True for a throwaway temp (fetch download); move=False to copy
    a user file we must not destroy. src_bytes/src_name allow parking a downloaded PDF
    directly from memory, without requiring an intermediate reread from disk. Returns
    the kept path (relative to run_dir) or None if the file could not be kept (the queue
    entry is still recorded). Once the scan is OCR'd into parsed/, the kept file is
    deleted via resolve_unreadable()."""
    qdir = ocr_queue_dir(run_dir)
    os.makedirs(qdir, exist_ok=True)
    refnum = ref.get("ref_number") if ref else None
    refid = ref.get("id") if ref else None
    src_label = src_name or src_path or "downloaded"
    if refnum is not None:
        base = f"{refnum}_fulltext_{origin}.unreadable.pdf"
    elif refid:
        base = f"{_safe(refid)}_fulltext_{origin}.unreadable.pdf"
    else:
        stem = _safe(os.path.splitext(os.path.basename(src_label))[0])
        base = f"{stem}_{origin}.unreadable.pdf"
    dest = os.path.join(qdir, base)
    kept = None
    try:
        if src_bytes is not None:
            with open(dest, "wb") as f:
                f.write(src_bytes)
        elif move:
            os.replace(src_path, dest)
        else:
            shutil.copyfile(src_path, dest)
        kept = f"sources/{OCR_SUBDIR}/{base}"
    except Exception:
        kept = None

    repo = _repo_require(run_dir)
    try:
        _ensure_reference(repo, ref)
        repo.park_unreadable_source(
            ref_id=refid,
            ref_number=refnum,
            kept_path=kept,
            source_ref=None if move or src_bytes is not None else os.path.abspath(src_path),
            origin=origin,
            reason=reason,
        )
    finally:
        repo.close()
    return kept


def store_provided_raw(run_dir, src_path, *, ref=None) -> str | None:
    """Copy a USER-supplied original into sources/provided/ so the run is
    self-contained and auditable. Returns the path relative to run_dir, or None on
    failure. Only user-provided files are kept raw — downloads keep only parsed text."""
    pdir = provided_dir(run_dir)
    os.makedirs(pdir, exist_ok=True)
    refnum = ref.get("ref_number") if ref else None
    stem = _safe(os.path.splitext(os.path.basename(src_path))[0])
    ext = os.path.splitext(src_path)[1]
    base = f"{refnum}_{stem}{ext}" if refnum is not None else f"{stem}{ext}"
    try:
        shutil.copyfile(src_path, os.path.join(pdir, base))
        return f"sources/{PROVIDED_SUBDIR}/{base}"
    except Exception:
        return None


def resolve_unreadable(run_dir, *, ref_id=None, kept_as=None, method=None) -> bool:
    """Retire a parked scan once it has been OCR'd into parsed/: delete the kept PDF
    and mark its queue entry ocr_status='done'. Match by kept_as (exact, retires that
    one) or by ref_id (retires the pending entries for that reference). Returns True if
    any entry was updated."""
    repo = _repo_require(run_dir)
    try:
        pending = []
        for entry in repo.unreadable_payload().get("entries", []):
            if entry.get("ocr_status") == "done":
                continue
            if (kept_as and entry.get("kept_as") == kept_as) or \
               (ref_id and entry.get("ref_id") == ref_id):
                pending.append(entry)
        for entry in pending:
            kept = entry.get("kept_as")
            if kept:
                fp = os.path.join(run_dir, kept)
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
        return repo.resolve_unreadable_source(ref_id=ref_id, kept_path=kept_as, method=method)
    finally:
        repo.close()
