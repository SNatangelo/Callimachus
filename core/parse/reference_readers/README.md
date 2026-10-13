# Reference field readers

Each Python module in this package is discovered when it exposes a callable
`parse_fields(entry)` function. A reader declares `NAME`, `VERSION`, `ORDER`,
and `STRUCTURAL`. `parse_fields` returns `None` when its shape does not apply;
otherwise it returns a record with `status`, `fields`, and `conflicts`.

Each field record contains a typed `value`, a non-empty `rule`, and one or more
source `spans`. A span uses offsets into the NFC-normalized input and must
include the exact `raw_value` at those offsets. The dispatcher validates spans
before accepting a reading. `ORDER` controls deterministic presentation only;
conflicting values or source extents cause abstention.

A publication year must come from the reader's bibliographic format anchor.
Recognizing a source kind alone does not establish that anchor. Recognized
entries without a year claim leave the year empty; an ambiguous structural
reading cannot substitute a generic first/last-date scan.

An optional `starts_entry(line)` hook may propose a physical bibliography-line
start. It does not split entries by itself: the parser accepts a boundary only
when the adjacent chunks each yield a complete, unambiguous structural record.
Only configured, enabled readers can make proposals. Generic text reading is
the explicit last-resort path and cannot replace a recognized ambiguity.

Reader enablement and ordering live in `core/parse/parsers.json` under
`reference_readers` and `reference_reader_order`. A new reader needs no central
dispatch switch; keep recognition local to its module and add focused contract
tests for fields, spans, ambiguity, and any start hook.
