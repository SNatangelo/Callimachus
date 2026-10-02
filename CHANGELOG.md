# Changelog

## 1.1.0 — prepared

- Correct publication-year parsing when the first year-shaped token is the
  start of a terminal four-digit page range, as in Dropout (`1929–1958,
  2014`).
- Replace the repository's full-text example HTML with a redacted public
  derivative and a provenance manifest. The public example records its
  correction of the historical Dropout citation year to 2014.
- Include the standalone HTML redaction utility used for publication copies.
- Keep the desktop LLM status aligned with the selected models after a run starts.
- Use the official transparent Callimachus logo in the desktop and Guided Fetch
  headers, with a light wordmark for dark theme.
- Add a gated public-report export to the CLI and desktop History/Analysis
  views. Export writes a separate redacted HTML and manifest after Verify.
- Fix protected database snapshot synchronization and binary artifact reads/copies
  on Windows, preserving exact bytes, SHA-256 identities and durability checks.
