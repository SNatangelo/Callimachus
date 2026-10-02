# Publication export utility

`redact_report_html.py` creates a public derivative of a human HTML report.
It retains claim and verdict excerpts and removes nonessential source prose,
abstracts, prompt payloads, fetch traces and manuscript context from the
embedded audit data. The original remains untouched. A sidecar manifest and
embedded provenance record the transformation, without a visible banner.
Preview and debug runs are rejected. Review retained quotations before publication.

```sh
python tools/redact_report_html.py run/report.html --output run/report.public.html
python tools/redact_report_html.py run/report.html --check
```

For completed Verify runs with a verified report seal, use the integrated command:

```sh
python run.py report-export --run <run> --output <public.html>
```

Both commands refuse to overwrite existing output files. The integrated
command also refuses to overwrite its sidecar manifest. The desktop Export
button calls the same export function.

The repository example includes a separately documented publication-year
correction; see [the example notes](../examples/report/README.md).
