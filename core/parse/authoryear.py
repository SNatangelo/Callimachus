#!/usr/bin/env python3
# core/parse/authoryear.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
authoryear.py — deterministic converter for author-year citations
(humanities style: "(Smith 2021)", "Smith et al. (2021)", "(Jones & Lee 2019)").

Does NOT change the pipeline: the parser uses this module to produce the SAME
parse payload (claims/references/citations) as numeric markers. That way resolve, style,
Verifier, guard, manifest and report all work identically.

Discipline (as everywhere in this project):
- deterministic match on (first-author surname, year, suffix a/b/c);
- UNIQUE -> linked;
- AMBIGUOUS (same surname+year, multiple entries, no distinguishing suffix) -> NOT
  guessed: citation with ref_id=null and candidate_ref_ids, reported to the user;
- ORPHAN (no entry) -> ref_id=null, no candidates (finding).

The fragile point is in-text DETECTION: 'In 2021', 'see 2021', 'p. 2021' are traps.
Mitigations: stoplist on fake surnames + everything listed in the debug view, never
silently absorbed.
"""
from functools import lru_cache
import json
import os
import re
import unicodedata

from core.invocation import run_command

try:
    from core.parse.parsing_common import RANGE_DASHES
except ImportError:
    from parsing_common import RANGE_DASHES

YEAR = r"(?:1[5-9]|20)\d{2}"
# Surname-word: initial capital (including accented letters), not all-caps short acronyms.
# The lookahead makes it consume the WHOLE word.  Without it the engine, having failed
# to read "See Smith (2019)" as "See" plus a co-author list, backtracks and reads the
# shorter "Se" — which leaves "e Smith", a legal co-author list in Italian ("Rossi e
# Bianchi").  The citation then keys on the surname "se", which no bibliography can
# ever satisfy, and slips under the stoplist that knows "see" is no surname.
NAME = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ'’‐-]+(?![A-Za-zÀ-ÿ])"

# Capitalised words that are NOT surnames, loaded from the external config — editable
# without touching code.  Two kinds, because they fail in two different ways: a SIGNAL
# word stands before a citation ("See Smith 2019"), so the real author is behind it; a
# STOP name is the head of the name being read ("Journal of Academic Ethics (2025)"),
# so what follows it is the rest of that name and no author at all.  See _scan.
_STOP_NAMES_FILE = os.path.join(
    os.path.dirname(__file__), "config", "stop_names.json")


@lru_cache(maxsize=2)
def _no_surnames(key: str) -> frozenset:
    try:
        with open(_STOP_NAMES_FILE, encoding="utf-8") as f:
            raw = json.load(f).get(key, [])
    except (OSError, ValueError):
        raw = []
    return frozenset(w.strip().lower() for w in raw if isinstance(w, str) and w.strip())


def signal_words() -> frozenset:
    """Words a citation may stand behind."""
    return _no_surnames("signal_words")


def stop_names() -> frozenset:
    """Every word that is no surname, of either kind."""
    return _no_surnames("stop_names") | signal_words()


# A source not yet published dates itself with a phrase, not a year — "in press",
# "forthcoming".  The phrases are an external vocabulary (config/noyear_dates.json),
# and all fold to one sentinel key so an "(Author, in press)" citation can reach the
# entry that is otherwise a reference nobody cites.  Additive and demand-driven: the
# key sits beside the real ones and only ever resolves what was already unmatched.
NOYEAR = "\x00noyear"   # a sentinel "year"; a string, so never equal to a real (int) year
_NOYEAR_FILE = os.path.join(os.path.dirname(__file__), "config", "noyear_dates.json")


@lru_cache(maxsize=1)
def _noyear_alt() -> str:
    try:
        with open(_NOYEAR_FILE, encoding="utf-8") as f:
            raw = json.load(f).get("noyear_markers", [])
    except (OSError, ValueError):
        raw = []
    markers = [re.escape(m.strip()).replace(r"\ ", r"\s+")
               for m in raw if isinstance(m, str) and m.strip()]
    return "|".join(markers) if markers else r"(?!x)x"  # matches nothing when empty


@lru_cache(maxsize=1)
def _entry_noyear_re() -> re.Pattern:
    # The marker standing where a year would be: "(in press)", "(n.d.)".
    return re.compile(r"\(\s*(?:" + _noyear_alt() + r")\s*\)", re.IGNORECASE)


def _entry_is_noyear(raw_entry: str) -> bool:
    return bool(_entry_noyear_re().search(raw_entry or ""))


# The scheme only — PDF extraction breaks URLs across lines and sprinkles spaces
# through the path, so the URL itself is not reliably one token.
_URL_SCHEME_RE = re.compile(r"https?://|www\.", re.I)
# An organisational author's cited acronym: "… for Refugees (UNHCR)".
_ORG_ACRONYM_RE = re.compile(r"\(([A-Z][A-Z0-9&.\-]{1,9})\)")

# (Author Year) inside parentheses — one or more citations separated by ';'
_PAREN = re.compile(r"\(([^()]*?\b" + YEAR + r"[a-z]?\b[^()]*?)\)")
# A chunk of such a group that carries a year and nothing else.
_BARE_YEAR_RE = re.compile(r"(?P<year>" + YEAR + r")(?P<suf>[a-z])?")
_ORG_WORD = r"(?:of|and|the|for|in|on|" + NAME + r")"
_ORG_TAIL = r"(?:\s+" + _ORG_WORD + r"){1,7}"
_PAREN_ORG = re.compile(
    r"(?P<sur>" + NAME + r")" + _ORG_TAIL +
    r"\s*,\s*(?P<year>" + YEAR + r")(?P<suf>[a-z])?"
)
_NARRATIVE_ORG = re.compile(
    r"(?P<sur>" + NAME + r")" + _ORG_TAIL +
    r"\s*\(\s*(?P<year>" + YEAR + r")(?P<suf>[a-z])?\s*\)"
)
# The co-authors trailing the keying surname.  Beyond "et al." and "and X", this
# accepts a comma-separated list — "Frelick, Kysel, and Podkul 2016", the ordinary
# Chicago/Harvard form for three or more authors.  Without the ",\s*NAME" run the
# pattern cannot start at the first author, so it starts at the *second* one and
# keys the citation on "kysel" — a surname no bibliography entry carries, hence a
# permanent orphan.  The citation key is always the FIRST author.
#
# The list may itself end in "et al.": when two works share a first author AND a
# year, APA does not letter them, it names co-authors until they differ and cuts
# the rest — "(Mackinger, Pachinger, et al., 2000)" against "(Mackinger, Loschin,
# & Leibetseder, 2000)".  That form belongs to neither of the older branches (the
# comma blocks "\s+et al.", and there is no closing "and X"), so _CITE matched
# NOTHING across the whole marker — not a wrong key, no citation at all, and so no
# orphan either: a silent miss, and the entry it names reported uncited.  The list
# run is shared with the "and X" branch below; only its ending differs.
#
# A co-author may be named with initials — "M. A. Conway and C. W. Pleydell-Pearce",
# the form APA prints in an abstract.  NAME cannot cross the "C.": the dot is not in
# its class, so the run broke at the initial and the scan fell through to the second
# author, keying the citation on "pleydell-pearce" — the kysel case above, reached by
# a different road.  The initials are matched and then ignored: the key is the surname.
_INITIALS = r"(?:[A-ZÀ-Þ]\.\s*)*"
_COAUTHORS = (
    r"(?:(?:\s*,\s*" + _INITIALS + NAME + r")*\s*,?\s*et\s+al\.?)"
    r"|(?:(?:\s*,\s*" + _INITIALS + NAME + r")*"
    r"(?:\s*,?\s*(?:and|&|e)\s+" + _INITIALS + NAME + r")+)"
)
# Single author-year citation (parenthetical or narrative): surname [et al.|& X|and X] year[suffix]
_CITE = re.compile(
    r"(?P<sur>" + NAME + r")"
    r"(?P<rest>" + _COAUTHORS + r")?"
    r"[\s,]*"
    r"(?:\(\s*)?(?P<year>" + YEAR + r")(?P<suf>[a-z])?"
)
# Narrative: Surname [et al.] (Year[suffix][, locator])
# The locator is the pinpoint the styles print inside the parentheses — a page, a
# chapter, a section ("Khalili’s (2012, 22) work").  It belongs to the citation, so
# without it here the whole narrative citation goes unread: not an orphan, a silent
# miss.  It is bounded (no nested parens, one clause) and is not read as anything but
# a locator — except for a further year, which _MORE_YEARS_RE below picks out of it.
_NARRATIVE = re.compile(
    r"(?P<sur>" + NAME + r")"
    r"(?P<rest>" + _COAUTHORS + r")?"
    r"\s*\(\s*(?P<year>" + YEAR + r")(?P<suf>[a-z])?"
    r"(?P<tail>\s*,[^()]{0,40}?)?"
    r"\s*\)"
)
# One author, several works: "(Besteman 2016, 2020)", "(Fassin 2005, 2011)",
# "(Radford et al. 2018a, 2018b)".  Each year is a citation of its own; reading only
# the first drops the others silently — no orphan is raised for a marker never seen.
#
# A four-digit page pinpoint ("2016, 1732") has this shape too, so what is not a year
# is not merely dropped: it is cited like any other, and if the bibliography has no
# such work it surfaces as an orphan.  A visible false orphan is the safe error here;
# dropping the unmatched ones would hide the very thing the tool exists to find — a
# claim resting on a source the bibliography does not carry.  The page RANGE form
# ("2016, 1732-35") is excluded: the dash says page, not year.
_MORE_YEARS_RE = re.compile(
    r"\s*,\s*(?P<year>" + YEAR + r")(?P<suf>[a-z])?(?![\w.’'-]|\s*[" + RANGE_DASHES + r"]\s*\d)"
)

# A secondary citation names the work the paper did NOT read, then the source it did:
# "(Bateman 1948 as cited in Tang-Martínez 2016)".  The verifiable source is the one
# AFTER the phrase — Tang-Martínez, which the bibliography carries — so the work before
# it is not a citation the paper owes an entry for, and reporting it missing is a false
# orphan.  Only the operative source keeps its citation; the primary is folded into the
# sentence (which still records the full "X as cited in Y" attribution).
_CITED_IN_RE = re.compile(r"\b(?:as\s+)?(?:cited|quoted)\s+in\b", re.IGNORECASE)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def _surname_key(s: str) -> str:
    k = _norm(s).strip().lower().replace("’", "'")
    # Strip a trailing possessive "'s" ("Freire’s (2002)" -> key "freire") so a
    # narrative possessive still matches its reference.  Real surnames do not end
    # in apostrophe-s, and internal apostrophes (O'Malley, D'Angelo) are kept.
    if k.endswith("'s"):
        k = k[:-2]
    # An accent is not a different name.  Papers disagree with themselves about them —
    # one cites "(Dykova 2023)" and files the entry under "Dyková"; another cites
    # "(Tang-Martínez 2005)" and files "Tang-Martinez" — so keying on the accent breaks
    # the citation off its own reference, in whichever direction the author slipped.
    # Both sides of the join come through here, so they fold together or not at all.
    #
    # This can only ever merge two keys, never split one, and a merge that is wrong
    # (a real Muller beside a real Müller, same year) lands in the AMBIGUOUS path:
    # reported with its candidates, not guessed.
    return "".join(c for c in unicodedata.normalize("NFD", k)
                   if not unicodedata.combining(c))


def detection_text(sentence: str) -> str:
    """Normalized text that in-text detection (and its spans) refers to."""
    text = _norm(sentence)
    text = re.sub(r"\bRETRACTED\s+ARTICLE\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"([^\W\d_])-\s+([^\W\d_])", r"\1\2", text)
    return re.sub(r"\s+", " ", text)


_detection_text = detection_text


def _overlaps(span, spans) -> bool:
    start, end = span
    return any(start < other_end and end > other_start for other_start, other_end in spans)


def entry_key(raw_entry: str):
    """From an author-year bibliography entry -> (surname, year, suffix).

    Surname = last word of the first-author name block (the first sequence of
    consecutive capitalized NAME words).  This handles both "LastName, FirstName"
    and "FirstName LastName, …" styles: "Smith, John" → "smith";
    "Alan Akbik, …" → "akbik".  Year = first 4-digit year, with optional
    attached suffix 'a'/'b'.

    LaTeX ``~`` (non-breaking space) and middle-initial periods ("Samuel R. Bowman")
    are normalised before matching so that the name block captures through to the
    actual surname."""
    e = _norm(raw_entry).strip()
    e = e.replace("~", " ")
    # Strip periods from single-letter middle initials only: "R." → "R",
    # but not from "Journal.", "Source.", "et al.", etc.
    e = re.sub(r"\b([A-Z])\.(?=\s)", r"\1", e)
    # Drop arXiv identifiers ("arXiv:1903.10520") before reading the year: their
    # leading YYYY.NNNNN group is not a year and would mask the real publication
    # year (e.g. 1903 vs the actual 2019).  The dot-between-4-and-5-digits shape
    # is unique to arXiv ids; page ranges use "-"/":" separators.
    e_year = re.sub(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", " ", e)
    m_year = re.search(r"\b(" + YEAR + r")([a-z])?\b", e_year)
    year = int(m_year.group(1)) if m_year else None
    suffix = (m_year.group(2) or "") if m_year else ""
    # Read the name off a base-Latin fold of the entry, so a surname does not
    # truncate at a letter the class below cannot see: "Zaviačič" (č is outside
    # À-ÿ) keeps all of itself as "Zaviacic".  _surname_key folds the same way, so
    # the entry and the citation that keys it meet on the same folded form.
    e_ascii = "".join(c for c in unicodedata.normalize("NFD", e)
                      if not unicodedata.combining(c))
    # A legal citation opens on a signal — "See, e.g.,", "See generally", "But
    # see", "Cf." — and the name block below starts at the first capitalised word,
    # so the signal itself was being read as the first author's surname.  85
    # entries across the corpus filed themselves under "see", and because the
    # author is a load-bearing part of metadata corroboration, every one of them
    # scored `author_match: false` against candidates that carried the RIGHT
    # author: Crossref returned Broeders for the note citing Broeders, and it was
    # rejected.  The failure mode is not a wrong match, it is a correct one
    # thrown away.
    e_ascii = _CITATION_SIGNAL_RE.sub("", e_ascii, count=1)
    # Match the first block of consecutive capitalized words (the first-author
    # name).  The block ends at a comma, " and ", " & ", " e ", or an
    # uncapitalised word.
    # _NAME_OR_INIT accepts single-letter middle initials (e.g. "F" in "Peter F Brown").
    _NAME_OR_INIT = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ'’‐-]*"
    # A surname can open on a lower-case particle the capital-first token cannot see —
    # "de Decker, A.", "van Minnen, N." — and then the entry files itself under None
    # and its citation, keyed on "decker"/"minnen", can never reach it.  Let the block
    # open on up to two known particles; the surname stays its last word (or, in the
    # descriptive-word branch below, its first word past the particles).
    _PARTICLE = (r"(?:(?:" + "|".join(sorted(_NAME_PARTICLES, key=len, reverse=True))
                 + r")\s+){0,2}")
    m_sur = re.match(r"\s*(" + _PARTICLE + _NAME_OR_INIT
                     + r"(?:\s+" + _NAME_OR_INIT + r")*)", e_ascii)
    surname = None
    if m_sur:
        words = m_sur.group(1).split()
        # What stopped the name block?
        after = e_ascii[m_sur.end():]
        # If stopped by a comma, " and ", " & ", or " e " (Italian/Portuguese
        # "and"), the whole block is the first-author name — take the last
        # word as the surname ("Tjong Kim Sang and …" → "sang",
        # "Samuel R. Bowman, …" → "bowman").
        # Otherwise a descriptive capitalized word ("Numeric source one")
        # ended the block — take the first word as surname (original heuristic).
        # Initials that TRAIL the surname are not a surname.  CSE and Vancouver set
        # them bare and unpunctuated — "Ahnesjö I, Brealey JC", "Barresi MJF" — so the
        # last word of the name block is "I", and every multi-author entry in the
        # bibliography files itself under an initial.  Nothing an author cites can
        # reach it: the citation says "Ahnesjö", the entry answers to "i".
        # A compound forename keeps its hyphen in the initials — Marc-André is "M-A" —
        # and left standing, "Lachance M-A, Burke C, …" files itself under "m-a".
        while len(words) > 1 and re.fullmatch(r"[A-ZÀ-Þ]{1,4}(?:-[A-ZÀ-Þ]{1,4})*", words[-1]):
            words = words[:-1]
        if re.match(r'\s*(?:,|and\b|&|e\b)', after):
            surname = _surname_key(words[-1])
        # Period + year pattern: "Wilson L Taylor. 1953." — the period between
        # the author block and the publication year is a reliable terminator.
        # Also handles "F. Rosenblatt. 1958." and similar natural-order names.
        elif after and re.match(r'\s*\.\s*\d{4}', after):
            surname = _surname_key(words[-1])
        else:
            # Skip a leading particle so "de Decker Autobiographical…" keys on
            # "decker", not "de".
            first = 0
            while first < len(words) - 1 and words[first].lower() in _NAME_PARTICLES:
                first += 1
            surname = _surname_key(words[first])
    return surname, year, suffix


def _coauthor_keys(raw: str, first_surname: str) -> list[str]:
    """Additional cited surnames beyond the first author, e.g. "Gardner" in
    "(Clark and Gardner, 2018)".  Used to disambiguate two bibliography entries
    that share (first-surname, year).  Drops stop-words / the first surname itself."""
    text = _norm(raw)
    my = re.search(YEAR, text)
    if my:
        text = text[:my.start()]
    # "et al." ends the named run rather than replacing it: it stands for the authors
    # NOT named, so what precedes it was named and what follows it was not.  Reading
    # it as "no co-author named" and giving up here is right for "(Mackinger et al.,
    # 2000)" — nothing precedes but the first surname, so this still yields nothing —
    # and wrong for "(Mackinger, Pachinger, et al., 2000)", where it discarded the one
    # co-author APA printed for the sole purpose of telling that entry from the other
    # two Mackinger 2000s.  Truncating covers both.
    m_etal = re.search(r"\bet\s+al", text, re.IGNORECASE)
    if m_etal:
        text = text[:m_etal.start()]
    keys: list[str] = []
    for tok in re.findall(NAME, text):
        k = _surname_key(tok)
        if k == first_surname or k in stop_names() or k in keys:
            continue
        keys.append(k)
    return keys


# What separates one author from the next.  A name ends here, and so does everything
# that may be said about it.
_AUTHOR_SEP_RE = re.compile(r"[,;&]|\bet\s+al\.?|\band\b|\be\b", re.IGNORECASE)


def _own_name_keys(raw: str, surname: str) -> list[str]:
    """The other words of the SAME author's name — the keys this citation could equally
    have been filed under.

    A name of several words has several words that might key it, and the two sides of
    the join do not choose the same one.  "Donna Haraway's (1989)" keys on the forename
    that opens the run; the entry, "Haraway D. 1989.", keys on the surname.  An
    organisation naming itself twice over — "(ACOG: American College of Obstetrics and
    Gynecology 2018)" — keys on a word out of the middle of its expansion, while its
    entry keys on the acronym.  Every one of them is a citation of a work the paper
    carries, reported as resting on a source the bibliography does not have.

    So the citation stops insisting on one word and offers what its author's name could
    be filed under; the bibliography, which alone knows, decides (see ``match``).

    Bounded by the author separators, and that bound is the whole safety of it: the
    words belong to the citation's OWN author, never to the next one.  "(Jones and
    Miller 2019)" offers nothing — Miller is a second author, and a Miller 2019 in the
    bibliography is not licence to hang a claim about Jones on it."""
    text = _norm(raw)
    my = re.search(YEAR, text)
    if my:
        text = text[:my.start()]
    for seg in _AUTHOR_SEP_RE.split(text):
        keys = [_surname_key(t) for t in re.findall(NAME, seg)]
        if surname in keys:
            return [k for k in dict.fromkeys(keys)
                    if k != surname and k not in stop_names() and k not in signal_words()]
    return []


def _despace(s: str) -> str:
    """A surname with its hyphens and spaces stripped — the form the typesetter reaches
    for when it drops them ("FeldmanBarrett" for "Feldman-Barrett", "PleydellPearce"
    for "Pleydell-Pearce")."""
    return re.sub(r"[\s\-‐-―−]", "", s or "")


def _entry_has_surname(raw_entry: str, surname: str) -> bool:
    ent = _norm(raw_entry)
    if re.search(r"\b" + re.escape(surname) + r"\b", ent, re.IGNORECASE) is not None:
        return True
    # A concatenated citation name ("PleydellPearce") still finds the hyphenated entry
    # ("Pleydell-Pearce"): match with the separators stripped from both.  The length
    # guard keeps a short key from landing inside an unrelated word.
    bare = _despace(surname).lower()
    return len(bare) >= 5 and bare in _despace(ent).lower()


_ENTRY_YEAR_PAREN_RE = re.compile(r"\(\s*(?:1[6-9]|20)\d{2}")
# The year that closes the author block, however the style writes it: "(2024)" or
# the bare "1953." of "Wilson L Taylor. 1953."
_ENTRY_YEAR_RE = re.compile(r"\(\s*(?:1[6-9]|20)\d{2}"
                            r"|\b(?:1[6-9]|20)\d{2}[a-z]?\s*[.,]")
_INITIALS_RE = re.compile(r"^(?:[A-Z]\.?\s*){1,4}$")


def _entry_author_class(raw_entry: str) -> str:
    """'single' | 'multi' from the author block before the first parenthesised
    year.  A co-author conjunction ("and" / "&" / "et al.") in that block marks a
    multi-author entry — used to tell "Jackson, M. (2024)" from "Jackson, M., Von
    Dohlen, H. and Black-Chen, M. (2024)" when both share the (surname, year) key.
    """
    raw = raw_entry or ""
    m = _ENTRY_YEAR_PAREN_RE.search(raw)
    head = raw[:m.start()] if m else raw[:80]
    return "multi" if re.search(r"\bet\s+al|\band\b|&", head, re.IGNORECASE) else "single"


def _entry_author_block(raw_entry: str) -> str:
    """The author list alone, cut at the year that ends it.

    Cutting at a fixed 80 characters instead, as the coarse class above does, drags
    the title in when the style writes the year bare ("… 2018. Simple and effective
    multi-paragraph reading comprehension") — and a title carrying an "and" then
    reads as a second author."""
    raw = raw_entry or ""
    m = _ENTRY_YEAR_RE.search(raw)
    return raw[:m.start()] if m else raw[:80]


# A name particle is lower-case and still part of a name ("van den Oord").  Any
# OTHER lower-case word of four letters or more is prose: the title has started.
# The introductory signal of a legal citation, stripped before the first-author
# name block is read.  The trailing lookahead is the guard: the signal is removed
# only when a capitalised word follows it, so an entry that merely BEGINS with one
# of these words keeps its own reading, and "Seeley" is untouched because "see"
# needs a word boundary the "l" denies it.
# The lookahead is scoped out of IGNORECASE on purpose: under `re.I` the
# capital-letter class also matches lower case, which silently disables the whole
# guard — "See for example SADC …" then loses its "See" and files under nothing.
_CITATION_SIGNAL_RE = re.compile(
    r"^\s*(?:but\s+)?(?:see|cf|accord|compare|contra|citing|quoting)\b\.?"
    r"(?:\s+also|\s+generally)?[\s,]*(?:e\.?\s?g\.?[\s,]*)?(?=(?-i:[A-ZÀ-Þ]))",
    re.I)

_NAME_PARTICLES = {"van", "von", "der", "den", "del", "della", "de", "di", "da",
                   "du", "la", "le", "el", "dos", "bin", "ibn", "ter", "ten"}
_LOWER_WORD_RE = re.compile(r"\b([a-z][a-z'’-]{3,})\b")


def _fragment_is_prose(fragment: str) -> bool:
    """True once the author list has ended and the title has begun.

    Needed because the Nature/ACS styles put the year LAST — "Jain, C., … & Aluru,
    S. High throughput ANI analysis … Nat. Commun. 9, 5114 (2018)" — so cutting at
    the year leaves the whole title inside the author block, and its words would be
    counted as authors."""
    return any(w not in _NAME_PARTICLES for w in _LOWER_WORD_RE.findall(fragment))


def _entry_author_count(raw_entry: str) -> int:
    """How many authors the entry lists — 3 when it says "et al." itself.

    Authors are separated by commas and by the closing "and"/"&", but in the
    "surname, initials" styles a comma ALSO separates a name from its initials
    ("Jain, C., Rodriguez-R, L. M."), which would double the count.  A fragment
    that is nothing but initials is therefore folded back into the name before it,
    and the scan stops as soon as a fragment turns into title prose.

    Every way this can be wrong makes the count too coarse, never confidently
    wrong: it is only ever used to tighten a candidate group, and a group it fails
    to tighten falls back to the older, blunter rule."""
    head = _entry_author_block(raw_entry)
    if re.search(r"\bet\s+al", head, re.IGNORECASE):
        return 3
    head = re.sub(r"\band\b", ",", head.replace("&", ","), flags=re.IGNORECASE)
    n = 0
    for part in head.split(","):
        part = part.strip(" .;")
        if not part:
            continue
        if _fragment_is_prose(part):
            break
        if n and _INITIALS_RE.match(part):
            continue
        n += 1
    return n


def _mk_cite(m, raw, span):
    sur = _surname_key(m.group("sur"))
    return {
        "marker_raw": raw,
        "surname": sur,
        "year": int(m.group("year")),
        "suffix": (m.group("suf") or ""),
        "span": span,
        "coauthors": _coauthor_keys(raw, sur),
        "own_name_keys": _own_name_keys(raw, sur),
    }


def _year_after(m) -> int:
    """Where the citation's own year (with its suffix) ends."""
    return m.end("suf") if m.group("suf") else m.end("year")


def _more_years(text: str, pos: int, base: dict, span) -> list[dict]:
    """The further works of the same author listed after its year — one citation each.

    "(Besteman 2016, 2020)" cites two books; the author is named once because the
    style names it once.  They are citations of the base one's author, so they carry
    its surname and co-authors, and its group span (the parser scopes a group to its
    own sentence fragment)."""
    out = []
    while (m := _MORE_YEARS_RE.match(text, pos)):
        out.append({
            "marker_raw": base["marker_raw"],
            "surname": base["surname"],
            "year": int(m.group("year")),
            "suffix": m.group("suf") or "",
            "span": span,
            "coauthors": base["coauthors"],
            "own_name_keys": base["own_name_keys"],
        })
        pos = m.end()
    return out


def _scan(pattern, text: str):
    """Matches of *pattern*, minus those keyed on a word the config knows is no surname.

    Where the scan RESUMES is the whole question, and it differs by kind of word:

      * a signal word — "See Smith (2019)" reads as a citation of "See".  Discarding the
        match and moving past it would take Smith down with See, in silence: the citation
        the author actually wrote would go unseen.  So the scan resumes right after the
        signal word, and "Smith (2019)" is read where it stands.

      * a stop name — "Journal of Academic Ethics (2025)" is a running head, and
        "Journal" is the head of its title.  Resuming inside it would read the tail,
        "Academic Ethics (2025)", as a citation of someone called Academic.  Nothing in
        this marker is an author, so the scan resumes past all of it."""
    pos = 0
    while (m := pattern.search(text, pos)):
        key = _surname_key(m.group("sur"))
        if key in signal_words():
            pos = m.end("sur")
            continue
        if key in stop_names():
            pos = m.end()
            continue
        yield m
        pos = m.end()


@lru_cache(maxsize=1)
def _inpress_res():
    """(paren, narrative) patterns for a no-year citation: "(Crane …, in press)" and
    "Spinhoven … and Williams (in press)".  Built from the same NAME/co-author
    machinery the year scans use, with the marker standing where the year would."""
    # Case-insensitive on the MARKER only — a global re.IGNORECASE would let NAME
    # match a lower-case common noun ("prior work (in preparation)" -> "work").
    alt = r"(?i:" + _noyear_alt() + r")"
    # Parenthetical "(Crane, …, in press)": the parens and the comma are already a
    # strong citation signal, so a lone author is allowed.
    #
    # A citation need not be the whole parenthetical, and the year scans have always
    # known it — they split the group on ';' and read each chunk.  Anchored instead on
    # '(' and ')' themselves, this pattern could only ever read a group that held ONE
    # citation and nothing else, so "(Crane et al., in press; Spinhoven et al., in
    # press)" yielded neither, and an instrument named before the cite ("(Acceptance
    # and Action Questionnaire; Hayes et al., in press)") hid it too.  A ';' bounds a
    # citation exactly as the parens do: accept either at both ends.
    paren = re.compile(r"(?<=[(;])\s*(?P<sur>" + NAME + r")(?P<rest>" + _COAUTHORS + r")?"
                       r"\s*,\s*(?:" + alt + r")\s*(?=[);])")
    # Narrative "Spinhoven, …, and Williams (in press)": in open prose require the
    # co-author run, so a capitalised common noun before "(in preparation)" cannot
    # pass as a one-author citation.
    narr = re.compile(r"(?P<sur>" + NAME + r")(?P<rest>" + _COAUTHORS + r")"
                      r"\s*\(\s*(?:" + alt + r")\s*\)")
    return paren, narr


def find_intext(sentence: str):
    """All author-year citations in a sentence. Discards fake surnames (stoplist).
    Returns a list of dicts {marker_raw, surname, year, suffix, span}. 'span' is
    the citation GROUP span on detection_text(sentence): citations from the same
    parenthetical group "(A 2020; B 2021)" share one span, so the parser can
    scope each group to its own sentence fragment."""
    sentence = detection_text(sentence)
    found = []
    spans = []

    def _add(m, raw, more_years_in=None):
        cite = _mk_cite(m, raw, m.span())
        found.append(cite)
        spans.append((m.start(), m.end()))
        if more_years_in is not None:
            found.extend(_more_years(more_years_in, _year_after(m), cite, m.span()))

    for m in _scan(_NARRATIVE_ORG, sentence):
        _add(m, m.group(0))
    # Narrative: Surname (Year[, further years])
    for m in _scan(_NARRATIVE, sentence):
        if _overlaps(m.span(), spans):
            continue
        _add(m, m.group(0), more_years_in=sentence)
    # Parenthetical: (Surname Year; Surname Year)
    for mp in _PAREN.finditer(sentence):
        inner = mp.group(1)
        # Within one parenthetical group, a chunk that is nothing but a year means
        # "the same author again, that year": "(Radford et al., 2018; 2019)" is two
        # citations, not one.  Carry the surname of the previous chunk into it.
        prev = None
        for chunk in re.split(r";", inner):
            m_year = _BARE_YEAR_RE.fullmatch(chunk.strip())
            if m_year and prev is not None:
                found.append({
                    "marker_raw": "(" + chunk.strip() + ")",
                    "surname": prev["surname"],
                    "year": int(m_year.group("year")),
                    "suffix": m_year.group("suf") or "",
                    "span": mp.span(),
                    "coauthors": prev["coauthors"],
                    "own_name_keys": prev["own_name_keys"],
                })
                continue
            before = len(found)
            # A parenthesised URL is a link, not a citation: its path segments read
            # as "(Surname Year)" to the patterns below ("…/Annual-Report-2016.pdf"
            # -> surname "Annual-Report-", year 2016) and would become an orphan no
            # bibliography could ever satisfy.  Everything from the scheme onward is
            # URL; a real citation before it ("see Smith 2020, https://…") still
            # matches.  Matching the scheme alone, not the whole URL, because PDF
            # extraction sprinkles spaces through the path.
            m_url = _URL_SCHEME_RE.search(chunk)
            url_start = m_url.start() if m_url else None
            # A work named before "as cited in" is the secondary source — not the
            # paper's own, so it is dropped and only the source after the phrase is read.
            m_cin = _CITED_IN_RE.search(chunk)
            cin_start = m_cin.start() if m_cin else None
            chunk_spans = []
            raw = "(" + chunk.strip() + ")"
            for m in _scan(_PAREN_ORG, chunk):
                if url_start is not None and m.start() >= url_start:
                    continue
                if cin_start is not None and m.start() < cin_start:
                    continue
                cite = _mk_cite(m, raw, mp.span())
                found.append(cite)
                found.extend(_more_years(chunk, _year_after(m), cite, mp.span()))
                chunk_spans.append(m.span())
            for m in _scan(_CITE, chunk):
                if url_start is not None and m.start() >= url_start:
                    continue
                if cin_start is not None and m.start() < cin_start:
                    continue
                if _overlaps(m.span(), chunk_spans):
                    continue
                cite = _mk_cite(m, raw, mp.span())
                found.append(cite)
                found.extend(_more_years(chunk, _year_after(m), cite, mp.span()))
            if len(found) > before:
                prev = found[-1]
    # No-year citations: "(Crane, Barnhofer, & Williams, in press)" and the narrative
    # "Spinhoven … and Williams (in press)".  A demand-driven addition — it reads a
    # shape the year scans cannot, keying on the NOYEAR sentinel so it meets an entry
    # indexed the same way; a cite with no such entry is an orphan like any other.
    for _rx in _inpress_res():
        for m in _scan(_rx, sentence):
            if _overlaps(m.span(), spans):
                continue
            g0 = m.group(0)
            sur = _surname_key(m.group("sur"))
            found.append({
                "marker_raw": g0 if g0.lstrip().startswith("(") else "(" + g0.strip() + ")",
                "surname": sur, "year": NOYEAR, "suffix": "", "span": m.span(),
                "coauthors": _coauthor_keys(g0, sur),
                "own_name_keys": _own_name_keys(g0, m.group("sur")),
            })
            spans.append(m.span())
    # Dedup by what identifies the WORK, keeping the first marker_raw.  The co-authors
    # belong in that key: where two entries share a first author and a year, the names
    # the citation goes on to print are the only thing that says which of them is meant,
    # so "(Mackinger, Loschin, & Leibetseder, 2000; Mackinger, Pachinger, et al., 2000)"
    # is two citations of two works.  Keyed on (surname, year, suffix) alone the second
    # read as a repeat of the first and was dropped here — after the scans had found it
    # — leaving the work it cited reported as never cited at all.
    seen, out = set(), []
    for c in found:
        k = (c["surname"], c["year"], c["suffix"], tuple(c.get("coauthors") or ()))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


# Words the styles let stand between the author and its year — "Arendt famously put
# it (1973)", "(Berhane, unpublished manuscript, 2024)".  Only lowercase words, and
# at most four: a capitalised word would be another name (and the citation would key
# on the wrong author), a digit would be another number, and a long run is prose that
# happens to mention a year, not a citation.
_REV_GAP = r"(?:\s*,)?(?:\s+[a-zà-ÿ][a-zà-ÿ’'‐-]*){1,4}\s*,?\s*"


def _paren_bounds(det: str, pos: int) -> tuple[int, int] | None:
    """The parenthetical group containing *pos*, or None if it is in open prose."""
    op = det.rfind("(", 0, pos)
    if op < 0 or det.rfind(")", 0, pos) > op:
        return None
    close = det.find(")", pos)
    return (op, close) if close >= 0 else None


def _reads_as_citation(det: str, sur_start: int, year_start: int, year_end: int) -> bool:
    """True when a year set apart from its author still reads as that author's citation.

    Prose names years for its own reasons ("the 1973 oil crisis"), so a gap is only
    allowed where the page still marks the year as a citation: it opens its own
    parentheses ("Arendt famously put it (1973)"), or it shares the author's
    parentheses ("(Berhane, unpublished manuscript, 2024)")."""
    if det[:year_start].rstrip().endswith("("):
        return True
    group = _paren_bounds(det, sur_start)
    return group is not None and group[1] >= year_end


def find_intext_reverse(sentence: str, references: list[dict]) -> list[dict]:
    """Supplementary reverse lookup: search text for bibliography (surname, year) pairs.

    For each bibliography entry, search the sentence for the surname appearing as
    a capitalized word near the year.  This catches compound surnames (e.g.
    "Tjong Kim Sang" where the bib has surname="sang" but the forward regex
    extracts the first capitalized word "Tjong") and garbled PDF text where
    at least the surname survives.

    Returns citations in the same format as find_intext():
    [{marker_raw, surname, year, suffix, span}, ...]

    Span-based dedup is handled by the caller via (surname, year, suffix) key
    comparison after matching — this avoids suppressing reverse hits that
    overlap with forward ORPHAN citations."""

    det = detection_text(sentence)
    found = []
    # Collect forward-found (surname, year, suffix) keys for dedup.
    # We still track internal spans to avoid self-duplicates within the
    # reverse pass, but we do NOT suppress overlaps with forward spans.
    internal_spans: list = []

    # _NAME_OR_INIT accepts single-letter middle initials — same as entry_key().
    _NAME_OR_INIT = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ'’‐-]*"
    _NAME = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ'’‐-]+"
    # Multi-word name: handles compound surnames like "De Meulder", "Van der Waals".
    _MULTI_NAME = _NAME_OR_INIT + r'(?:\s+' + _NAME_OR_INIT + r'){0,3}'

    for ref in references:
        surname = ref.get("ay_surname")
        year = ref.get("ay_year")
        suffix = ref.get("ay_suffix", "")
        if not surname or not year:
            continue
        # A surname that is a stop-word (e.g. "May") would never appear as a
        # bibliography surname, but guard anyway.
        if surname in stop_names():
            continue

        escaped = re.escape(surname)
        # A reprint is cited by its edition year ("Carl Schmitt (2005)") while the
        # entry keys on the bracketed original (1920), so look for either; the cite
        # is still reported under the entry's own key, which build_index also
        # indexes under the edition year.
        years = [year]
        rep = _reprint_year(ref.get("raw_entry"), year)
        if rep:
            years.append(rep)
        year_str = "(?:" + "|".join(str(y) for y in years) + ")"

        # Flexible pattern: optional preceding name-words + surname +
        # optional co-authors + separator + year[optional-suffix].
        # Non-greedy preceding block so we find the *closest* surname occurrence.
        pattern = (
            r'(?:(?:' + _NAME + r'\s+)*?)'          # preceding names (non-greedy)
            r'\b(' + escaped + r')\b'                # the surname (group 1)
            r'(?:'                                    # optional co-authors
            r'\s+et\s+al\.?'                           # "et al." — complete, no following names
            r'|'
            r'(?:\s*,\s*' + _MULTI_NAME + r')*'       # "…, Kysel," — comma-separated list
            r'\s*,?\s*(?:and|&|e)\s+' + _MULTI_NAME + # "and/& Name" — requires at least one name
            r'(?:\s*,?\s*(?:and|&|e)\s+' + _MULTI_NAME + r')*'
            r')?'
            r'[\s,]*'
            r'(?:\(\s*)?' + year_str + r'([a-z])?'   # year + optional suffix (group 2)
        )
        surname_re = re.compile(pattern, re.IGNORECASE)

        for m in surname_re.finditer(det):
            # The matched surname word (group 1) must start with uppercase in the
            # original text — otherwise it is an accidental mid-word match.
            actual_word = m.group(1)
            if not actual_word[0].isupper():
                continue
            if _overlaps(m.span(), internal_spans):
                continue

            # Bib suffix takes precedence, but a more specific text suffix wins.
            text_suffix = m.group(2) or ""
            effective_suffix = text_suffix or suffix

            found.append({
                "marker_raw": m.group(0).strip(),
                "surname": surname,
                "year": year,
                "suffix": effective_suffix,
                "span": m.span(),
                # The citation proper, surname to year.  The full span is wider and
                # its start is not meaningful: the pattern opens with a run of
                # "preceding names" and is compiled IGNORECASE, so lowercase words
                # match it too and the match can begin several words early
                # ("Inspired by the writings of Carl Schmitt (2005)").  Callers that
                # need to know WHERE the citation is must use this.
                "key_span": (m.start(1), m.end()),
            })
            internal_spans.append(m.span())

        # Second reading: the author and its year set apart by a few words of prose.
        # Only where the year still reads as a citation (_reads_as_citation), and only
        # for a (surname, year) this bibliography actually carries — the pass is driven
        # by the entries, so a gap can never invent a source, at worst it can attach an
        # existing one to a sentence that merely names the author and a year of his.
        # Case-sensitive, unlike the adjacent reading above: IGNORECASE would let the
        # gap's lowercase-only class match a capitalised word, and the pass would key
        # "Arendt and later Fanon wrote of it (1973)" on Arendt — the wrong author.
        # Only the surname itself is folded, for the ALL-CAPS and McDONALD spellings.
        gapped = re.compile(r'\b((?i:' + escaped + r'))\b' + _REV_GAP +
                            r'\(?\s*' + year_str + r'([a-z])?\b')
        for m in gapped.finditer(det):
            if not m.group(1)[0].isupper():
                continue
            if _overlaps(m.span(), internal_spans):
                continue
            year_end = m.end()
            year_start = year_end - 4 - len(m.group(2) or "")
            if not _reads_as_citation(det, m.start(1), year_start, year_end):
                continue
            found.append({
                "marker_raw": m.group(0).strip(),
                "surname": surname,
                "year": year,
                "suffix": (m.group(2) or "") or suffix,
                "span": m.span(),
                "key_span": (m.start(1), m.end()),
            })
            internal_spans.append(m.span())

    return found


# A reprint carries both years: "Fanon, Frantz [1963] 2008 Black Skin, White Masks"
# — the original in brackets, this edition's after it.  entry_key keys on the first
# year it sees (1963), but the text cites the edition ("Fanon 2008", "Frantz Fanon
# (2008)"), so the entry has to answer to both.
_REPRINT_RE = re.compile(r"\[\s*(" + YEAR + r")\s*\]\s*(" + YEAR + r")")


def _reprint_year(raw_entry: str, year) -> int | None:
    """The edition year of a reprint whose key is the bracketed original, else None."""
    m = _REPRINT_RE.search(raw_entry or "")
    if not m:
        return None
    original, edition = int(m.group(1)), int(m.group(2))
    return edition if year == original and edition != original else None


def _org_acronym(raw_entry: str, year) -> str | None:
    """The acronym an organisational author is cited by.  "United Nations High
    Commission for Refugees (UNHCR) 2021 …" is cited "(UNHCR 2021)", never
    "(United 2021)", but the surname heuristic can only see the first word.

    Restricted to the author position — the text before the entry's year — so a
    bracketed acronym inside a *title* cannot be mistaken for the author."""
    head = raw_entry or ""
    if year is not None:
        i = head.find(str(year))
        if i > 0:
            head = head[:i]
    m = _ORG_ACRONYM_RE.search(head)
    return _surname_key(m.group(1)) if m else None


def build_index(references):
    """References enriched with (surname, year, suffix) -> indices for matching."""
    by_sy = {}        # (surname, year) -> [ref...]
    for r in references:
        sur, year, suf = r.get("ay_surname"), r.get("ay_year"), r.get("ay_suffix", "")
        if sur is None:
            continue
        if year is None:
            # A source that dates itself "in press" carries no year to key on, so it is
            # a reference nobody can cite — unless its citation says "in press" too.
            # Index it under the no-year sentinel; additive, so it only ever answers an
            # otherwise-unmatched "(Author, in press)" citation, never re-points one.
            if _entry_is_noyear(r.get("raw_entry")):
                by_sy.setdefault((sur, NOYEAR), []).append(r)
            continue
        by_sy.setdefault((sur, year), []).append(r)
        # A citation may drop a surname's hyphen or space ("FeldmanBarrett" for
        # "Feldman-Barrett"): index the entry under the separator-stripped form too.
        # Additive — the printed key stays first, so it only resolves an orphan.
        bare = _despace(sur)
        if bare and bare != sur:
            by_sy.setdefault((bare, year), []).append(r)
        # Also index an organisational author under its acronym, and a reprint under
        # its edition year.  Both are additive: the primary key stays in place, so
        # they can only resolve a citation that was an orphan, never re-point one
        # that already matched.
        acro = _org_acronym(r.get("raw_entry"), year)
        if acro and acro != sur:
            by_sy.setdefault((acro, year), []).append(r)
        rep = _reprint_year(r.get("raw_entry"), year)
        if rep:
            by_sy.setdefault((sur, rep), []).append(r)
        # Keys a citation writes but the entry does not carry — a name the authors
        # mistyped, resolved against the PDF's own link layer (see the author-year
        # scheme's link rescue).  Same additive rule as above: the entry keeps its
        # own key, so an alias can only resolve what was an orphan.
        for alias in r.get("ay_aliases") or ():
            group = by_sy.setdefault((alias[0], alias[1]), [])
            if not any(other is r for other in group):
                group.append(r)
    return by_sy


def match(cite, by_sy):
    """('unique', ref) | ('ambiguous', [ref...]) | ('orphan', [])."""
    group = by_sy.get((cite["surname"], cite["year"]), [])
    if not group:
        # A citation that dropped a surname's separators ("FeldmanBarrett") reaches its
        # entry under the stripped alias build_index laid down for it.
        bare = _despace(cite["surname"])
        if bare != cite["surname"]:
            group = by_sy.get((bare, cite["year"]), [])
    if not group:
        # Nothing answers to the word this citation was keyed on.  Before calling the
        # work missing, ask the rest of its author's own name: which word keys a name
        # of several is not something the two sides of the join agree on, and the
        # bibliography is the one that knows (see _own_name_keys).
        #
        # Only where the entry was already lost, so a citation that matched cannot be
        # re-pointed; and only if exactly ONE entry answers, in which case it is the
        # work, spelt as the paper spelt it.  Two answers is not a reason to pick: it
        # goes to the reader as ambiguous, with its candidates.
        for key in cite.get("own_name_keys") or ():
            group = group + [r for r in by_sy.get((key, cite["year"]), [])
                             if not any(other is r for other in group)]
        if len(group) > 1:
            return "ambiguous", group
    if not group:
        return "orphan", []
    if cite["suffix"]:
        exact = [r for r in group if r.get("ay_suffix", "") == cite["suffix"]]
        if len(exact) == 1:
            return "unique", exact[0]
        if len(exact) > 1:
            return "ambiguous", exact
        return "orphan", []   # suffix cited but not in bibliography
    if len(group) == 1:
        return "unique", group[0]
    # Disambiguate by co-author surname(s) named in-text: "(Clark and Gardner,
    # 2018)" names Gardner, present in exactly one candidate's author list.
    # Only tighten the group when the co-author uniquely selects one entry; if
    # it selects none or several, fall back to the full ambiguous set.
    coauthors = cite.get("coauthors") or []
    if coauthors:
        narrowed = [r for r in group
                    if all(_entry_has_surname(r.get("raw_entry", ""), ca)
                           for ca in coauthors)]
        if len(narrowed) == 1:
            return "unique", narrowed[0]
        if len(narrowed) > 1:
            group = narrowed
    # Disambiguate by author count.  "et al." is not decoration: the styles reserve
    # it for three authors or more, and name both authors of a two-author work every
    # time ("Clark and Gardner, 2018").  So an "et al." cite cannot mean the
    # two-author entry — which is the whole difference between BERT's [11]
    # (Christopher Clark and Matt Gardner, 2018) and its [12] (Kevin Clark, Luong,
    # Manning and Le, 2018), two entries that collide on (clark, 2018) and are told
    # apart by nothing else.  A bare surname, symmetrically, means the lone author.
    cite_etal = bool(re.search(r"\bet\s+al", (cite.get("marker_raw") or ""), re.IGNORECASE))
    cite_multi = cite_etal or bool(cite.get("coauthors"))
    if cite_etal:
        strict = [r for r in group if _entry_author_count(r.get("raw_entry", "")) >= 3]
    elif not cite_multi:
        strict = [r for r in group if _entry_author_count(r.get("raw_entry", "")) == 1]
    else:
        # The cite names every one of its authors (no "et al."), so the entry it means
        # has exactly that many.  That is the only thing that tells "Raes, Hermans,
        # Williams, & Eelen (2006)" — four authors — from those same four followed by
        # two more ("… Brunfaut, Hamelinck, & Eelen"): the superset carries all four
        # co-authors and so survives the co-author narrowing above, and only its author
        # COUNT gives it away.  Coarse on its own (the count can err), so it only ever
        # tightens: 0 or several here falls through to the blunter single/multi split.
        named = 1 + len(coauthors)
        strict = [r for r in group if _entry_author_count(r.get("raw_entry", "")) == named]
    if len(strict) == 1:
        return "unique", strict[0]
    # Fall back to the coarser single/multi split, so an author who writes "et al."
    # for a two-author work still resolves exactly as it did before.
    singles = [r for r in group if _entry_author_class(r.get("raw_entry", "")) == "single"]
    multis = [r for r in group if _entry_author_class(r.get("raw_entry", "")) == "multi"]
    if cite_multi and len(multis) == 1:
        return "unique", multis[0]
    if not cite_multi and len(singles) == 1:
        return "unique", singles[0]
    return "ambiguous", group


# The trailing author run of a marker: the maximal sequence of names joined by author
# conjunctions or commas that ends at the citation's year.  Two authors are joined by a
# conjunction or a comma, never by a bare space — so "Overgeneral Memory Functional
# Avoidance Conway and Pleydell-Pearce's (2000)", a section heading the org scan keyed on
# the heading word "Avoidance", still carries its real run "Conway and Pleydell-Pearce's",
# and the space-juxtaposed heading words before it are not part of it.
_AUTHOR_RUN_RE = re.compile(
    r"(?P<run>" + NAME +
    r"(?:(?:\s*,\s*(?:and\s+|&\s+|e\s+)?|\s+(?:and|&|e)\s+)" + NAME + r")+)\s*$",
    re.IGNORECASE)


def retrace_surname(marker_raw: str, year):
    """Re-read a marker's own author run and return a cite keyed on its FIRST author,
    the rest as co-authors — or None when there is no multi-author run to re-key on.

    Only ever consulted for a citation that is otherwise an orphan, when a heading word
    or other noise took the surname slot the org scan keyed on.  It keys on the run's
    FIRST author and never a later one, so it corrects a mis-read surname without ever
    chasing a work by one of a citation's trailing co-authors (the second-author case the
    join deliberately leaves alone)."""
    text = _norm(marker_raw or "")
    my = re.search(r"\(?\s*" + YEAR, text)
    head = (text[:my.start()] if my else text).strip()
    m = _AUTHOR_RUN_RE.search(head)
    if not m:
        return None
    keys: list[str] = []
    for tok in re.findall(NAME, m.group("run")):
        k = _surname_key(tok)
        if k and k not in stop_names() and k not in signal_words() and k not in keys:
            keys.append(k)
    if len(keys) < 2:
        return None
    return {"surname": keys[0], "year": year, "suffix": "",
            "coauthors": keys[1:], "own_name_keys": [], "marker_raw": marker_raw}


def looks_authoryear(body: str, sample_sentences) -> int:
    """Counts author-year citations in the body (for auto mode detection)."""
    return sum(len(find_intext(s)) for s in sample_sentences)


def resolve_ambiguity(parsed: dict, marker_raw: str, chosen) -> int:
    """User choice on an ambiguous citation: links citations with that marker to the
    chosen reference (by ref_number or ref_id). Deterministic, inspectable.
    Returns how many citations were updated."""
    refs = parsed["references"]
    ref = next((r for r in refs if str(r.get("ref_number")) == str(chosen)
                or r["id"] == chosen), None)
    if ref is None:
        raise SystemExit(f"chosen reference '{chosen}' does not exist")
    n = 0
    for c in parsed["citations"]:
        if c.get("marker_raw") == marker_raw and not c.get("ref_id") \
                and ref["id"] in (c.get("candidate_ref_ids") or []):
            c["ref_id"] = ref["id"]
            c["ref_number"] = ref["ref_number"]
            c.pop("candidate_ref_ids", None)
            c["resolved_by"] = "user"
            # update the linked claim
            cl = next((x for x in parsed["claims"] if x["id"] == c["claim_id"]), None)
            if cl and ref["ref_number"] not in cl["marker_numbers"]:
                cl["marker_numbers"] = sorted(cl["marker_numbers"] + [ref["ref_number"]])
                cl["is_multisource"] = len(cl["marker_numbers"]) > 1
            n += 1
    parsed["ambiguities"] = [a for a in parsed.get("ambiguities", [])
                             if a.get("marker_raw") != marker_raw]
    return n


def main():
    import argparse
    import json
    try:
        from core.infra.db import RunRepository
    except ImportError:
        from db import RunRepository
    ap = argparse.ArgumentParser(description="manual resolution of ambiguous citations")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("resolve", help="link an ambiguous citation to the chosen source")
    pr.add_argument("--run", required=True, help="run directory (DB-backed)")
    pr.add_argument("--marker", required=True, help="the ambiguous marker, e.g. '(Lee 2018)'")
    pr.add_argument("--ref", required=True, help="ref_number or ref_id of the chosen source")
    args = ap.parse_args()
    # This legacy command used to rebuild Parse from a projection and thereby
    # erased manuscript/Resolve evidence.  Attribution is now a hash-bound task.
    raise SystemExit(
        "authoryear resolve no longer mutates a DB run; use "
        f"{run_command('tasks', 'answer-review', '--run', '<run>', '--task', '<task-id>', '--target-sha256', '<hash>', '--action', 'select-reference', '--ref', '<ref-id>', '--reason', '<reason>')}"
    )


if __name__ == "__main__":
    main()
