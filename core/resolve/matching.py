# core/resolve/matching.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import html
import re
import unicodedata
import urllib.parse

try:
    from . import sources as _sources
except ImportError:  # direct execution
    from resolve import sources as _sources
try:
    from core.parse.authoryear import _CITATION_SIGNAL_RE
except ImportError:  # direct execution
    from parse.authoryear import _CITATION_SIGNAL_RE


TITLE_MISMATCH_MAX = 0.20     # below this: hard identifier mismatch
TITLE_WARN_MAX = 0.50         # between MISMATCH and WARN: yellow warning "check the DOI"
TITLE_MIN_TOKENS = 4          # titles too short: check skipped (too noisy)
ABBREVIATED_TITLE_MAX_TOKENS = 4

# A near-exact title is the same work — an author "mismatch" there is usually a
# garbage-parsed author, an initial, a group author or a diacritic, not a real
# conflict. Below this overlap a first-author contradiction is a strong signal of
# a *different* work with a similar title (a title collision).
AUTHOR_CONFLICT_MAX_OVERLAP = 0.90
_AUTHOR_CONFLICT_STOPWORDS = frozenset({
    "see", "id", "ibid", "cf", "eg", "accord", "supra", "infra", "note",
    "the", "a", "an", "but", "compare", "also",
})
_CITED_COORDINATE_FIELDS = {
    "container": "container-title",
    "volume": "volume",
    "issue": "issue",
    "article_page_range": "page",
    "chapter_page_range": "page",
    "elocator": "page",
    "article_number": "article-number",
    "article_locator": "page",
}
_CONTAINER_INITIAL_IGNORES = frozenset({"and", "of", "the"})
_CONTAINER_SERIES_MARKERS = frozenset({
    "conference", "congress", "proceedings", "symposium", "workshop",
})


def _expand_abbreviated_page_range(value: str) -> str:
    match = re.fullmatch(r"(\d+)-(\d+)", value)
    if match is None or len(match[2]) >= len(match[1]):
        return value
    start = int(match[1])
    base = 10 ** len(match[2])
    end = (start // base) * base + int(match[2])
    if end < start:
        end += base
    return f"{start}-{end}"


def _coordinate_comparison_value(kind: str, value: object) -> str:
    """Return the conservative comparison form for a cited coordinate."""
    text = html.unescape(str(value)).strip()
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    if kind in {"issue", "article_page_range", "chapter_page_range"}:
        text = re.sub(r"\s*-\s*", "-", text)
    if kind in {"article_page_range", "chapter_page_range"}:
        text = _expand_abbreviated_page_range(text)
    if kind == "container":
        text = text.casefold().replace("&", " and ")
        text = re.sub(r"[^\w]+", " ", text)
        text = re.sub(r"^the\s+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold() if kind in {"container", "elocator", "article_number", "article_locator"} else text


def _container_comparison_is_ambiguous(cited_value: str, matched_value: str) -> bool:
    """Recognize venue forms that cannot safely support a mismatch."""
    cited = _coordinate_comparison_value("container", cited_value)
    matched = _coordinate_comparison_value("container", matched_value)
    cited_tokens = cited.split()
    matched_tokens = matched.split()
    if not cited_tokens or not matched_tokens:
        return True
    cited_initials = "".join(
        token[0] for token in cited_tokens if token not in _CONTAINER_INITIAL_IGNORES
    )
    matched_initials = "".join(
        token[0] for token in matched_tokens if token not in _CONTAINER_INITIAL_IGNORES
    )
    # A one-token acronym has only one token initial (``BMJ`` -> ``b``), so the
    # ordinary initials comparison cannot see that it expands to "British
    # Medical Journal". Treat this as ambiguity, never as a mismatch.
    for short_tokens, short_value, long_initials in (
        (cited_tokens, cited_value, matched_initials),
        (matched_tokens, matched_value, cited_initials),
    ):
        compact = re.sub(r"[^A-Za-z]", "", short_value).casefold()
        if (
            len(short_tokens) == 1
            and 2 <= len(compact) <= 10
            and compact == long_initials
        ):
            return True
    if len(cited_initials) >= 2 and cited_initials == matched_initials:
        return True
    all_tokens = set(cited_tokens) | set(matched_tokens)
    if all_tokens & _CONTAINER_SERIES_MARKERS:
        return True
    shared = (
        set(cited_tokens) & set(matched_tokens)
    ) - _CONTAINER_INITIAL_IGNORES - {"journal", "journals"}
    has_abbreviation = any(
        len(token) <= 4 for token in (*cited_tokens, *matched_tokens)
    )
    return bool(shared and has_abbreviation)


def _cited_coordinate_comparisons(ref: dict, msg: dict) -> list[dict]:
    """Compare parse-derived coordinates without influencing resolver decisions."""
    coordinates = ref.get("cited_coordinates")
    if not isinstance(coordinates, (list, tuple)):
        return []

    comparisons: list[dict] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, dict):
            continue
        kind = coordinate.get("kind")
        field = _CITED_COORDINATE_FIELDS.get(kind)
        cited_value = coordinate.get("normalized_value")
        if field is None or not isinstance(cited_value, str) or not cited_value.strip():
            continue
        matched_value = msg.get(field)
        if kind == "container" and isinstance(matched_value, list):
            matched_value = matched_value[0] if matched_value else None
        if matched_value is not None and not isinstance(matched_value, str):
            matched_value = str(matched_value)
        if matched_value is None or not matched_value.strip():
            status = "inconclusive"
            matched_value = None
        elif _coordinate_comparison_value(kind, cited_value) == _coordinate_comparison_value(kind, matched_value):
            status = "match"
        elif (
            kind in {"article_page_range", "chapter_page_range"}
            and re.fullmatch(r"\d+", _coordinate_comparison_value(kind, matched_value))
            and re.split(
                r"[-–—]", _coordinate_comparison_value(kind, cited_value), maxsplit=1
            )[0] == _coordinate_comparison_value(kind, matched_value)
        ):
            # Crossref frequently records only an article's first page.  That
            # cannot contradict a cited range beginning on the same page, but
            # it also cannot corroborate the cited end page.
            status = "inconclusive"
        elif kind == "container" and _container_comparison_is_ambiguous(
            cited_value, matched_value
        ):
            status = "inconclusive"
        else:
            status = "mismatch"
        comparisons.append({
            "kind": kind,
            "cited_value": cited_value,
            "matched_value": matched_value,
            "status": status,
        })
    return comparisons


def _fold_author(name: str | None) -> str:
    """Diacritic-fold + lowercase a first-author token for comparison."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z ]", "", text).strip()


def _is_author_acronym(short: str, long: str) -> bool:
    """Whether ``short`` reads as an acronym of ``long`` (initials, dropping the
    small connective words) — e.g. "unctad" of "un trade and development"."""
    short = re.sub(r"[^a-z]", "", short)
    if not (2 <= len(short) <= 10):
        return False
    initials = "".join(word[0] for word in re.findall(r"[a-z]+", long))
    remaining = iter(initials)
    return all(ch in remaining for ch in short)


def _author_names_equivalent(first: str, second: str) -> bool:
    return bool(
        first == second
        or first in second
        or second in first
        or _is_author_acronym(first, second)
        or _is_author_acronym(second, first)
    )


def _genuine_author_conflict(profile: dict | None) -> bool:
    """Whether a metadata match's first authors genuinely contradict.

    Fires only on a moderate title overlap (a near-exact title is the same work).
    Diacritics, containment and acronym/expansion are folded away, and obvious
    non-author tokens (legal-citation words like "see"/"id") are ignored, so the
    result flags a real different-work collision rather than parsing noise.
    """
    if not isinstance(profile, dict) or profile.get("author_match") is True:
        return False
    overlap = profile.get("title_overlap")
    if overlap is None or overlap >= AUTHOR_CONFLICT_MAX_OVERLAP:
        return False
    cited = _fold_author(profile.get("cited_first_author"))
    matched = _fold_author(profile.get("matched_first_author"))
    if not cited or not matched or cited in _AUTHOR_CONFLICT_STOPWORDS:
        return False
    if _author_names_equivalent(cited, matched):
        return False
    return True


def _anchored_metadata_author_conflict(profile: dict | None, ay_surname: str | None) -> bool:
    """Detect a reliable parsed-author contradiction for metadata DOI promotion.

    Unlike the global title-collision guard, this is used only after the parser
    supplied an author-year surname anchor.  It deliberately does not apply the
    near-exact-title exception: a discovered DOI is not authoritative when the
    citation's independently parsed author contradicts that record.
    """
    if not isinstance(profile, dict) or profile.get("author_match") is not False:
        return False
    anchor = _fold_author(ay_surname)
    cited = _fold_author(profile.get("cited_first_author"))
    matched = _fold_author(profile.get("matched_first_author"))
    if not anchor or not cited or not matched or cited in _AUTHOR_CONFLICT_STOPWORDS:
        return False
    # A multi-word author field may be a group-author expansion rather than a
    # personal surname (for example UNCTAD).  It is not a reliable collision
    # anchor without a stronger structured author representation.
    if " " in cited or " " in matched:
        return False
    if not _author_names_equivalent(anchor, cited):
        return False
    return not _author_names_equivalent(cited, matched)

_QUOTED_TITLE_RE = re.compile(r"[\"“]([^\"”]{6,})[\"”]")

# A locator is not a title.  When a parser leaves the entry's link or DOI in the
# title field, searching on it asks an index to match a work by its address.
# Twin of sources.LOCATOR_TITLE_RE, which asks the same question of the same field
# for the identity probe; they cannot share one definition because sources reaches
# this module through core.fetch, so keep the two in step by hand.
_LOCATOR_TITLE_RE = re.compile(r"^(?:https?://|www\.|doi:|10\.\d{4,9}/)", re.IGNORECASE)

# Function words that never open a cited title but very often open the fragment
# an extractor cut out of the middle of one.  Deliberately closed and small: a
# lowercase content word ("de novo assembly ...", "eLife ...") stays allowed.
_OPENS_MID_PHRASE_RE = re.compile(
    r"^(?:on|of|in|for|and|the|a|an|to|with|at|by|from|as|or|but|into|over|under|about)\b",
)

_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4,
    "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10,
}


def _title_ordinals(text: str | None) -> set[int]:
    values = set()
    for token in re.findall(r"[a-z]+|\d+", str(text or "").lower()):
        if token in _ORDINALS:
            values.add(_ORDINALS[token])
        elif token.isdigit() and 1 <= int(token) <= 10:
            values.add(int(token))
    return values


# Longest run of adjacent pieces a rejoin will try to weld back into one word.
_REJOIN_MAX_PIECES = 4
_ALPHA_PIECES_RE = re.compile(r"[a-zà-ÿ]+")


def _rejoined_tokens(raw_entry: str) -> set[str]:
    """Words recoverable by welding back together pieces a broken text layer split.

    A PDF whose text layer carries word-internal spaces spells its bibliography in
    pieces — "coho rt Oto mo rpha" for "cohort Otomorpha" — and the tokenizer keeps
    whichever pieces reach three characters, so the entry ends up full of fragments
    that match nothing.  Welding adjacent pieces recovers the real words.

    Only runs containing a piece too short to be a word on its own are welded: an
    intact "internal fertilization" is never joined into "internalfertilization".
    The welds are then compared by token EQUALITY like everything else, so a weld
    that happens to be nonsense simply matches nothing.  That last point is what
    keeps this safe, and it is easy to lose: comparing a de-spaced entry by
    SUBSTRING instead rescues a genuinely wrong DOI, because "leaders" sits inside
    "leadership".  A test pins the difference.
    """
    pieces = _ALPHA_PIECES_RE.findall(_sources._norm(raw_entry))
    welded: set[str] = set()
    for start in range(len(pieces)):
        for size in range(2, _REJOIN_MAX_PIECES + 1):
            run = pieces[start:start + size]
            if len(run) < size:
                break
            if all(len(piece) >= 3 for piece in run):
                continue
            word = "".join(run)
            # Filtered through _tokens rather than against the stop list directly,
            # so a weld is kept on exactly the terms the comparison would count.
            if len(word) >= 4 and _sources._tokens(word):
                welded.add(word)
    return welded


def title_overlap(matched_title: str | None, raw_entry: str | None) -> float | None:
    """Fraction of distinctive tokens in the service-returned title that appear in the
    cited entry. None if not computable (title absent or too short)."""
    if not matched_title or not raw_entry:
        return None
    title_tokens = _sources._tokens(matched_title)
    if len(title_tokens) < TITLE_MIN_TOKENS:
        # Short titles are ambiguous in a token-set comparison.  They only
        # match when the complete normalized strings are identical.
        matched_key = _title_key(matched_title)
        entry_key = _title_key(raw_entry)
        if not matched_key or not entry_key or matched_key != entry_key:
            return None
        return 1.0
    entry_tokens = _sources._tokens(raw_entry)
    if not entry_tokens:
        return None
    overlap = len(title_tokens & entry_tokens) / len(title_tokens)
    if overlap < TITLE_MISMATCH_MAX:
        # About to read as a different work.  Before saying so, ask whether the
        # entry could be read at all: an unreadable citation is not evidence
        # against the identifier its author supplied.  Welding the entry's
        # fragments can only ADD tokens, so this can rescue a comparison and never
        # damage one — and it is attempted nowhere else, so an entry that was not
        # about to be called a mismatch is untouched.
        repaired = title_tokens & (entry_tokens | _rejoined_tokens(raw_entry))
        overlap = max(overlap, len(repaired) / len(title_tokens))
    return round(overlap, 3)


def title_flag(overlap: float | None) -> str | None:
    """None=ok | 'warn'=check manually | 'mismatch'=different work."""
    if overlap is None:
        return None
    if overlap < TITLE_MISMATCH_MAX:
        return "mismatch"
    if overlap < TITLE_WARN_MAX:
        return "warn"
    return None


_AUTHOR_BLOCK_SPLIT = r"\s+(?:and|&)\s+|,|;"
# How many words an author block may run to before the text is prose rather than a
# name list.  Six covers "American Psychiatric Association." and "Fed. Foreign Off.";
# a footnote sentence runs well past it.
_AUTHOR_BLOCK_MAX_WORDS = 6


def _author_key_from(segment: str, clean: str) -> str | None:
    """The surname carried by an already-isolated author block."""
    tokens = re.findall(r"[A-Z][^\W\d_][\w'’.-]*", segment)
    if len(tokens) >= 2:
        if re.fullmatch(r"[A-Z]{1,4}", tokens[1]):
            return tokens[0].lower()
        return tokens[-1].lower()
    m = re.match(r"\s*([A-Z][^\W\d_][\w'’.-]+)", clean)
    return m.group(1).lower() if m else None


def _first_author_key(raw_entry: str | None) -> str | None:
    if not raw_entry:
        return None
    # Strip LaTeX artifacts that interfere with name extraction:
    #   {\L}ukasz  -> ukasz  (braces + \L command removed)
    #   \L{}ukasz  -> ukasz  (\L{} command removed)
    #   {Vinyals   -> Vinyals (braces stripped)
    clean = re.sub(r"[{}]", "", raw_entry)
    clean = re.sub(r"\\[a-zA-Z]+\s*\{\}", "", clean)
    clean = re.sub(r"\\[a-zA-Z]+", "", clean)
    # A leading citation signal ("See, e.g., ", "Cf. ") is not part of the author
    # block: without stripping it, "See, e.g., Anne Edmundson..." keys on "see"
    # rather than the real first author. Only a LEADING signal is stripped — one
    # buried mid-sentence ("... has also visualized ... See Barrett Lyon...") is
    # not the entry's own opening and must not be touched.
    clean = _CITATION_SIGNAL_RE.sub("", clean, count=1)
    m = re.match(r"\s*([A-Z][^\W\d_][\w'’.-]+)\s+[A-Z]{2,4}\b", clean)
    if m:
        return m.group(1).lower()
    m = re.match(r"\s*([A-Z][^\W\d_][\w'’.-]+),\s*[A-Z]", clean)
    if m:
        return m.group(1).lower()
    m = re.match(r"\s*[A-Z][^\W\d_][\w'’.-]+(?:\s+[A-Z]\.)+\s+([A-Z][^\W\d_][\w'’.-]+)", clean)
    if m:
        return m.group(1).lower()
    plain = re.split(_AUTHOR_BLOCK_SPLIT, clean, maxsplit=1)[0].strip()
    # In "Bivins R. 2000. Sex cells: gender ..." or "Alex Krizhevsky. Learning multiple
    # layers ..." the author block closes with a period, not a comma, so a comma-only
    # split swallows the title and the last capitalised word is read as the author --
    # "sex", "technical", "encyclopedia". Cut at a period as well, with three limits:
    # not one closing an initial ("Bivins R."), not when the cut leaves only a legal
    # signal word ("Cf. Carol Rose, ..." must still give Rose), and not when what
    # precedes the period runs longer than a name block, which is how a prose footnote
    # reads ("Barrett Lyon has also visualized the impact of Iran's ...").
    cut = re.split(_AUTHOR_BLOCK_SPLIT + r"|(?<![A-Z])\.\s+", clean, maxsplit=1)[0].strip()
    if len(cut.split()) <= _AUTHOR_BLOCK_MAX_WORDS:
        key = _author_key_from(cut, clean)
        if key and _fold_author(key) not in _AUTHOR_CONFLICT_STOPWORDS:
            return key
    return _author_key_from(plain, clean)


def _crossref_first_author(msg: dict) -> str | None:
    authors = msg.get("author") or []
    if not authors:
        return None
    fam = authors[0].get("family") or authors[0].get("name")
    return fam.lower() if fam else None


def _crossref_year(msg: dict) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = (((msg.get(key) or {}).get("date-parts")) or [])
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except Exception:
                return None
    return None


def _crossref_container(msg: dict) -> str | None:
    vals = msg.get("container-title") or []
    return vals[0] if vals else None


def _conference_or_online_first(msg: dict, ref: dict) -> bool:
    """Whether a one-year metadata drift has a normal publication explanation."""
    blob = " ".join(str(value or "") for value in (
        msg.get("type"), _crossref_container(msg), ref.get("raw_entry"), ref.get("source_type"),
    )).lower()
    conference = any(marker in blob for marker in (
        "proceeding", "conference", "workshop", "symposium", "meeting",
    ))
    online_first = bool(msg.get("published-online")) and bool(
        msg.get("published-print") or msg.get("published") or msg.get("issued")
    )
    return conference or online_first


def _metadata_match_profile(ref: dict, msg: dict, matched_title: str | None) -> dict:
    raw = ref.get("raw_entry")
    cited_title = ref.get("title") or _article_title_candidate(ref) or raw
    cited_tokens = _sources._tokens(cited_title)
    if len(cited_tokens) < TITLE_MIN_TOKENS:
        title_score = _title_match_score(cited_title, matched_title)
    elif _title_key(cited_title) and (
        _title_key(cited_title) == _title_key(matched_title)
        or _title_key(cited_title).replace(" ", "")
        == _title_key(matched_title).replace(" ", "")
    ):
        # The parsed title is the strongest title boundary available here.  Treat
        # punctuation-only and OCR-joined word-boundary differences as exact
        # before consulting the complete raw entry.  This covers both line-wrap
        # compounds (``post- traumatic``) and lost separators
        # (``Secondharmonic``).  The long-title gate above plus independent
        # author, year, venue and ordinal guards keep short/generic fragments
        # outside this equivalence.
        title_score = 1.0
    else:
        # Long parsed titles can be column-interleaved or can name a proceedings
        # volume instead of the cited paper.  The complete bibliography entry
        # retains author/title/venue context and was the historically safer
        # comparison target.  Exact-only short-title handling remains separate.
        title_score = title_overlap(matched_title, raw)
        if title_score is None:
            title_score = _title_match_score(cited_title, matched_title)
    if title_score == 0.0 and len(cited_tokens) < TITLE_MIN_TOKENS:
        # A short parsed title can be a parser truncation while the raw entry
        # still contains the complete title.  Accept only near-complete raw
        # containment, which is as discriminating as an exact long-title match.
        raw_score = title_overlap(matched_title, raw)
        if raw_score is not None and raw_score >= 0.95:
            title_score = raw_score
    cited_author = _first_author_key(raw)
    matched_author = _crossref_first_author(msg)
    # Compare the names diacritic-folded.  A manuscript routinely prints "Zarate"
    # for an index's "Zárate", and raw containment then reads that as a different
    # author: one such reference was reported as a suspected fabrication on a
    # title it matched exactly.  _genuine_author_conflict already folds before it
    # will call two names a conflict; this is the same question asked earlier, so
    # it gets the same treatment.  The unfolded values stay in the profile below,
    # because the record of what each side actually said is what makes a wrong
    # attribution auditable afterwards.
    author_match = bool(
        cited_author
        and matched_author
        and _fold_author(cited_author) in _fold_author(matched_author)
    )
    matched_year = _crossref_year(msg)
    year_match = bool(ref.get("year") and matched_year and int(ref["year"]) == int(matched_year))
    container = _crossref_container(msg)
    venue_score = title_overlap(container, raw)
    cited_ordinals = _title_ordinals(cited_title)
    matched_ordinals = _title_ordinals(matched_title)
    ordinal_conflict = bool(cited_ordinals and matched_ordinals and cited_ordinals != matched_ordinals)
    year_conflict = bool(
        ref.get("year") is not None
        and matched_year is not None
        and not year_match
    )
    year_mismatch_plausible = bool(
        year_conflict
        and abs(int(ref["year"]) - int(matched_year)) <= 1
        and author_match
        and title_score is not None
        and title_score >= 0.85
        and _conference_or_online_first(msg, ref)
    )
    score = 0.0
    if title_score is not None:
        score += 0.45 * title_score
    if author_match:
        score += 0.25
    if year_match:
        score += 0.15
    if venue_score is not None and venue_score >= 0.50:
        score += 0.15
    profile = {
        "score": 0.0 if ordinal_conflict else round(score, 4),
        "title_overlap": title_score,
        "author_match": author_match,
        "cited_first_author": cited_author,
        "matched_first_author": matched_author,
        "year_match": year_match,
        "matched_year": matched_year,
        "venue_overlap": venue_score,
        "matched_venue": container,
    }
    # Keep the serialized metadata profile stable for ordinary matches; emit
    # edition details only when the hard guard actually fires.
    if ordinal_conflict:
        profile.update({
            "ordinal_conflict": True,
            "cited_ordinals": sorted(cited_ordinals),
            "matched_ordinals": sorted(matched_ordinals),
        })
    if year_mismatch_plausible:
        profile["year_mismatch_plausible"] = True
    coordinate_comparisons = _cited_coordinate_comparisons(ref, msg)
    if coordinate_comparisons:
        profile["coordinate_comparisons"] = coordinate_comparisons
    return profile


def _explicit_preyear_surnames(ref: dict) -> list[str]:
    """Return named surnames from the bibliography author block, conservatively."""
    raw = str(ref.get("raw_entry") or "")
    year = str(ref.get("year") or "")
    if not raw or not re.fullmatch(r"(?:18|19|20)\d{2}", year):
        return []
    before_year = re.split(rf"\b{re.escape(year)}\b", raw, maxsplit=1)[0]
    author_block = re.split(r"\bet\s+al\.?", before_year, maxsplit=1, flags=re.I)[0]
    names = [
        match.group(1)
        for match in re.finditer(
            r"(?:^|[,;]\s*|\band\s+|&\s*)"
            r"([A-Z][A-Za-z'’.-]{1,})\s*,?\s+"
            r"(?:[A-Z](?:[.-]?\s*)){1,8}"
            r"(?=,|;|\bet\s+al\b|\band\b|&|\s*\(?\s*$)",
            author_block,
        )
    ]
    return list(dict.fromkeys(_fold_author(name) for name in names if _fold_author(name)))


def is_same_work_correction_shape(
    ref: dict, msg: dict, matched_title: str | None, profile: dict | None = None,
) -> bool:
    """Whether a candidate has the non-author dimensions of the narrow recovery."""
    cited_title = ref.get("title")
    cited_tokens = _sources._tokens(cited_title) if isinstance(cited_title, str) else set()
    matched_tokens = _sources._tokens(matched_title)
    if len(cited_tokens) < 8 or not matched_tokens:
        return False
    cited_coverage = len(cited_tokens & matched_tokens) / len(cited_tokens)
    matched_coverage = len(cited_tokens & matched_tokens) / len(matched_tokens)
    profile = profile or _metadata_match_profile(ref, msg, matched_title)
    comparisons = {item["kind"]: item for item in profile.get("coordinate_comparisons") or ()}
    return bool(
        cited_coverage >= 0.85 and matched_coverage >= 0.60
        and profile.get("year_match") is True
        and comparisons.get("volume", {}).get("status") == "match"
        and comparisons.get("container", {}).get("status") != "mismatch"
        and profile.get("ordinal_conflict") is not True
    )


def is_unique_same_work_correction_candidate(
    ref: dict, msg: dict, matched_title: str | None, profile: dict | None = None,
) -> bool:
    """Narrow recovery rule for a unique, Crossref-backed corrupted citation."""
    profile = profile or _metadata_match_profile(ref, msg, matched_title)
    if not is_same_work_correction_shape(ref, msg, matched_title, profile):
        return False
    surnames = _explicit_preyear_surnames(ref)
    families = [
        _fold_author(str(author.get("family") or ""))
        for author in msg.get("author") or () if isinstance(author, dict)
    ]
    return bool(
        len(surnames) >= 2
        and all(any(_author_names_equivalent(surname, family) for family in families) for surname in surnames)
    )


def requires_same_work_correction_author_gate(ref: dict) -> bool:
    """Whether an entry declares enough named authors for the exceptional route."""
    return len(_explicit_preyear_surnames(ref)) >= 2


def quoted_title(raw_entry: str | None) -> str | None:
    """The title an entry prints verbatim in quotes, if it prints one.

    Kept separate from the wider candidate search below because this one answer is
    quotable evidence -- the citation's own words -- while the heuristics that
    follow are query hints.  The identity probe needs the former: a yardstick it
    can hold a document against must come from the citation, and on this corpus
    the looser paths sometimes return an author block ("Peter Rosendorff and
    Helen V") which would confirm the wrong document just as readily.
    """
    raw = re.sub(r"[{}]", "", str(raw_entry or ""))
    found = _QUOTED_TITLE_RE.search(raw)
    if not found:
        return None
    candidate = found.group(1).strip().rstrip(".,;")
    if len(_sources._tokens(candidate)) < TITLE_MIN_TOKENS:
        return None
    return candidate


def _short_quoted_title_identifier_fallback_match(
    ref: dict,
    matched_title: str | None,
    metadata_match: dict | None,
    *,
    identifier_fallback: bool,
) -> bool:
    """Corroborate a short quoted title only after a hard identifier failure.

    Three-word article titles are too noisy for the normal token-overlap policy:
    prose footnotes quote phrases of the same size.  In the identifier-fallback
    path only, the citation can nevertheless identify the work when four
    independent fields agree exactly: the quoted title is an ordered prefix of
    the indexed title, the first author is identical, the indexed container is
    printed in the citation, and the indexed year is within one year of the cited
    year.  Requiring the caller to state the fallback context keeps ordinary
    metadata discovery unchanged and fail-closed.
    """
    if identifier_fallback is not True or not isinstance(metadata_match, dict):
        return False
    raw = re.sub(r"[{}]", "", str(ref.get("raw_entry") or ""))
    found = _QUOTED_TITLE_RE.search(raw)
    if not found:
        return False
    quoted = found.group(1).strip().rstrip(".,;")
    if len(_sources._tokens(quoted)) < 3:
        return False

    def _ordered_words(value: str | None) -> list[str]:
        return _sources._WORD.findall(_sources._norm(str(value or "")))

    quoted_words = _ordered_words(quoted)
    matched_words = _ordered_words(matched_title)
    if not quoted_words or matched_words[:len(quoted_words)] != quoted_words:
        return False

    cited_author = str(metadata_match.get("cited_first_author") or "").strip().casefold()
    matched_author = str(metadata_match.get("matched_first_author") or "").strip().casefold()
    if (metadata_match.get("author_match") is not True
            or not cited_author or cited_author != matched_author):
        return False

    venue_words = _ordered_words(metadata_match.get("matched_venue"))
    raw_words = _ordered_words(raw)
    if not venue_words or not any(
        raw_words[index:index + len(venue_words)] == venue_words
        for index in range(len(raw_words) - len(venue_words) + 1)
    ):
        return False

    cited_year = ref.get("year")
    matched_year = metadata_match.get("matched_year")
    try:
        return cited_year is not None and matched_year is not None and abs(
            int(cited_year) - int(matched_year)
        ) <= 1
    except (TypeError, ValueError):
        return False


def _article_title_candidate(ref: dict) -> str | None:
    """Best-effort title candidate from a prose bibliography entry."""
    title = ref.get("title")
    if title and not _degraded_title(title):
        # Strip LaTeX braces from titles parsed from .bib / thebibliography
        title = re.sub(r"[{}]", "", title)
        return _repair_interleaved_title(title, ref.get("raw_entry"))
    raw = ref.get("raw_entry") or ""
    if not raw:
        return None
    raw = re.sub(r"[{}]", "", raw)
    # An entry that prints its title in quotes carries it verbatim, so prefer that
    # over anything reconstructed from punctuation.  This is the recovery path for
    # a parsed title that came out unusable: an author-year bibliography whose
    # author block leaked into the field ("and Sebastian, J") still has
    # "Toward a framework for preparing leaders for social justice" right there.
    quoted = quoted_title(raw)
    if quoted:
        return quoted
    # A raw fallback is a query hint only.  Matching still passes through the
    # normal title/author/year profile, so this cannot manufacture a DOI or
    # turn a weak candidate into a resolved work.
    initial_title = _raw_title_fallback(raw)
    if initial_title:
        return initial_title
    parts = [p.strip(" ,;") for p in re.split(r"\.\s+", raw) if p.strip(" ,;")]
    # In common Vancouver-ish entries: authors. title. journal. year...
    # A ". " split shatters multi-initial author names ("A. M.", "L. M."), so in a
    # Nature-style entry with many authors the title lands several parts out — well
    # past a fixed 3-part window.  Scan further, skipping author fragments: a part
    # carrying two or more standalone initials ("M., Phillippy, A", "T. & Aluru, S")
    # is an author-block remnant, never the title.  One initial is allowed so a
    # genuine title like "E. coli genome dynamics ..." survives.
    for part in parts[1:8]:
        if part.lower() in {"et al", "et al."} or part.startswith(("&", ".", ",")):
            continue
        if len(re.findall(r"(?<![A-Za-z])[A-Z]\.?(?![A-Za-z])", part)) >= 2:
            continue
        if re.search(r"\b\d{1,4}\s*,\s*(?:e?\d+)(?:\s*[–—-]\s*\d+)?\b", part):
            continue
        toks = _sources._tokens(part)
        if len(toks) >= 3 and not re.search(r"\b(19|20)\d{2}\b", part):
            return part
    return None


# Abbreviations that accompany a number in a locator and never stand as a title's
# own vocabulary.  An addition here can only discard a field that has no other word
# in it, which bounds the risk, but keep the set to locator shorthand: "no", "nos"
# and "p" are the ones this corpus needs, the rest are their ordinary companions.
_LOCATOR_ONLY_WORDS = frozenset(
    "no nos vol vols iss issue art arts p pp pg pgs sec secs suppl".split())


def _only_locator_words(clean: str) -> bool:
    """True when the field carries no word of its own beyond locator shorthand."""
    if not clean:
        return False
    words = re.findall(r"[A-Za-zÀ-ÿ]{2,}", clean)
    return all(word.lower() in _LOCATOR_ONLY_WORDS for word in words)


def _degraded_title(title: str | None) -> bool:
    clean = str(title or "").strip()
    return bool(
        not clean
        or clean.lower() in {"et al", "et al."}
        or clean.startswith(("&", ".", ","))
        # A candidate opening on a lowercase function word is a phrase the
        # extractor entered midway, not a title: the venue tail of a law-review
        # note ("2017 Int'l Conf. on Software Sec." -> "on Software Sec"), the
        # continuation of an author block ("and Young, M.D"), or a quoted gloss
        # from a parenthetical ("the law of nature and nations").  Searching on
        # one finds whatever shares those few words - a JOSS paper on a Python
        # simulation library was stored as the full text of an IEEE paper on the
        # Mirai botnet, the two sharing only an author surname.  Style always
        # capitalises a real title's first word, so this costs nothing.
        or _OPENS_MID_PHRASE_RE.match(clean) is not None
        # The link or DOI left in the title field: an address, not a title.  One
        # entry reached Crossref with "https://doi.org/10.1111/ anti.12579" as its
        # search key, and the field then travelled into the manifest as the work's
        # identity.  Its real title, "Smuggling, Trafficking, and Extortion ...",
        # was printed in quotes in the same entry.
        or _LOCATOR_TITLE_RE.match(clean) is not None
        # A parser can retain only the venue tail (``Journal ... 69, 99-118``).
        # It is useful source text, but not a safe title query when the raw
        # entry still contains a title after the author initials.
        or re.search(
            r"\b(?:journal|proceedings)\b.*\b\d{1,4}\s*[,;:]\s*(?:e?\d+)",
            clean,
            re.I,
        ) is not None
        or re.search(r"\b\d{1,4}\s*,\s*(?:e?\d+)(?:\s*[–—-]\s*\d+)?\b", clean) is not None
        # Nothing but a locator: volume, issue, page span, or a statute section
        # ("40 No", "43 Nos 3/4, p", "1: 95-115", "V, SS 3-4").  The patterns above
        # catch a locator that trails a venue or an address, not one standing alone,
        # so these reached the indexes as the search key -- and an index always
        # answers something.  Two real jea references were resolved to "* Writing"
        # and "2. Green Chemistry: Progress and Barriers" this way, then reported as
        # suspected fabrications: the gravest verdict here, earned by searching for a
        # volume number.  A field carrying no word of its own beyond those
        # abbreviations cannot be a title, while one real word keeps it ("BERT").
        or _only_locator_words(clean)
    )


def _raw_title_fallback(raw: str) -> str | None:
    """Conservative raw-entry title fallback for lost Vancouver boundaries."""
    pattern = re.compile(
        r"(?:\b[A-Z]\.\s*){2,4}(?:et al\.\s*)?"
        r"(?P<title>(?:(?:A|An|The)(?:\s+[^.!?]+)?|[A-Z][a-z][^.!?]*?))\."
    )
    for match in pattern.finditer(raw):
        candidate = match.group("title").strip(" ,;")
        if (
            len(_sources._tokens(candidate)) >= TITLE_MIN_TOKENS
            and not re.match(r"^[A-Z][a-zÀ-ÿ'’-]+,\s+[A-Z][a-zÀ-ÿ'’-]+", candidate)
            and not re.match(
                r"^[A-Z][a-zÀ-ÿ'’-]+\s+[A-Z][a-zÀ-ÿ'’-]+,\s+(?:and|&)",
                candidate,
                re.I,
            )
        ):
            return candidate
    return None


def _repair_interleaved_title(title: str, raw_entry: str | None) -> str:
    """Repair a narrowly evidenced two-column venue insertion.

    ICLR references can be extracted as ``... and In International Conference
    on Learning hardware`` while ``Representations`` lands on the following
    line.  Removing that known venue fragment restores the paper title without
    attempting a general parser rewrite inside the resolver.
    """
    if not re.search(r"\bRepresentations\b", str(raw_entry or ""), re.I):
        return title
    split_word = re.match(
        r"^(?P<head>.*?)(?P<prefix>[A-Za-z]{3,})In\s+International\s+"
        r"Conference\s+on\s+Learning\s+(?P<tail>[A-Za-z]+)\s*$",
        title,
        re.I,
    )
    if split_word:
        repaired_word = f"{split_word.group('prefix')}{split_word.group('tail')}"
        if repaired_word.lower() == "representations":
            return f"{split_word.group('head')}{repaired_word}".strip()

    match = re.search(
        r"\s+In\s+International\s+Conference\s+on\s+Learning\s+"
        r"(?P<tail>[a-z][\w-]*)\s*$",
        title,
        re.I,
    )
    if not match:
        return title
    return f"{title[:match.start()].rstrip()} {match.group('tail')}".strip()


def _title_match_score(cited_title: str | None, matched_title: str | None) -> float | None:
    """Score a title comparison, making very short titles exact-match only."""
    if not cited_title or not matched_title:
        return None
    cited_tokens = _sources._tokens(cited_title)
    if len(cited_tokens) < TITLE_MIN_TOKENS:
        cited_key = _title_key(cited_title)
        matched_key = _title_key(matched_title)
        if not cited_key or not matched_key:
            return None
        return 1.0 if cited_key == matched_key else 0.0
    return title_overlap(matched_title, cited_title)


def _article_like_resolution_candidate(ref: dict) -> bool:
    """Permit academic search for clearly misclassified proceedings only."""
    if ref.get("source_type") != "book":
        return bool(_article_title_candidate(ref))
    if ref.get("isbn") or not _article_title_candidate(ref):
        return False
    text = " ".join(
        str(ref.get(key) or "")
        for key in ("title", "venue", "journal", "raw_entry")
    ).lower()
    if re.search(
        r"\b(?:proceedings?|conference|workshop|symposium|colloquium|congress)\b",
        text,
    ):
        return True
    # Proceedings chapters are often parsed as books even without the word
    # "proceedings".  An explicit "In <volume>, pages N-M" plus an academic
    # publisher is sufficiently narrow to permit article/chapter discovery.
    return bool(
        re.search(r"\bin\s+[^.\n]{1,160},\s+pages?\s+\d", text)
        and re.search(r"\b(?:springer|elsevier|wiley|acm|ieee)\b", text)
    )


def _title_key(text: str | None) -> str:
    # Use the same conservative PDF-artifact normalization as token matching.
    # Otherwise a short title split as ``de-\npression`` takes the exact-match
    # branch and is treated as a different work even when every other identity
    # field agrees.
    return " ".join(re.findall(r"[a-z0-9]+", _sources._norm(str(text or ""))))


def _canonical_link_host(url: str | None) -> str:
    host = (urllib.parse.urlparse(str(url or "")).netloc or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":", 1)[0]


_METADATA_CANONICAL_HOSTS = (
    "arxiv.org",
    "aclanthology.org",
    "aclweb.org",
    "papers.nips.cc",
    "proceedings.neurips.cc",
    "openaccess.thecvf.com",
    "jmlr.org",
    "jmlr.csail.mit.edu",
    "cdn.aaai.org",
    "ronan.collobert.com",
    "sferics.idsia.ch",
    "pmc.ncbi.nlm.nih.gov",
    "europepmc.org",
)


def _metadata_has_canonical_host(result: dict) -> bool:
    canonical_hosts = set(_METADATA_CANONICAL_HOSTS)
    try:
        from . import provider_config
        canonical_hosts.update(provider_config.provider_canonical_hosts())
    except (ImportError, AttributeError):
        pass
    for link in result.get("fulltext_links") or []:
        host = _canonical_link_host(link.get("url"))
        if any(host == item or host.endswith(f".{item}") for item in canonical_hosts):
            return True
    return False


def _same_work_title_match(left: str | None, right: str | None) -> bool:
    left_key = _title_key(left)
    right_key = _title_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    overlap = title_overlap(left, right)
    return overlap is not None and overlap >= 0.85


def _title_key_contains(container: str | None, contained: str | None) -> bool:
    container_key = _title_key(container)
    contained_key = _title_key(contained)
    if not container_key or not contained_key:
        return False
    return contained_key in container_key


def colon_suffix_title_match(cited_title: str | None, article_title: str | None) -> bool:
    """Whether a citation's post-colon suffix exactly names the article title.

    Series-title citations (``Series title: Article title``) are common in
    medical guidance.  This is intentionally *only* a title-shape predicate:
    callers must pair it with an exact identifier or author+year evidence before
    treating it as identity confirmation.
    """
    cited = str(cited_title or "")
    if ":" not in cited:
        return False
    suffix = cited.rsplit(":", 1)[1]
    suffix_tokens = set(re.findall(r"[a-z0-9]+", suffix.lower()))
    article_tokens = set(re.findall(r"[a-z0-9]+", str(article_title or "").lower()))
    return bool(
        len(suffix_tokens) >= 3
        and _title_key(suffix) == _title_key(article_title)
    )


def _abbreviated_title_tokens(text: str | None) -> set[str]:
    """Lightly normalize title tokens for abbreviated-title detection.

    This keeps the guard conservative while smoothing simple word-form
    differences such as singular/plural variants ("network" vs "networks").
    """
    tokens = _sources._tokens(text or "")
    normalized = set()
    for token in tokens:
        tok = str(token or "").strip().lower()
        if not tok:
            continue
        if len(tok) > 4 and tok.endswith("ies"):
            tok = tok[:-3] + "y"
        elif len(tok) > 4 and tok.endswith("es"):
            tok = tok[:-2]
        elif len(tok) > 3 and tok.endswith("s"):
            tok = tok[:-1]
        normalized.add(tok)
    return normalized


def _aggregate_metadata_identity_signals(
    result: dict,
    attempts: list[dict] | None = None,
) -> dict[str, bool]:
    """Aggregate corroborating metadata signals across attempts for the same work.

    Some providers expose venue corroboration while others expose canonical full-text
    hosts. When multiple providers independently converge on the same matched title,
    the abbreviated-title guard should be allowed to combine those independent signals
    instead of judging only the final winner in isolation.
    """
    matched_title = result.get("matched_title")
    candidates = [result]
    for attempt in attempts or []:
        if attempt is result or attempt.get("status") != "resolved":
            continue
        if not _same_work_title_match(matched_title, attempt.get("matched_title")):
            continue
        candidates.append(attempt)

    author_match = False
    venue_match = False
    canonical_host = False
    for candidate in candidates:
        mm = candidate.get("metadata_match") or {}
        author_match = author_match or mm.get("author_match") is True
        venue_match = venue_match or (mm.get("venue_overlap") or 0.0) >= 0.50
        canonical_host = canonical_host or _metadata_has_canonical_host(candidate)
    return {
        "author_match": author_match,
        "venue_match": venue_match,
        "canonical_host": canonical_host,
    }


def _abbreviated_title_guard(
    ref: dict,
    result: dict | None,
    attempts: list[dict] | None = None,
) -> dict | None:
    if not result or result.get("status") != "resolved":
        return result
    if result.get("resolution_basis") != "metadata_search":
        return result
    cited_title = _article_title_candidate(ref)
    matched_title = result.get("matched_title")
    if not cited_title or not matched_title:
        return result
    cited_tokens = _abbreviated_title_tokens(cited_title)
    matched_tokens = _abbreviated_title_tokens(matched_title)
    if not cited_tokens or not matched_tokens:
        return result
    abbreviated = (
        len(cited_tokens) <= ABBREVIATED_TITLE_MAX_TOKENS
        and cited_tokens < matched_tokens
    )
    if not abbreviated:
        return result
    signals = _aggregate_metadata_identity_signals(result, attempts)
    exact_title = _title_key(cited_title) == _title_key(matched_title)
    venue_match = signals["venue_match"]
    canonical_host = signals["canonical_host"]
    author_match = signals["author_match"]
    if author_match and (exact_title or venue_match or canonical_host):
        return result
    guarded = dict(result)
    guarded["status"] = "unverified"
    guarded["reason"] = (
        "abbreviated cited title matched a longer work without enough independent "
        "identity signal; title-only related-work match downgraded to unverified"
    )
    guarded["existence_confidence"] = "low"
    guarded["title_guard"] = "abbreviated_title"
    return guarded
