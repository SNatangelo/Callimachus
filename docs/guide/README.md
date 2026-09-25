# Complete Callimachus Guide

This guide documents the behavior of Callimachus on the current branch: how to start a verification run, which phases execute, where evidence is stored, when processing stops, and which manual interventions are allowed.

Callimachus checks three questions independently:

1. Does the source exist, and has it been identified correctly?
2. Does the citation follow the selected citation style?
3. Does the source actually support the claim linked to it?

The abbreviated public flow is `parse → resolve → fetch → verify → report`. The driver also executes `gaps` and `style`; the retained `web_research` state performs no third-party evidence collection.

## Reading path

For a first installation, read Chapter 01 with Chapters 10 and 11 open as
setup references. The remaining chapter numbers follow the operating workflow;
the two reference chapters are not steps that should be postponed until the
end.

| Chapter | Use it to |
|---|---|
| [01 — Quick start](01-quickstart.md) | Install dependencies, configure a backend, and complete a first run. |
| [02 — CLI reference](02-cli-reference.md) | Look up options, subcommands, exit codes, and lifecycle modes. |
| [03 — Configuration](03-configuration.md) | Select an accuracy regime, LLM backend, context policy, OCR, network settings, and signing. |
| [04 — Pipeline](04-pipeline.md) | Understand the exact phase order, persisted data, pauses, and gates. |
| [05 — Tasks and recovery](05-tasks-and-recovery.md) | Supply missing sources, answer reviews, use OCR, and resume a run. |
| [06 — Verification and evidence](06-verification-and-evidence.md) | Understand Jury1, grounding, Jury2, source identity, and evidence levels. |
| [07 — Artifacts and provenance](07-artifacts-and-provenance.md) | Audit `run.sqlite`, source files, reports, journals, hashes, and signatures. |
| [08 — Capabilities and limits](08-capabilities-and-limits.md) | Check supported formats, styles, providers, backends, and explicit non-capabilities. |
| [09 — Troubleshooting](09-troubleshooting.md) | Diagnose pauses, locks, network failures, OCR, RAG, report gates, and integrity failures. |
| [10 — Keys and credentials](10-keys-and-credentials.md) | **Setup reference:** generate the signing key, obtain provider credentials, and preserve the trust boundary. |
| [11 — Environment reference](11-environment-reference.md) | **Setup reference:** look up the meaning, format, default, and sensitivity of every `.env.example` label. |
| [12 — Desktop packages](12-desktop-packages.md) | Install a native release, locate its separate configuration and runs, and understand bundled notices and external requirements. |

## Mental model

- `python run.py --input <manuscript>` is the normal entry point. Python owns phase order; an operator or agent cannot skip phases and still produce a valid report.
- `runs/<id>/run.sqlite` is the authoritative record. It stores Parse and Resolve projections, tasks, frozen configuration, Verify attempts, and terminal states.
- The filesystem holds source copies and operator-readable artifacts. It does not replace the database.
- The LLM is confined to semantic evaluation of a claim-source pair. Source identity, evidence grounding, state transitions, provenance, reporting, and the completion gate remain deterministic.
- Missing information stays missing. Callimachus records `unresolved`, `no_text`, `uncertain`, or a typed task; it does not turn uncertainty into success.
- “Run complete” means that required artifacts and terminal records are present and authentic. It does not mean that every citation supports the manuscript.

## Source tree map

The `core/` tree follows the pipeline and keeps technical infrastructure separate
from domain behavior:

| Path | Responsibility |
|---|---|
| `core/app/` | Application control: operator-facing `commands/`, pipeline `phases/`, and shared runtime support in `runtime/`. |
| `core/parse/` | Deterministic manuscript extraction and citation parsing. Parser data lives in `parsers.json`; format, citation-scheme, extractor, and reference-reader implementations have dedicated subpackages. |
| `core/resolve/` | Source identification. `service.py` owns orchestration, `providers/` owns adapters, and `providers.json` plus `provider_config.py` define the registry and policy. The package root is only the public facade. |
| `core/search/` | Optional web search: `router.py` selects an ordered backend, `transport.py` performs requests, and `backends/` contains provider adapters. |
| `core/fetch/` | Full-text acquisition. `service.py` orchestrates and `queue.py` executes frozen candidates; the focused subpackages below own admission, transport, extraction, persistence, fallback, and diagnostic concerns. |
| `core/verify/` | Claim/evidence verification, deterministic grounding, configured model backends, and optional researchers. |
| `core/report/` | Deterministic report projection plus the optional human-readable renderer. |
| `core/style/` | Citation-style detection and checking. |
| `core/shared/` | Small deterministic primitives that are genuinely shared across pipeline phases. |
| `core/infra/` | Technical services only: `db/`, `integrity/`, `llm_runtime/`, performance timing, and startup preflight. Domain phase behavior and provider configuration do not belong here. |

Inside `core/fetch/`, ownership is intentionally explicit:

| Path | Responsibility |
|---|---|
| `service.py`, `queue.py` | Public orchestration and execution of the frozen candidate queue. |
| `candidates.py`, `refdata.py`, `hosts.py` | Fetch-wide candidate construction, resolved-reference interpretation, and host/budget policy shared by the focused subpackages. |
| `admission/` | Fail-closed admission of user-provided source material. |
| `transport/` | HTTP behavior, rate limiting, learned host backoff, pacing, and transport telemetry. |
| `http_profiles/` | Named HTTP request profiles used by the transport layer. |
| `extraction/` | HTML/PDF/OCR extraction, document-relation checks, and text quality validation. |
| `storage/` | Code for the content store, fetch-result store, and fetch cache. This directory contains Python code, not stored run data. |
| `fallbacks/` | Deterministic archive/preview fallbacks and opt-in browser retrieval modes. |
| `diagnostics/` | Fetch-attempt summaries and unresolved-gap reporting. |

`core/storage/` is different from `core/fetch/storage/`: it is generated,
gitignored local runtime state. It is not an importable source package.

## Authoritative sources

This guide summarizes observable behavior from:

- `run.py` and `python run.py --help` for the public dispatcher;
- `core/app/run.py` and `core/app/phases/` for lifecycle control;
- `core/infra/db/` for persistence and tasks;
- `core/verify/claim_evidence/` for the Verify contract;
- `core/report/` and `core/verify/verify_run.py` for reporting and the completion gate;
- `.env.example` for the versioned public environment template, together with the label-by-label [Environment reference](11-environment-reference.md).

If this guide and the implementation diverge, code and tests take precedence. `PLAYBOOK.md` retains the extended operational procedure and historical examples.

---

Next: [Quick start](01-quickstart.md).
