# 06 — Verification and Evidence

## Three independent axes

Callimachus never collapses these findings into one opaque score:

| Axis | Question | Main owner |
|---|---|---|
| Identity/existence | Is this the cited source, does it exist, and is it retracted? | Resolve plus deterministic source-identity gates |
| Citation style | Is the bibliography/citation formatted correctly for its source type? | Style modules |
| Claim support | What relationship does this source have to this cited claim? | Guarded Verify runtime |

A stylistically correct reference can be fabricated. A real source can be off-topic. A source can support a claim despite a style error. The report preserves those distinctions.

## Unit of semantic verification

Verify operates on this logical pair identity:

```text
verify:<claim_id>:<ref_id>:<scope>
```

Pending tasks carry frozen source hashes, context, prompt/contract configuration, provider policy, and selection controls. A mismatching payload for an existing pending task is rejected. Existing terminal tasks remain historical and are not automatically replaced solely because one of those inputs changed; use the intended rerun or fork lifecycle when a new verification basis is required.

For a multi-source citation marker, each source is judged only for the role assigned to that source. Cross-reference markers use the admitted antecedent source rather than treating “Id.” or “ibid.” as a new document.

## Evidence scopes

| Persisted Verify scope | What the jury sees | Reliability boundary |
|---|---|---|
| `fulltext_complete` | Complete normalized primary-source text. | Strongest available basis, subject to identity and extraction quality. |
| `abstract_only` | Title/abstract where full text is known not to exist or is not applicable. | Authorized abstract-level conclusion under the selected regime. |
| `abstract_fallback` | Abstract used while fuller text may exist but was not obtained. | Provisional or limited; cannot be presented as complete full-text verification. |
| `preview_snippet` | An attributed Google Books snippet from the cited book. | Limited source text; preserved separately from full text and abstracts. |

Evidence scope is stored per verification fact. A later full-text result does not rewrite a historical abstract or preview attempt; it adds a stronger, separately traceable basis.

Context preparation is orthogonal to the persisted scope. Full text can use an effective context mode of `full_text` or `extractive_rag`; the standalone preprocessing interface also supports `fulltext_trimmed`. Those labels are not additional scopes emitted by the current Verify phase. Google Books preview acquisition is key-gated and emits `preview_snippet`; generic third-party web pages are not Verify evidence.

## Source-identity admission

### Manual remediation is bounded

Source identity attestation is a conditional Verify task for non-corroborated
full text when no hard-deny identity finding applies. `attest-identity` is a
typed, hash-bound operator attestation, not an override of deterministic
validation; `keep-unverified` explicitly retains the uncertainty. It cannot
override identifier mismatch, suspected fabrication, retraction, identity
conflict, identity-context, identity-relation, or identity-probe hard denies.

Technical/mechanical and infrastructure failures are repaired and retried via
a new child where that route applies. Semantic outcomes require revising the
claim or source and running again; they are never user-overridden. Structural
contamination requires editing the input and an ordinary new run. A retracted
source must be replaced (and, where appropriate, the manuscript edited) before
a new run. If no semantic outcome exists, follow the recorded terminal cause.
An incomplete pair only supports a generic child command; it does not promise
that a particular task will be emitted.

### Assessment reasons and permitted remedy

These are the canonical report reason codes. A conditional route is offered
only when a fresh child actually emits the corresponding task.

| Reason code | Remedy and limit |
|---|---|
| `hard.claim.orphan_citation`, `technical.claim.ambiguous_citation` | Manual attribution may select a reference/claim or retain uncertainty; the automatic finding remains. |
| `hard.claim.uncontested_contradiction`, `hard.claim.all_off_topic`, `review.claim.off_topic`, `review.claim.related_only`, `review.claim.non_decidable`, `review.claim.partial_support`, `minor.claim.partial_support`, `technical.claim.contested_assurance` | Revise the claim/source and run again; never user-overridden. |
| `technical.claim.no_semantic_outcome` | Follow its recorded terminal cause. |
| `technical.claim.incomplete_pair_coverage` | Use a generic completed-report child; no specific task is promised. |
| `review.manuscript.low_reference_coverage` | Inspect the manuscript's citation coverage and the parser output. The finding does not decide whether references are genuinely uncited or markers were missed. |
| `hard.source.not_found`, `review.source.searched_not_found` | Conditional Fetch or identity-correction task; not-found retains uncertainty. |
| `hard.source.identifier_mismatch`, `hard.source.suspected_fabricated`, `hard.source.suspected_fabricated_tag`, `hard.source.reference_refuted`, `hard.source.high_fabrication_suspicion`, `review.source.title_flag_warn` | Conditional identity correction after Parse; never identity attestation for a hard finding. A refutation or high fabrication suspicion makes the HTML assessment red. |
| `review.source.elevated_bibliographic_suspicion` | Conditional identity correction. Independent searches missed the source, but no closed article inventory was established; the report keeps this below a fabrication diagnosis. |
| `review.source.author_list_discrepancy` | Inspect the resolved identity and provider metadata. An explicitly cited author is absent from a returned list whose completeness is not attested, so the work remains identified and the label is review-only. |
| `hard.source.retracted` | Replace the source and/or edit the manuscript, then run again. |

Before full text can influence a verdict, deterministic code checks that the text belongs to the cited reference. Depending on source type, evidence can include DOI, PMID, ISBN, canonical URL, title/author/year overlap, provider identity, and trusted source provenance.

Important consequences:

- a downloaded PDF with the wrong identity cannot produce accepted support;
- a manually mapped file records manual provenance and still passes the applicable identity gate;
- text found after a `not_found` existence result does not erase that earlier existence finding;
- suspiciously fabricated references do not become eligible for semantic verification without admitted source text;
- identity-inadmissible full text terminates as uncertainty/non-crediting evidence rather than being ignored silently.

## Jury1 contract

Jury1 receives only the bound claim, its allowed manuscript context, and the selected source context. The staged contract determines one of six semantic outcomes:

| Outcome | Meaning | Evidence spans required? |
|---|---|---|
| `supports` | The source establishes the complete role assigned to it. | Yes |
| `partial` | The source establishes a material part but leaves another material part unsupported or overstated. | Yes |
| `contradicts` | The source contains an incompatible proposition. | Yes |
| `related` | The source is directly related to the cited topic/object but contributes no material support to the claim. | No |
| `off_topic` | The source has no direct scholarly connection to the assigned claim role. | No |
| `non_decidable` | The contract cannot decide because of attribution, material limits, lack of consensus, verification unavailability, or retrieval limits. | No |

Negative findings are valid findings. They do not trigger a retry merely because they are negative.

### Probability-only providers and abstention

A registered backend may declare a versioned confidence policy and a
provider-specific response decoder. The common Verify controller does not know
the provider name or wire format. A structurally valid answer below the frozen
threshold is persisted as the provider's observed answer and then becomes a
non-crediting `provider_uncertain` abstention; it is not rewritten as a protocol
error, a semantic “no”, or a reason to retry.

For Jury2, the confidence floor gates permission to issue a verdict rather than
the validity of the binary Choice response. A below-floor answer records no
Jury2 yes/no vote and requeues the same claim/source pair to Jury1 for another
candidate. This is a new candidate cycle, not a retry of the Jury2 request. If
every bounded candidate cycle reaches the same condition, the pair terminates
`jury2_provider_uncertain` without a semantic outcome. Such abstentions never
participate in the rejected-candidate majority tally.

TypeSafe Jev uses policy `typesafe-citation-confidence-v1` with a default floor
of `0.8`. This is a conservative auto-action floor taken from TypeSafe's
citation-check guidance while Callimachus lacks a labeled calibration set; it
is not a claimed probability that the citation is correct. For Choice answers,
Callimachus compares Jev's reported choice confidence with the floor. For Noul
evidence selection, each span's certainty is `max(p, 1-p)`: ambiguous spans are
excluded individually, while sufficiently certain affirmative spans remain
eligible. If none remain because the available span answers are ambiguous, the
pair terminates uncertain. The actual threshold and policy ID are frozen into
the run so an override remains auditable; override `0.8` only from a documented
calibration against representative labeled citations.

Jev remains available as Jury1, including an explicitly configured confidence
floor, but it is not currently recommended for that role. In the controlled
Attention canary at `0.8`, 57 of 58 pairs ended
`jury1_provider_uncertain`; the correction successfully avoided protocol
errors and retries, but did not establish useful Jury1 coverage. Until a
representative labeled calibration supports a different policy, prefer Jev as
a Jury2-only lane behind a separate Jury1 provider. This is an operational
recommendation, not a runtime prohibition or an invitation to lower the floor
merely to force a verdict.

## Deterministic grounding

For `supports`, `partial`, and `contradicts`, Jury1 must select one or more source spans. The grounding layer then verifies every passage against the exact locally stored source and records:

- raw offsets;
- stable span ID;
- source hash;
- match mode and score;
- candidate and request fingerprints.

If any required passage is absent, overlaps illegally, comes from the wrong source basis, or violates the evidence-cardinality contract, the candidate is rejected. Grounding proves that the cited passage exists in the admitted text. It does not, by itself, prove that the passage semantically justifies the outcome; that is why Jury2 exists.

## Jury2 policy

Jury2 receives the Jury1-derived asserted relation, passage subject, and grounded selected passages. It does not receive the complete source text or serialize the full claim. It answers whether those passages alone justify the Jury1 outcome.

| Level | Behavior when Jury2 rejects or cannot complete |
|---|---|
| `off` | No second judge is run; Jury1 plus deterministic grounding is the terminal basis. |
| `low` | A rejected Jury2 result may still accept the Jury1 outcome, but the result is flagged and reliability is downgraded. |
| `medium` | Re-verifies within the configured budget. On exhaustion, accepts only when the observed Jury1 attempts provide the policy-required majority; otherwise terminates uncertain. |
| `high` | Re-verifies within budget and never uses the majority fallback on exhaustion; the pair becomes uncertain. |

Jury2 level is a required configuration decision so a run cannot accidentally change enforcement policy between environments.

## Retries, candidates, and terminal state

Retries are bounded and occur for technical or guard failures such as:

- rate limit, invalid credentials, unavailable lane, timeout, transport, or provider failure;
- malformed JSON or contract mismatch;
- missing/invalid evidence basis;
- grounding or provenance failure;
- cross-stage disagreement that the policy marks retryable.

The runtime does not retry to seek a more favorable semantic answer. Each physical dispatch and each mechanically rejected candidate remains persisted.

Controller terminal classifications are internal. Persisted pair terminal statuses are `accepted`, `uncertain`, `exhausted`, and `cancelled`; the repository also supports the technical terminals `deadline_exceeded` and `infrastructure_error`. A provider abstention terminates as `uncertain` with `jury1_provider_uncertain` or `jury2_provider_uncertain`, no semantic outcome, and no retry. Assurance is preserved in terminal/projection fields and in report rollup, which may surface uncertainty or exhaustion separately from the six semantic outcomes.

## Deterministic and model-owned responsibilities

| Deterministic code owns | The LLM may propose |
|---|---|
| Phase order and completion | A semantic classification for one bound pair |
| Source identity and admissibility | Which admitted source spans justify that classification |
| Context preparation and hashing | A contract-constrained explanation |
| Prompt/schema validation | Jury2's passage-fit decision |
| Exact evidence grounding | Nothing outside the current request payload |
| Retry caps and state transitions | No direct terminal or report write |
| Persistence, provenance, report, journal, and seal | No override of deterministic validation |

Provider/model variability can change proposals. It cannot bypass schema, source, grounding, provenance, identity, or state-machine checks.

## Table-only citations

By default, a citation found only in a table row counts toward reference coverage but is not turned into a semantic claim. A row label or comparison-grid entry often lacks an assertion that can be judged reliably.

Use `--verify-table-citations` only when the table row genuinely expresses a claim and that broader behavior is desired. The choice is frozen in the run.

## What a passing gate does and does not prove

A passing completion gate proves that required artifacts, source-bearing pair terminals, report projections, and authenticity checks are internally complete under the selected policy.

It does not prove:

- that every citation supports its claim;
- that an LLM's accepted semantic judgment matches a human gold label;
- that missing or unreadable sources secretly contain support;
- that abstract or web evidence is equivalent to full text;
- that a non-strict run has at least one positive/crediting result.

Use `python run.py verify --strict-crediting` when the last property is required, and use a labeled gold set when measuring semantic accuracy.

---

Previous: [Tasks and recovery](05-tasks-and-recovery.md) · Next: [Artifacts and provenance](07-artifacts-and-provenance.md).
