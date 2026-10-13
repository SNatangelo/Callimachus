# tools/

This folder contains the public HTML report redaction utility. It is not part of the shipped pipeline.

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
