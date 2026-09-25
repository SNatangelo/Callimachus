# Providers

`core/resolve/providers/` is the single registry for external scholarly sources.

Each provider is one module, discovered by file drop. A provider may expose:

- Resolve capability: `supports(ref)` + `discover(ref)`
- Optional resolve enrichment: `enrich(ref)`
- Fetch capability: any of `enabled(...)`, `disabled_reason(...)`, `candidate_items(...)`, `candidate_rows(...)`, `direct_text_items(...)`, `direct_text_rows(...)`
- Optional DOI/PDF hooks: `pdf_url_from_doi(...)`, `landing_to_pdf(...)`
- Optional preprint-only fetch role: `PREPRINT_RESOLVER = True`
- Optional local resolve accelerator: `LOCAL_ACCELERATOR = True`
- Optional authoritative non-DOI identifier capability:
  `AUTHORITATIVE_IDENTIFIER`, `supports(ref)`, and `discover(ref)`
- Optional closed-issue attestation capability:
  `ISSUE_ATTESTATION`, `supports_issue_attestation(ref)`, and `attest_issue(ref)`
- Optional value-free credential declarations: `CREDENTIAL_SPECS = (...)`

Credential-bearing providers declare each binding in their own module:

```python
CREDENTIAL_SPECS = ({
    "provider": NAME,
    "env_name": "EXAMPLE_API_KEY",
    "channels": ("fetch",),
    "label": "Example",
},)
```

The registry derives transport attribution, startup inventory, and runtime
warnings from this declaration. Adding a provider credential must not require a
central credential map or a schema edit. Credential values remain confined to
the provider's outbound-request boundary and must never enter descriptors,
candidates, logs, or persisted provenance.

### Candidate contract and observability

`candidate_items()` returns dictionaries with `method` and `url`.  The following
fields are optional and backward-compatible: `candidate_key`, `discovery_reason`,
`fallback_stage`, and `identity_context`.  The registry derives a conservative
logical `candidate_key` when absent.  It normalizes scheme/host/default-port and
fragments, and treats DOI resolver aliases as one DOI; it does not rewrite paths
or query strings.  When aliases merge, the selected URL remains usable and
`url_aliases`/`provenance` preserve the evidence.

`candidate_rows()` and `direct_text_rows()` always return a row for each enabled
provider. Rows include `provider`, `items`, `status` (`ok`, `partial`, `skipped`,
or `error`), `error`, and an actionable `reason` when applicable. Consumers should
use rows for diagnostics and may keep using the flattened item APIs unchanged.

If a provider intentionally needs the same URL fetched with different request
semantics, put `profile` (or `fetch_profile`) and `strategy` (or `fetch_strategy`)
on its item. Those requests are deliberately not collapsed.

## Required naming

Every provider module must define:

- `NAME`

If the resolve-facing alias differs from the fetch/provider key, also define:

- `RESOLVE_NAME`

Examples:

- `openalex.py`: `NAME = "openalex"`, `RESOLVE_NAME = "openalex_search"`
- `lens.py`: `NAME = "lens_search"` because it is resolve-only

## MANIFEST

`MANIFEST` is optional, but new providers should use it whenever they expose metadata
that orchestration can consume without hardcoding the source name elsewhere.

Supported keys today:

- `origin`: canonical provenance label for `via`-like bookkeeping
- `via_aliases`: extra `via` strings that should map back to this provider
- `doi_prefixes`: DOI prefixes owned by this provider or venue family
- `preprint_host`: whether the provider is a preprint-only/non-record source
- `canonical_hosts`: trusted/canonical hosts owned by the provider
- `host_markers`: text markers associated with the provider
- `weak_abstract_origin`: marks abstract-only metadata that should stay in the
  "weak metadata" bucket unless corroborated

If a provider needs a new manifest key and the orchestrator cannot consume it without
editing a core file, that is a gap in this contract and should be fixed here, not worked
around ad hoc.

## Ordering and config

`core/resolve/providers.json` is the external control plane.

It defines:

- `resolve_order`: priority for resolve-capable providers
- `fetch_order`: priority for fetch-capable providers
- `providers.<name>.enabled`
- `providers.<name>.rate`
- `default_rate`
- host-family metadata such as `trusted_hosts_extra`, `challenge_prone_hosts`,
  `preprint_*`, and related extras

The registry merges file discovery with config order:

- configured names keep their configured position
- newly discovered modules append automatically
- stale configured names are ignored

`LOCAL_ACCELERATOR` modules are an explicit pre-Crossref stage, ordered by
`resolve_order`. They must be safe when unconfigured, must not set
`OPTIONAL_STAGE`, and must return normal auditable resolver results; they are
excluded from generic fallback retries so each logical lookup happens once.

## Authoritative identifiers

A source-specific identifier resolver declares:

```python
AUTHORITATIVE_IDENTIFIER = {"scheme": "my_id", "supersedes": ("doi",)}
```

The adapter must opt in only when the citation contains one unambiguous identifier
on the source's canonical host, and must return a normal resolve result containing
`resolved_identifier`. `supersedes` is reserved for parser-derived schemes that the
explicit source identifier proves were misclassified. When that depends on the
concrete citation, implement `superseded_identifier_schemes(ref)` and return only a
subset of the declared upper bound. An unrelated declared identifier must still be
checked. All applicable authority
adapters run; conflicting resolved identities remain unverified and are never chosen
by provider order.

## Issue attestations

An issue adapter declares a stable provider/rule identity and exposes the generic
attestation boundary:

```python
ISSUE_ATTESTATION = {
    "provider": "my_source_official_issue",
    "rule_version": "issue-attestation/v1",
}
```

`attest_issue(ref)` returns the canonical provider-neutral shape used by the database:
status (`complete`, `enumerated`, `incomplete`, `not_applicable`), target status,
cited scope, members, source response hashes, and typed observations. Only a
`complete` attestation may assert target absence. `enumerated` can corroborate a
present member but cannot prove absence. Exceptions and malformed payloads become
`incomplete`, preserving fail-closed semantics and provenance.

Provider-specific HTML, JSON, XML, or PDF parsing stays inside its module. Adding an
issue adapter must not require edits to the service, adjudicator, or database schema.

## Add a new source in 3 steps

1. Drop `core/resolve/providers/<name>.py`
2. Add its config entry to `core/resolve/providers.json`
3. Run the suite

That is the contract. If adding a source requires touching `core/resolve/`,
`core/fetch.py`, `core/app/pipeline.py`, or `core/app/run.py`, the provider interface is
missing a hook and the gap should be fixed before merging the new source.

## Minimal example

See [`_example.py.txt`](./_example.py.txt). It is intentionally not importable; it is a
copy-paste starter showing the minimum resolve + fetch contract in one file.
