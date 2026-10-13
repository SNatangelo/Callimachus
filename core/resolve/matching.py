# core/resolve/matching.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import html
import html.entities
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET

try:
    from . import sources as _sources
except ImportError:  # direct execution
    from resolve import sources as _sources
try:
    from core.parse.authoryear import _CITATION_SIGNAL_RE
    from core.parse.citation_coordinates import (
        _prefixed_locator_token_parts,
        _surname_first_author_list_end,
        _uniform_prefixed_locator_range_parts,
    )
except ImportError:  # direct execution
    from parse.authoryear import _CITATION_SIGNAL_RE
    from parse.citation_coordinates import (
        _prefixed_locator_token_parts,
        _surname_first_author_list_end,
        _uniform_prefixed_locator_range_parts,
    )


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
_CONTAINER_PREFIX_FUNCTION_WORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of",
    "on", "or", "the", "to", "with",
})
_CONTAINER_SERIES_MARKERS = frozenset({
    "conference", "congress", "proceedings", "symposium", "workshop",
})
_MATHML_FRAGMENT_RE = re.compile(
    r"(?P<open><(?P<prefix>[A-Za-z_][\w.-]*:|)math\b[^>]*>)"
    r"(?P<body>.*?)"
    r"(?P<close></(?P=prefix)math\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML"
_FACE_MARKUP_FRAGMENT_RE = re.compile(
    r"<(?P<tag>sub|sup)>(?P<body>[^<>]+)</(?P=tag)\s*>"
)
_INLINE_STYLE_MARKUP_FRAGMENT_RE = re.compile(
    r"<(?P<tag>i|em|b|strong)>(?P<body>[^<>]+)</(?P=tag)\s*>"
)
_CROSSREF_ITALIC_EMPH_MARKUP_FRAGMENT_RE = re.compile(
    r"<EMPH\s+TYPE\s*=\s*(?P<quote>['\"])ITAL(?P=quote)\s*>"
    r"(?P<body>.*?)</EMPH\s*>",
    re.DOTALL,
)
_CROSSREF_LT_DELIMITER = r"(?:<|&lt;|&LT;|&#0*60;|&#[xX]0*3[cC];)"
_CROSSREF_GT_DELIMITER = r"(?:>|&gt;|&GT;|&#0*62;|&#[xX]0*3[eE];)"
_CROSSREF_EMPH_FRAME_RE = re.compile(
    rf"(?P<open>(?P<open_lt>{_CROSSREF_LT_DELIMITER})EMPH\b"
    rf"(?:(?!{_CROSSREF_GT_DELIMITER}).)*(?P<open_gt>{_CROSSREF_GT_DELIMITER}))"
    r"(?P<body>.*?)"
    rf"(?P<close>(?P<close_lt>{_CROSSREF_LT_DELIMITER})/EMPH\s*"
    rf"(?P<close_gt>{_CROSSREF_GT_DELIMITER}))",
    re.DOTALL,
)
_CROSSREF_ITALIC_EMPH_OPEN_RE = re.compile(
    rf"{_CROSSREF_LT_DELIMITER}EMPH\s+TYPE\s*=\s*"
    r"(?P<quote>\"|'|&quot;|&apos;|&#34;|&#39;|&#[xX]22;|&#[xX]27;)"
    rf"ITAL(?P=quote)\s*{_CROSSREF_GT_DELIMITER}"
)
_VISIBLE_XMLISH_TAG_RE = re.compile(
    r"</?[A-Za-z_][\w:.-]*(?:\s+[^<>]*?)?\s*/?>"
)
_QUALIFIED_RELATION_TAIL_RE = re.compile(
    r"\s*(?P<operator>≠|≤|≥|≈|=|<|>)\s*"
    r"(?P<number>\d+(?:\.\d+)?)(?![\w.])"
)
_PLAIN_QUALIFIED_RELATION_RE = re.compile(
    r"(?<![\w])(?P<context>[^\W\d_]{2,})\s+"
    r"(?P<variable>[^\W\d_])\s*"
    r"(?P<operator>≠|≤|≥|≈|=|<|>)\s*"
    r"(?P<number>\d+(?:\.\d+)?)(?![\w.])",
    re.UNICODE,
)
_XMLISH_CONTEXT_TAG_RE = re.compile(
    r"<(?P<closing>/)?(?P<name>[A-Za-z_][\w:.-]*)[^<>]*>"
)
_COMPLETE_ENTITY_RE = re.compile(r"&(?:#[xX][0-9A-Fa-f]+|#\d+|[A-Za-z][A-Za-z0-9]+);")
_ENTITY_DECODED_EMPH_TAG_RE = re.compile(r"</?EMPH\b", re.IGNORECASE)
_MATHML_MINUS = "−"


def _decode_complete_entities(text: str) -> str:
    """Decode only complete, known HTML5 entities and valid Unicode scalars."""
    def replace(match: re.Match[str]) -> str:
        entity = match.group(0)[1:-1]
        if entity.startswith("#"):
            try:
                codepoint = int(entity[2:], 16) if entity[1:2].casefold() == "x" else int(entity[1:])
            except ValueError:
                return match.group(0)
            if codepoint <= 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                return match.group(0)
            return chr(codepoint)
        return html.entities.html5.get(entity + ";", match.group(0))

    return _COMPLETE_ENTITY_RE.sub(replace, text)


def _decode_unframed_entities(text: str) -> str:
    """Avoid activating EMPH markup synthesized from ordinary text entities."""
    decoded = _decode_complete_entities(text)
    if _ENTITY_DECODED_EMPH_TAG_RE.search(decoded):
        return text
    return decoded


def _mathml_visible_text(root: ET.Element) -> str:
    """Extract visible MathML text while preserving unary-context minus operators."""
    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            local_name = child.tag.rsplit("}", 1)[-1].casefold()
            operator = (child.text or "").strip()
            if local_name == "mo" and operator in _SCIENTIFIC_DASHES:
                parts.append(f" {_MATHML_MINUS} ")
            else:
                visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(root)
    return " ".join(" ".join(parts).split())


def _comparison_text_with_mathml(text: str | None) -> tuple[str, bool]:
    """Expose complete markup for comparison, decoding XML entities only once."""
    original = str(text or "")
    found_valid = False

    markup = [(match, "math") for match in _MATHML_FRAGMENT_RE.finditer(original)]
    # Preserve valid and malformed face fragments verbatim until their own
    # XML-aware comparison parser consumes them; decoding inside them here
    # would turn `&amp;amp;` into `&` twice.
    markup.extend((match, "face") for match in _FACE_MARKUP_FRAGMENT_RE.finditer(original))
    markup.extend((match, "style") for match in _INLINE_STYLE_MARKUP_FRAGMENT_RE.finditer(original))
    markup.extend(
        (match, "style")
        for match in _CROSSREF_ITALIC_EMPH_MARKUP_FRAGMENT_RE.finditer(original)
    )
    markup.extend(
        (match, "emph-frame")
        for match in _CROSSREF_EMPH_FRAME_RE.finditer(original)
    )
    markup.sort(key=lambda item: item[0].start())
    parts: list[str] = []
    cursor = 0
    for match, kind in markup:
        if match.start() < cursor:
            continue
        parts.append(_decode_unframed_entities(original[cursor:match.start()]))
        fragment = match.group(0)
        if kind == "math":
            try:
                root = ET.fromstring(fragment)
            except ET.ParseError:
                parts.append(fragment)
            else:
                if root.tag.startswith("{"):
                    namespace, local_name = root.tag[1:].split("}", 1)
                else:
                    namespace, local_name = "", root.tag
                if local_name.casefold() == "math" and namespace in {"", _MATHML_NAMESPACE}:
                    visible_text = _mathml_visible_text(root)
                    if _ENTITY_DECODED_EMPH_TAG_RE.search(visible_text):
                        parts.append(fragment)
                    else:
                        found_valid = True
                        parts.append(f" {visible_text} ")
                else:
                    parts.append(fragment)
        elif kind == "emph-frame":
            escaped_emph = match
            # Decode only the wrapper delimiters; the XML-aware style parser
            # consumes the inner entities once.
            escaped_open = escaped_emph.group("open")
            if _CROSSREF_ITALIC_EMPH_OPEN_RE.fullmatch(escaped_open):
                opening = _decode_complete_entities(escaped_open)
            else:
                open_lt = escaped_emph.group("open_lt")
                open_gt = escaped_emph.group("open_gt")
                opening = "<" + escaped_open[len(open_lt):-len(open_gt)] + ">"
            escaped_close = escaped_emph.group("close")
            close_lt = escaped_emph.group("close_lt")
            close_gt = escaped_emph.group("close_gt")
            closing = (
                "<" + escaped_close[len(close_lt):-len(close_gt)] + ">"
            )
            parts.append(opening + escaped_emph.group("body") + closing)
        else:
            parts.append(fragment)
        cursor = match.end()
    parts.append(_decode_unframed_entities(original[cursor:]))
    return "".join(parts), found_valid


def _valid_face_markup_fragment(fragment: str) -> tuple[str, str] | None:
    """Return script kind and text for a complete, plain-text face element."""
    try:
        root = ET.fromstring(fragment)
    except ET.ParseError:
        return None
    if (
        root.tag not in {"sub", "sup"}
        or root.attrib
        or len(root)
        or root.text is None
    ):
        return None
    return root.tag, root.text


def _valid_inline_style_markup_fragment(fragment: str) -> tuple[str, str] | None:
    """Return visible text for a complete, supported text-only style element."""
    try:
        root = ET.fromstring(fragment)
    except ET.ParseError:
        return None
    if root.tag == "EMPH":
        if (
            root.attrib != {"TYPE": "ITAL"}
            or len(root)
            or root.text is None
            or not root.text.strip()
            or _VISIBLE_XMLISH_TAG_RE.search(root.text)
        ):
            return None
        return "emph", root.text
    if (
        root.tag not in {"i", "em", "b", "strong"}
        or root.attrib
        or len(root)
        or root.text is None
        or _ENTITY_DECODED_EMPH_TAG_RE.search(root.text)
        # A numeric-only style wrapper is ambiguous with an incorrectly typed
        # sub/superscript. Keep it opaque unless the source supplies the
        # explicit semantic script tag.
        or root.text.strip().isdigit()
    ):
        return None
    return root.tag, root.text


def _qualified_italic_variable_relation(
    text: str, counterpart: str | None = None,
) -> tuple[str, tuple[tuple[object, ...], ...]]:
    """Boundary a bare italic variable only when the peer spells its equation."""
    original = str(text or "")
    if counterpart is None:
        return original, ()

    def plain_relations(source: str) -> set[tuple[str, ...]]:
        return {
            (
                match.group("context").casefold(),
                match.group("variable").casefold(),
                match.group("operator"),
                match.group("number"),
            )
            for match in _PLAIN_QUALIFIED_RELATION_RE.finditer(source)
        }

    def italic_relations(
        source: str,
    ) -> list[tuple[tuple[str, ...], int, int, bool]]:
        found: list[tuple[tuple[str, ...], int, int, bool]] = []
        for candidate in _INLINE_STYLE_MARKUP_FRAGMENT_RE.finditer(source):
            if _inside_markup_element(source, candidate.start()):
                continue
            parsed = _valid_inline_style_markup_fragment(candidate.group(0))
            if parsed is None or parsed[0] != "i":
                continue
            variable = parsed[1].strip()
            if len(variable) != 1 or not unicodedata.category(variable).startswith("L"):
                continue
            tail = _QUALIFIED_RELATION_TAIL_RE.match(source, candidate.end())
            if tail is None:
                continue
            prefix = source[:candidate.start()]
            preceding_word = re.search(
                r"(?<!\w)([^\W\d_]{2,})$", prefix.rstrip(), re.UNICODE,
            )
            if preceding_word is None or not (
                prefix[-1:].isalnum() or prefix[-1:].isspace()
            ):
                continue
            relation = (
                preceding_word.group(1).casefold(),
                variable.casefold(),
                tail.group("operator"),
                tail.group("number"),
            )
            found.append((relation, candidate.start(), candidate.end(), prefix[-1:].isalnum()))
        return found

    peer_plain = plain_relations(str(counterpart))
    own_plain = plain_relations(original)
    own_italic = italic_relations(original)
    peer_italic = italic_relations(str(counterpart))
    # Pair on the closed prose/variable/operator structure, while retaining
    # each complete numeric literal in its signature so changed values differ.
    matches = [entry for entry in own_italic if entry[0][:3] in {r[:3] for r in peer_plain}]
    matched_relations = {entry[0] for entry in matches}
    signatures = [
        ("italic-variable-relation", *relation)
        for relation in sorted(matched_relations)
    ]
    # The plain side receives the same comparison-only signature only when the
    # other title supplies the corresponding valid italic-variable expression.
    signatures.extend(
        ("italic-variable-relation", *relation)
        for relation in sorted(
            relation for relation in own_plain
            if relation[:3] in {entry[0][:3] for entry in peer_italic}
        )
    )
    if not matches:
        return original, tuple(signatures)

    parts: list[str] = []
    cursor = 0
    matched_spans = {(start, end): adjacent for _, start, end, adjacent in matches}
    for candidate in _INLINE_STYLE_MARKUP_FRAGMENT_RE.finditer(original):
        adjacent = matched_spans.get((candidate.start(), candidate.end()))
        if adjacent is None:
            continue
        parts.append(original[cursor:candidate.start()])
        if adjacent:
            parts.append(" ")
        parts.append(candidate.group(0))
        cursor = candidate.end()
    parts.append(original[cursor:])
    return "".join(parts), tuple(signatures)


def _comparison_inline_markup_matches(text: str) -> list[re.Match[str]]:
    matches = list(_FACE_MARKUP_FRAGMENT_RE.finditer(text))
    matches.extend(_INLINE_STYLE_MARKUP_FRAGMENT_RE.finditer(text))
    matches.extend(_CROSSREF_ITALIC_EMPH_MARKUP_FRAGMENT_RE.finditer(text))
    return sorted(matches, key=lambda match: match.start())


def _valid_comparison_inline_markup_fragment(
    fragment: str,
) -> tuple[str, str] | None:
    return (
        _valid_face_markup_fragment(fragment)
        or _valid_inline_style_markup_fragment(fragment)
    )


def _inside_markup_element(text: str, position: int) -> bool:
    """Fail closed when a candidate fragment is nested in another markup tag."""
    stack: list[str] = []
    for match in _XMLISH_CONTEXT_TAG_RE.finditer(text, 0, position):
        fragment = match.group(0)
        name = match.group("name").casefold()
        if match.group("closing"):
            if not stack or stack[-1] != name:
                return True
            stack.pop()
        elif not fragment.rstrip().endswith("/>"):
            stack.append(name)
    return bool(stack)


def _comparison_text_with_valid_inline_markup(
    text: str | None,
) -> tuple[str, bool]:
    """Render only complete plain-text style and script fragments for comparison."""
    original = str(text or "")
    parts: list[str] = []
    cursor = 0
    found_valid = False
    for match in _comparison_inline_markup_matches(original):
        if _inside_markup_element(original, match.start()):
            continue
        parsed = _valid_comparison_inline_markup_fragment(match.group(0))
        if parsed is None:
            continue
        tag, visible_text = parsed
        preceding = original[cursor:match.start()]
        if tag in {"sub", "sup"}:
            # Providers sometimes pretty-print a script element onto its own
            # indented line. That whitespace is formatting around the script,
            # so attach it to the preceding base before comparing operators.
            preceding = preceding.rstrip()
            visible_text = visible_text.strip()
        parts.append(preceding)
        parts.append(visible_text)
        cursor = match.end()
        found_valid = True
    parts.append(original[cursor:])
    rendered = "".join(parts)
    if _ENTITY_DECODED_EMPH_TAG_RE.search(rendered):
        return original, False
    return rendered, found_valid


def _comparison_text_with_valid_inline_styles(
    text: str | None,
) -> tuple[str, bool]:
    """Render only complete supported styling tags without changing token semantics."""
    original = str(text or "")
    parts: list[str] = []
    cursor = 0
    found_valid = False
    style_matches = list(_INLINE_STYLE_MARKUP_FRAGMENT_RE.finditer(original))
    style_matches.extend(
        _CROSSREF_ITALIC_EMPH_MARKUP_FRAGMENT_RE.finditer(original)
    )
    for match in sorted(style_matches, key=lambda item: item.start()):
        if match.start() < cursor or _inside_markup_element(original, match.start()):
            continue
        parsed = _valid_inline_style_markup_fragment(match.group(0))
        if parsed is None:
            continue
        _tag, visible_text = parsed
        parts.append(original[cursor:match.start()])
        parts.append(visible_text)
        cursor = match.end()
        found_valid = True
    parts.append(original[cursor:])
    rendered = "".join(parts)
    if _ENTITY_DECODED_EMPH_TAG_RE.search(rendered):
        return original, False
    return rendered, found_valid


def _inline_script_signature_tokens(
    text: str, *, drop_stopwords: bool,
) -> list[tuple[object, ...]]:
    """Record script kind, visible value, and the operand it follows."""
    signatures: list[tuple[object, ...]] = []
    for match in _FACE_MARKUP_FRAGMENT_RE.finditer(text):
        parsed = _valid_face_markup_fragment(match.group(0))
        if parsed is None:
            continue
        tag, visible_text = parsed
        prefix, _ = _comparison_text_with_valid_inline_markup(text[:match.start()])
        base_tokens = _face_markup_comparison_tokens(prefix, drop_stopwords=drop_stopwords)
        value_tokens = _face_markup_comparison_tokens(
            visible_text.strip(), drop_stopwords=drop_stopwords,
        )
        signatures.append((
            "face-script",
            tag,
            base_tokens[-1] if base_tokens else None,
            tuple(value_tokens),
        ))
    return signatures


def _comparison_text_with_face_markup(text: str | None) -> tuple[str, bool]:
    """Expose text in valid bare sub/sup fragments only for comparisons."""
    original = str(text or "")
    found_valid = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found_valid
        parsed = _valid_face_markup_fragment(match.group(0))
        if parsed is None:
            return match.group(0)
        found_valid = True
        return parsed[1]

    normalized = _FACE_MARKUP_FRAGMENT_RE.sub(replace, original)
    return normalized, found_valid


def _unicode_comparison_tokens(
    text: str | None, *, drop_stopwords: bool,
) -> list[str]:
    normalized = _sources._norm(str(text or ""))
    normalized = re.sub(
        r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])",
        " ",
        normalized,
    )
    tokens = [
        token.casefold()
        for token in re.findall(r"[^\W_]+", normalized, re.UNICODE)
    ]
    if drop_stopwords:
        tokens = [token for token in tokens if token not in _sources._STOP]
    return tokens


_SCIENTIFIC_OPERATOR_ASCII_PUNCTUATION = frozenset("/%^&*!")
_SCIENTIFIC_DASHES = frozenset("-‐‑‒–—−")


def _scientific_comparison_tokens(
    text: str | None, *, drop_stopwords: bool,
) -> list[str]:
    """Tokenize scientific comparison text without discarding its symbols."""
    normalized = str(text or "")
    # A dash between single-letter operands is a mathematical minus; longer
    # hyphenated words keep the existing lexical-join behavior.
    normalized = re.sub(
        r"(?<!\w)([^\W\d_])\s*[-‐‑‒–—−]\s*([^\W\d_])(?!\w)",
        rf"\1 {_MATHML_MINUS} \2",
        normalized,
    )
    normalized = _sources._norm(normalized)
    normalized = re.sub(
        r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])",
        " ",
        normalized,
    )
    tokens: list[str] = []
    word: list[str] = []

    def flush_word() -> None:
        if not word:
            return
        token = "".join(word).casefold()
        if not drop_stopwords or token not in _sources._STOP:
            tokens.append(token)
        word.clear()

    for index, char in enumerate(normalized):
        category = unicodedata.category(char)
        if category[0] in {"L", "N", "M"}:
            word.append(char)
            continue

        flush_word()
        if category.startswith("S") or char in _SCIENTIFIC_OPERATOR_ASCII_PUNCTUATION:
            tokens.append("-" if char == "−" else char.casefold())
            continue
        if category == "Pd" and char in _SCIENTIFIC_DASHES:
            previous = normalized[index - 1] if index else ""
            following = normalized[index + 1] if index + 1 < len(normalized) else ""
            if previous.isdigit() and (
                following.isdigit() or not following or not following.isalnum()
            ):
                tokens.append("-")

    flush_word()
    return tokens


def _face_markup_comparison_tokens(
    text: str | None, *, drop_stopwords: bool,
) -> list[str]:
    """Keep visible operators when a title pair uses valid face markup."""
    return _scientific_comparison_tokens(text, drop_stopwords=drop_stopwords)


def _face_markup_token_sequence(
    text: str | None, *, include_script: bool, drop_stopwords: bool,
) -> tuple[list[str | tuple[object, ...]], bool]:
    original = str(text or "")
    visible_text, found_valid = _comparison_text_with_valid_inline_markup(original)
    tokens: list[str | tuple[object, ...]] = _face_markup_comparison_tokens(
        visible_text, drop_stopwords=drop_stopwords,
    )
    if include_script:
        tokens.extend(_inline_script_signature_tokens(
            original, drop_stopwords=drop_stopwords,
        ))
    return tokens, found_valid


def _has_valid_inline_script(text: str | None) -> bool:
    return bool(_inline_script_signature_tokens(str(text or ""), drop_stopwords=False))


def _scientific_operator_signature(
    text: str | None, counterpart: str | None = None,
) -> tuple[tuple[object, ...], ...]:
    relation_text, relation_signatures = _qualified_italic_variable_relation(
        str(text or ""), counterpart,
    )
    visible_text, _ = _comparison_text_with_mathml(relation_text)
    visible_text, _ = _comparison_text_with_valid_inline_markup(visible_text)
    visible_text, _ = _comparison_text_with_valid_inline_styles(visible_text)
    tokens = _scientific_comparison_tokens(visible_text, drop_stopwords=False)
    operator_signatures = tuple(
        (
            tokens[index - 1] if index else None,
            token,
            tokens[index + 1] if index + 1 < len(tokens) else None,
        )
        for index, token in enumerate(tokens)
        if len(token) == 1 and (
            unicodedata.category(token).startswith("S")
            or token in _SCIENTIFIC_OPERATOR_ASCII_PUNCTUATION
            or token == "-"
        )
    )
    return operator_signatures + relation_signatures


def _title_pair_tokens(left: str | None, right: str | None) -> tuple[set, set]:
    """Tokenize title pairs while preserving valid scientific markup semantics."""
    left_text, left_mathml = _comparison_text_with_mathml(left)
    right_text, right_mathml = _comparison_text_with_mathml(right)
    left_text, _ = _comparison_text_with_valid_inline_styles(left_text)
    right_text, _ = _comparison_text_with_valid_inline_styles(right_text)
    left_face_tokens, left_face = _face_markup_token_sequence(
        left_text, include_script=False, drop_stopwords=True,
    )
    right_face_tokens, right_face = _face_markup_token_sequence(
        right_text, include_script=False, drop_stopwords=True,
    )
    if left_face or right_face:
        if (
            left_face and right_face
            and _has_valid_inline_script(left_text)
            and _has_valid_inline_script(right_text)
        ):
            left_face_tokens, _ = _face_markup_token_sequence(
                left_text, include_script=True, drop_stopwords=True,
            )
            right_face_tokens, _ = _face_markup_token_sequence(
                right_text, include_script=True, drop_stopwords=True,
            )
        return set(left_face_tokens), set(right_face_tokens)
    if left_mathml or right_mathml:
        # MathML may serialize a subscript/superscript as ``U`` and ``235``
        # while citations write the same identifier as ``U235``. Split both
        # sides at letter/digit boundaries only in this MathML comparison.
        return (
            set(_scientific_comparison_tokens(left_text, drop_stopwords=True)),
            set(_scientific_comparison_tokens(right_text, drop_stopwords=True)),
        )
    return _sources._tokens(left_text), _sources._tokens(right_text)


def _titles_exactly_match(
    left: str | None, right: str | None, *, allow_joined: bool = True,
) -> bool:
    left_text, left_mathml = _comparison_text_with_mathml(left)
    right_text, right_mathml = _comparison_text_with_mathml(right)
    left_text, _ = _comparison_text_with_valid_inline_styles(left_text)
    right_text, _ = _comparison_text_with_valid_inline_styles(right_text)
    left_face_tokens, left_face = _face_markup_token_sequence(
        left_text, include_script=False, drop_stopwords=False,
    )
    right_face_tokens, right_face = _face_markup_token_sequence(
        right_text, include_script=False, drop_stopwords=False,
    )
    if left_face or right_face:
        if (
            left_face and right_face
            and _has_valid_inline_script(left_text)
            and _has_valid_inline_script(right_text)
        ):
            left_face_tokens, _ = _face_markup_token_sequence(
                left_text, include_script=True, drop_stopwords=False,
            )
            right_face_tokens, _ = _face_markup_token_sequence(
                right_text, include_script=True, drop_stopwords=False,
            )
        return left_face_tokens == right_face_tokens
    if left_mathml or right_mathml or _has_literal_symbols(left_text, right_text):
        return _scientific_comparison_tokens(
            left_text, drop_stopwords=False,
        ) == _scientific_comparison_tokens(right_text, drop_stopwords=False)
    left_key = _title_key(left_text)
    right_key = _title_key(right_text)
    return bool(left_key and right_key and (
        left_key == right_key
        or (allow_joined and left_key.replace(" ", "") == right_key.replace(" ", ""))
    ))


def _has_literal_symbols(left: str, right: str) -> bool:
    for text in (left, right):
        for index, char in enumerate(text):
            category = unicodedata.category(char)
            if category.startswith("S") and char not in "<>":
                return True
            if category.startswith("L") and ord(char) > 127:
                return True
            if char == "-" and index > 0 and text[index - 1].isdigit():
                return True
    return False


def _comparison_needs_direct_score(
    left: str | None, right: str | None, left_text: str, right_text: str,
) -> bool:
    """Use decoded title representations directly when markup changed visibility."""
    return (
        _has_literal_symbols(left_text, right_text)
        or left_text != str(left or "")
        or right_text != str(right or "")
    )


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


def _same_printed_range_with_electronic_extension(cited: str, matched: str) -> bool:
    """Match a complete printed interval when metadata appends a positive e-page."""
    cited_match = re.fullmatch(r"(\d+)-(\d+)", cited)
    matched_match = re.fullmatch(r"(\d+-\d+)\.e([1-9]\d*)", matched)
    if cited_match is None or matched_match is None:
        return False
    cited_range = _expand_abbreviated_page_range(cited)
    matched_range = _expand_abbreviated_page_range(matched_match[1])
    cited_start, cited_end = map(int, cited_range.split("-", maxsplit=1))
    matched_start, matched_end = map(int, matched_range.split("-", maxsplit=1))
    return (
        cited_start <= cited_end
        and matched_start <= matched_end
        and cited_range == matched_range
    )


def _coordinate_comparison_value(kind: str, value: object) -> str:
    """Return the conservative comparison form for a cited coordinate."""
    text = html.unescape(str(value)).strip()
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    if kind == "elocator":
        prefixed_range = re.fullmatch(
            r"([A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*)\s*-\s*"
            r"([A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*)",
            text,
            re.I,
        )
        if prefixed_range is not None:
            start, end = prefixed_range.groups()
            if start.casefold() == end.casefold():
                # Providers may print one electronic locator as a repeated range.
                text = start
            else:
                # Normalize separator spacing only for prefixed locator tokens.
                text = f"{start}-{end}"
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


def _neutral_prefixed_range_comparison(
    cited_value: str, msg: dict,
) -> tuple[str, str | None]:
    """Compare a role-ambiguous prefixed range only with complete native ranges."""
    cited_parts = _uniform_prefixed_locator_range_parts(cited_value)
    if cited_parts is None:
        return "inconclusive", None
    cited_normalized = f"{cited_parts[0]}{cited_parts[1]}-{cited_parts[0]}{cited_parts[2]}"
    compatible: list[tuple[str, str]] = []
    first_page_value: str | None = None
    for field in ("page", "article-number"):
        candidate = msg.get(field)
        if candidate is None or not str(candidate).strip():
            continue
        value = str(candidate)
        candidate_parts = _uniform_prefixed_locator_range_parts(value)
        if candidate_parts is not None:
            normalized = (
                f"{candidate_parts[0]}{candidate_parts[1]}-"
                f"{candidate_parts[0]}{candidate_parts[2]}"
            )
            compatible.append((normalized, value))
            continue
        if field == "page":
            scalar_parts = _prefixed_locator_token_parts(value)
            if scalar_parts == cited_parts[:2]:
                first_page_value = value

    if not compatible:
        return "inconclusive", first_page_value
    candidate_values = {normalized for normalized, _value in compatible}
    if len(candidate_values) > 1:
        return "inconclusive", None
    matched_value = compatible[0][1]
    normalized_value = next(iter(candidate_values))
    if first_page_value is not None and normalized_value != cited_normalized:
        return "inconclusive", None
    return (
        "match" if normalized_value == cited_normalized else "mismatch",
        matched_value,
    )


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
    return bool(
        (shared and has_abbreviation)
        or _container_ordered_prefix_abbreviation(cited_tokens, matched_tokens)
    )


def _container_ordered_prefix_abbreviation(
    cited_tokens: list[str], matched_tokens: list[str],
) -> bool:
    """Recognize aligned word-prefix venue abbreviations as inconclusive.

    This does not equate venues. It only avoids treating a compact, ordered
    abbreviation as decisive contrary evidence when every semantic token can
    be aligned to the other form. Function words may be omitted. A trailing
    acronym is accepted only when its letters are derivable from the aligned
    preceding title words.
    """
    cited = [
        token for token in cited_tokens
        if token not in _CONTAINER_PREFIX_FUNCTION_WORDS
    ]
    matched = [
        token for token in matched_tokens
        if token not in _CONTAINER_PREFIX_FUNCTION_WORDS
    ]
    if not cited or not matched:
        return False

    def is_prefix_abbreviation(left: str, right: str) -> bool:
        if left == right:
            return False
        shorter, longer = (left, right) if len(left) < len(right) else (right, left)
        return (
            len(shorter) >= 3
            and shorter.isalpha()
            and longer.isalpha()
            and longer.startswith(shorter)
        )

    aligned_canonical_words: list[str] = []
    prefix_used = False
    index = 0
    while index < min(len(cited), len(matched)):
        cited_token = cited[index]
        matched_token = matched[index]
        if cited_token == matched_token:
            aligned_canonical_words.append(cited_token)
        elif is_prefix_abbreviation(cited_token, matched_token):
            aligned_canonical_words.append(matched_token)
            prefix_used = True
        elif is_prefix_abbreviation(matched_token, cited_token):
            aligned_canonical_words.append(cited_token)
            prefix_used = True
        else:
            return False
        index += 1

    if index == len(cited) == len(matched):
        return prefix_used
    if not prefix_used:
        return False

    # An acronym may trail either the cited abbreviation or the expanded
    # provider title, but it must be derivable from all aligned title words.
    acronym = "".join(word[0] for word in aligned_canonical_words)
    if len(acronym) < 2:
        return False
    if index == len(cited) and len(matched) == index + 1:
        return matched[index] == acronym
    if index == len(matched) and len(cited) == index + 1:
        return cited[index] == acronym
    return False


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
        if (
            kind == "article_locator"
            and _uniform_prefixed_locator_range_parts(cited_value) is not None
        ):
            status, matched_value = _neutral_prefixed_range_comparison(
                cited_value, msg,
            )
            comparisons.append({
                "kind": kind,
                "cited_value": cited_value,
                "matched_value": matched_value,
                "status": status,
            })
            continue
        matched_value = msg.get(field)
        if kind == "elocator":
            registered_locator = msg.get("article-number")
            if registered_locator is not None and str(registered_locator).strip():
                matched_value = registered_locator
        elif kind == "article_number" and (
            matched_value is None or not str(matched_value).strip()
        ):
            # Older deposits store a complete numeric article locator as page.
            # A page range or partial endpoint cannot certify that identifier.
            page_locator = msg.get("page")
            if page_locator is not None and re.fullmatch(r"[0-9]+", str(page_locator).strip()):
                matched_value = page_locator
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
            kind == "article_page_range"
            and _same_printed_range_with_electronic_extension(
                _coordinate_comparison_value(kind, cited_value),
                _coordinate_comparison_value(kind, matched_value),
            )
        ):
            status = "match"
        elif (
            kind == "article_locator"
            and re.fullmatch(
                r"\d+", _coordinate_comparison_value(kind, cited_value)
            )
            and re.fullmatch(
                rf"{re.escape(_coordinate_comparison_value(kind, cited_value))}\s*-\s*\d+",
                _coordinate_comparison_value(kind, matched_value),
            )
        ):
            # A neutral scalar can be a page or an article number. Matching
            # the first page of a provider range is compatible, but cannot
            # establish which role the citation intended.
            status = "inconclusive"
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


_TITLELESS_SURNAME_TOKEN = r"[^\W\d_]+(?:[-'’][^\W\d_]+)*"
_TITLELESS_ETAL_AUTHOR_PREFIX = re.compile(
    rf"\s*{_TITLELESS_SURNAME_TOKEN}(?:[ \t]+{_TITLELESS_SURNAME_TOKEN}){{0,2}},"
    r"\s*(?:[A-Za-z]\s*\.(?:\s*-\s*[A-Za-z]\s*\.)?)(?:\s*[A-Za-z]\s*\.){0,7}"
    r"\s+et\s+al\.\s*",
    re.IGNORECASE | re.UNICODE,
)
_TITLELESS_LOCATOR_KINDS = (
    "article_page_range", "elocator", "article_number", "article_locator",
)
_TITLELESS_DEFINITE_LOCATOR_KINDS = (
    "article_page_range", "elocator", "article_number",
)


def _source_spanned_coordinate(ref: dict, kind: str, raw: str) -> tuple[int, int] | None:
    """Return one coordinate span only when it still quotes its source text."""
    rows = [
        item for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict) and item.get("kind") == kind
    ]
    if len(rows) != 1:
        return None
    row = rows[0]
    spans = row.get("spans")
    if not isinstance(spans, list) or len(spans) != 1 or not isinstance(spans[0], dict):
        return None
    start, end = spans[0].get("raw_start"), spans[0].get("raw_end")
    raw_value = row.get("raw_value")
    if (
        not isinstance(start, int) or isinstance(start, bool)
        or not isinstance(end, int) or isinstance(end, bool)
        or start < 0 or end <= start or end > len(raw)
        or not isinstance(raw_value, str) or raw[start:end] != raw_value
    ):
        return None
    normalized = " ".join(
        raw_value.replace("‐", "-").replace("‑", "-")
        .replace("‒", "-").replace("–", "-")
        .replace("—", "-").replace("−", "-").split()
    )
    if row.get("normalized_value") != normalized:
        return None
    return start, end


def _definite_locator_values_are_comparable(kind: str, cited: str, matched: str) -> bool:
    """Require the source and native values to have the same lexical locator role."""
    cited = cited.strip()
    matched = matched.strip()
    if kind == "article_number":
        return bool(re.fullmatch(r"[0-9]+", cited) and re.fullmatch(r"[0-9]+", matched))
    if kind == "elocator":
        cited_match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", cited)
        matched_match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", matched)
        return bool(
            cited_match is not None
            and matched_match is not None
            and cited_match[1] == matched_match[1]
            and len(cited_match[2]) == len(matched_match[2])
        )
    if kind == "article_page_range":
        normalized_cited = re.sub(r"[‐‑‒–—−]", "-", cited)
        normalized_matched = re.sub(r"[‐‑‒–—−]", "-", matched)
        for value in (normalized_cited, normalized_matched):
            compact = re.sub(r"\s+", "", value)
            if not re.fullmatch(r"[0-9]+-[0-9]+", compact):
                return False
            start, end = map(int, _expand_abbreviated_page_range(compact).split("-", maxsplit=1))
            if end < start:
                return False
        return True
    return False


def _punctuation_only_gap(value: str) -> bool:
    return bool(value) and all(
        character.isspace() or unicodedata.category(character).startswith("P")
        for character in value
    )


def _exact_native_title_page_range_error_supported(
    ref: dict, profile: dict, *, work_type: object, matched_title: object,
) -> bool:
    """Allow a page-range error only when the complete literal title identifies the work."""
    native_type = re.sub(r"[\s_]+", "-", str(work_type or "").strip().casefold())
    if native_type != "journal-article":
        return False
    if profile.get("author_match") is not True or profile.get("year_match") is not True:
        return False
    cited_year = ref.get("year")
    matched_year = profile.get("matched_year")
    if (
        not isinstance(cited_year, int) or isinstance(cited_year, bool)
        or matched_year != cited_year
    ):
        return False
    raw = ref.get("raw_entry")
    if not isinstance(raw, str) or not raw or not isinstance(matched_title, str) or not matched_title:
        return False
    if raw.count(matched_title) != 1:
        return False
    title_start = raw.index(matched_title)
    title_end = title_start + len(matched_title)

    coordinates = ref.get("cited_coordinates")
    comparisons = profile.get("coordinate_comparisons")
    if not isinstance(coordinates, (list, tuple)) or not isinstance(comparisons, (list, tuple)):
        return False
    required_kinds = ("container", "volume", "article_page_range")
    spans = {
        kind: _source_spanned_coordinate(ref, kind, raw)
        for kind in required_kinds
    }
    if any(span is None for span in spans.values()):
        return False
    if any(
        isinstance(item, dict)
        and item.get("kind") in {
            "article_number", "elocator", "article_locator", "chapter_page_range",
            "chapter_locator",
        }
        for item in coordinates
    ):
        return False

    container_start, container_end = spans["container"]
    volume_start, volume_end = spans["volume"]
    locator_start, locator_end = spans["article_page_range"]
    if not (
        title_end <= container_start < container_end <= volume_start < volume_end
        <= locator_start < locator_end
        and _punctuation_only_gap(raw[title_end:container_start])
        and _punctuation_only_gap(raw[container_end:volume_start])
        and _punctuation_only_gap(raw[volume_end:locator_start])
    ):
        return False
    year_text = str(cited_year)
    if len(re.findall(rf"(?<![0-9]){re.escape(year_text)}(?![0-9])", raw)) != 1:
        return False

    rows_by_kind: dict[str, list[dict]] = {}
    for item in comparisons:
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            rows_by_kind.setdefault(item["kind"], []).append(item)
    if any(len(rows_by_kind.get(kind, ())) != 1 for kind in required_kinds):
        return False
    container_row = rows_by_kind["container"][0]
    volume_row = rows_by_kind["volume"][0]
    locator_row = rows_by_kind["article_page_range"][0]
    coordinate_by_kind = {
        item.get("kind"): item for item in coordinates
        if isinstance(item, dict) and item.get("kind") in required_kinds
    }
    if len(coordinate_by_kind) != len(required_kinds):
        return False
    for kind, comparison in (
        ("container", container_row), ("volume", volume_row),
    ):
        cited_value = coordinate_by_kind[kind].get("normalized_value")
        if (
            comparison.get("status") != "match"
            or not isinstance(cited_value, str)
            or comparison.get("cited_value") != cited_value
            or not isinstance(comparison.get("matched_value"), str)
            or _coordinate_comparison_value(kind, cited_value)
            != _coordinate_comparison_value(kind, comparison.get("matched_value"))
        ):
            return False
    locator_value = coordinate_by_kind["article_page_range"].get("normalized_value")
    native_locator = locator_row.get("matched_value")
    if (
        locator_row.get("status") != "mismatch"
        or locator_row.get("cited_value") != locator_value
        or not isinstance(locator_value, str)
        or not isinstance(native_locator, str)
        or not _definite_locator_values_are_comparable(
            "article_page_range", locator_value, native_locator,
        )
    ):
        return False
    cited_bounds = tuple(map(int, _expand_abbreviated_page_range(re.sub(
        r"\s+", "", _coordinate_comparison_value("article_page_range", locator_value),
    )).split("-")))
    native_bounds = tuple(map(int, _expand_abbreviated_page_range(re.sub(
        r"\s+", "", _coordinate_comparison_value("article_page_range", native_locator),
    )).split("-")))
    # A retained page boundary is independent location evidence. An identical
    # title cannot turn a wholly different native interval into a field typo.
    if not any(cited == native for cited, native in zip(cited_bounds, native_bounds)):
        return False
    if any(
        item.get("status") == "mismatch"
        and not (item.get("kind") == "article_page_range" and item is locator_row)
        for item in comparisons if isinstance(item, dict)
    ):
        return False
    anchor = _first_author_key(raw)
    matched_author = profile.get("matched_first_author")
    return bool(
        isinstance(anchor, str) and anchor.strip()
        and isinstance(matched_author, str) and matched_author.strip()
        and _author_names_equivalent(_fold_author(anchor), _fold_author(matched_author))
    )


def _metadata_candidate_has_source_spanned_definite_locator_conflict(
    ref: dict, profile: dict | None, *, work_type: object, matched_title: object = None,
) -> bool:
    """Whether direct journal-article metadata contradicts one cited locator.

    Only a unique, source-quoted page range, e-locator, or article number can
    veto a metadata-discovered DOI. Generic locators and incomplete or
    ambiguous comparisons do not establish that contradiction.
    """
    if not isinstance(ref, dict) or not isinstance(profile, dict):
        return False
    native_type = re.sub(r"[\s_]+", "-", str(work_type or "").strip().casefold())
    if native_type != "journal-article":
        return False
    source_type = str(ref.get("source_type") or "").strip().casefold()
    source_kind = str(ref.get("source_kind") or "").strip().casefold()
    if (
        source_type not in {"article", "unknown"}
        or source_kind in {"book_like", "chapter_like", "legal_instrument"}
    ):
        return False
    raw = ref.get("raw_entry")
    if not isinstance(raw, str) or not raw:
        return False

    locator_kinds = {"article_page_range", "elocator", "article_number"}
    coordinates = ref.get("cited_coordinates")
    comparisons = profile.get("coordinate_comparisons")
    if not isinstance(coordinates, (list, tuple)) or not isinstance(comparisons, (list, tuple)):
        return False
    for kind in locator_kinds:
        cited_rows = [
            item for item in coordinates
            if isinstance(item, dict) and item.get("kind") == kind
        ]
        if len(cited_rows) != 1 or _source_spanned_coordinate(ref, kind, raw) is None:
            continue
        comparison_rows = [
            item for item in comparisons
            if isinstance(item, dict) and item.get("kind") == kind
        ]
        if len(comparison_rows) != 1:
            continue
        coordinate = cited_rows[0]
        comparison = comparison_rows[0]
        cited_value = coordinate.get("normalized_value")
        matched_value = comparison.get("matched_value")
        if (
            comparison.get("status") != "mismatch"
            or not isinstance(cited_value, str)
            or not cited_value.strip()
            or comparison.get("cited_value") != cited_value
            or not isinstance(matched_value, str)
            or not matched_value.strip()
        ):
            continue
        if not _definite_locator_values_are_comparable(kind, cited_value, matched_value):
            continue
        if (
            _coordinate_comparison_value(kind, cited_value)
            != _coordinate_comparison_value(kind, matched_value)
        ):
            if _exact_native_title_page_range_error_supported(
                ref, profile, work_type=work_type, matched_title=matched_title,
            ):
                continue
            return True
    return False


def _has_structurally_titleless_article_coordinates(ref: dict) -> bool:
    """Whether the parser proves an empty title before one source-spanned tuple.

    This intentionally uses raw coordinate spans to distinguish a genuinely
    titleless citation from a parser that failed to extract a literal title.
    """
    if not isinstance(ref, dict):
        return False
    raw = ref.get("raw_entry")
    if not isinstance(raw, str) or not raw:
        return False
    container = _source_spanned_coordinate(ref, "container", raw)
    volume = _source_spanned_coordinate(ref, "volume", raw)
    locator_rows = [
        item for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict) and item.get("kind") in _TITLELESS_LOCATOR_KINDS
    ]
    if container is None or volume is None or len(locator_rows) != 1:
        return False
    locator_kind = locator_rows[0].get("kind")
    locator = _source_spanned_coordinate(ref, locator_kind, raw)
    if locator is None:
        return False
    container_start, container_end = container
    volume_start, volume_end = volume
    locator_start, locator_end = locator
    if not (container_start > 0 and container_end <= volume_start <= volume_end <= locator_start < locator_end):
        return False

    # Preserve the already-supported surname-first tuple route unchanged.
    # The parser certificate below is an additional source-closure proof for
    # given-first initialized heads and recognized repository annotations.
    author_prefix = raw[:container_start]
    legacy_author_closed = bool(_TITLELESS_ETAL_AUTHOR_PREFIX.fullmatch(author_prefix))
    if not legacy_author_closed:
        closed_end = _surname_first_author_list_end(author_prefix.rstrip())
        legacy_author_closed = closed_end == len(author_prefix.rstrip())
    if not re.fullmatch(r"\s*\.?\s+", raw[container_end:volume_start]):
        return False

    issue_rows = [
        item for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict) and item.get("kind") == "issue"
    ]
    if len(issue_rows) > 1:
        return False
    if issue_rows:
        issue = _source_spanned_coordinate(ref, "issue", raw)
        if issue is None:
            return False
        issue_start, issue_end = issue
        if not (volume_end <= issue_start < issue_end <= locator_start):
            return False
        if not re.fullmatch(r"\s*\(\s*", raw[volume_end:issue_start]):
            return False
        if not re.fullmatch(r"\s*\)\s*[,;:]\s*", raw[issue_end:locator_start]):
            return False
    elif not re.fullmatch(r"\s*[,;:]\s*", raw[volume_end:locator_start]):
        return False

    legacy_tail = re.fullmatch(
        r"\s*[,.;]?\s*(?:\(\s*(\d{4})\s*\)|(\d{4}))\s*[.,;]?\s*",
        raw[locator_end:],
    )
    if legacy_tail is not None:
        literal_year = legacy_tail.group(1) or legacy_tail.group(2)
        if legacy_author_closed and str(ref.get("year") or "").strip() == literal_year:
            return True

    # Reuse the parser's closed-boundary certificate for titleless forms that
    # the legacy author/tail grammar does not represent. Import locally to avoid
    # coupling parser initialization to resolver initialization.
    try:
        from core.parse.parsing_common import _closed_journal_title_extent
    except ImportError:  # direct execution fallback
        from parse.parsing_common import _closed_journal_title_extent

    title_extent = _closed_journal_title_extent(
        raw, ref.get("year"), ref.get("cited_coordinates"),
    )
    return title_extent is not None and title_extent[0] is None


def _has_structurally_titled_article_coordinates(
    ref: dict, *, allow_generic_scalar: bool = False,
) -> bool:
    """Whether a literal title is bounded by a closed author head and full tuple."""
    if not isinstance(ref, dict) or ref.get("doi"):
        return False
    raw = ref.get("raw_entry")
    title = ref.get("title")
    if not isinstance(raw, str) or not raw or not isinstance(title, str) or not title.strip():
        return False
    title_pieces = re.split(r"\s+", title.strip())
    title_pattern = r"(?<!\w)" + r"\s+".join(
        re.escape(piece) for piece in title_pieces
    ) + r"(?!\w)"
    title_matches = list(re.finditer(title_pattern, raw))
    if len(title_matches) != 1:
        return False
    title_start, title_end = title_matches[0].span()

    # The parsed title must begin after a complete surname-first initialized
    # author list, with only citation punctuation between the two spans.
    author_prefix = raw[:title_start].rstrip()
    author_end = _surname_first_author_list_end(author_prefix)
    separators = r"[\s,.;:!?\"'“”‘’()]*"
    if (
        author_end is None
        or not re.fullmatch(separators, author_prefix[author_end:])
        or not re.fullmatch(separators, raw[len(author_prefix):title_start])
    ):
        return False

    coordinate_rows = [
        item for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict)
    ]
    if any(item.get("kind") in {"chapter_page_range", "chapter_locator"}
           for item in coordinate_rows):
        return False
    containers = [item for item in coordinate_rows if item.get("kind") == "container"]
    volumes = [item for item in coordinate_rows if item.get("kind") == "volume"]
    locators = [
        item for item in coordinate_rows
        if item.get("kind") in _TITLELESS_LOCATOR_KINDS
    ]
    if (
        len(containers) != 1
        or len(volumes) != 1
        or len(locators) != 1
        or (
            locators[0].get("kind") not in _TITLELESS_DEFINITE_LOCATOR_KINDS
            and not (allow_generic_scalar and locators[0].get("kind") == "article_locator")
        )
    ):
        return False
    container = _source_spanned_coordinate(ref, "container", raw)
    volume = _source_spanned_coordinate(ref, "volume", raw)
    locator_kind = locators[0]["kind"]
    locator = _source_spanned_coordinate(ref, locator_kind, raw)
    if container is None or volume is None or locator is None:
        return False
    container_start, container_end = container
    volume_start, volume_end = volume
    locator_start, locator_end = locator
    if not (
        author_end <= title_start < title_end <= container_start
        and container_end <= volume_start <= volume_end <= locator_start < locator_end
        and re.fullmatch(separators, raw[title_end:container_start])
        and re.fullmatch(r"\s*\.?\s+", raw[container_end:volume_start])
    ):
        return False

    issue_rows = [item for item in coordinate_rows if item.get("kind") == "issue"]
    if len(issue_rows) > 1:
        return False
    if issue_rows:
        issue = _source_spanned_coordinate(ref, "issue", raw)
        if issue is None:
            return False
        issue_start, issue_end = issue
        if not (
            volume_end <= issue_start < issue_end <= locator_start
            and re.fullmatch(r"\s*\(\s*", raw[volume_end:issue_start])
            and re.fullmatch(r"\s*\)\s*[,;:]\s*", raw[issue_end:locator_start])
        ):
            return False
    elif not re.fullmatch(r"\s*[,;:]\s*", raw[volume_end:locator_start]):
        return False

    tail = re.fullmatch(
        r"\s*[,.;]?\s*(?:\(\s*(\d{4})\s*\)|(\d{4}))\s*[.,;]?\s*",
        raw[locator_end:],
    )
    if tail is None:
        return False
    literal_year = tail.group(1) or tail.group(2)
    return str(ref.get("year") or "").strip() == literal_year


def _pubmed_titleless_coordinate_profile_supported(
    ref: dict, profile: dict | None,
) -> bool:
    """Require strict typed author/year/journal/volume/locator support."""
    if (
        not _has_structurally_titleless_article_coordinates(ref)
        or not isinstance(profile, dict)
        or profile.get("author_match") is not True
        or profile.get("year_match") is not True
        or profile.get("ordinal_conflict") is True
        or profile.get("metadata_conflict") is True
    ):
        return False
    comparisons = {
        item.get("kind"): item
        for item in profile.get("coordinate_comparisons") or ()
        if isinstance(item, dict)
    }
    if any(item.get("status") == "mismatch" for item in comparisons.values()):
        return False
    if (
        comparisons.get("container", {}).get("status") not in {"match", "inconclusive"}
        or comparisons.get("volume", {}).get("status") != "match"
    ):
        return False
    cited_locators = [
        item for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict) and item.get("kind") in _TITLELESS_LOCATOR_KINDS
    ]
    if len(cited_locators) != 1:
        return False
    if cited_locators[0].get("kind") == "article_locator":
        generic_rows = [
            item for item in profile.get("coordinate_comparisons") or ()
            if isinstance(item, dict) and item.get("kind") == "article_locator"
        ]
        container_rows = [
            item for item in profile.get("coordinate_comparisons") or ()
            if isinstance(item, dict) and item.get("kind") == "container"
        ]
        volume_rows = [
            item for item in profile.get("coordinate_comparisons") or ()
            if isinstance(item, dict) and item.get("kind") == "volume"
        ]
        if (
            len(generic_rows) != 1
            or generic_rows[0].get("status") != "match"
            or len(container_rows) != 1
            or container_rows[0].get("status") != "match"
            or len(volume_rows) != 1
            or volume_rows[0].get("status") != "match"
        ):
            return False
        cited_value = cited_locators[0].get("normalized_value")
        profile_cited_value = generic_rows[0].get("cited_value")
        matched_value = generic_rows[0].get("matched_value")
        if not all(
            isinstance(value, str)
            for value in (cited_value, profile_cited_value, matched_value)
        ):
            return False
        normalized_values = [
            _coordinate_comparison_value("article_locator", value)
            for value in (cited_value, profile_cited_value, matched_value)
        ]
        if any(re.fullmatch(r"[0-9]+", value) is None for value in normalized_values):
            return False
        return len(set(normalized_values)) == 1

    locator_kinds = [
        item.get("kind") for item in cited_locators
        if item.get("kind") in _TITLELESS_DEFINITE_LOCATOR_KINDS
    ]
    return bool(
        len(locator_kinds) == 1
        and comparisons.get(locator_kinds[0], {}).get("status") == "match"
    )


def _pubmed_titled_coordinate_profile_supported(
    ref: dict, profile: dict | None,
) -> bool:
    """Require a closed literal title/author/article tuple and exact EFetch facts."""
    if (
        not _has_structurally_titled_article_coordinates(ref)
        or not isinstance(profile, dict)
        or profile.get("author_match") is not True
        or profile.get("year_match") is not True
        or profile.get("ordinal_conflict") is True
        or profile.get("metadata_conflict") is True
    ):
        return False
    comparisons: dict[str, list[dict]] = {}
    for item in profile.get("coordinate_comparisons") or ():
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            comparisons.setdefault(item["kind"], []).append(item)
    if any(
        item.get("status") == "mismatch"
        for rows in comparisons.values() for item in rows
    ):
        return False
    locators = [
        item.get("kind") for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict) and item.get("kind") in _TITLELESS_LOCATOR_KINDS
    ]
    if len(locators) != 1 or locators[0] not in _TITLELESS_DEFINITE_LOCATOR_KINDS:
        return False
    required = {
        "container": {"match", "inconclusive"},
        "volume": {"match"},
        locators[0]: {"match"},
    }
    for kind, statuses in required.items():
        rows = comparisons.get(kind, ())
        if len(rows) != 1 or rows[0].get("status") not in statuses:
            return False
        coordinate = next(
            (item for item in ref.get("cited_coordinates") or ()
             if isinstance(item, dict) and item.get("kind") == kind),
            None,
        )
        if (
            coordinate is None
            or rows[0].get("cited_value") != coordinate.get("normalized_value")
            or not isinstance(rows[0].get("matched_value"), str)
            or not rows[0]["matched_value"].strip()
        ):
            return False
    return True


def _metadata_candidate_has_volume_and_locator_conflict(profile: dict | None) -> bool:
    """Whether a metadata candidate contradicts both cited volume and locator.

    A venue abbreviation, issue, or chapter page alone is not a work-identity
    contradiction. Require the existing typed comparisons to show a mismatch
    for volume and for one unambiguous article locator; multiple locator fields
    still contribute only one independent dimension.
    """
    if not isinstance(profile, dict):
        return False
    comparisons = {
        item.get("kind"): item
        for item in profile.get("coordinate_comparisons") or ()
        if isinstance(item, dict)
    }
    volume_conflict = comparisons.get("volume", {}).get("status") == "mismatch"
    locator_kinds = {"article_page_range", "elocator", "article_number"}
    locator_conflict = any(
        comparisons.get(kind, {}).get("status") == "mismatch"
        for kind in locator_kinds
    )
    return volume_conflict and locator_conflict


def _source_container_gap_has_nonconflicting_full_extent(
    ref: dict, raw: str, container_span: tuple[int, int], profile: dict,
) -> bool:
    """Whether a partial container's full title-to-span text avoids mismatch.

    Only an unexplained title gap makes the full literal extent relevant. If
    that extent still contradicts the same populated metadata container, the
    original source-spanned suffix remains valid hard evidence.
    """
    title = ref.get("title")
    if not isinstance(title, str) or not title.strip():
        return False
    title_pieces = re.split(r"\s+", title.strip())
    title_pattern = r"(?<!\w)" + r"\s+".join(
        re.escape(piece) for piece in title_pieces
    ) + r"(?!\w)"
    matches = list(re.finditer(title_pattern, raw))
    if len(matches) != 1:
        return False
    title_end = matches[0].end()
    container_start, container_end = container_span
    if title_end > container_start:
        return False

    gap = raw[title_end:container_start]
    year = str(ref.get("year") or "").strip()
    if re.fullmatch(r"\d{4}", year):
        gap = re.sub(rf"(?<!\d){re.escape(year)}(?!\d)", " ", gap, count=1)
    has_unexplained_text = any(
        not character.isspace()
        and not unicodedata.category(character).startswith("P")
        for character in gap
    )
    if not has_unexplained_text:
        return False

    comparisons = [
        item for item in profile.get("coordinate_comparisons") or ()
        if isinstance(item, dict) and item.get("kind") == "container"
    ]
    if len(comparisons) != 1:
        return False
    matched_value = comparisons[0].get("matched_value")
    if not isinstance(matched_value, str) or not matched_value.strip():
        return False

    full_extent = raw[title_end:container_end]
    return (
        _coordinate_comparison_value("container", full_extent)
        == _coordinate_comparison_value("container", matched_value)
        or _container_comparison_is_ambiguous(full_extent, matched_value)
    )


def _metadata_candidate_has_container_and_locator_conflict(
    ref: dict, profile: dict | None,
) -> bool:
    """Whether source-spanned container and article locator both conflict.

    This is a second independent-coordinate pair for cases where volume is
    absent. Both cited coordinates must be uniquely source-spanned, and the
    existing typed comparisons must report populated, definite mismatches.
    """
    if not isinstance(ref, dict) or not isinstance(profile, dict):
        return False
    raw = ref.get("raw_entry")
    if not isinstance(raw, str) or not raw:
        return False

    coordinates = [
        item for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict)
    ]
    container_rows = [item for item in coordinates if item.get("kind") == "container"]
    definite_locator_rows = [
        item for item in coordinates
        if item.get("kind") in {"article_page_range", "elocator", "article_number"}
    ]
    if (
        len(container_rows) != 1
        or len(definite_locator_rows) != 1
        or any(item.get("kind") == "article_locator" for item in coordinates)
    ):
        return False

    container = container_rows[0]
    locator = definite_locator_rows[0]
    container_span = _source_spanned_coordinate(ref, "container", raw)
    if (
        container_span is None
        or _source_spanned_coordinate(ref, locator.get("kind"), raw) is None
        or _source_container_gap_has_nonconflicting_full_extent(
            ref, raw, container_span, profile,
        )
    ):
        return False

    comparisons_by_kind: dict[str, list[dict]] = {}
    for item in profile.get("coordinate_comparisons") or ():
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            comparisons_by_kind.setdefault(item["kind"], []).append(item)

    def is_explicit_mismatch(kind: str, coordinate: dict) -> bool:
        rows = comparisons_by_kind.get(kind, ())
        if len(rows) != 1:
            return False
        comparison = rows[0]
        matched_value = comparison.get("matched_value")
        return bool(
            comparison.get("status") == "mismatch"
            and comparison.get("cited_value") == coordinate.get("normalized_value")
            and isinstance(matched_value, str)
            and matched_value.strip()
        )

    return (
        is_explicit_mismatch("container", container)
        and is_explicit_mismatch(locator["kind"], locator)
    )


def _metadata_candidate_has_container_and_volume_conflict(
    ref: dict, profile: dict | None,
) -> bool:
    """Whether source-spanned container and volume both explicitly conflict."""
    if not isinstance(ref, dict) or not isinstance(profile, dict):
        return False
    raw = ref.get("raw_entry")
    if not isinstance(raw, str) or not raw:
        return False

    coordinates = [
        item for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict)
    ]
    containers = [item for item in coordinates if item.get("kind") == "container"]
    volumes = [item for item in coordinates if item.get("kind") == "volume"]
    if len(containers) != 1 or len(volumes) != 1:
        return False
    container, volume = containers[0], volumes[0]
    container_span = _source_spanned_coordinate(ref, "container", raw)
    if (
        container_span is None
        or _source_spanned_coordinate(ref, "volume", raw) is None
        or _source_container_gap_has_nonconflicting_full_extent(
            ref, raw, container_span, profile,
        )
    ):
        return False

    comparisons_by_kind: dict[str, list[dict]] = {}
    for item in profile.get("coordinate_comparisons") or ():
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            comparisons_by_kind.setdefault(item["kind"], []).append(item)

    def is_explicit_mismatch(kind: str, coordinate: dict) -> bool:
        rows = comparisons_by_kind.get(kind, ())
        if len(rows) != 1:
            return False
        comparison = rows[0]
        matched_value = comparison.get("matched_value")
        return bool(
            comparison.get("status") == "mismatch"
            and comparison.get("cited_value") == coordinate.get("normalized_value")
            and isinstance(matched_value, str)
            and matched_value.strip()
        )

    return (
        is_explicit_mismatch("container", container)
        and is_explicit_mismatch("volume", volume)
    )


def _metadata_candidate_has_complete_source_supported_article_coordinates(
    ref: dict, profile: dict | None,
) -> bool:
    """Require unique raw-spanned volume and complete article-locator matches."""
    if not isinstance(ref, dict) or not isinstance(profile, dict):
        return False
    raw = ref.get("raw_entry")
    if not isinstance(raw, str) or not raw:
        return False
    coordinates = [
        item for item in ref.get("cited_coordinates") or ()
        if isinstance(item, dict)
    ]
    volume_rows = [item for item in coordinates if item.get("kind") == "volume"]
    locator_rows = [
        item for item in coordinates
        if item.get("kind") in _TITLELESS_LOCATOR_KINDS
    ]
    if (
        len(volume_rows) != 1
        or len(locator_rows) != 1
        or locator_rows[0].get("kind") not in _TITLELESS_DEFINITE_LOCATOR_KINDS
    ):
        return False

    comparisons_by_kind: dict[str, list[dict]] = {}
    for item in profile.get("coordinate_comparisons") or ():
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            comparisons_by_kind.setdefault(item["kind"], []).append(item)

    for coordinate in (volume_rows[0], locator_rows[0]):
        kind = coordinate["kind"]
        if _source_spanned_coordinate(ref, kind, raw) is None:
            return False
        rows = comparisons_by_kind.get(kind, ())
        if len(rows) != 1:
            return False
        comparison = rows[0]
        matched_value = comparison.get("matched_value")
        if (
            comparison.get("status") != "match"
            or comparison.get("cited_value") != coordinate.get("normalized_value")
            or not isinstance(matched_value, str)
            or not matched_value.strip()
        ):
            return False
        recomputed = _cited_coordinate_comparisons(
            {"cited_coordinates": [coordinate]},
            {_CITED_COORDINATE_FIELDS[kind]: matched_value},
        )
        if (
            len(recomputed) != 1
            or recomputed[0].get("status") != "match"
            or recomputed[0].get("cited_value") != comparison.get("cited_value")
            or recomputed[0].get("matched_value") != matched_value
        ):
            return False
    return True


def _metadata_candidate_has_unsupported_short_title_year_conflict(
    ref: dict, matched_title: str | None, profile: dict | None,
) -> bool:
    """Detect exact short-title collisions without independent identity support."""
    if ref.get("doi") or not isinstance(profile, dict):
        return False
    cited_year = str(ref.get("year") or "").strip()
    matched_year = str(profile.get("matched_year") or "").strip()
    if (
        re.fullmatch(r"\d{4}", cited_year) is None
        or re.fullmatch(r"\d{4}", matched_year) is None
        or cited_year == matched_year
        or profile.get("author_match") is True
    ):
        return False
    cited_title = ref.get("title") or _article_title_candidate(ref)
    if not cited_title or not matched_title:
        return False
    cited_tokens, candidate_tokens = _title_pair_tokens(cited_title, matched_title)
    if (
        not cited_tokens
        or not candidate_tokens
        or len(cited_tokens) >= TITLE_MIN_TOKENS
        or len(candidate_tokens) >= TITLE_MIN_TOKENS
        or not _titles_exactly_match(cited_title, matched_title)
        or _metadata_candidate_has_complete_source_supported_article_coordinates(
            ref, profile,
        )
    ):
        return False
    return True


_RESPONSE_TO_ARTICLE_LEAD_IN = "response to letter regarding article"
_RESPONSE_TO_CITED_WORK_LEAD_INS = (
    tuple(_RESPONSE_TO_ARTICLE_LEAD_IN.split()),
    ("reply", "to"),
    ("commentary",),
)
_CORRECTION_RELATION_LABEL = (
    r"(?:(?:publisher|author)(?:['’]s)?\s+)?"
    r"(?:erratum|corrigendum|correction)"
)
_CORRECTION_RELATION_LEAD_PATTERNS = (
    re.compile(
        rf"^\s*{_CORRECTION_RELATION_LABEL}\s+(?:to|for)\s*:?\s*"
        r"(?P<title>.+?)\s*[.!?]*\s*$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        rf"^\s*{_CORRECTION_RELATION_LABEL}\s*[:;–—-]\s*"
        r"(?P<title>.+?)\s*[.!?]*\s*$",
        re.IGNORECASE | re.DOTALL,
    ),
)
_CORRECTION_RELATION_SUFFIX = re.compile(
    rf"^\s*(?P<title>.+?)(?:\s*[.:;–—-]\s*|\s+)"
    rf"{_CORRECTION_RELATION_LABEL}\s*[.!?]*\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CONTAINER_WORK_TYPES = frozenset({"book", "edited-book", "monograph", "proceedings"})


def _formal_correction_cited_title(candidate_title: str | None) -> str | None:
    """Extract the complete cited title from a closed correction label shape."""
    title = str(candidate_title or "")
    for pattern in _CORRECTION_RELATION_LEAD_PATTERNS:
        match = pattern.fullmatch(title)
        if match is not None:
            return match.group("title").strip()
    match = _CORRECTION_RELATION_SUFFIX.fullmatch(title)
    return match.group("title").strip() if match is not None else None


def _raw_entry_names_correction_candidate(ref: dict, candidate_title: str) -> bool:
    """Keep a correction eligible when the citation itself names that unit."""
    raw = str(ref.get("raw_entry") or "")
    raw, _ = _comparison_text_with_mathml(raw)
    raw, _ = _comparison_text_with_valid_inline_styles(raw)
    candidate, _ = _comparison_text_with_mathml(candidate_title)
    candidate, _ = _comparison_text_with_valid_inline_styles(candidate)
    raw_tokens = _scientific_comparison_tokens(raw, drop_stopwords=False)
    candidate_tokens = _scientific_comparison_tokens(candidate, drop_stopwords=False)
    return bool(candidate_tokens) and any(
        raw_tokens[index:index + len(candidate_tokens)] == candidate_tokens
        for index in range(len(raw_tokens) - len(candidate_tokens) + 1)
    )


def _metadata_candidate_is_formal_correction_of_cited_article(
    ref: dict, matched_title: str | None,
) -> bool:
    """Match only a closed formal correction title of the complete cited work."""
    if ref.get("doi"):
        return False
    cited_title = ref.get("title")
    correction_title = _formal_correction_cited_title(matched_title)
    if (
        not cited_title
        or not correction_title
        or not _titles_exactly_match(correction_title, str(cited_title))
    ):
        return False
    return not _raw_entry_names_correction_candidate(ref, str(matched_title))


def _metadata_candidate_is_response_to_cited_article(
    ref: dict, matched_title: str | None,
) -> bool:
    """Detect a titled Reply/Commentary that embeds the cited work's title.

    The candidate must start with one of the captured relation lead-ins and
    then embed the complete parsed citation title. A citation that itself
    names that relation in its title or raw entry remains eligible.
    """
    candidate_tokens = _title_key(matched_title).split()
    cited_tokens = _title_key(ref.get("title")).split()
    lead_tokens = next(
        (
            lead for lead in _RESPONSE_TO_CITED_WORK_LEAD_INS
            if candidate_tokens[:len(lead)] == list(lead)
        ),
        None,
    )
    if lead_tokens is None or not cited_tokens:
        return _metadata_candidate_is_formal_correction_of_cited_article(
            ref, matched_title,
        )

    def contains_sequence(tokens: list[str], sequence: tuple[str, ...]) -> bool:
        return any(
            tokens[index:index + len(sequence)] == list(sequence)
            for index in range(len(tokens) - len(sequence) + 1)
        )

    raw_tokens = _title_key(ref.get("raw_entry")).split()
    cited_names_relation = (
        any(cited_tokens[:len(lead)] == list(lead) for lead in _RESPONSE_TO_CITED_WORK_LEAD_INS)
        or any(
            contains_sequence(raw_tokens, (*lead, *cited_tokens))
            for lead in _RESPONSE_TO_CITED_WORK_LEAD_INS
        )
    )
    if cited_names_relation:
        return _metadata_candidate_is_formal_correction_of_cited_article(
            ref, matched_title,
        )

    embedded_tokens = candidate_tokens[len(lead_tokens):]
    if embedded_tokens[:len(cited_tokens)] == cited_tokens:
        return True
    return _metadata_candidate_is_formal_correction_of_cited_article(
        ref, matched_title,
    )


def _raw_in_container_title_tail(ref: dict) -> str | None:
    """Return a strictly anchored In: container title after cited-work evidence.

    This narrow shape needs a literal ``. In:``, text for the citation-owned
    work before it, and a closed ``pp. N-M`` range later in the same entry.
    Editor names before the container title are allowed only with an explicit
    ``(ed.)`` or ``(eds.)`` marker.
    """
    raw = str(ref.get("raw_entry") or "")
    in_match = re.search(r"\.\s+In:\s*", raw)
    if in_match is None:
        return None

    prelude = raw[:in_match.start()]
    parsed_title = _title_key(ref.get("title"))
    prelude_key = _title_key(prelude)
    container_tail = raw[in_match.end():]
    page_range = re.search(
        r"(?i)(?:,\s*)pp?\.?\s*\d+\s*[-\u2010\u2011\u2012\u2013\u2014\u2212]\s*\d+(?!\d)",
        container_tail,
    )
    if page_range is None:
        return None
    container_tail = container_tail[:page_range.start()].rstrip()

    editor_marker = re.search(r"\(\s*eds?\.?\s*\)\s*", container_tail, re.IGNORECASE)
    editor_names = container_tail[:editor_marker.start()] if editor_marker else ""
    has_named_editor_marker = bool(
        editor_marker and len(_sources._tokens(editor_names)) >= 2
    )
    parsed_title_is_owned = bool(
        parsed_title
        and not parsed_title.startswith("in ")
        and parsed_title in prelude_key
    )
    if not parsed_title_is_owned:
        # A degraded parser can mistake the literal ``In:`` container for the
        # cited title. Recover only a standalone, article-led or quoted title
        # immediately before a parenthesized year, and only when named editors
        # and a closed ``pp. N-M`` range independently mark a contribution.
        own_title_year = re.fullmatch(
            r"\s*(?P<title>.+?)\s+\((?:18|19|20)\d{2}\)\s*",
            prelude,
        )
        own_title = own_title_year.group("title").strip(" \t,;.") if own_title_year else ""
        own_title_tokens = _sources._tokens(own_title)
        title_shape = bool(
            len(own_title_tokens) >= 2
            and (
                re.match(r"^(?:the|a|an)\b\s+", own_title, re.IGNORECASE)
                or re.fullmatch(r"[\"“‘].+[\"”’]", own_title)
            )
        )
        closed_pp_range = re.search(
            r"(?i)(?:,\s*)pp\.?\s*\d+\s*[-\u2010\u2011\u2012\u2013\u2014\u2212]\s*\d+(?!\d)",
            raw[in_match.end():],
        )
        # Preserve the existing colon-based fallback for styles whose parser
        # title starts at the container but leaves a work title after the author.
        author_title_separator = prelude.rfind(":")
        colon_owned_title = bool(
            author_title_separator >= 0
            and _title_key(prelude[author_title_separator + 1:])
        )
        degraded_in_title_owned = bool(
            parsed_title.startswith("in ")
            and title_shape
            and has_named_editor_marker
            and closed_pp_range
        )
        if not (colon_owned_title or degraded_in_title_owned):
            return None

    if editor_marker is not None:
        container_tail = container_tail[editor_marker.end():]
        collaboration_clause = re.match(
            r"\s*\(\s*in\s+collaboration\s+with\b[^()]*\)\s*",
            container_tail,
            re.IGNORECASE,
        )
        if collaboration_clause is not None:
            container_tail = container_tail[collaboration_clause.end():]
    return container_tail.lstrip(" \t,;:")


def _candidate_title_is_container_tail(
    candidate_title: str | None, container_tail: str,
) -> bool:
    """Require the complete normalized candidate title at the container head."""
    title_tokens = _title_key(candidate_title).split()
    if not title_tokens:
        return False
    pattern = r"[\W_]+".join(re.escape(token) for token in title_tokens)
    match = re.match(pattern, container_tail, re.IGNORECASE)
    if match is None:
        return False
    suffix = container_tail[match.end():].lstrip()
    return not suffix or suffix[0] in ".,;:("


def _raw_titleless_edited_container_tail(ref: dict) -> str | None:
    """Return a container head only for a closed initialized-author contribution."""
    raw = str(ref.get("raw_entry") or "")
    parsed_title = _title_key(ref.get("title"))
    if parsed_title and not parsed_title.startswith("in "):
        return None

    editor_pattern = re.compile(
        r"\(\s*eds?\.?\s+(?P<names>[^()]+?)\s*\)", re.IGNORECASE,
    )
    page_range_pattern = re.compile(
        r"\s*,?\s*(?:pp?\.?\s*)?\d+\s*"
        r"[-\u2010\u2011\u2012\u2013\u2014\u2212]\s*\d+(?!\d)",
        re.IGNORECASE,
    )
    publisher_year_pattern = re.compile(
        r"\s*\(\s*(?P<publisher>[^()]*\S)\s*,\s*"
        r"(?P<year>(?:18|19|20)\d{2})\s*\)\s*\.?\s*",
        re.IGNORECASE,
    )

    for in_match in re.finditer(r"\s+in\s+", raw, re.IGNORECASE):
        # The final period may be part of the last initialized name (``M.``)
        # or ``et al.``; preserve it for the closed-author grammar.
        author_head = raw[:in_match.start()].rstrip(" \t,;:")
        closed_author_end = _surname_first_author_list_end(author_head)
        if closed_author_end != len(author_head):
            continue

        tail = raw[in_match.end():]
        for editor_match in editor_pattern.finditer(tail):
            editor_names = editor_match.group("names").strip()
            closed_editor_end = _surname_first_author_list_end(editor_names)
            if closed_editor_end != len(editor_names):
                continue
            after_editors = tail[editor_match.end():]
            page_match = page_range_pattern.match(after_editors)
            if page_match is None:
                continue
            publisher_year = publisher_year_pattern.fullmatch(
                after_editors[page_match.end():],
            )
            if publisher_year is None or not publisher_year.group("publisher").strip(" ,"):
                continue
            container_head = tail[:editor_match.start()].rstrip(" \t,;:")
            if container_head:
                return container_head
    return None


def _is_explicit_edited_book_parent(
    ref: dict, matched_title: str | None,
) -> bool:
    """Detect an edited-book parent after a separately named chapter title."""
    raw = str(ref.get("raw_entry") or "")
    year = ref.get("year")
    if not re.fullmatch(r"(?:18|19|20)\d{2}", str(year or "")):
        return False
    year_match = re.search(rf"\b{re.escape(str(year))}\b", raw)
    if year_match is None:
        return False

    candidate_tokens = _title_key(matched_title).split()
    if not candidate_tokens:
        return False
    candidate_pattern = re.compile(
        r"[\W_]+".join(re.escape(token) for token in candidate_tokens),
        re.IGNORECASE,
    )
    editor_pattern = re.compile(r"\(\s*[^(),]+?\s*,\s*eds?\.?\s*\)", re.IGNORECASE)
    page_pattern = re.compile(
        r"(?i)(?:,\s*)pp?\.?\s*\d+\s*[-\u2010\u2011\u2012\u2013\u2014\u2212]\s*\d+(?!\d)"
    )

    after_year = raw[year_match.end():]
    for separator in re.finditer(r"\.\s+", after_year):
        explicit_chapter_title = after_year[:separator.start()].strip(" \t,;:")
        if not _title_key(explicit_chapter_title):
            continue
        parent_tail = after_year[separator.end():]
        candidate_match = candidate_pattern.match(parent_tail)
        if candidate_match is None:
            continue
        suffix = parent_tail[candidate_match.end():].lstrip()
        if suffix and suffix[0] not in ".,;:(":
            continue
        editor_match = editor_pattern.search(parent_tail)
        page_match = page_pattern.search(parent_tail)
        if (
            editor_match is not None
            and page_match is not None
            and editor_match.start() >= candidate_match.end()
            and page_match.start() >= editor_match.end()
        ):
            return True
    return False


def _is_explicit_afterword_parent(ref: dict, matched_title: str | None) -> bool:
    """Detect a parent title after an explicitly named afterword contribution."""
    raw = str(ref.get("raw_entry") or "")
    quote_match = re.search(
        r'"(?P<straight>[^"\r\n]{6,})"|'
        r'“(?P<curly_double>[^”\r\n]{6,})”|'
        r'‘(?P<curly_single>[^’\r\n]{6,})’|'
        r"'(?P<single>[^'\r\n]{6,})'",
        raw,
    )
    afterword = re.search(r"\bAfterword\s+in\b", raw, re.IGNORECASE)
    if quote_match is None or afterword is None:
        return False
    quoted = next(value for value in quote_match.groupdict().values() if value is not None)
    if len(_sources._tokens(quoted)) < 2:
        return False
    if quote_match.end() > afterword.start():
        return False

    quoted_key = _title_key(quoted)
    candidate_tokens = _title_key(matched_title).split()
    if not quoted_key or quoted_key == _title_key(matched_title) or not candidate_tokens:
        return False

    parent_pattern = re.compile(
        r"[\W_]+".join(re.escape(token) for token in candidate_tokens),
        re.IGNORECASE,
    )
    afterword_body = raw[afterword.end():]
    parent_match = parent_pattern.search(afterword_body)
    if parent_match is None:
        return False
    suffix = afterword_body[parent_match.end():].lstrip()
    if suffix and suffix[0] not in ".,;:(":
        return False

    page_range = re.search(
        r"(?<!\d)\d+\s*[-\u2010\u2011\u2012\u2013\u2014\u2212]\s*\d+(?!\d)",
        afterword_body[parent_match.end():],
    )
    return page_range is not None


def _metadata_candidate_is_in_container(
    ref: dict, matched_title: str | None, candidate_type: str | None,
) -> bool:
    candidate_type_key = re.sub(r"[\s_]+", "-", str(candidate_type or "").strip().casefold())
    if candidate_type_key not in _CONTAINER_WORK_TYPES:
        return False
    container_tail = _raw_in_container_title_tail(ref)
    if container_tail is not None and _candidate_title_is_container_tail(
        matched_title, container_tail,
    ):
        return True
    titleless_container = _raw_titleless_edited_container_tail(ref)
    if titleless_container is not None and _candidate_title_is_container_tail(
        matched_title, titleless_container,
    ):
        return True
    if _is_explicit_afterword_parent(ref, matched_title):
        return True
    return bool(
        candidate_type_key == "edited-book"
        and _is_explicit_edited_book_parent(ref, matched_title)
    )


def _metadata_candidate_is_dataset_for_journal_article(
    ref: dict, matched_title: str | None, candidate_type: str | None,
) -> bool:
    if str(candidate_type or "").strip().casefold() != "dataset":
        return False
    if not (
        ref.get("source_type") == "article"
        and ref.get("source_kind") == "article_like"
        and ref.get("source_type_confidence") == "high"
        and "journal_volume_issue_pages" in (ref.get("source_type_evidence") or [])
    ):
        return False
    cited_title = _title_key(ref.get("title"))
    return bool(cited_title and cited_title == _title_key(matched_title))


def _quoted_title_candidate_coverage(
    ref: dict, matched_title: str | None,
) -> tuple[float, float] | None:
    """Return coverage when a candidate title is nearly contained by an aligned quote."""
    quoted = quoted_title(ref.get("raw_entry"))
    if not quoted:
        return None
    cited_title = ref.get("title") or _article_title_candidate(ref)
    if not _titles_exactly_match(quoted, cited_title):
        return None
    quoted_tokens, candidate_tokens = _title_pair_tokens(quoted, matched_title)
    if (
        len(quoted_tokens) < TITLE_MIN_TOKENS
        or len(candidate_tokens) < TITLE_MIN_TOKENS
    ):
        return None
    if _titles_exactly_match(matched_title, quoted):
        # Preserve punctuation-only and OCR-joined exact-title matches.
        return 1.0, 1.0
    shared = len(quoted_tokens & candidate_tokens)
    candidate_coverage = shared / len(candidate_tokens)
    if candidate_coverage < 0.95:
        return None
    return candidate_coverage, shared / len(quoted_tokens)


def _metadata_candidate_rejection_reason(
    ref: dict,
    matched_title: str | None,
    profile: dict | None,
    *,
    candidate_type: str | None = None,
) -> str | None:
    """Explain deterministic metadata-candidate identity exclusions."""
    if _metadata_candidate_is_response_to_cited_article(ref, matched_title):
        return (
            "metadata candidate is a response or correction related to the cited article, "
            "not the cited work; "
            "candidate identity withheld"
        )
    if _metadata_candidate_is_in_container(ref, matched_title, candidate_type):
        return (
            "metadata candidate is the In: book/proceedings container, not the "
            "citation-owned work; candidate identity withheld"
        )
    if _metadata_candidate_is_dataset_for_journal_article(
        ref, matched_title, candidate_type,
    ):
        return (
            "metadata candidate is a dataset record, not the cited journal article; "
            "candidate identity withheld"
        )
    if _metadata_candidate_has_volume_and_locator_conflict(profile):
        return (
            "metadata candidate contradicts both cited volume and article locator; "
            "candidate identity withheld"
        )
    if _metadata_candidate_has_container_and_locator_conflict(ref, profile):
        return (
            "metadata candidate contradicts both cited container and article locator; "
            "candidate identity withheld"
        )
    if _metadata_candidate_has_container_and_volume_conflict(ref, profile):
        return (
            "metadata candidate contradicts both cited container and volume; "
            "candidate identity withheld"
        )
    if _metadata_candidate_has_unsupported_short_title_year_conflict(
        ref, matched_title, profile,
    ):
        return (
            "metadata candidate exactly matches a short cited title but conflicts on year "
            "without author or complete article-coordinate support; candidate identity withheld"
        )
    if not ref.get("doi"):
        quote_coverage = _quoted_title_candidate_coverage(ref, matched_title)
        comparisons = {
            item.get("kind"): item
            for item in (profile or {}).get("coordinate_comparisons") or ()
            if isinstance(item, dict)
        }
        matching_article_coordinates = (
            comparisons.get("volume", {}).get("status") == "match"
            and any(
                comparisons.get(kind, {}).get("status") == "match"
                for kind in ("article_page_range", "article_number")
            )
        )
        if (
            quote_coverage is not None
            and quote_coverage[0] >= 0.95
            and quote_coverage[1] < 0.85
            and (not isinstance(profile, dict) or profile.get("author_match") is not True)
            and not matching_article_coordinates
        ):
            return (
                "metadata candidate title is only a fragment of the quoted citation title; "
                "candidate identity withheld"
            )
    return None


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
    title_tokens, entry_tokens = _title_pair_tokens(matched_title, raw_entry)
    if len(title_tokens) < TITLE_MIN_TOKENS:
        # Short titles are ambiguous in a token-set comparison.  They only
        # match when the complete normalized strings are identical.
        if not _titles_exactly_match(matched_title, raw_entry, allow_joined=False):
            return None
        return 1.0
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
    from core.parse.reference_readers import parse_entry

    parsed = parse_entry(raw_entry, include_generic=False)
    author = parsed["fields"].get("first_author")
    if author is not None:
        return author["value"].casefold()
    if parsed["status"] == "ambiguous":
        return None
    return _generic_first_author_key(raw_entry)


def _generic_first_author_key(raw_entry: str | None) -> str | None:
    """Author matching for entries without a supported structural field read."""
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
    try:
        from core.parse.parsing_common import (
            _closed_colon_author_entry,
            _collective_personal_author_head,
        )
    except ImportError:  # direct execution fallback
        from parse.parsing_common import (
            _closed_colon_author_entry,
            _collective_personal_author_head,
        )
    collective_head = _collective_personal_author_head(clean)
    if collective_head is not None:
        return collective_head[1].lower()
    # A small set of complete quoted-article references starts with a given
    # initial and surname closed by a period. Read that author only when the
    # next source text is one balanced literal title; never scan the title for
    # a surname-looking word.
    closed_initial_given = re.match(
        r"\s*(?:[A-Z]\.\s*){1,3}"
        r"(?P<surname>[A-Z][^\W\d_][\w'’.-]*)\.\s*"
        r"(?:“[^“”\r\n]+”|\"[^\"\r\n]+\")",
        clean,
    )
    if closed_initial_given:
        return closed_initial_given.group("surname").lower()
    # Some biomedical and edited-reference styles put the surname before
    # undotted initials (``Smith J Jones RD.`` / ``Smith JR.``).  Accept that
    # order only when the initials are followed by evidence that the author
    # block has closed: a list separator, an ``et al.`` marker, another
    # surname-plus-initials pair, or a period-terminated final initial before
    # the title. This avoids
    # treating a surname mentioned later in prose as the cited author.
    surname_initial = re.match(
        r"\s*(?P<surname>[A-Z][^\W\d_][\w'’.-]*)(?:\s*,\s*|\s+)"
        r"(?P<initials>(?:(?:[A-Z](?:-[A-Z])?|[A-Z]{2,4})\b\.?"
        r"(?:\s+(?:[A-Z](?:-[A-Z])?|[A-Z]{2,4})\b\.?)*))"
        r"(?P<tail>.*)",
        clean,
        re.DOTALL,
    )
    if surname_initial:
        initials = surname_initial.group("initials")
        tail = surname_initial.group("tail")
        has_list_separator = re.match(r"\s*(?:,|;|&|\band\b)", tail) is not None
        has_et_al = re.match(
            r"\s+et\s+al\.?(?=\s|[,;.]|$)", tail, re.IGNORECASE,
        ) is not None
        has_next_author = re.match(
            r"\s+[A-Z][^\W\d_][\w'’.-]*\s+"
            r"(?:[A-Z](?:-[A-Z])?|[A-Z]{2,4})\b",
            tail,
        ) is not None
        has_parenthesized_year = re.match(
            r"\s*\(\s*(?:18|19|20)\d{2}[a-z]?\s*\)\s+\S",
            tail,
            re.IGNORECASE,
        ) is not None
        has_period_terminated_initial = (
            initials.rstrip().endswith(".")
            and re.match(r"\s+[A-Z]", tail) is not None
            # A following surname closed by an author-list separator belongs
            # to the existing natural-order name grammar (Alex P. Rivera, ...).
            # Its middle initial does not close a surname-first author head.
            and re.match(
                r"\s+[A-Z][^\W\d_][\w'’.-]*(?=\s*(?:[,;]|&|\band\b))",
                tail,
            ) is None
        )
        has_closed_colon_tail = (
            re.match(r"\s*:", tail) is not None
            and _closed_colon_author_entry(raw_entry)
        )
        if (
            has_list_separator
            or has_et_al
            or has_next_author
            or has_parenthesized_year
            or has_period_terminated_initial
            or has_closed_colon_tail
        ):
            return surname_initial.group("surname").lower()
    initialed = re.match(
        r"\s*(?:[A-Z]\.\s*){1,3}"
        r"(?P<surname>[A-Z][^\W\d_][\w'’.-]+)"
        r"(?=\s*(?:,|\band\b|&))",
        clean,
    )
    if initialed:
        return initialed.group("surname").lower()
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
    if _has_structurally_titleless_article_coordinates(ref):
        cited_title = None
    cited_tokens, _matched_tokens = _title_pair_tokens(cited_title, matched_title)
    if len(cited_tokens) < TITLE_MIN_TOKENS:
        title_score = _title_match_score(cited_title, matched_title)
    elif (
        _comparison_needs_direct_score(
            cited_title, matched_title,
            _comparison_text_with_mathml(cited_title)[0],
            _comparison_text_with_mathml(matched_title)[0],
        )
    ):
        # When visible symbols are part of either title, score the two title
        # values directly; token overlap against the whole citation can otherwise
        # borrow an unrelated author/operator token and erase a scientific change.
        title_score = _title_match_score(cited_title, matched_title)
    elif _titles_exactly_match(cited_title, matched_title):
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
    quote_coverage = _quoted_title_candidate_coverage(ref, matched_title)
    if quote_coverage is not None:
        title_score = round(min(quote_coverage), 3)
    cited_author = (ref.get("first_author") or "").casefold() or _first_author_key(raw)
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
    ordinal_title = _comparison_text_with_mathml(cited_title)[0]
    if not ref.get("title") and cited_title == raw:
        # Raw-title fallback can contain citation-owned identifiers. Their
        # digits are not title versions; literal title ordinals remain intact.
        ordinal_title = " ".join(
            token for token in ordinal_title.split()
            if not _LOCATOR_TITLE_RE.match(token.lstrip("([{<'\""))
        )
    cited_ordinals = _title_ordinals(ordinal_title)
    matched_ordinals = _title_ordinals(_comparison_text_with_mathml(matched_title)[0])
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


def _crossref_terminal_numeric_title_range_identity_supported(
    ref: dict, msg: dict,
) -> bool:
    """Support one narrow Crossref title suffix using a source-closed article tuple.

    The suffix is treated only as a possible omitted numeric range.  The cited
    title must still occur literally and uniquely in the raw entry before the
    unique source-spanned container; all available article coordinates,
    author, year, and native work type must independently agree.
    """
    if not isinstance(ref, dict) or not isinstance(msg, dict):
        return False
    if (
        ref.get("source_type") != "article"
        or ref.get("source_kind") != "article_like"
        or ref.get("source_type_confidence") != "high"
        or msg.get("type") != "journal-article"
    ):
        return False

    raw = ref.get("raw_entry")
    cited_title = ref.get("title")
    native_titles = msg.get("title")
    subtitles = msg.get("subtitle")
    if (
        not isinstance(raw, str) or not raw
        or not isinstance(cited_title, str) or not cited_title
        or not isinstance(native_titles, list) or len(native_titles) != 1
        or not isinstance(native_titles[0], str)
        or not native_titles[0]
        or subtitles not in (None, []) and (
            not isinstance(subtitles, list)
            or any(not isinstance(value, str) or value.strip() for value in subtitles)
        )
        or len(_sources._tokens(cited_title)) < TITLE_MIN_TOKENS
    ):
        return False

    # Keep the citation's full title byte-for-byte in the metadata prefix.
    # Case folding permits ordinary title casing, while exact slicing keeps
    # scientific symbols, internal numbers, and punctuation intact.
    native_title = native_titles[0].strip()
    cited_title = cited_title.strip()
    if (
        not cited_title
        or native_title[:len(cited_title)].casefold() != cited_title.casefold()
    ):
        return False
    suffix = native_title[len(cited_title):]
    suffix_match = re.fullmatch(
        r",\s+([0-9]{4})\s*[-–—]\s*([0-9]{4})", suffix,
    )
    if suffix_match is None or int(suffix_match.group(1)) >= int(suffix_match.group(2)):
        return False

    coordinates = ref.get("cited_coordinates")
    if not isinstance(coordinates, (list, tuple)):
        return False
    if any(
        not isinstance(row, dict) or row.get("kind") not in _CITED_COORDINATE_FIELDS
        for row in coordinates
    ):
        return False
    rows_by_kind: dict[str, list[dict]] = {}
    for row in coordinates:
        rows_by_kind.setdefault(row["kind"], []).append(row)
    locator_kinds = {
        kind for kind in (
            "article_page_range", "article_number", "chapter_page_range",
            "elocator", "article_locator",
        ) if rows_by_kind.get(kind)
    }
    if (
        len(rows_by_kind.get("container", ())) != 1
        or len(rows_by_kind.get("volume", ())) != 1
        or len(locator_kinds) != 1
        or not locator_kinds <= {"article_page_range", "article_number"}
        or len(rows_by_kind.get(next(iter(locator_kinds)), ())) != 1
    ):
        return False
    locator_kind = next(iter(locator_kinds))
    container_span = _source_spanned_coordinate(ref, "container", raw)
    volume_span = _source_spanned_coordinate(ref, "volume", raw)
    locator_span = _source_spanned_coordinate(ref, locator_kind, raw)
    if container_span is None or volume_span is None or locator_span is None:
        return False
    container_start, container_end = container_span
    volume_start, volume_end = volume_span
    locator_start, locator_end = locator_span
    if not (
        0 < container_start < container_end <= volume_start < volume_end
        <= locator_start < locator_end
    ):
        return False

    title_occurrences = list(re.finditer(re.escape(cited_title), raw, re.IGNORECASE))
    if len(title_occurrences) != 1:
        return False
    title_start, title_end = title_occurrences[0].span()
    if not (
        title_start < title_end <= container_start
        and re.fullmatch(r"[\s.\)\]\}\"'’”»]*", raw[title_end:container_start])
    ):
        return False
    if not _has_structurally_titled_article_coordinates(ref) and not (
        _crossref_year_before_volume_tuple_is_closed(
            ref, raw, title_start, container_span, volume_span, locator_span,
            locator_kind,
        )
    ):
        return False

    matched_author = _crossref_first_author(msg)
    cited_author = (ref.get("first_author") or "").casefold() or _first_author_key(raw)
    matched_year = _crossref_year(msg)
    try:
        cited_year = int(ref.get("year"))
    except (TypeError, ValueError):
        return False
    if (
        not cited_author or not matched_author
        or _fold_author(cited_author) != _fold_author(matched_author)
        or matched_year != cited_year
        or not isinstance(msg.get("volume"), str) or not msg["volume"].strip()
    ):
        return False

    profile = _metadata_match_profile(ref, msg, native_title)
    if (
        profile.get("author_match") is not True
        or profile.get("year_match") is not True
        or profile.get("ordinal_conflict") is True
        or profile.get("metadata_conflict") is True
    ):
        return False
    comparisons = profile.get("coordinate_comparisons") or ()
    if len(comparisons) != len(coordinates):
        return False
    by_kind: dict[str, list[dict]] = {}
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            return False
        kind = comparison.get("kind")
        if (
            comparison.get("status") != "match"
            or not isinstance(kind, str)
            or _coordinate_comparison_value(kind, comparison.get("cited_value"))
            != _coordinate_comparison_value(kind, comparison.get("matched_value"))
        ):
            return False
        by_kind.setdefault(kind, []).append(comparison)
    required_kinds = {"container", "volume", locator_kind}
    return (
        required_kinds <= by_kind.keys()
        and all(len(by_kind.get(kind, ())) == 1 for kind in required_kinds)
        and all(len(by_kind.get(kind, ())) == len(rows)
                for kind, rows in rows_by_kind.items())
    )


def _crossref_year_before_volume_tuple_is_closed(
    ref: dict,
    raw: str,
    title_start: int,
    container_span: tuple[int, int],
    volume_span: tuple[int, int],
    locator_span: tuple[int, int],
    locator_kind: str,
) -> bool:
    """Close the cited title and one year-before-volume page-range tuple.

    This supports the source form ``Journal 1978;57:70-87 (URL)`` that the
    shared structural-title helper's terminal-year grammar does not cover.
    All boundary text remains literal and closed; it is not a general citation
    parser.
    """
    year = ref.get("year")
    if type(year) is not int or locator_kind != "article_page_range":
        return False

    # The raw form has one surname followed by undotted initials (``Trauner
    # JB.``). Keep this fallback narrow and source-closed; the normal tuple
    # path above continues to reuse the shared initialized-author recognizer.
    author_head = re.fullmatch(
        r"\s*(?P<family>[A-Z][^\W\d_][\w'’.-]*)\s+"
        r"(?:[A-Z](?:-[A-Z])?|[A-Z]{2,4})\.?\s*",
        raw[:title_start],
    )
    if author_head is None:
        return False

    container_end = container_span[1]
    volume_start, volume_end = volume_span
    locator_start, locator_end = locator_span
    source_year = re.fullmatch(
        r"\s+(?P<year>(?:18|19|20)\d{2})\s*;\s*",
        raw[container_end:volume_start],
    )
    if (
        source_year is None
        or int(source_year.group("year")) != year
        or not re.fullmatch(r"\s*:\s*", raw[volume_end:locator_start])
    ):
        return False

    # A single terminal URL is retained for the JSTOR form; other trailing
    # prose or a second citation unit cannot be mistaken for part of this tuple.
    return re.fullmatch(
        r"\s*(?:\(\s*https?://[^()\s]+\s*\))?\.?\s*",
        raw[locator_end:],
        re.IGNORECASE,
    ) is not None


def _crossref_terminal_numeric_title_range_result_supported(
    ref: dict, result: dict,
) -> bool:
    """Rebuild strict Crossref native evidence carried by a resolved result."""
    if (
        not isinstance(ref, dict) or not isinstance(result, dict)
        or result.get("status") != "resolved"
        or result.get("via") != "crossref_metadata"
        or result.get("resolution_basis") != "metadata_search"
        or ref.get("doi")
        or not isinstance(result.get("matched_title"), str)
    ):
        return False
    profile = result.get("metadata_match")
    if not isinstance(profile, dict) or not isinstance(
        profile.get("coordinate_comparisons"), list,
    ):
        return False
    matched_author = profile.get("matched_first_author")
    matched_year = profile.get("matched_year")
    matched_venue = profile.get("matched_venue")
    candidate_msg = {
        "type": result.get("work_type"),
        "title": [result["matched_title"]],
        "author": [{"family": matched_author}] if isinstance(matched_author, str) else [],
        "published": {"date-parts": [[matched_year]]},
        "container-title": [matched_venue] if isinstance(matched_venue, str) else [],
    }
    fields = {
        "container": "container-title", "volume": "volume", "issue": "issue",
        "article_page_range": "page", "article_number": "article-number",
        "article_locator": "page", "elocator": "article-number",
    }
    for comparison in profile.get("coordinate_comparisons") or ():
        if not isinstance(comparison, dict):
            continue
        field = fields.get(comparison.get("kind"))
        if field:
            value = comparison.get("matched_value")
            candidate_msg[field] = [value] if field == "container-title" else value

    recomputed_profile = _metadata_match_profile(
        ref, candidate_msg, result["matched_title"],
    )
    profile_fields = (
        "title_overlap", "score", "author_match", "matched_first_author",
        "cited_first_author", "year_match", "matched_year", "matched_venue",
        "venue_overlap", "coordinate_comparisons", "ordinal_conflict",
        "metadata_conflict",
    )
    return (
        all(
            recomputed_profile.get(field) == profile.get(field)
            for field in profile_fields
        )
        and _crossref_terminal_numeric_title_range_identity_supported(
            ref, candidate_msg,
        )
    )


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
    cited_tokens, matched_tokens = _title_pair_tokens(cited_title, matched_title)
    cited_text, cited_mathml = _comparison_text_with_mathml(cited_title)
    matched_text, matched_mathml = _comparison_text_with_mathml(matched_title)
    cited_score_text, matched_score_text = cited_text, matched_text
    cited_text, _ = _comparison_text_with_valid_inline_styles(cited_text)
    matched_text, _ = _comparison_text_with_valid_inline_styles(matched_text)
    _, cited_face = _face_markup_token_sequence(
        cited_text, include_script=False, drop_stopwords=True,
    )
    _, matched_face = _face_markup_token_sequence(
        matched_text, include_script=False, drop_stopwords=True,
    )
    if (
        cited_face or matched_face or cited_mathml or matched_mathml
        or _has_literal_symbols(cited_text, matched_text)
    ) and (
        _scientific_operator_signature(cited_title, matched_title)
        != _scientific_operator_signature(matched_title, cited_title)
    ):
        return 0.0
    if cited_face or matched_face:
        if not cited_tokens or not matched_tokens:
            return None
        if len(cited_tokens) < TITLE_MIN_TOKENS:
            return 1.0 if _titles_exactly_match(cited_title, matched_title) else 0.0
        return round(len(cited_tokens & matched_tokens) / len(matched_tokens), 3)
    if (
        _comparison_needs_direct_score(
            cited_title, matched_title, cited_score_text, matched_score_text,
        )
        and not (cited_face or matched_face)
    ):
        cited_symbol_tokens = set(_scientific_comparison_tokens(cited_text, drop_stopwords=True))
        matched_symbol_tokens = set(_scientific_comparison_tokens(matched_text, drop_stopwords=True))
        if not cited_symbol_tokens or not matched_symbol_tokens:
            return None
        if len(cited_symbol_tokens) < TITLE_MIN_TOKENS:
            return 1.0 if _titles_exactly_match(cited_title, matched_title) else 0.0
        return round(len(cited_symbol_tokens & matched_symbol_tokens) / len(matched_symbol_tokens), 3)
    if len(cited_tokens) < TITLE_MIN_TOKENS:
        if (
            cited_mathml or matched_mathml or cited_face or matched_face
            or _comparison_needs_direct_score(
                cited_title, matched_title, cited_score_text, matched_score_text,
            )
        ):
            if not cited_tokens or not matched_tokens:
                return None
            return 1.0 if _titles_exactly_match(cited_title, matched_title) else 0.0
        cited_key = _title_key(cited_text)
        matched_key = _title_key(matched_text)
        if not cited_key or not matched_key:
            return None
        return 1.0 if cited_key == matched_key else 0.0
    return title_overlap(matched_text, cited_text)


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


def _raw_title_proof_tokens(
    text: str | None, *, include_face_script: bool = False,
) -> tuple[list[str | tuple[object, ...]], bool]:
    """Tokenize only the literal-title proof, retaining Unicode and operators."""
    mathml_text, _ = _comparison_text_with_mathml(text)
    normalized = _sources._norm(mathml_text)
    visible_text, _ = _comparison_text_with_valid_inline_markup(normalized)
    tokens: list[str | tuple[object, ...]] = _scientific_comparison_tokens(
        visible_text, drop_stopwords=False,
    )
    script_signatures = _inline_script_signature_tokens(
        normalized, drop_stopwords=False,
    )
    if include_face_script:
        tokens.extend(script_signatures)
    return tokens, bool(script_signatures)


def _raw_title_proof_pair_tokens(
    left: str | None, right: str | None,
) -> tuple[list[str | tuple[str, str]], list[str | tuple[str, str]]]:
    left_tokens, left_face_markup = _raw_title_proof_tokens(left)
    right_tokens, right_face_markup = _raw_title_proof_tokens(right)
    if left_face_markup and right_face_markup:
        left_tokens, _ = _raw_title_proof_tokens(left, include_face_script=True)
        right_tokens, _ = _raw_title_proof_tokens(right, include_face_script=True)
    return left_tokens, right_tokens


def _raw_full_title_has_independent_coordinate_support(
    ref: dict, matched_title: str | None, metadata_match: dict | None,
) -> bool:
    """Whether a longer matched title is printed in raw text and independently verified.

    This only repairs a title field that ends at a printed prefix: the complete
    candidate must continue that prefix in order and appear before the cited
    container span. Exact author/year, volume, and article-locator agreement are
    required, and any coordinate conflict keeps the ordinary guard in force.
    """
    if not isinstance(metadata_match, dict):
        return False
    if metadata_match.get("author_match") is not True or metadata_match.get("year_match") is not True:
        return False

    raw = ref.get("raw_entry")
    cited_title = _article_title_candidate(ref)
    candidate_tokens_for_citation, cited_tokens = _raw_title_proof_pair_tokens(
        matched_title, cited_title,
    )
    if (
        not isinstance(raw, str)
        or not raw
        or not cited_tokens
        or len(candidate_tokens_for_citation) <= len(cited_tokens)
        or candidate_tokens_for_citation[:len(cited_tokens)] != cited_tokens
    ):
        return False

    container_span = _source_spanned_coordinate(ref, "container", raw)
    if container_span is None:
        return False
    container_start, _container_end = container_span
    candidate_tokens, raw_tokens = _raw_title_proof_pair_tokens(
        matched_title, raw[:container_start],
    )
    cited_tokens_in_raw, _raw_prefix_tokens = _raw_title_proof_pair_tokens(
        cited_title, raw[:container_start],
    )
    prefix_starts = [
        start
        for start in range(len(raw_tokens) - len(cited_tokens_in_raw) + 1)
        if raw_tokens[start:start + len(cited_tokens_in_raw)] == cited_tokens_in_raw
    ]
    if len(prefix_starts) != 1:
        return False
    title_start = prefix_starts[0]
    if raw_tokens[title_start:title_start + len(candidate_tokens)] != candidate_tokens:
        return False

    comparisons = metadata_match.get("coordinate_comparisons")
    if not isinstance(comparisons, list) or any(
        not isinstance(item, dict) or item.get("status") == "mismatch"
        for item in comparisons
    ):
        return False
    if not any(
        item.get("kind") == "volume" and item.get("status") == "match"
        for item in comparisons
    ):
        return False
    locator_kinds = {"article_page_range", "article_number", "article_locator", "elocator"}
    return any(
        item.get("kind") in locator_kinds and item.get("status") == "match"
        for item in comparisons
    )


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
    if _raw_full_title_has_independent_coordinate_support(
        ref, matched_title, result.get("metadata_match"),
    ):
        return result
    signals = _aggregate_metadata_identity_signals(result, attempts)
    exact_title = _titles_exactly_match(cited_title, matched_title)
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
