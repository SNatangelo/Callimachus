# tools/

Development utilities for corpus analysis. These are not part of the shipped pipeline; nothing in `core/` or `run.py` imports from `tools/`.

The three documented below measure parse and fetch across the corpus. The other files in this folder predate this README and are not described here.

## redact_report_html.py

Creates a public derivative of a human HTML report. It retains claim and
verdict excerpts and removes copied source passages, abstracts, prompt
payloads, fetch traces and manuscript context from the embedded audit data.
The original report stays untouched; the output has a sidecar redaction
manifest and embedded provenance, without a visible banner. Preview and debug
runs are rejected. Review the retained quotations before publication.

```
python tools/redact_report_html.py run/report.html --output run/report.public.html
python tools/redact_report_html.py run/report.html --check
```

The tool refuses to overwrite an output. For completed Verify runs with a
verified `report.html`, use:

```
python run.py report-export --run <run> --output <public.html>
```

The command writes the same redacted derivative and sidecar manifest and
refuses to overwrite either file. The desktop Export button uses the same
export function.

The repository example also has a separately documented publication-year
correction; see `examples/report/README.md`.

## parsed_sources_snapshot.py

Reparses every corpus paper and writes one Markdown file per paper, plus an INDEX table, describing each reference exactly as the resolver receives it. Output format matches `Paper test/parsed_sources/` baseline, enabling paper-by-paper diffs after parser changes. Refuses to write into the baseline folder.

```
python3 tools/parsed_sources_snapshot.py --out "Paper test/parsed_sources_<date>"
```

## fetch_corpus_sweep.py

Runs resolve+fetch over every corpus paper, one run directory each, then extracts a per-reference fingerprint from each `run.sqlite`. Captures tier reached, resolve status, via, and resolution basis for each reference. The `--only NAME ...` flag sweeps a subset and merges results into any existing fingerprint.

Each run is left where the driver stops on its own, at the FETCH pause: the fingerprint therefore describes what resolve and the deterministic OA fetch obtained unaided, before any agent or human answered a retrieval task. It is not a verified run.

```
python3 tools/fetch_corpus_sweep.py --out "Paper test/resolve_fetch_<version>"
```

## fetch_fingerprint_diff.py

Diffs two fingerprints, per paper and per reference. References are matched on `ref_number`, never on reference id, because ids are regenerated on every run.

```
python3 tools/fetch_fingerprint_diff.py OLD.sqlite NEW.sqlite
```

## About the corpus

The corpus lives in `Paper test/` and `Paper test/Nuovo/` (18 PDFs total). A full fetch sweep is a long network operation—several hours—so use `--only` while iterating.

Note on identifier counts in a snapshot's header: tallies for doi/pmid/isbn/url span every reference, cross-references included, while `title_only` counts only what still needs resolving by title and year, so cross-references are left out — they inherit an antecedent's result. The asymmetry is deliberate: it reproduces how the historical baseline was counted, and changing it would make every diff against that baseline meaningless.
