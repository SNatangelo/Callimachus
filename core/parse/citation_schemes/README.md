# Citation Schemes

`core/citation_schemes/` is the single registry for in-text citation parsing
strategies.

Each scheme is one module, discovered by file drop.

## Required contract

Every scheme module must define:

- `NAME`
- `detect(body, sentences, references) -> float`
- `build(sentences, references, window, manuscript_id) -> (claims, citations, debug_rows, extra)`

Optional:

- `synthesize_references(sentences, manuscript_id, start_num=0) -> list[dict]`

Rules:

- `NAME` is the parser-visible scheme name written into
  `manuscript.citation_mode`.
- `detect(...)` participates in `mode="auto"` selection.
- `build(...)` is the single source of truth for claim/citation emission.
- `synthesize_references(...)` is for schemes whose in-text identifiers are the
  references themselves (for example inline DOI/arXiv draft mode).

## Ordering and config

`core/parse/parsers.json` is the external control plane.

It defines:

- `scheme_order`
- `schemes.<name>.enabled`

The registry merges file discovery with config order:

- configured names keep their configured position
- newly discovered modules append automatically
- stale configured names are ignored
- disabled schemes disappear from the registry and are rejected if explicitly named

Under the default config, auto-selection preserves the historical behavior:

- numeric wins no-signal and numeric ties
- author-year beats numeric only when its score is strictly higher
- inline-doi beats numeric only when it is at least equal to numeric and above author-year
- if author-year and inline-doi tie above numeric, `scheme_order` breaks the tie

## Add a new scheme in 3 steps

1. Drop `core/citation_schemes/<name>.py`
2. Add or update its entry in `core/parse/parsers.json`
3. Run the parser suite

If adding a scheme requires editing `core/parse/parse_manuscript.py` to special-case
its runtime behavior, the scheme interface is missing a hook and the gap should
be fixed here instead of hardcoding another branch in the orchestrator.

## Minimal example

See [`_example.py.txt`](./_example.py.txt). It is intentionally not importable;
it is a copy-paste starter showing the minimum scheme contract.
