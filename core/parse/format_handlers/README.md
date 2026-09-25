# Format Handlers

`core/format_handlers/` is the single registry for format-specific parser
behaviour.  Each module provides parsing hooks that adapt the generic parser
pipeline to the artifacts and structure of a particular input format.

The three existing families — LaTeX, Markdown, and plain-text (PDF / TXT /
DOCX) — are reference implementations of the contract below.

## Required contract

Every format-handler module must define one of:

- `NAME` — a single format label (e.g. `"latex"`)
- `FORMATS` — a list of format labels (e.g. `["pdf", "txt", "docx"]`)

The module is auto-discovered by `__init__.py` at import time.  The label(s)
must match the `fmt` string produced by the corresponding extractor in
`core/extractors/`.

### Hooks

Each module **may** override any subset of these four hooks.  Hooks that are
**not** overridden fall back to the shared defaults in `parsing_common`.

#### `split_body_bibliography(text: str, *, meta: dict | None = None) -> tuple[str, str, dict, int | None]`

Split extracted text into body and bibliography.

Returns:
- `body` — the text before the bibliography heading
- `biblio` — the bibliography section text (entries only)
- `debug` — dict with `cut_line_index`, `total_lines`, `context`
- `end_idx` — index of the last bibliography line, or `None` for formats
  with no post-bibliography prose-bleed risk (LaTeX, Markdown)

Override this when the format has **structural bibliography boundaries**
(heading patterns, list-item structure) that make the PDF-oriented heuristics
in the default unnecessary or harmful.

#### `segment_sentences(body: str, *, meta: dict | None = None) -> list[str]`

Split body text into sentences.

Override this when the format's sentence-boundary conventions differ from
the default (e.g. LaTeX commands affecting punctuation spacing, or Markdown
link syntax spanning sentence boundaries).

#### `parse_references(biblio: str, *, meta: dict | None = None) -> list[dict]`

Parse a bibliography block into reference dicts.

Override this when the format's bibliography structure makes the
default's PDF artifacts heuristics (`_find_biblio_end_index`,
`_truncate_bleed`) unnecessary.  LaTeX and Markdown both override this
to skip prose-bleed detection entirely.

#### `is_table_row(sentence: str, markers: list) -> bool`

Detect whether a sentence is a table/data row whose citation markers should
be suppressed (not treated as claims).

Returns `True` when the sentence looks like a table row, `False` otherwise.

This is the **primary extensibility point** — each format has distinct
table artifacts:

| Format     | Artifacts                                     |
| ---------- | --------------------------------------------- |
| LaTeX      | `&` column separators, `\\\\` row endings     |
| Markdown   | `|` pipe columns                              |
| plain-text | decimal density (`28.4`, `91.5`), `&` from PDF |

## Ordering and config

`core/parse/parsers.json` is the external control plane.

It defines:

- `format_handler_order` — priority when multiple handlers match
- `format_handlers.<name>.enabled` — toggle individual handlers

The registry merges file discovery with config order:

- configured names keep their configured position
- newly discovered modules append automatically
- stale configured names are ignored
- disabled handlers drop their claimed format labels from the registry

## Defaults (what you inherit for free)

Every hook defaults to the shared implementation in `parsing_common`.  When
you write a new handler module, you only need to define the hooks that differ.
Hook defaults:

| Hook                      | Default                             |
| ------------------------- | ----------------------------------- |
| `split_body_bibliography` | `default_split_body_bibliography`   |
| `segment_sentences`       | `default_segment_sentences`         |
| `parse_references`        | `default_parse_references`          |
| `is_table_row`            | `lambda s, m: False` (never suppress) |

The `is_table_row` fallback in the registry itself (when no handler exists
for a format) delegates to the `"txt"` handler.

## Add a new format handler in 3 steps

1. Drop `core/format_handlers/<name>.py`
2. Add or update its entry in `core/parse/parsers.json`
3. Run the parser suite

If adding a handler requires editing `core/citation_schemes/numeric.py` to
special-case its table-row detection, the handler interface is missing a
hook and the gap should be fixed here instead of hardcoding another branch
in the scheme.

## Family facade

`select(fmt)` in `__init__.py` returns a `Family` object with all four hooks
resolved — either the format-specific override or the `parsing_common` default.
Callers in `parse_manuscript.py` never branch on `fmt`; they call
`family.split_body_bibliography()`, `family.segment_sentences()`,
`family.parse_references()`, and the numeric scheme calls
`format_handlers.is_table_row(sentence, markers, fmt)` without knowing which
handler is active.

This is the **plug-and-play guarantee**: dropping a new `.py` file into
this directory and a matching extractor into `core/extractors/` fully
integrates a new format — no orchestrator changes needed.

## Minimal example

See [`_example.py.txt`](./_example.py.txt).  It is intentionally not
importable; it is a copy-paste starter showing the minimum handler contract.
