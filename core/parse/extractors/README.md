# Extractors

`core/extractors/` is the single registry for manuscript input formats.

Each extractor is one module, discovered by file drop.

## Required contract

Every extractor module must define:

- `EXTENSIONS`
- `extract(path, *, ocr_lang, ocr_notice, manuscript_ocr) -> (text, fmt, meta)`

Optional:

- `postprocess(text, meta) -> text`

Rules:

- `EXTENSIONS` lists every suffix the module claims, lowercased.
- `fmt` is the canonical parser-visible format string written into
  `manuscript.format`; changing it is a behavior change.
- `meta` is extractor-specific debug metadata carried through parse output.
- `postprocess` is where format-specific cleanup belongs. The orchestrator should
  not special-case one format after extraction.

## Ordering and config

`core/parse/parsers.json` is the external control plane.

It defines:

- `extractor_order`
- `extractors.<name>.enabled`

The registry merges file discovery with config order:

- configured extensions keep their configured position
- newly discovered extensions append automatically
- stale configured extensions are ignored
- disabled extractors drop their claimed extensions from the supported set

## Add a new format in 3 steps

1. Drop `core/extractors/<name>.py`
2. Add or update its entry in `core/parse/parsers.json`
3. Run the parser suite

If adding a format requires touching `core/parse/parse_manuscript.py` for
format-specific cleanup, the extractor contract is missing a hook and the gap
should be fixed here instead of hardcoding another branch in the orchestrator.

## Minimal example

See [`_example.py.txt`](./_example.py.txt). It is intentionally not importable;
it is a copy-paste starter showing the minimum extractor contract.
