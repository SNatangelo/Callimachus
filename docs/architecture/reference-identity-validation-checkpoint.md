# Reference identity validation checkpoint

Date: 2026-10-08. Status: scoped repairs validated; the wider identity audit remains in progress.

## Validation update — 2026-10-13

The reviewed modular-reader and coordinate corrections were qualified on
production manifest `dbbfdcb4c392fb764237286b1fb355ed8fa736522c6b3292cd205f7fc15cf2a6`.
Parse: 1,532 passed, 11 skipped, 191 subtests. Resolve: 1,560 passed,
82 subtests. Report/Verify/packaging: 407 passed, 55 subtests. Focused
coordinate tests: 122 passed; the persistence selection: 160 passed.
An offline comparison of the fixed 18 PDFs retained 1,427 literal references,
with zero losses/extras or ordinary-field deltas. The neutral range-versus-opaque
identifier correction received an Astra review without paper-specific rules.

Four local native packages were built and checked from that manifest. After
operator testing, one additional presentation defect was reproduced: both Guided
Fetch browser buttons opened the first full-text retrieval API link. Navigation
now filters recognizable API hosts and XML/JSON MIME/query representations,
keeps normal article/PDF links, and falls back to a valid resolved DOI.
The GUI regression failed before and passed after; the current presentation
suite passed 34 tests and combined relevant suites passed 124 tests, one skip.
Only the presentation model changes production behavior after that package pin;
identity validation, acquisition evidence and verdict semantics are unchanged.
No new packages or independently scored corpus results are claimed for this
last correction. Older entries below remain source-specific historical results.

## Validation update — 2026-10-10

Source087's independent Nature Physics capture and strict self-replay qualify:
108 references, equal stable semantic projections, and no LLM or Verify
execution. Sealed predictions against74 frozen positive labels give71 correct,
three abstentions and zero different DOI identities:95.95% recall and100%
admission precision. Both cohort criteria pass. The same71 previously correct
identities are retained;34 references without confirmed labels remain unscored.
The publication-unit gate repair passed407 tests and15 subtests.

The repaired Source088 completes a fresh Physics capture and strict replay:
108 references and2,058/2,058 events. Predictions sealed before label access
again give71/74 correct,3 abstentions and0 different identities:95.95% recall
and100% admission precision. All108 raw keys and typed identity, status and
validated-identifier projections equal Source087. The source-own score receipt
is f0f448b102d8a271a921ba12fbea768a66dd351298c0e546a638bdfc5e626263;
full pair seal cc68ada8731adcd35259ba22c35eaacf7197733f6c7a34addfddee13d29c5bdb.
This confirms the current Physics cohort, without inheriting an older score.

The next Nature document exposed an extraction failure before resolution:
both native capture and replay produced zero references against81 expected.
Its bibliography has no heading and starts with a mixture of journal articles,
an institutional report and a preprint. The old headless detector required
journal coordinates and initialized-author syntax on every one of the first
five entries, rejecting the complete list.

The focused parser repair preserves the unique consecutive1–5 prefix and
independent journal-coordinate evidence. Other entries must have a closed
authored title, explicit DOI or preprint identifier, and terminal publication
year. Identifier-only lists remain unsplit; arbitrary URLs, incomplete entries,
body-text loans and ambiguous starts are rejected. No publication-specific
name, title, identifier or abbreviation rules were added.
Synthetic regressions fail before the repair and pass afterward. The relevant
parser and historical corpus selection passes87 tests and49 subtests, with
no skips. The actual document yields81 references numbered1–81; Nature
Physics retains all108 identical raw reference keys.

Earth Source088 now has a qualified capture and corrected strict replay:81
references,1,516/1,516 events and zero external replay calls. Typed predictions
were sealed before scoring29 confirmed positives:27 correct,one different DOI
and one abstention. Recall93.10% and admission precision96.43% fail both targets.
The52 unscored references are not negatives. Pair seal:
92256f38d550f9a1748eb4173956828c5e57884db09e0d0c10e653d7b68fe02d;
score receipt d57bd316f00fcb30f9f0e2647031c156c64c3299d8efb752d233ff569b680661.

The wrong identity starts with an incorrect parsed year: a date in the title
overrides the terminal publication year when the closed journal tuple contains
an article number. Extending the existing source-aligned year recovery to that
locator restores the existing posted-content withholding gate. The abstention
starts with uppercase-only venue recognition: a short lowercase prefix before
multiple dotted abbreviation atoms contaminates the venue and truncates the
question/subtitle. A narrow generic shape predicate restores the full literal
title. No named venue aliases, corpus identifiers, admission thresholds or
resolver decision rules are added or changed.

The respective focused regressions reproduce4 failures and10 failures before
the changes; their completed suites pass45 and71 tests afterward. Root's
combined parser, coordinate, publication-unit and actual historical PDF
regression selection passes249 tests and40 subtests, zero failures or skips.
All382 runtime and17 selected test-file hashes remain stable during the run.
Log SHA f7cc53e722c871f438f7e3a89b3f7b29e7ac24118e0b4bb848229026c964e9c7.
The fresh Source089 Physics capture and strict replay complete108 references
and2,031/2,031 events. Source-own typed predictions against the same74 frozen
positive labels give71 correct,3 abstentions and0 different identities:
95.95% recall and100% admission precision. The discrepant ordinals remain
2,18,103, so all71 previously correct identities are retained. Pair seal:
2baf0bd6ae5a8e6b83ffc39291ed850a84dc6474cd363295d5e2f7bf509592f7;
score receipt c8e1564439f07b8a7ec86b7e4b19d5680899de0bad5217052388da9af2743d7d.
The34 unscored references are not negatives. Labels had been inspected in prior
development; this is a fresh regression evaluation, not an untouched holdout.
Source089 Earth completes81 references and1,420/1,420 events, but fails the
identity criteria:25/29 correct,1 different DOI and3 abstentions;86.21% recall
and96.15% admission precision. Two previously correct identities are lost.
Ref16 now admits a preprint;29 withholds the previous wrong identity without
recovering its correct article;48 resolves an arXiv record without a strong DOI;
67 withholds a previously correct dataset. Its parsed1950 year already existed
before these parser changes. Provider availability versus validation/selection
causes are under focused investigation; no further change is yet accepted.
Pair seal1aa5d17cda11db2002ccfd691bd856be5050175a9af083641f50f4dda636795e;
score9a8b0e910d2a004a0b9995a6d76f3eec6158c57f82b4193184eb5a34db81ef7d.
Combined retention preserves71/71 Physics and25/27 Earth previously correct
identities;96/98 retained fails the regression gate. Failed outcomes remain
preserved. The next independent document has24 aligned references and23
independently confirmed DOI labels frozen before Resolve; its preliminary
controls pass, but prediction is withheld until this development repair passes.

The next bounded correction proves a journal unit with a short scalar locator
only from an initialized author head, unique literal title and complete,
source-aligned closed publication tuple. It uses that proof to withhold an
incompatible preprint; positive identity certificates keep their definite-locator
requirements. Crossref filters this incompatibility before ranking, retaining
the rejected candidate in the audit trail. A declared DOI can use the existing
Handle existence check after explicit transient Crossref transport failure;
completed metadata conflicts remain unchanged, and an unavailable registry plus
a negative Handle response is not reported as two completed negative checks.
Integrated regression:548 tests and59 subtests pass, with runtime/test pins
unchanged. Fresh Source090 Physics capture and strict replay qualify all108
references. The same74 fixed positives give72correct,2abstentions and0different
identities:97.30% recall and100% admission precision. A source-own exact-identity
comparison retains all71previous correct identities and all108ordinal/raw hashes.
The additional recovery has no declared DOI: old and new captures contain the
same ordered provider records; rejecting the incompatible posted record before
ranking lets the already-available equal-score journal record be selected.
This is bounded development evidence, not a fresh untouched-holdout score.
Pairseal572c898a21840af9553682f8db00c91f4d88b5e3846085b3b617fe9eb34e40ed;
scoref6df8dcee59ca73d82799cba2c71bad17609e185a0a8e1a3e9a98259fe3d3b45.
Source090 Earth capture and strict replay qualify all81references and1,520
events. Its29fixed positives give29correct,0wrong and0abstentions:100%recall
and100%admitted exact-DOI precision. Full Source088-to-090 retention preserves
all98previous correct identities, with0newwrong. Earthpairseal:
54ac162c5729c4830b506c8720082ff06cff51b5dba04b192517baaad1f04e77;
score540a753ef6b09a1279c09b0ff4072fdc612b37b5facfda11e05d9106489d0551;
retentiondb030e157df4bb315d3d893583de9323a5717a7fa7e754fc38611b63376fe7e4.

Bounded sealed attribution distinguishes code from availability. Physics103
and Earth29 recover the already-available journal record through pre-ranking
unit filtering. Earth16 first withholds the unchanged native preprint using
the generic short-scalar journal tuple. Earth48 recovers after fresh Crossref
availability;67 uses the preexisting404-to-Handle existence path. Neither is
native evidence for the newly expanded transient-transport branch, which is
covered by22focused controls. No counterfactual provider replay was performed.

The first previously unused24-reference document now has qualified capture
and strict replay, equal typed identities, zero LLM/Verify and zero replay
network calls. Its original23positive/1unscored label freeze is unchanged:
20correct,3abstentions(refs7,19,20),0wrong;86.96%recall and100%admitted
exact-DOI precision. Recall fails the95%criterion. Predictions were sealed
before scoring; the independent label preparer had inspected expected targets
before Resolve. This document is development evidence after this first test,
not an untouched holdout for subsequent fixes. It primarily cites publications
and standards; it does not establish dataset/software-release version accuracy.
Pairseal057c0b8cf7057f1dba3a3baf8f578928d7dd982b7f9302c6ea4555d0227a7009;
typedseal809544f41b9ec2d36cc5c1d915da3fc73bfc4deae4bec43e44e701842f20721d;
scoref5298fff73df23fe7912e63b4e55a15c9d3e4ed779c7894aeefb474956e87c14.
The three abstentions are under focused root-cause investigation; no further
production change is yet accepted.

An exploratory broader selection also exposed two outdated Resolve-test
expectations. Both failures reproduce against unchanged frozen Source087,
so they are not parser regressions and remain outside this focused repair.

Historical ViLT replay now qualifies54 references after correcting an audit
permit-order deadlock. Baseline ViT replay qualifies58 references and all1,431
events after correcting audit HTTP-error reconstruction and supplying the PDF
reader that had confirmed identity in the native run. The PDF executable is an
audit-only artifact with a pinned per-process environment; no production
dependency or system configuration was added. Full pair audits preserve
transport/header provenance, typed identities, frozen inputs and zero external
replay calls. Fifteen historical papers have qualified native/replay pairs on both frozen
sources, covering684 references per source. ICAE138 adds86aligned references
after authenticated V12 URL-capsule audit and source-own Repository projections
on exact analysis copies. All original cases and runtimes remain unchanged.
Three papers/743references per source remain; the fixed120positive historical
labels remain unopened. ICAE138 full-pin seal:
95007c474994e37eb919fe747ddfe7f272b044996372b981e26af5feabf4206c. The complete18-paper comparison
and its identity score remain pending; this separate Source080 campaign does
not establish current Source090 historical accuracy.

Source080 ViT's original capture qualifies1,545 events. Its corrected HTTP-error
replay consumes all events but makes8 unmatched calls and remains unqualified.
A native RemoteDisconnected prompted an OSError retry; its incomplete recording
instead raised CassetteError in replay and changed the provider branch.
A new audit-only codec preserves the exact canonical safe exception type,
arguments and state; unsupported forms and tampered proofs remain rejected.
The3 round-trip regressions fail on the parent; all87 combined HTTP, URL, SSL
and codec controls pass after the correction. Old recordings without exception
arguments are preserved and are not guessed or converted. A fresh pinned
campaign passes offline setup, header controls and the real DPAPI canary;
the first new ViT capture reaches the30-minute worker limit with43/58
references completed and is excluded. The whole failed case is preserved.
A fresh capture uses an audit-only two-hour acquisition window; frozen runtime,
strict900-second replay and bibliographic quality gates are unchanged. It
completes58 references, with56 resolved and2 unverified; capture/replay qualify
all1,565 events. The full pinned seal verifies unchanged case files, runtime,
inputs, typed semantic projections and provenance, with zero external replay
calls. Full seal0184770a43bab7a040c917b954324d1332b29b634e614c32bc357b24f3960e78.
JEA also qualifies65 references on both sources, with810 baseline and1,091
Source080 events. Its full seal verifies all65 raw keys across four immutable
databases and preserves both closed cases. Seal:
242353e87f79907db1738fc9ac68891bc077e5fb5a73bd7154fd57e7f8a46a24.
ICAE native and replay have completed on baseline; its full audit exposed an
older comparator that rejects the already supported authenticated URL capsule.
A fresh audit wrapper passes eight tamper controls and ordinary delegation,
preserving the original case. Full qualification is pending and ICAE is not
counted. No historical positive-label scoring has occurred.

## Scope

This branch preserves the ongoing bibliography identity audit and repairs. The
changes cover deterministic source coordinate parsing, source/native metadata
comparison, discovered identifier admission, provider candidate selection, and
rechecking retained identity evidence. Focused synthetic controls accompany the
repairs. Production rules use source text and provider evidence, not corpus DOI
lists or exceptions keyed to particular papers.

Source spans, native titles and coordinate conflicts remain auditable. Ambiguous
or incomplete certificates must fail closed. The coordinate extractor is now v2;
the bootstrap database constraint and storage tests are updated together. Existing
v1 run databases are not migrated by this checkpoint.

## Acceptance criteria

At least 95% recall and 99% admission precision on each independently labelled
cohort, without losing previously correct identifications or introducing new wrong
identities. Uncertain references do not count as negatives or successful matches.
Corpus acquisition and strict replay are distinct validation steps.

## Last complete regression checkpoint

The locally frozen source023 checkpoint has the following independently labelled
results. This Git checkpoint contains later changes and must not inherit these
scores as proof of its current performance.

| Cohort | Correct / positive labels | Recall | Admission precision |
| --- | ---: | ---: | ---: |
| First | 947 / 992 | 95.46% | 99.47% |
| Second | 402 / 419 | 95.94% | 99.26% |
| Third | 402 / 430 | 93.49% | 99.26% |
| Modern | 78 / 87 | 89.66% | 100% |
| Fourth | 80 / 81 | 98.77% | 100% |

The subsequent source024 improved the Modern cohort to 83/87 (95.40% recall,
100% admission precision) but introduced regressions in other cohorts. It was
rejected globally. Recorded requests and complete response sequences were
retained; differing request multiplicities were explicitly inspected.

## Current changes and verification

A later candidate could replace a correct discovered identifier before its direct
validation. The service now validates and checks the existing source-aware identity
lock before candidate replacement, while preserving required provider searches.
A service-only replay recovered all 14 selected lost identities and preserved one
control that requires replacing a source-conflicting candidate. This is a targeted
result, not a complete integrated corpus qualification.

The generic article locator route requires a closed source tuple, one compatible
native candidate in a complete search response, and a matching direct identifier
lookup. Its private evidence is rechecked after serialization. Native primary and
short venue composition uses exact ordered token evidence from that provider
record; it does not introduce field-specific abbreviation dictionaries.

Initial checkpoint checks (commit `d9fad35`):

- Parser suite: 1,069 passed, 33 skipped, 137 subtests passed. Parser production
  source has not changed since this run.
- Resolve suite before the latest call-shape correction: 1,541 passed, 4 failed,
  87 subtests passed. The four failures came from passing an unnecessary optional
  keyword to the existing promotion call; this was corrected.
- Focused service regression check after that correction: 31 passed.
- Generic locator certificate and additional edge controls after correcting an
  inconsistent synthetic venue: 27 passed, 9 failed. The existing 18 certificate
  tests pass; the additional controls require diagnosis. No live network requests
  occurred in this check.
- Diff whitespace check passes. Graphify update could not run because its CLI is
  unavailable in the current environment.

The initial checkpoint had not yet repeated the full Resolve suite after the last
corrections. The integrated source has not yet completed the five-cohort corpus gate.
Full First/Second service-only replay also encountered 21 newly required response
captures, so those runs cannot qualify final scores.

### Subsequent verification

The remaining nine edge assertions used inappropriate fixtures: a leading-zero
scalar already takes the existing explicitly typed article-number route, and the
synthetic unrelated title was below the existing distinctive-token comparison
minimum. The tests now assert the existing typed certificate and exact lexical
coordinate equality, and use a sufficiently long unrelated title for the title
disagreement controls. Both certificate routes are rejected in the altered-scalar
negative controls. Production thresholds were not changed.

The combined certificate and edge controls pass: **36 passed**, with zero live or
blocked network requests. The complete pertinent Resolve suite also passes:
**1,563 passed and 87 subtests passed**. Source hashes are identical before and
after that run; 121 attempted network operations were blocked, with no live calls.

The source025 snapshot is frozen with aggregate source SHA-256
`ac8d458dbc2759f14348aebbd3d18a905d3e6df92ef90e3b4cc94b40705dbdff`.
Its five preparatory response pools retain all **12,796** prior records, with zero
omissions or cursor shifts. An independent ordered-record audit confirms only
physical body-file path remapping, targeting the same existing blobs; original
index hashes remain unchanged. Corpus acquisition and subsequent strict replay
are still pending qualification. The fifth independent cohort remains untouched.

### Source025 corpus findings and current repairs

Source025 is rejected globally. On unchanged independent positive labels, Third
has 396/430 correct identities, 31 abstentions and 3 wrong identities; Modern has
82/87 correct identities, 5 abstentions and no wrong identities. The Third run
loses ten previously correct identities and Modern loses one. The Modern strict
replay reproduces all 94 acquisition outcomes, with no missing or outside
transports and an unchanged response-pool index.

The generic certificate attempt returned before the existing ordinary title
search, including when a cited title was available. The current repair preserves
the failed certificate evidence and continues title matching for titled sources;
titleless failures remain closed. A separate strict snapshot mismatch came from
an absent subtitle versus an empty subtitle list. Only absent, null and empty-list
subtitle representations are now equivalent in the private material snapshot;
nonempty subtitles and raw captured bodies remain unchanged. The combined focused
controls pass: **52 passed**. These changes still require the complete corpus gate.

Two other titleless candidates pass the existing native coordinate/role checks,
but their broad queries return incomplete candidate collections. The current
recovery uses a source-spanned unique NLM entity, recognized exactly or through
ordered literal tokens from one registered canonical-title/alias pair. It queries
all registered ISSNs and retains complete collection, unique compatible identity
and direct verification requirements. It runs only for a successful, well-formed
query whose reported total exceeds the retrieved rows. Serialized evidence
rechecks the catalogue, source spans, native records and the retained direct proof.

The source026 snapshot is frozen at aggregate source SHA-256
`f76100a228f221049cdea5b4757877bb9b42816b1e42d49fabca090b3240d72d`.
The new scope suite passes **42 tests**, and six adjacent suites pass **130 tests**.
The complete pertinent suite passes **1,621 tests and 87 subtests**; all 356 Python
source hashes are unchanged during the run, with 121 blocked network attempts and
no live calls. Full source026 acquisition is complete:

| Cohort | Correct / independently confirmed | Abstentions | Wrong | Recall | Admission precision |
|---|---:|---:|---:|---:|---:|
| First | 955/992 | 32 | 5 | 96.2702% | 99.4792% |
| Next | 403/419 | 13 | 3 | 96.1814% | 99.2611% |
| Third | 408/430 | 19 | 3 | 94.8837% | 99.2701% |
| Modern | 83/87 | 4 | 0 | 95.4023% | 100% |
| Fourth | 81/81 | 0 | 0 | 100% | 100% |

Every acquisition has zero missing/nonreplayable responses and zero unexpected
transports. Against both source023 and source025, every cohort has zero lost
previously correct identities and zero new wrong identities. Source026 still
fails the global gate because Third is below 95% recall.

Modern strict replay reproduces all94 ordered captured outcome/DOI tuples, with
zero missing/outside transports and an unchanged pool index. The complete
descriptor comparator still reports three shared request keys consumed twice by
the candidate versus once by baseline. Actual saved response bytes, status,
final URL and request descriptors are identical within each duplicate pair;
physical body paths and multiplicities differ. Full vectors are retained.

The remaining diagnosed titleless failure has a complete negative scoped
numeric-text query, not scoped truncation. Three independent diagnostic requests
retain raw bodies, response headers, final URLs, timestamps and hashes. Both
locator-only and explicit-ISSN volume/locator text queries return0/0, while the
same registered ISSN, author and exactyear without numeric text returns a
complete61/61 collection with one source-compatible native certificate.

Source027 therefore changes only registered titleless query projection: retrieve
by author/exactyear/registered ISSN and validate volume/locator natively.
All registered ISSNs, source/catalogue provenance, complete collection, unique
compatible identity, direct material repeat and serialized relock remain
mandatory. No corpus-specific DOI, journal shortcut or threshold change is used.
Its regression was RED in both generic and coordinate cases before the fix;
afterward44 scope tests and60 related tests pass with no live calls.
Frozen source027 aggregateSHA is
`814c20b985d05c9cae301a11c322c8c15d2c8e21462a732c1336ab8064ce570b`.
The complete pertinent suite passes **1,623 tests and87 subtests** with121 blocked
network attempts and zero live calls. All356 Python source hashes remain stable.
Historical source027 qualification is complete on all five cohorts:

| Cohort | Correct / supported positives | Abstain | Wrong | Recall | Admission precision |
|---|---:|---:|---:|---:|---:|
| First | 955/992 | 32 | 5 | 96.2702% | 99.4792% |
| Next | 403/419 | 13 | 3 | 96.1814% | 99.2611% |
| Third | 409/430 | 18 | 3 | 95.1163% | 99.2718% |
| Modern | 83/87 | 4 | 0 | 95.4023% | 100% |
| Fourth | 81/81 | 0 | 0 | 100% | 100% |

All five full baseline/candidate strict pairs have zero missing/nonreplayable or
outside events. Every candidate reproduces its complete ordered acquisition
vector, including input, outcome and actual DOI; pool indexes stay unchanged.
No previously correct supported identity is lost and no new wrong identity is
introduced against source023, source025 or source026. Root rehashed all376 frozen
source files against the attestation before qualifying the historical gate.

Full response-vector comparators retain nonzero exits for additional repeated
consumption and physical body paths:35 First request keys,19 Next,18 Third,3
Modern and1 Fourth. These vectors are preserved. Distinct successful repeated
payloads change only indexing metadata and citation counts; identity-material
digests, explicit ISSNs and identity fields remain identical. Two empty429
events are followed by the original body. Affected cases were already correct;
none of these differences supplies a recovered identity. Successful response
headers remain unrecorded, so equality of response headers is unestablished.

The first-ever prediction on the preregistered fifth cohort started only after
this historical gate passed. The six previously unused Nature Communications
source articles contribute 240 unchanged DOI-free publisher literals. Independent
labels, source selection and inputs were frozen before prediction: 151 confirmed
identities and 89 uncertain references, with no historical source/cited DOI or
literal overlap.

The complete fifth acquisition and strict replay both return **150/151 correct,
zero abstentions and one wrong identity** among confirmed positives: **99.3377%
recall and admission precision**. All 240 ordered key/input/outcome/actual-DOI
tuples match exactly between acquisition and strict replay. There are zero
missing/nonreplayable or outside events; the completed 1,004-record pool index
remains unchanged during replay at SHA-256
`9c858651ec460cb248907193cd5c67fcf946a7b164d917e662cb1ecf17b233f1`.
All 376 frozen source files were rehashed again at closeout. The strict audit is
`.t/identity-validation-20261006/fifth-source027-strict-accepted-audit-001.json`,
SHA-256 `fb44f99f05f27f5262199b7cab4e609cfca0c9e9414418fdfe8b9996595a052c`.

The 89 uncertain fifth references remain unscored: 76 admitted and 13 abstained.
No accuracy claim applies to them. The confirmed error NC25F-0110 selects book
chapter DOI `10.1007/978-94-015-9795-1_27` instead of journal article DOI
`10.1023/a:1011852603162`. Its literal joins venue and volume as
`Environ. Biol. Fishes62`. Parser word-boundary patterns miss the attached venue
and volume, leaving no recognized venue, coordinates or publication kind. The
existing chapter-container guard requires those coordinates, so ordinary
title/author/year matching admits the first-ranked chapter. The journal article
is also present in the captured results; both records share title, authors, year
and pages. Metadata does not establish full-text equivalence. The frozen label
remains unchanged. No old-version single-case replay establishes whether this
error is a regression. The bounded read-only diagnosis is retained at
`.t/identity-validation-20261006/NEW/fifth-source027-case-0110-readonly-diagnosis-001.json`.

Response-vector audit limitation: successful-response headers were not captured.
Shared body hashes, status, final URL and request descriptors can be checked, but
the request-header fingerprint does not establish response-header equality.
Differing response multiplicities remain reported without truncating vectors.

## Measured closeout and remaining limitations

All six cohorts meet the agreed per-cohort numerical acceptance criterion:
recall at least 95% and admission precision at least 99%. The five historical
cohorts additionally lose zero prior correct supported identities and introduce
zero new wrong identities. Source implementation stops here; the independent
fifth validation completes the requested subsequent-corpus test.

This is threshold qualification, not perfect verification. Eleven historical
wrong identities, the one fifth wrong identity, and 128 historical plus 89 fifth
uncertain references remain explicit. Successful-response headers were not
captured in the benchmark pools, so response-header equality is unestablished.

A future separately scoped audit may reproduce the venue/volume boundary and
publication-unit ambiguity with independent synthetic controls. Any proposed
correction should first demonstrate the earliest incorrect parse/admission state,
preserve fail-closed validation, and repeat the full historical gate. It is
recorded as remaining work; no additional source change is included in this
accepted checkpoint.

Large acquisition payloads, raw logs, temporary runtimes and local frozen source
copies remain outside this Git checkpoint. They are preserved locally under the
repository's audit worktree; none are removed or overwritten by publication.


## Complete checkpoint audit — 2026-10-06

The complete audit of published commit `a01eae7` confirms all six empirical cohort
thresholds and reviews all 15 changed production files. It does **not** close
audit-readiness: two P1 validation gaps were reproduced offline. Production code,
tests, labels and original captures remain unchanged.

1. **Publisher-native PII proof after serialization.** Relock checks the current
   citation URI/PII, but response digests occur only in diagnostic text and their
   values/native observations are not revalidated. Starting from the existing
   full-service fixture, both zeroed response hashes and
   `identity_search.completion=incomplete` still lock. Changing the current source
   PII correctly rejects. The smallest next correction must preserve structured
   native observations bound to captured evidence, reject incomplete or corrupted
   proof, and add typed-store round-trip mutation controls.
2. **Published article versus preprint DOI.** The unit guard returns false for
   every non-arXiv DOI before checking further version evidence. Third355 has a
   proven journal tuple but admits a preprint DOI. Pure helper reproduction
   confirms the guard gap; the full selection trace was not rerun. The next
   correction needs publication-unit evidence bound to the selected identifier.
   A DOI prefix or `posted-content` type alone is insufficient; an actual journal
   DOI with an available preprint file must remain eligible.

The 12 exact-DOI discordances include one independently verified publisher alias:
Third235 has reciprocal Crossref `is-identical-to` relations and matching
electronic ISBN in both captured native bodies. Frozen scoring remains unchanged.
Other version/unit cases retain their evidence and uncertainty. Nine of 12 have
unknown parsed source kind; Fifth0110's attached venue/volume is a confirmed
source-extraction loss. A parser-only correction will not cover the known-article
preprint case.

The 67 supported positives without the target operative DOI comprise 53
non-admitted/unverified rows, 12 resolved-status rows without an admitted DOI,
and two accepted arXiv identifiers. Observed reason groups are 23 low-confidence
candidates, 19 failed coordinate/locator certificates, 12 withheld metadata,
related-work or unit-conflict candidates, eight search-needed rows, two absent
parsed titles, two arXiv outcomes and one OpenAlex outcome. These groups describe
recorded states, not 67 independently proved root causes.

All 217 uncertain labels remain unscored. Eligibility selection and an all-positive
benchmark do not establish false-admission performance on nonexistent/altered
references. The fifth sample's naive binomial interval for 150/151 is 96.34–99.88%
at 95% confidence; citation clustering and sampling bias are unadjusted. The
reported thresholds are observed cohort results, not a general precision
guarantee. Successful-response headers and full real-PDF pipeline measurement
remain outside the established evidence; raw benchmark captures/runners are local
and excluded from Git.

A coverage check found eight changed test modules absent from the last recorded
suite selections and nine current source hashes differing from the older cache
suite. The fresh targeted integration run includes those modules, the cache
checks, fetch handoff/resume, pipeline fixtures and reports: **232 passed and
27 subtests**, zero failures/skips, ten blocked network attempts and zero live
calls. All 356 current core hashes remain unchanged. The earlier Resolve
**1,623 passed + 87 subtests** is still exact for current source; Parse
**1,069 passed + 137 subtests, 33 skipped** matches all 38 tracked parser/matching
source hashes. Skips include unavailable PDFs and GUI dependencies.

The audit concludes with repair priorities rather than source changes:
persisted proof, identifier-bound publication unit, bounded source extraction
and explicit version/alias policy, then an independent negative/adversarial
cohort and real-PDF validation. No corpus-specific production exception was found
in the targeted source review.

The complete machine-readable audit and its evidence hashes are
`.t/global-audit-20261006/complete-audit-001.json`; component review and reproducer
are `implementation-001.md` and `implementation-001-repro.py` in that directory.


## 2026-10-07 — persisted native proof repair qualified

The audit's reason-only admission gap is corrected by a private, typed proof on the retained publisher attempt. It includes the actual direct Crossref and publisher XML observations, response-body digests, request/status/final-URL facts and exact source URI/PII/raw-entry digest. On replay/resume, admission recomputes the source binding, ordinary metadata profile, native DOI/PII/title/year/container and complete native query contract. Missing, malformed, altered, incomplete or differently sourced proof fails closed. Diagnostic reason text has no admission authority. The selected profile may be absent only when the complete retained proof reconstructs it; a contradictory selected profile remains rejected.

The private current bootstrap advances from schema109 to110 for the typed proof column. Previous runs remain preserved and are explicitly rejected rather than migrated. Public evidence/report schemas, verdicts and interfaces are unchanged. Source027's artifact/labels were not rewritten.

Focused RED/GREEN reproductions cover missing persisted proof and a changed raw citation. Final focused suite: **44 passed**; schema gate: **2 passed**; current integration gate: **232 passed +27 subtests**, zero failures/skips and ten blocked socket attempts, zero live network. The initial full Resolve033 process ended abruptly after493 completed tests with no final test/stability output; its infrastructure/process cause remains unidentified and it is not counted as a passing suite. A fresh identical selection with durable progress, isolated state and exclusive artifacts passed **1,641 tests +87 subtests**, zero failures/errors/skips,125 blocked socket attempts and zero live calls. All356 core Python hashes remained stable.

Source028 freezes382 manifest files/376 code files, aggregate SHA-256 `88167ad4fee6674ddd149972893062e9ab4558f7cdc497039dda7bb58c68f4a4`; only the four proof/storage/bootstrap production files differ from027. Every six-corpus full strict replay completes with immutable pool indexes, zero missing/nonreplayable or outside events, and zero previously correct identities lost or new wrong identities. Independently frozen positive scores are unchanged: First955/992, Next403/419, Third409/430, Modern83/87, Fourth81/81, Fifth150/151. Every cohort still meets recall>=95% and admitted-DOI precision>=99%. Reproducible local summaries and source attestation are under `.t/post-audit-20261007/`, including `six-corpus-source028-strict-001.json`; raw artifacts are retained locally rather than committed.

The next authorized focused repair is identifier-bound native preprint rejection for a citation of a published journal unit. Prefix-based rejection, posted-content alone and preprint file availability are insufficient evidence of a preprint identity. The attached venue/volume grammar repair remains a separate step. No source-specific exception or gold-label change has been introduced.

A bounded version-family audit found no source-text version discriminator in the F1000, OSF or eLife cases. F1000 native records establish version lineage with matching citation coordinates; eLife's selected native record declares `is-same-as` the expected version; OSF's selected native record lacks a relation and the expected base record is absent from that pool. These are evidence-qualified distinctions, not permission to revise frozen exact-DOI scoring. General version/equivalence policy remains backlog.


## 2026-10-07 — identifier-bound native preprint repair qualified

The selected DOI can no longer be admitted as a published journal unit when a resolved native EuropePMC record identifies that same DOI as a preprint. The guard binds both attempt identifiers to the selected DOI and rejects contradictory or incomplete bindings as preprint evidence. A DOI prefix, posted-content alone, or the availability of a preprint file does not establish the selected publication unit. The guard covers metadata preference, validated-identifier promotion, and finalization before and after enrichment, including the previous direct-validation shortcut. Public verdicts and evidence/report interfaces are unchanged.

The semantic finalization reproducer failed before the repair. Focused GREEN: 23 tests +11 subtests; adjacent proof/decision/adjudication checks: 121 passed. The full current Resolve selection passed **1,647 tests +95 subtests**, zero failures/errors/skips,125 blocked socket attempts and zero live calls, with all356 core Python hashes stable.

Source029 code aggregate is `421db22608397fe29bd2a14e0b25e66cc389e7192ee9e5ee936dd008969cdd7c`. Six full strict cohorts qualify with zero missing/outside requests, lost correct identities or new wrong identities. First955/992, Next403/419, Third409/430, Modern83/87, Fourth81/81 and Fifth150/151 remain correct. Third's wrong preprint admission becomes an abstention:409 correct/19 abstain/2 wrong, admission precision99.5134%. Overall frozen positive counts:2081 correct,11 wrong,68 abstain out of2160;217 other inputs remain uncertain and unscored. Every cohort meets recall>=95% and admitted-DOI precision>=99%.

First and Third required additional native fallback responses after withholding. New forks preserve every original ordered response descriptor; fresh full baseline/candidate pairs use the same filled pool and reproduce the original baseline outcome vectors. Native captures were generated by production requests without gold input; successful-response header fidelity remains unestablished. Earlier incomplete-environment and missing-response runs are preserved and excluded. A missing journal-authority environment variable caused baseline branch divergence; all qualifying repeats explicitly pin catalog SHA `f7466d0106e2ffa2f3367472336718c1baccbc9d9bc2da909f4693f9ee3ed0a4`. The helper's Unicode line-separator parsing failure was corrected using physical JSONL line iteration, without changing valid result files.

Qualification: `.t/post-audit-20261007/six-corpus-source029-strict-002.json`, SHA `340c2fd399c97a18d745707e5b9757af35d359a55848c61508015fec8e2e72b9`; root physical source/result/summary/pool verification: `source029-root-verification-001.json`. The attached venue/volume parser repair is now the next focused step; a correctly imported Source029 two-fixture probe failed2/2 before changes, with zero network attempts. No new-corpus prediction has occurred.

### Source030: attached venue/volume grammar qualified (2026-10-07)

The parser's ordinary word boundary left attached journal-volume forms without source coordinates. A narrow closed author/title + attached volume + page range + terminal year grammar now produces exact source spans and article classification. Broad coordinate boundaries and existing book/ISBN/URL/chapter safeguards remain in place. Production contains no title, DOI, author, or venue lookup added for this fixture.

Focused regression: 25 passed. Full adjacent selection: 142 passed, 2 skipped, zero blocked network calls. Parse025: 1,079 passed, 33 skipped, 137 subtests passed, zero blocked calls. Resolve036: 1,657 passed, 95 subtests passed, no failures/errors/skips; 125 blocked socket attempts and zero live calls. Relevant source hashes stayed unchanged through these runs. The initial adjacent temp-directory permission failure and supplemental reduced run were preserved and excluded from the final qualification.

Frozen Source030 tree: `260dbf215e98fa69b811fa7d2a0b266532e9646a3c1e1437214d9c2819a7bb45` (376 code files). All six cohorts pass their strict transport and score gates. First/Next/Third/Modern/Fourth are unchanged from qualified029; Fifth recovers NC25F-0110 and is 151/151 correct. Totals: 2,082 correct, 10 different admitted DOIs, 68 abstentions among 2,160 independently positive labels; 217 uncertain references remain unscored. No lost correct or new wrong outcome against either027 or029.

The initial030 Fifth replay was incomplete because three enrichment requests for the newly selected journal DOI had not been captured. Its score was provisional. An exclusive fork preserved all 1,004 inherited descriptors/body hashes; production replay-fill of only the affected source literal captured three native HTTP200 responses. Full027,029,030 Fifth replays used this same new pool with zero missing/outside requests. Both baseline vectors remained exactly equal to their previously qualified outputs. Final six-corpus report002 and root verification001 qualify the result; failed report001 remains preserved.

Remaining-state audit is `.t/post-audit-20261007/remaining-source030-audit-001.json` (SHA256 `78b14a5527c9f9f6ea0f68f933d3bcda0fc59fb671e0441b9cfd981be406fa4e`). It records case keys, source evidence, and native attempt reasons without changing frozen labels. Generic version/equivalence policy and incomplete independent coordinate certification remain investigation items; no further production rule was added after the six-cohort criteria passed.

The Seventh corpus's first record run has begun only after all six030 gates and root checks passed. Its 234 source literals/112 positive labels/122 uncertain labels were frozen before prediction. Negative controls and the real PDF pipeline have not run yet.

### Source033: final qualification after new-corpus and PDF tests (2026-10-07)

Final code aggregate: 16332986f09d40f6da88919729201a1cceb14a348c3af5cb478c01670cdc7e3b (376 code files;382 frozen nonsecret manifest files). Rules use source grammar, geometry and identifier-bound metadata, with no corpus title/DOI/author/venue exception.

New failures and corrections:

* A unique native PDF block physically above References was emitted inside reference6. Relocation requires exact unique text and nonoverlapping preheading geometry, preserves characters and leaves unproven cases unchanged. Standalone copyright ownership tails no longer enter reference29. Parser RED:3 failures/11 passes; final adjacent GREEN:363 passes,1 skip,42 subtests.
* Seven controlled first-author alterations were admitted cleanly despite a different surname in retained direct Crossref validation. An already locked DOI now retains an author refutation only from the same successfully validated identifier and a closed literal author head agreeing with the cited surname. Existing name equivalence remains in force. RED:2 failures/9 passes; complete adjudication module:64 passes.
* The real PDF initially achieved9/11, then8/11 after parsing repair. Remaining attempts explicitly reported HTTP429/HostCooldownExceeded. Crossref was configured at20 requests/s with overlapping requests. The [current public-pool contract](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/) specifies5/s and one outstanding request. Existing family rate is now conservatively5/s; a process-local REST GET permit spans response consumption and retries. Ordinary worker settings and validation semantics remain unchanged. RED:3 failures/3 passes; HTTP/config/limiter GREEN:77 passes/23 subtests. An existing Windows path assertion was made separator-independent; production backoff-store bytes are unchanged.

The Seventh corpus was frozen before predictions:234 untouched DOI-free Nature Communications2026 literals,112 independently positive labels,122 uncertain labels. Native Source030 record/strict replay and final Source033 replay agree on every input/target/outcome/DOI vector. First V4 record remains preserved but unqualified because a generic built-in timeout could not replay. The test-only V5 cassette preserves the exact built-in exception type across hosts; unknown/custom subclasses still fail closed. Five focused instrumentation cases pass; production was not changed for that repair.

| Cohort | Correct | Abstain | Different DOI | Recall | Admission precision |
| --- | ---: | ---: | ---: | ---: | ---: |
| First |955/992|32|5|96.2702%|99.4792%|
| Next |403/419|13|3|96.1814%|99.2611%|
| Third |409/430|19|2|95.1163%|99.5134%|
| Modern |83/87|4|0|95.4023%|100%|
| Fourth |81/81|0|0|100%|100%|
| Fifth |151/151|0|0|100%|100%|
| Seventh |110/112|1|1|98.2143%|99.0991%|

Every cohort meets recall>=95% and admitted-DOI precision>=99%; final repeats lose no previously correct DOI or add a wrong DOI. Totals:2192 correct/11 different admitted DOIs/69 abstentions among2272 positive labels;339 uncertain rows unscored. These are observed sample results, not a general accuracy or specificity guarantee.

The frozen contradiction cohort has8 unchanged controls and24 variants changing only title, first surname or both. Final:8/8 exact controls;14 altered inputs withheld,10 admitted with visible bibliographic refutations,0 clean unflagged admissions. Seven author errors become visible while DOI vectors remain unchanged. Labels prove field inconsistency relative to the controls, not global nonexistence or automatic wrong-DOI ground truth for all variants.

Final public CLI run of unchanged official NEJMoa2603406.pdf, with ordinary workers:29 references;11/11 independently positive DOI labels correct,0 wrong/abstained positives,18 uncertain rows unscored. All29 literals equal frozen native text after numbering/line-wrap/typographic normalization; boundaries1/6/15/29 pass. Zero Crossref429/cooldown resolve attempts. Runtime108.02s; requested pause at Fetch/CLI10, wrapper0. Verify/Report were not executed; no full-content verification or complete full-text retrieval claim is made. Source bytes, Git head, PDF bytes,65 native files and journal catalog stayed unchanged.

Final tests: Resolve0391745 passed +118 subtests,0fail/error/skip,124blocked socket attempts/0live; Parse0271098 passed +155 subtests,30skipped,0blocked/0live, explicitly using the main-root Paper test directory; integration003232 passed +27 subtests,0fail/skip,10blocked/0live. Current parser/matching hashes still equal Parse027; full source hashes independently rechecked for Resolve/integration and all cohorts.

Evidence under .t/post-audit-20261007: source033-final-qualification-001.json, source033-root-verification-001.json, seventh-source033-root-verification-001.json, negative-source033-qualification-001.json and real-pdf-preparation-001/pipeline-runs/source033-native-003/attestation/. Failed/provisional runs and original cassettes remain preserved; frozen labels were not changed.

Changed production files since published checkpoint: core/infra/db/resolve_attempt_storage.py and schema.py; core/parse/citation_coordinates.py, extractors/pdf.py, parsing_common.py; core/resolve/bibliographic_adjudication.py, decision.py, http.py, provider_config.py, providers.json, service.py. Eight test modules and this checkpoint accompany them. Private bootstrap110 rejects old runs without migration; public report/verdict/interfaces stay stable.

Criteria are met; implementation stops. Remaining version/alias and unsupported publication-unit cases are retained for a separately scoped global audit. Seventh NC26S-0196 still selects a posted-content DOI with unknown parsed source kind despite scalar coordinates; source closure and identifier-bound unit evidence need investigation before choosing a rule. NC26S-0182 remains uncorroborated; year selection is a hypothesis, not a proved root cause. Affiliation body markers and invisible-format URL cleanup are nonessential backlog. Graphify update was attempted but the command is unavailable; generated graph files were not edited.
# 2026-10-08 — residual evidence and new-corpus repairs

The latest retained Crossref response proof binds preprint subtype evidence to
the exact selected DOI. Published journal citations withhold that preprint
identity through direct validation and after persisted attempts are reloaded.
Bootstrap schema111 and storage controls retain the proof without a compatibility
migration. Initialized contributor heads and closed historical publication-year
positions receive deterministic parsing corrections; numeric title, page,
volume and identifier content does not by itself establish an old publication
year.

Source038 passed1,764 Resolve tests plus122 subtests with no failures, errors or
skips. Its eight-cohort paired audit against Source035 has2,364 independently
qualified positive labels:2,289 correct,10 different admitted identifiers and65
abstentions, with398 additional uncertain inputs unscored. Both versions have
the same complete outcome vectors, with no previous correct loss or new wrong
gain. The separate32-input contradiction cohort has8 valid controls,14 altered
inputs withheld,10 admitted with visible conflicts and no clean unflagged
contradiction admission. Local qualification receipts are under
`.t/residual-audit-20261007/`; these are observed sample results.

The new-corpus candidate adds generic Unicode dash recognition in author runs,
blank-line-tolerant author/date lookahead preserving original line indexes, and
one shared pagination-tail publication-year recognizer for reference fields and
author-year linking. The latter only replaces free selected dates inside an
earlier closed year range; explicit author dates and existing publication tuples
keep precedence. Rules contain no corpus title, author or DOI exceptions.

The same eight frozen Cioffi development PDFs now yield333 entries against333
TEI rows. The source-confirmed NUR_44 book/article merge is separated: the book
has its own2014 date and no DOI; the following article retains2005 and its DOI.
Other seven counts are unchanged. Equal counts do not prove extraction recall or
identity correctness;36 definite title omissions and a partial article title
remain. LinkedBooks reran8,549 unchanged development spans, improving strict
year agreement from4,636 to4,647 among7,888 eligible spans, with no previously
exact title/year agreement lost. Title agreement remains202/8,437. These are
field agreements with supplied annotation boundaries, not identity scores.

Final candidate core SHA-256:
`de0c1f9debca6e30e798812518e17b9cfe4a2e7ce9bfb4a37267748f46464b5a`.
Broader Parse/author-year validation:764 passed,149 subtests passed,28 skipped,
zero failures/errors. Nineteen skips require unavailable PySide6 and nine require
optional local PDF samples. The aggregate historical Parse fingerprint tests
pass on all18 verified PDFs. Source and pinned NLM hashes remain stable. Final
local acceptance is
`.t/new-corpora-20261008/continuation-001/accepted-review-003.json`.

Cioffi11-document and LinkedBooks75-document holdouts remain untouched. The
remaining37 Cioffi development PDFs, identity-gold adjudication and NASA,
preprint/version and dataset/software execution remain open. Historical18 native
identity requalification remains at11 qualified members/1,070 references, with
357 remaining. Protected redirect-proof collection is prepared and mock-tested;
the user authorised the previously discussed limited Windows DPAPI profile-key
exception on2026-10-08. Native collection has not yet resumed at this checkpoint.
No Fetch/Verify/report quality claim is made.


### Closed container title and publication-year repair — 2026-10-08

The next scoped parser repair is accepted as Source040. A closed initialized-author citation with a unique structural in: marker, a strong container cue and terminal labelled pagination now retains its literal work title. The same recognizer supplies the source-spanned publication year to reference fields and author-year keys. It reuses the existing historical-year domain, preserves explicit author-date precedence and declines malformed years, ambiguous markers and unmatched quotes. It contains no corpus-specific title, author, DOI or venue substitutions.

Validation passed 792 Parse tests and 149 subtests, with 28 preexisting optional skips, plus 1,764 Resolve tests and 122 subtests without failures, errors or skips. Three historical-PDF checks initially lacked the corpus environment path; their fresh completion run passed, including all 18 pinned PDF inputs. Offline Resolve blocked the same 124 preexisting optional transport attempts; actual egress was zero.

On unchanged development inputs, Cioffi still produces 333 references across eight successful PDFs. Against the same 245 frozen alignment pairs, exact titles rise from 129 to 153: 14 previously missing titles and 10 incomplete titles improve. One year changes from 2014 to the annotated publication year 2015. No previously exact title/year is lost, no DOI changes, and 22 of the original 36 definite missing titles remain. LinkedBooks is unchanged across 8,549 annotated development spans. These are conditional field comparisons, not independent identity accuracy. Eleven Cioffi and 75 LinkedBooks holdout documents remain unpredicted.

The Source040 runtime pins 382 files and changes only core/parse/parsing_common.py from the published candidate. Source040 aggregate: 0b22e4ad22c0fb67df5d61028fe21ed3baa42a7ddc4608c7e19b0e64539ab169. Root acceptance: .t/new-corpora-20261008/continuation-002/accepted-review-040-001.json, SHA256 1dc034555d9be36100f17eb92d83e19d8174e1d25965444b2ee0a10f9392ee89.

Historical native evidence is now complete for all 18 papers and 1,427 references, including one protected redirect capsule. Full comparison remains pending: its first execution failed before workers because the audit helper used a serialized path as a Path object. That isolated harness correction is being tested in a fresh namespace; the captured evidence and previous attempts are preserved. The remaining 37 Cioffi development PDFs are byte-verified and prepared for a separate Source039/040 comparison. NASA, preprint/version and dataset/software qualification, independent new-corpus identity labels and Fetch/Verify/report quality remain separate work.


### Extended development regression and provider-attempt correction — 2026-10-08

The remaining 37 Cioffi development documents exposed one Source040 title regression: a closed author-date book citation's uppercase geographic code was treated as the structural in: marker. This supersedes the original eight-document acceptance as evidence for the broader development set; those earlier results and artifacts remain preserved. Source041 restores only the generic closed title-sentence + separate capitalized place/code + terminal publisher structure. It uses no place, state, title, author or DOI exception and retains malformed/ambiguous container rejection.

Historical qualification009 reached baseline001 replay-fill 18/18, then Source035 failed while persisting the AIP paper's attempts: unknown resolve attempt field matched_venue. No Source039 full fill/strict comparison or identity score completed. The current Source039 boundary also passes provider results unchanged. Source042 copies only the persisted attempt and removes a type/value-exact duplicate of the canonical metadata_match.matched_venue. It preserves the resolver result and complete nested profile, and does not expand storage's allowed fields. Missing, mismatched or unknown shapes remain rejected.

Source042 broader validation passes 800 Parse tests + 149 subtests (28 preexisting optional skips), and 1,770 Resolve tests + 122 subtests, with zero failures/errors. Resolve retains all 1,764 prior selected nodes and adds six boundary regression cases. Offline transport guards block 124 preexisting socket attempts with zero actual egress. All 376 core pins and the journal catalog remain unchanged. Frozen runtime: 382 files; aggregate c63b264955f872b06d410638f8dac4430ffc22c46a2bc184b9bd08405736e1ab.

The first native-only AIP smoke preserved all original evidence and completed both databases, but encountered 44 unrecorded request occurrences for both versions (354 replay hits each, zero outside transport). It therefore qualifies neither version and did not exercise the reported storage failure. A fresh controlled native extension is required before the real before/after assertion or historical18 score. The 45-document Cioffi rerun and LinkedBooks stability comparison remain pending at this checkpoint. No Fetch/Verify/report claim is made.


The completed Source041 parser comparison is now accepted on all 45 frozen Cioffi development documents: 1,949 predicted entries versus 2,053 TEI rows. On the same 1,630 fixed Source039 alignment pairs, exact work titles rise from 1,081 to 1,105 (+24; 14 missing and ten previously nonmatching), and exact years rise from 1,573 to 1,574 among 1,616 rows with a single primary gold year. No previous exact title/year is lost; no DOI or independent gold assignment changes. The remaining 37-document subset retains all 952 exact titles and 1,332 exact years, including the publisher-place regression. Raw-entry Counters, per-document counts, all supplied gold rows and the four duplicate occurrences are unchanged. A successful parser execution may emit zero references; execution success does not establish extraction recall.

LinkedBooks retains all 8,549 development keys and all title/year outputs. Root independently reverified the 90 Cioffi PDF/XML files and 35 source/input pins. Accepted parser receipt: .t/new-corpora-20261008/continuation-002/accepted-parser-041-root-review-001.json; comparison SHA256 afc3d79b1df0a57049ec99ce68c85fd07d5e897124238f39249e0a92c0b265e9. Source042 uses identical parser bytes. The same 11 Cioffi and 75 LinkedBooks holdout documents remain untouched. These results qualify the scoped parser changes; they are conditional field agreements, not new-corpus identity accuracy. The provider-attempt correction passes its six focused regression cases and the broader Resolver suite; native AIP extension and historical identity qualification remain open.

### Generic-locator certificate storage contract — 2026-10-08

The controlled AIP extension with Source042 passes the top-level venue projection but stops at the fourth reference: its provider metadata profile contains an existing generic-locator identity certificate outside storage's closed field set. A fresh strict diagnostic reproduces this exact failure with 54 replay hits, zero misses and zero outside transport. The original response pool remains unchanged. No identity score completed.

Source043 adds dedicated typed persistence for that certificate and its precisely bound ordered-venue companion. Validation checks the closed source, selected native-record, venue and ordinary or registered search structures, canonical hashes, completeness and query types. Unselected native search records retain their closed JSON projection without having to satisfy the selected article's semantic requirements. Native optional nulls remain intact. The venue companion is required for an ordered-venue certificate and rejected on its own, with a literal-venue certificate or with different witness values. Bootstrap schema112 creates the current proof columns in fresh runs.

The regression uses the real provider producer with stubbed transport, the canonical attempt projection and RunRepository. Both ordinary and registered search paths survive database readback and pass the existing identity relock. Missing fields, unknown fields, forged hashes, malformed spans, unbound or missing companions, and float/bool query years are rejected. Root's offline focused validation passes 88 tests, with zero blocked attempts and zero actual egress. The broader Resolve suite passes 1,771 tests plus 122 subtests in 15 shards, with zero failures, errors or skips. Its 1,770 previous test nodes remain in the same order; the one new test exercises the complete producer/storage/relock contract. The same 124 preexisting transport attempts are blocked and actual egress is zero. Root receipt: .t/residual-audit-20261007/historical18-source035-001/resolve-source043-root-review-001.json, SHA256 d3915d77ed236c902c3d1588d12fcaae45200e6aefb505ab147108efa332f7cb. The parser bytes remain identical to Source042. The native Source042/043 AIP comparison is now accepted: Source042 stops with the expected certificate-storage error after two persisted results; Source043 completes all 28 references and reads back two certificates. Both strict phases have zero misses and zero outside transport, using the same frozen 433-entry/316-body native union. The original capture, seed, index and response bodies remain unchanged. The original fill process exit code was not persisted after a harness receipt-mode error and remains null; the recovered evidence is attested independently and fresh strict exits are recorded. Root receipt: .t/residual-audit-20261007/historical18-source035-001/aip-source043-native-root-review-004.json, SHA256 af0d195a1e4807c852972420a118d62e6454d2cc8e198977d64678ca55c06096. This validates the storage regression, not identity accuracy.

Source043 freezes 382 runtime files and changes only core/infra/db/resolve_attempt_storage.py and core/infra/db/schema.py from Source042. Core JSON-map aggregate: 4c8083d64533501c74fd12d4b1301d47c32945807d759f6dfc55786eb733c8c0; snapshot manifest SHA256: 24aa83af7504367b912833895b777740931e3b3dbc4020fa65c9647743135198. Native historical18 qualification remains open. The other registered-coordinate/source-scope certificate contracts and standalone venue-composition witness remain separate unresolved boundaries.

The independently frozen seven-reference LiftoffTools development sample now also has its original complete 14-page version2 PDF, with the declared PMC checksum verified and page6 visually reviewed. Source042 Parse-only emits eight entries: two genuine citation pairs are merged, journal containers replace the first two work titles, and the peer-review appendix is included as bibliography with unrelated report DOIs. These are retained extraction failures; no software identity accuracy is scored. The whole PDF is preserved, no identifier labels were injected, and the dormant JATS projection has not been used to diagnose production parsing. Its literal author-token projection limitations are recorded. Cioffi, LinkedBooks, NASA and GRIST holdout documents remain unpredicted.


### Structured peer-review appendix boundary — 2026-10-08

Source044 prevents an isolated review-appendix heading from extending the bibliography only when a nearby completed journal tuple and ordered review-status, version and dated-report markers independently support it. Any numbered or author-year reference resuming after the heading suppresses the cut, including a reference between metadata markers. The guard uses the existing year domain; no cited title, author, DOI or venue exception is added.

The focused regression was red before the change and passes43tests with one unchanged optional skip. Root's broader Parse suite passes812tests and149subtests with28unchanged optional skips, zero failures/errors or network attempts. All382runtime pins are stable. All45Cioffi development PDFs complete, and their1949prediction rows and2053gold rows are byte-identical to Source041; the fixed1630aligned pairs retain1105exact titles and1574exact years. All8549LinkedBooks development keys and compared fields remain unchanged. Root reverified35source/provenance pins and90PDF/XML inputs. Acceptance receipt: .t/new-corpora-20261008/continuation-002/accepted-parser-044-root-review-001.json, SHA2569574e5e5229e6073973ed796da88ec4fb93597050e2583725f5aae7e07194438.

The complete original software PDF now excludes reviewer material and preserves all seven literal reference heads before segmentation, but still produces six incorrectly segmented entries. A separate diagnosis shows the text splitter treating venue/year text as author starts and preferring it by fragment count. A three-entry synthetic control also fails despite all true colon-author heads being recognized, so increasing the recognized-start count alone is insufficient. Segmentation and title/year interpretation remain open; no new identity-accuracy claim or holdout prediction is made. Historical18 controlled Source043 qualification is running independently against its frozen runtime. Graphify update remains unavailable because its CLI is absent; generated graph files were not edited manually.


### Closed colon-author citation spine — 2026-10-08

Source045 fixes a separate segmentation error: the old chooser can prefer a larger fragment count after cutting inside titles or at venue/year text. A repeated initialized-author colon spine now wins only when every span is independently closed by a publication tuple or a typed software repository/year tail. Incomplete spans, identifier-contained years or tuples, ambiguous tails, non-repeated heads and a non-bibliographic prelude retain the established route. No title, author, DOI, venue or repository substitution is added.

Root's focused validation passes 97 tests and 41 subtests, with one preexisting optional skip. The broader Parse suite passes 822 tests and 149 subtests, with the same 28 optional skips, zero failures/errors and zero network attempts or egress. All 382 runtime pins remain stable. All 45 Cioffi development documents complete; their 1,949 prediction rows and 2,053 gold rows are byte-identical to Source044. The same 1,630 aligned pairs retain 1,105 exact titles and 1,574 exact years. All 8,549 LinkedBooks development keys and seven compared direct fields remain unchanged. Root reverified 35 source/provenance pins and 90 PDF/XML inputs. Acceptance: .t/new-corpora-20261008/continuation-002/accepted-parser-045-root-review-001.json, SHA256 0b6ab37b03017704e37255258c9072ad236386d1e8f3e96d4bbfa3e1ee14b9c0. The first comparison artifact had an incorrect LinkedBooks section label; it is preserved, and comparison-045-002 corrects only that label and output namespace.

The complete original software PDF now produces its seven separate literal citations, preserving order and the software version and excluding reviewer material. All seven years agree with the independently frozen labels. Work-title extraction remains a separate failure: venues, link labels or the software repository still occupy the title field. No Resolve/identity-accuracy claim is made. Cioffi and LinkedBooks holdouts remain unpredicted.

Historical18 Source043 qualification010 is stopped: baseline001 completed all 18 papers, Source043 completed 15 and failed on the 16th, s41467_Nature, while persisting metadata_match. A fresh strict replay reproduces the exact unsupported registered_coordinate_certificate key with 11 replay hits, zero misses and zero outside transport; the original response pool and index are unchanged. Its producer emits a closed six-field NLM proof, which existing identity relocking already recomputes. Typed persistence for this separate contract is the next correction. No full historical comparison or score completed; 120 labelled identities and 1,307 unscored references remain distinct. Graphify update remains unavailable because the CLI is absent.


### Registered-coordinate certificate persistence — 2026-10-08

Source046 fixes the separately reproduced Nature storage failure. The existing Crossref producer emits a six-field registered-coordinate certificate, and the existing identity relock recomputes it, but typed attempt storage rejected its metadata field. The current bootstrap schema113 now persists it in a dedicated JSON column. Storage requires the exact rule/registry and closed field set, a lowercase SHA256, nonempty registry version/record ID, and canonical, checksummed, sorted unique ISSNs. Malformed proofs remain rejected; structurally valid altered proofs still fail the existing semantic relock. No resolver admission, verdict or public-interface change is added, and fresh runs require no compatibility migration.

The regression exercises the real producer, canonical attempt projection, RunRepository readback and existing identity relock. Root focused validation passes42tests with zero failures/errors/skips/network attempts. Broader Resolve passes1,772tests plus122subtests in15shards, with zero failures/errors/skips; the1,771old nodes retain their relative order. The same124preexisting optional transport attempts are blocked, with zero actual egress. All382runtime pins remain stable. Root receipt: .t/residual-audit-20261007/historical18-source035-001/resolve-source046-root-review-001.json, SHA2569051a98b0686ea044e57ec366a27810c65bd3a5bed359be65981daaae4a780b1.

The native Nature comparison is accepted on its original45-reference PDF. A fresh copy of the attested805-entry pool acquires68missing native responses, yielding an873-entry/696-body union. Strict Source043 reproduces the expected storage failure (exit1,11replay hits); strict Source046 completes45references (exit0,777replay hits), reads back one registered certificate and passes its existing identity relock. Both strict runs have zero misses/outside transport and preserve the union index/bodies, original seed and failed010attempt. Actual child exits are recorded before harness checks. Root receipt: .t/residual-audit-20261007/historical18-source035-001/nature-source046-native-root-review-001.json, SHA2561393b7601b6103cf63d12600566779e2ed89840b3c0b887f6ca9a23f78bcf8c7.

Frozen Source046 manifest SHA2564a8cf3eaba641988dc60cb4149457b5b05fcf98d36ff689bfed43271c5077c3e; core aggregate d5f61b8d649212b7da05f7cfc0dac2a3f8fd68708cb8dd7ec3f54115a8ade328. Its exact production delta from Source045 is typed storage and schema. This proves the storage correction, not identity accuracy. Full historical18 qualification remains to be restarted in a fresh namespace; the software title-boundary failure remains a separate step. Graphify update was attempted but its CLI is absent; generated graph files were not edited manually.


### Closed colon-author work-title boundary — 2026-10-08

Source047 addresses a separate field error after the seven software-PDF citations were correctly segmented: sentence-based title extraction selected journal containers, link labels or repository text as work titles. A closed initialized-author colon citation now uses its exact source-bounded container/volume/locator spans, or its typed software repository/year tail, to retain the literal title. Identifier-contained coordinates, forged spans, multiple title sentences, missing boundaries and unmatched edge quotation marks remain unset. Entries outside this structural family keep their existing route. No author, title, journal, DOI or repository exception, resolver admission or schema change is added.

The focused regression was red against Source045 and root validation now passes 23 cases. The broader Parse suite passes 845 tests plus 149 subtests, with the same 28 optional skips, zero failures/errors and zero blocked or actual network attempts. All 382 runtime files remain pinned. All 45 Cioffi development PDFs complete; their 1,949 prediction rows and 2,053 gold rows are byte-identical to Source045 in every emitted field. The same 1,630 fixed pairs retain 1,105 exact titles and 1,574 exact years, with zero losses. All 8,549 LinkedBooks development keys and seven direct fields remain unchanged. Root independently verifies 100 source/provenance pins and 90 PDF/XML inputs. The dependency-missing dev-parse-012 attempt is preserved and excluded; fresh013 uses the established PDF runtime. Authoritative comparison SHA256 d08d650dd98923724a1cb6853c985f92aa6fdadfef7337e3bfb7d94f3758a41c; root acceptance .t/new-corpora-20261008/continuation-002/accepted-parser-047-root-review-001.json, SHA256 7613a641d5e68346fb3e2718f495ab04e3acab4727a9a67b862e92793acd9afc.

On the original 14-page software PDF, all seven raw entries, years, identifiers, coordinates and source kinds remain unchanged. Six literal work-title windows are recovered, including the software's explicit version. One article remains safely titleless because its numeric-parenthesis journal container has no extracted coordinates. Software classification still selects book_like because terminal publisher link text contributes publisher evidence. These are distinct unresolved structural failures; field gains are not identity accuracy. Both native workers completed with durably recorded exit0, but the smoke helper failed saving its final progress after an internal Path leaked through a shared dictionary. The failed helper/output are preserved and a fresh strict-only recovery is required before operational or identity acceptance.

Frozen Source047 manifest SHA256 09b0973c507b29fbb4453e6125d0885f911f2ab2600b9373ab82ac61522e21c2; core aggregate 6c624848e0b96321c2655ec596bd182a50a8e366c40085096ced96c75aabfda4. Its sole production delta from Source046 is core/parse/parsing_common.py, with the focused regression in tests/test_parse_closed_colon_author_title.py. Historical18 qualification and new-corpus identity scoring remain pending; 11 Cioffi and 75 LinkedBooks holdout documents, NASA and GRIST remain unpredicted. Graphify update was attempted but its CLI is absent; generated graph files were not manually edited.


Source047 native recovery is now root accepted. The original 89-entry/83-body pool was attested and copied into a fresh namespace; the new strict replay completes all seven raw references with 89 hits, zero misses and zero outside transport. The original seed and working index/bodies remain unchanged. Root verifies all three event ledgers (267 events), actual process/log pins, runtime, original PDF, Parse units and database integrity. The alias regression passes three offline tests. Root receipt SHA256 68be5f186833ce92c807b4b1814c4afd44ce5b456a8125304b3e060a7522c8e4. Only after qualification, the seven independently frozen positive DOI labels are applied: five correct strong identities, two abstentions (references3 and6), zero different identities. Identity score receipt SHA256 0cbd21ef7d328a157c62f6903d584376e5e30aa85c927ef07f0df85b729d4450. This is one development document, not general accuracy or negative-control qualification.


### Numeric-parenthesis container boundary — 2026-10-08

Source048 addresses the remaining titleless article in the original software PDF. The coordinate extractor rejected a parenthesized numeric journal qualifier as an extra publication year. A closed initialized-author colon citation may now retain that literal qualifier only when a distinct completed title, one outside-identifier publication tuple and exact container/volume/locator boundaries agree. Bare, multiple, unbalanced or ambiguous years remain rejected. No named journal, title, author or DOI exception, resolver admission, schema or public-interface change is added.

The focused regression passes 13 cases; the related focused suite passes 123. Root broader Parse validation passes 858 tests plus 149 subtests, with the same 28 optional skips, zero failures/errors and zero blocked or actual network attempts. All 382 runtime pins remain stable. All 45 Cioffi development PDFs complete: 1,949 prediction rows and 2,053 gold rows are byte-identical to Source047, including all seven emitted fields. The same 1,630 aligned pairs retain 1,105 exact titles and 1,574 exact years, with zero losses. All 8,549 LinkedBooks development rows remain byte-identical. Root independently verifies 100 source/provenance pins and 90 PDF/XML inputs.

On the full original 14-page software PDF, only reference3's title changes, from unset to its literal work title. Its numeric-parenthesis container, volume and page range are source-bounded; the other six coordinate vectors and all seven raw entries, years, identifiers and source kinds remain unchanged. Seven literal work-title windows are now recovered. This is a field correction, not a new identity score: Source047's qualified score remains five correct identities and two abstentions. Terminal publisher-link text still misclassifies the software as book_like; that separate regression test remains red and is excluded from this commit. Holdouts remain unpredicted.

Frozen Source048 manifest SHA256 34b37c66575b35d5c8079d40232335ed8600caa2985be18822bda3c283cfb14d; core aggregate 484dab2e895220e5dbab0783414e756d444ce74efce30a6d92a31a84c5086992. Its sole production delta from Source047 is core/parse/citation_coordinates.py. Broader root receipt SHA256 5c114b4ca788403b70a9feeb43ef3c7a991c2b8db4cfb28a0a04663dbfd9ad43; software field receipt aa6859a8dfc5c4ac753850e7fbabd3ca6bc77d329d7ec5d685faeb4007731442; LinkedBooks receipt f8cfa882292fdee4624a954795ed90801142e59c0adba162be06958d715e3380. Graphify update was attempted but its CLI is unavailable; generated graph files were not edited manually.

Authoritative Source048 corpus comparison: .t/new-corpora-20261008/continuation-002/corpus-comparison-048-002.json, SHA256 5c0337818482d86535336fa0316b8b5fe6f62d6cc456e64a6df8a1fd8c9d81d4. Report001 is preserved; report002 corrects only three stale cross-version result-key labels. Root acceptance: accepted-parser-048-root-review-001.json, SHA256 6bd398593742bdc3f295d1369e1f04b1c3eda2cb89c9766409fc92214cefed35.


### Closed citation UI labels and publisher evidence — 2026-10-08

Source049 fixes publisher evidence extracted from terminal linked-page UI. Only a complete citation accepted by the existing initialized-author colon closure may exclude the recognized terminal Publisher Full Text / PubMed Abstract label sequence from the publisher-name heuristic. Raw text, literal title/version, identifiers, coordinates and every other classification input remain unchanged. Genuine earlier publisher evidence and incomplete, identifier-contained, multi-tuple or extended-tail controls keep their established behavior. No named software, repository, title, author or journal exception, public source kind, schema, dependency or resolver-admission change is added.

The focused regression changes from three failures/five passes to eight passes. Related compatibility passes 69 tests plus 32 subtests, also with network blocked. Root broader Parse passes 866 tests plus 149 subtests with the same 28 optional skips, zero failures/errors and zero blocked or actual network attempts; all 382 runtime pins remain stable. Root receipt SHA256 edf8ac94ebcc7b8d164ba03a8a06c217a79de63e422251af19d29d7180a44b8b. On the full original software PDF, all seven raw entries, titles, years, identifiers and coordinate vectors are unchanged from Source048. Only publisher evidence and the software's book-to-unknown type/confidence change. Run-local reference/manuscript IDs are explicitly excluded from the field comparison, which pairs by exact ref_number and literal raw_entry.

The native comparison starts from the immutable accepted 89-entry/83-body Source047 pool. Current controlled fill appends 19 native responses and reuses 68, giving a 108-entry/101-body frozen union. Strict Source047 and Source049 complete with 89 and 87 hits, zero misses and zero outside transport. Root attests all six ledgers (530 events), actual process exits/log pins, 764 frozen runtime files and 382 current files. Seed, PDF, Parse units and frozen index/bodies remain unchanged. Qualification receipt SHA256 81a827c60de999695f74440b304097c9964455b8c95f7a2c49907fa1534a47a1. Only after qualification, seven independently frozen positive DOI labels give Source047 five correct/two abstained and Source049 six correct/one abstained, zero different identities; the newly resolved reference is the article with the numeric-parenthesis container. Score SHA256 93ce4f13fe9fd13e042770203362ff8dd49b00f541eb29768011668e8a1aef65. This is one development document, not global or negative-control accuracy.

Frozen Source049 manifest SHA256 5a908b1187445333105861868a9406d05fa5801d532e356bcd76e8103b6bf2ec; core aggregate 462e84eb5e2025c80aec6ede7b0b5dece74ea533e78a916dc5e4abcda01921b9. The sole production delta from Source048 is eight lines in core/parse/parsing_common.py. All 45 Cioffi development documents and 8,549 LinkedBooks fixed spans complete. Cioffi's 1,949 prediction rows and 2,053 gold rows are byte-identical to Source048; all seven emitted fields, raw-key counters, duplicate multisets and independent assignments remain unchanged. The fixed 1,630 pairs retain 1,105 exact titles and 1,574 exact years, with zero losses. LinkedBooks remains byte-identical in all seven compared fields. Root reverified 47 source/provenance pins, 90 PDF/XML inputs and 382 current/frozen runtime files. Comparison SHA256 0d97b2d63a05dda95dc6237b5cbd83dc82bac0e8c7043d0deea56a76a8e8f43e; root acceptance 8ff379783773ec7efe5715b2566872c0f00b180fc91b94cca7aa8633457df50b. Holdouts remain unpredicted. The software remains unverified: DataCite is registered/configured/eligible but has no event or attempt, following an OpenAlex HostCooldownExceeded result; separate first-author extraction reads title words for single-initial colon heads. These observations are recorded for bounded reproduction, with no additional production correction yet. Historical18 continuation is being prepared against its attested existing pools. Graphify update was attempted but its CLI is unavailable; generated graph files were not edited manually.


### Closed colon citation first author — 2026-10-08

Source050 fixes first-author extraction for a single undotted initial followed by a colon. The existing surname/initial branch now closes only if the original whole raw citation passes the existing closed-colon-entry validator. No new title, author, venue, software or repository exception, grammar, threshold, schema or public-interface change is added. Incomplete, prose-contained and multi-tuple controls retain prior behavior.

The focused regression reproduced three title-word author keys before the change; afterward 35 focused tests and 71 related tests pass offline. Root's broader Resolve suite passes 1,778 tests plus 122 subtests in 15 shards, with zero failures, errors or skips and zero actual egress. The previous 1,772 node IDs retain their relative order; six generic controls are added. The same 124 optional transport attempts are blocked with the same affected-test set. All 382 frozen runtime files and 376 current core Python/JSON pins are stable. The first attempt stopped on a Windows pytest temporary-folder ACL error and is preserved; the fresh retry passes unchanged tests with workspace-local temporary files.

Frozen Source050 manifest SHA256 d98e45531f46d7056bc3b0700bf8751fa8a3dc1c5de80cbf8a4e3f8f467b8afc; aggregate da4b1f04a81ff117d54a695f8c7ff63a19f32bf592cf57cfd43fb50dbdce12df. The sole production delta from Source049 is core/resolve/matching.py. Root acceptance: .t/residual-audit-20261007/historical18-source035-001/resolve-source050-root-review-001.json, SHA256 39d1e2eb06499c09f87847d7b321e5606c9bc3174730dbcfaa0d64f83e368063. On Source049's unchanged seven literal PDF entries, author keys change only for references1 and6, from title tokens to Daily and Shumate; the other five remain identical. Observation SHA256 a695603dd7fd723ee673354bd7016f8f33236419b920510817834fa87b562600. This is an author-field observation, not a new identity score. Parse code is unchanged.

Source049's four snapshot/Git byte differences are explicitly attested as CRLF-to-LF only; all 382 snapshot files remain exact-hash pinned, 377 of 381 tracked Git runtime blobs match exactly, four match after the recorded newline conversion, and .env is snapshot-only. Attestation SHA256 3b927715c55854e252103fc4e98d7fff41d921fe1f67472c44f4c00052260c34. Historical18 has not run on the latest candidate. The separate retryable-primary fallback regression is reproduced (one failure, five controls pass) and remains outside this commit pending its focused fix. Graphify update was attempted but its CLI is unavailable; generated graph files were not edited manually. Holdouts remain unpredicted.

### Retryable fallback and literal DataCite queries — 2026-10-08

Sources051–053 address two reproduced protocol failures. A primary unresolved response with the exact boolean retryable=True now reaches configured independent fallbacks. An unverified alternate cannot replace that retryable unresolved state: a later resolved alternate can win, otherwise the primary remains unresolved. Permanent failures, non-boolean values, resolved primaries, local-accelerator exclusions and the existing rate-limit route retain their controls. Two older Crossref test doubles now accept the already-existing keyword-only ref argument; the production interface is unchanged.

The native software citation reached DataCite under Source051, but its three title searches returned HTTP400 because literal colons and parentheses were parsed as query syntax. Source053 escapes reserved query operators and symbols within the title field value before the existing URL encoding. Free-term retrieval, the 200-character limit, Unicode, apostrophes, version dots, candidate ranking and identity validation remain unchanged. No named title, author, DOI or repository exception is introduced. Angle-bracket handling and the documentation discrepancy about dots remain unconfirmed boundaries, without an added rewrite.

Source051 initially failed the unchanged incomplete-check status regression and was rejected for publication. Source052 restores unresolved status generically; the focused provider/risk selection passes24tests. DataCite literal-query and resolver-registry controls pass31tests. The final Source053 broader Resolve suite passes1,811tests plus122subtests across16shards, with zero failures/errors/skips and zero actual network egress. All1,798prior Source051 node IDs retain their relative order; three fallback controls and ten query cases are added. The same124optional transport attempts are blocked with the same affected-test set. All382frozen runtime and376current core pins are stable. Root acceptance: .t/residual-audit-20261007/historical18-source035-001/resolve-source053-root-review-001.json, SHA256 b41d9f45a389a1ee2e7801c552a9498a54823662eebed3b99f86083ed6497ab9.

Frozen Source053 manifest SHA256294908108394ac2954187911531d4b47f5176efb78cea4386ae19b151a641a01; aggregate00b5c87587bc94624f5d24ce392461724770731efe1b7d0d42ea875e90365673. A fresh parse of the complete14-page software PDF preserves all seven reference fields from Source049, excluding run-local IDs. The accepted Source051 native pool is reusable solely as transport evidence despite that implementation's rejected regression. The current native comparison has not yet completed: its first launcher stopped before any phase on an obsolete audit-test filename and is preserved; a fresh version fixes the harness path. No new identity score, historical18 result or holdout accuracy is claimed. Graphify update was attempted but the CLI is unavailable.

### DataCite slash-query protocol correction — 2026-10-08

The real Source053 request still returned HTTP400. Twelve searches with invented titles isolated the single-backslash slash form as the lexical failure; escaped colons and parentheses and raw or escaped periods returned 200. Source054 makes one production-line change: slash runs become word boundaries in the search query before existing escaping. Both adjacent terms and literal version dots remain, while the original reference and title used for candidate validation are untouched. The existing field prefix, free-term search, candidate ranking, identity thresholds and public interfaces remain unchanged.

The focused DataCite/query-registry selection passes 34 tests. Root broader Resolve validation passes 1,814 tests plus 122 subtests in 16 shards with zero failures/errors/skips and zero actual egress. The same 124 optional calls and affected-test set remain blocked. The prior node order is preserved after explicitly mapping the changed expected-query parameter ID for the same RNA-seq input; three slash-boundary controls are added. All 382 runtime and 376 current core pins are stable. Acceptance receipt .t/residual-audit-20261007/historical18-source035-001/resolve-source054-root-review-001.json has SHA256 0e59461c29a790d1781f7496f72c4da640d3354eb77356b583941d58b185f62f. Frozen manifest 476609988f21bc45e096ace0596223bf9d6b61478de824ac9959121370518533, aggregate af9ee862f66f8f99cfa679587deeb416c20521d8e3858c5470a447411e8218b1; the sole production delta is datacite.py.

Native fill and strict Source047/054 replay complete with actual exits 0. Three new responses plus 105 hits extend the immutable 134-entry seed to 137 entries/117 bodies; all 12 ledgers/1,142 events are attested, both 764 frozen runtime files are verified, and strict runs have 89/108 hits with zero misses or outside transport. Root transport receipt 1d05788f34ce63f86ed5dac984840e75693a5f9d4db69d5a2485c5c8839479f0. The current DataCite requests return HTTP200 with zero records, so syntax success is not an identity-resolution claim. Only after qualification, seven independently frozen positive labels give Source047 five correct/two abstained and Source054 six correct/one abstained, zero different identities. Diagnostic score 2125c3c79bdbf1e24c65fa7ea6b9bb101de82eb5e5a4379e50e764563391fac1. The software retrieval residual remains under separate title/version-field investigation; no admission rule is relaxed. This is one development document, not global or holdout accuracy.

Historical18 preparation remains unexecuted. A corrected fresh preflight preserves a distinct, hash-bound seed-progress file instead of overwriting it with the final receipt; five guards pass. It reports 18 papers/1,427 references, without running workers or scoring. All failed preparations remain preserved. Historical identity labels cover 120 positives, leaving 1,307 references unscored. Graphify update was attempted but its CLI remains unavailable.


### DataCite explicit software version discovery — 2026-10-08

Source055 separates a terminal dotted v/Version release from the work name only when the raw citation explicitly marks software. The query combines title and version; candidates must be native Software records with an exact cited release. Existing title thresholds then compare the work name, with that same name as the short-title overlap context. Input fields and returned native titles are unchanged. No title, author, DOI or repository exception, schema or public-interface change is added.

Synthetic regressions reproduce the failure before correction; 48 focused software/query/registry tests pass. The broader suite passes 1,828 tests plus122subtests in16shards, with0failures/errors/skips and0actualegress. All1,814previousnodes retain their order;14software controls are added. The same124optional requests across20tests are blocked. Root acceptance f2bacd2d3a45d9baf3f03c392161f8a986bb4e037fdfd4c1fdb041f0ef9b03b2; the initial Windows temporary-folder ACL failure is preserved and the fresh unchanged elevated run succeeds. Manifest ecd44129d14565bd6b69db20a37845e6ca7e92c903ee7c8b92dca916db123a8f, core aggregate18811096343868b0b4bbfbdacc33715e1483b50ca7fc1f4951087866efea5ed2;382runtime/376corepins remain stable. Full14-pagePDFParse retains all7references and146non-IDfieldvalues, rootreceipt3c594c2fb8909bc1afd3957ba19e930fc201fc48ac0c7852d34e3f42b372d08a.

Native fill and strict047/055 comparisons complete with actualexits0:5newcalls88fillhits,139entries121bodies12ledgers1112events,89/93strict hits with0miss/outside. Roottransport370bd55d2d4d44b9ad547bfeda191aae620875fa22a3239b171fdfd23cd0b396. DataCite now returns the exact typed/versioned release with titleoverlap1.0, but downstream DOI admission still withholds strong identity after Crossref404 and a DOI redirect without metadata. Rootobservation5698b5bb7fd5840b6e9e127b28ee554fdb5ca053ba04df676a1ac14a85d13caa. Postqualified diagnostic score remains six correct/one abstained/zero different on one seven-positive development PDF; provider statusresolved is not a strong-identity claim. Score d710e45eba55a83198962d0773e730e0a52acfc26645ab195f95795584137cba.

The next bounded investigation is native registration-agency metadata validation and candidate admission. Historical18 latest execution and all held-out predictions remain pending; only120historical references have identity labels and1,307remain unscored. Graphify update was attempted, but its CLI is unavailable; generated files were untouched.


### Native DataCite DOI admission — 2026-10-08

A correctly discovered software release remained a weak candidate after Crossref returned 404 and the DOI handle supplied no metadata. Source056 validates the discovered DOI against DataCite's individual DOI endpoint. Admission requires one native record whose two DOI fields match the requested DOI, then rechecks the existing title, author, relation and citation-coordinate guards. Explicit software citations additionally require native Software type and the exact cited release. No title, author, DOI or repository exception is added, and title thresholds remain unchanged.

The exact request URL, HTTP status, response body and SHA-256 are retained as private typed in-run proof. Both initial promotion and central relock derive compatibility from that body. Detailed native Preprint type or an explicit IsPreprintOf DOI relation prevents admission as the cited published journal article. Invented controls reproduced unsafe first admission and relock before correction. Bootstrap schema114 contains the private proof fields; fresh runs are generated without migrations or compatibility adapters. Public result, identifier, report and verdict shapes are unchanged.

The focused selection passes 244 tests. Three existing attempt-list tests were updated to mock the new native authority call explicitly; all three pass with zero blocked calls. The fresh broader suite passes 1,861 tests plus 122 subtests across 16 shards, with zero failures, errors or skips. All 1,828 previous nodes preserve their relative order and 33 approved controls are added. The offline guard blocks 127 optional socket attempts across 21 tests, with zero egress; the increase from 124/20 is three blocked attempts in one existing different-DOI handle-probe test. Root broader review aaafbf69391c22cc5128c53b2535bfe6847a083b3aa6760b94767159c69491cc; guard review 71fb0bf0d0fd827c22434c12303fa2fd6a6dafdea92ff5876260c340157c8d7d. The first broader run's three stale assertion failures remain preserved.

The original full software PDF still produces seven references and all 146 non-ID field values unchanged. Native fill and strict047/056 replays complete with actual exits 0: nine new calls plus 88 fill hits, 143 union entries, 124 bodies, 12 ledgers and1, 120 events. Strict runs have 89/97 hits, zero misses and zero outside transport. Root transport review 1e89d8d6812ff7167c50e2ed697750c357523818a1ab1e8894d286a5bd5d1d12. A separate composite review binds the literal-derived name/version query, search-selected DOI, direct native request/body, recorded events, same-reference typed attempt and successful relock: 9eaa859fcf9904be4a01733e50f8890cabd5da323d2d72b076ef47aacee2b32b.

Only after native qualification, each frozen source's own read-only database reader scores seven independently frozen positive labels. Source047 has five correct identities and two abstentions; Source056 has seven correct identities, zero different identities and zero abstentions. Root verifies all eight gold evidence files and unchanged database hashes; score review b7bc054ad7d94927c24cb971166da2e0060608cdb099fb3fa0d7ab72870d4e15. This is one development document, not global accuracy or held-out performance.

Frozen manifest be47cc846abdca06daab5f9f950fcca7fe825f13d764a45e31f91e8c197cf5b9; core aggregate 227a544e4e8e51f3889ad213abd9ac440f88194298c18b266b3e9c3a2809d0d2. All 382 runtime and 376 core files remain stable. The only production changes are decision.py, datacite.py, resolve_attempt_storage.py and schema.py. Latest historical18 execution remains pending a fresh publication-bound preparation; score only 120 labelled positives and keep 1,307 unscored references separate. All document/family holdouts remain unpredicted. Graphify update was attempted, but its CLI is unavailable.


### Reject sequential prose as citation notes — 2026-10-08

Source057 adds positive citation-evidence checks to the text-only footnote detector. A sequential run of numbered prose, including years and counts, is insufficient. The gate reuses the existing 40% apparatus threshold and legal citation signals, plus explicit identifiers, confidently classified bibliographic records and an author/journal/volume/year shape. It preserves explanatory notes inside a genuine citation apparatus. No document, author, title, DOI, page or institution exception is added; the font-based detector, public interfaces and schema remain unchanged.

Synthetic negative controls failed before correction. Root focused validation passes 51 tests and 27 subtests; three existing real-PDF tests remain skipped because their worktree-relative sample paths are absent. Broader validation passes 869 tests and 149 subtests, with the same 28 optional skips as Source049, zero failures/errors and zero network attempts or egress. All 382 frozen runtime files are verified before and after. The sole production delta is core/parse/footnotes.py; tests/test_parse_footnote_refs.py gains three regressions.

On the original 303-page NASA handbook, full-PDF Parse changes from 22 spurious body-note references to zero references, with actual exit 0 and zero network calls. The 94 genuine bibliography occurrences in nine chapter/appendix lists remain omitted. This accepts only the false-promotion correction, not overall NASA extraction success or identity accuracy. NASA is observed development material for subsequent corrections. Multiple closed bibliography ranges and chapter-local numeric citation mapping require a separate change; concatenating restarted numbers would lose entries and risk wrong associations.

Frozen manifest24fd3340763238fadb0ab0b5dcb10d6e45827702053b82e407214e637df20dc9, aggregate b2ccb291b1c489e86c6cb5b5c9736ead3c9589e778cc5977d322bf99ffd56217, parent6ce37ab7c34afa625862b57712387d7e652bf3b1. Root review .t/new-corpora-20261008/continuation-002/source057-root-review-001.json. The independent frozen056 historical campaign is still active; no new 120-label historical score is claimed.


### Chapter-local bibliographies and numeric bindings — 2026-10-08

Source060 extracts multiple explicitly scoped Chapter/Appendix reference lists independently and assigns unique global reference numbers. The printed local labels remain in raw_entry; title/year/identifier analysis uses a private label-free view and coordinate certificates still refer to the complete stored text. Numeric body markers bind only within an established source scope. Missing scope, duplicate local labels and sentences crossing scopes remain unlinked. Ordinary single-list and footnote routes retain their previous paths. No named-source exception, dependency, public schema or verdict change is added.

Ten invented regressions cover restarted numbers, wrapped headings, duplicate labels, prose mentions, long first-level headings, corporate-author ordinals and cross-scope withholding. Source058 was rejected after extracting82/94 NASA occurrences; its failed artifacts are preserved. The final root parser suite passes879tests and149subtests, with the same28optional sample-dependent skips, zero failures/errors and zero network attempts or egress. All382 frozen runtime pins are stable. The full303-page NASA PDF produces94/94 source-aligned occurrences,157claims and138citation edges;32markers remain orphaned. Ten retained edges carry existing structural flags. The eight added059→060 edges were independently checked against Chapter3 local12–19. This validates extraction and source binding, not work identity accuracy; NASA is development material.

A full18-PDF Parse comparison retains1,427references and matches Source059 in all non-ID reference/claim fields, canonical bindings, full text, table-only citations and ambiguities. The62 raw-entry changes previously observed against056 are already reproduced by published057 in the two affected PDFs;060 introduces no further differences. Root acceptance: .t/new-corpora-20261008/continuation-002/source060-root-review-001.json. Frozen060 manifest d0c2eea792bebfd76acccf2425e30385c3bba75f22c04a1111523d3669dab251; aggregate0c45b07c3bb261f2a159b019498587d2cd24cf98399be2c7c6999aaac2c6c776; parent4a768531ee1c6dec2fd9d165d39271d48a21e044.

All three Source056 historical native phases completed18/18 with actualexit0, but the original controller remains failedexit1 because audit018 rejected eight intentionally suppressed typed-footnote parents. Fresh comparison020 completesexit0:1,427rawkeys align, the same8 typed exclusions occur on each side and0unexplained missing results remain. Fresh final qualification and120-positive scoring are pending;1,307references remain unscored. Original failed controllers/comparisons are preserved.

The first independent NIST whole-document trial exposed a distinct failure:15 fragments contain42of58 printed opaque keys, with16entries lost after an Appendix page footer;14numeric citations incorrectly bind generated ordinals. An invented-key probe reproduces both mechanisms. A generic keyed-bibliography correction is the next bounded task. NIST is now observed development material; no held-out accuracy is claimed. Graphify update was attempted, but its executable is unavailable.


### Opaque bibliography keys and early PDF boundaries — 2026-10-08

Source063 recognizes repeated bracketed bibliography keys with inline or detached entry text. It preserves each literal label and derives bibliographic fields from a private label-free view, with coordinate spans bound to stored raw text. Duplicate, malformed, orphan and numeric-mixed spines remain withheld. The existing numeric recognizer now requires substantive entry text in this keyed-list check, preventing whitespace after a standalone date from creating a false numbered entry. The early PDF split applies the same structural footer proof before moving an appendix tail to the body. Numeric body markers cannot bind generated ordinals in an opaque-key list. No title/key dictionary, source-specific exception, dependency, public schema or verdict change is introduced.

The invented regressions fail before the relevant corrections; the final focused file passes15tests. Root's77-file parser selection passes913tests and149subtests, with31optional sample/dependency skips, zero failures/errors and zero network attempts or egress. The prior75-file selection retains the same28skip names/reasons; three additional missing Harvard-PDF samples belong to the two newly included PDF-link files. All382runtime pins remain stable. Rejected061/062snapshots, their failed NIST criteria and preparation failures are preserved.

On the original183-page NIST PDF, all58source keys now yield separate references in source order, each with its literal label; synthetic numeric edges fall from14to0. This is extraction and false-binding validation, not identity scoring or newly added support for source-key body citations. NIST is development material. The NASA303-page PDF retains94references,157claims and138edges, with all fields/bindings/fulltext/table/ambiguity data equal060. All18historical PDFs retain1427references and exact non-ID semantics against060, including the four legitimate null endpoints. Root acceptance: .t/new-corpora-20261008/continuation-002/source063-root-review-001.json. Frozen manifest e2efcfd5e72e4bc54885a6839d75a892d4f4d0bba73f1b6af9a78f73fa8073ce; aggregate f4dab2b00d3db12962fe84360c06d8c34f971e8af4178f55604d8f5eeaf1ece4; parent673f40c1aef32f1cda521eea2e1130a412ac7548.

The historical056native recovery completes independently with actualexit0 while preserving the original failed parent. The old120-label scorer measures typed resolved_identifier columns only: its120astentions per side must not be interpreted as absent operational identities. A pinned Source056 native repository trace proves a declared DOI can be strongly confirmed and consumed by Fetch with resolved_identifier=None. A separate operational-identity scorer is being prepared using each frozen source's own repository payload contract; no replacement score is claimed yet, and1307references remain unscored. Cioffi's eleven reserved documents (485TEIextractionannotations, not identitygold) are prepared but unpredicted at this checkpoint. Graphify update was attempted; CLI unavailable, generated files untouched.


### Independent corpus trial and historical identity audit — 2026-10-09

The first Source063 trial on the eleven reserved Cioffi–Peroni documents completes offline and produces 360 references against 485 TEI extraction annotations. This count ratio is not precision, recall or work-identity accuracy. The original split, inputs and first predictions remain frozen; these observed documents become development material for subsequent corrections. An additive exact whole-title diagnostic finds 224 unique anchors, including 31 with a DOI on Parse only. The annotations expose no typed DOI values, so their absence cannot establish a DOI conflict. Duplicate titles remain unpaired. Heading-selection and merged-entry failures are investigated separately.

The historical native campaign now has a bounded operational-identity score using each frozen source's own repository projection. Only a resolved strong global DOI with a confirmed declared/discovered identity state receives credit; compatible metadata or provider candidates do not. Fourteen focused audit-reader controls and the root-authorized native score pass. All 758 frozen runtime files and 36 read-only run databases remain unchanged, with zero network attempts.

Of 120 independently labelled positives, baseline001 confirms 104 exact expected DOIs and abstains 16 times; frozen Source056 confirms 101 and abstains 19 times. Neither selects a different strong DOI. The 19 Source056 abstentions comprise 14 weakly corroborated records and 5 unverified records. The remaining 1,307 raw references are unscored. This is not an accuracy estimate for all 1,427 references or a fresh Source063 Resolve run.

All three additional abstentions begin with Crossref HostCooldownExceeded in Source056 while baseline receives a successful Crossref response. The observed decline cannot be attributed to the algorithm without a transport/scheduling-controlled comparison. The old typed-column-only scores and the original failed qualification controller remain preserved. Root review: .t/residual-audit-20261007/historical18-source035-001/historical-current-prep056001/historical-operative-identity-preparation002/root-operational-score-review001.json; SHA-256 ebd376e8f67c06851412a5981ca75569165cb9f432db4aec72b5ff0d05f15211.


### Multi-column bibliography checkpoint Source068 — 2026-10-09

The parser now recognizes References Cited / References and Notes, selects supported headings using the existing bibliography spine, shares lowercase author-name particles across boundary paths, accepts quoted detached-number entries, and orders proven multi-column bibliography blocks geometrically. Ambiguous cross-gutter/margin blocks with source evidence remain withheld. A repeated margin DOI can be removed only when it matches the sole bounded front-matter DOI, the complete block recurs at the same position on at least three pages, and source-entry/foreign-identifier guards pass. Broad all-caps running-head evidence is limited to bibliography pages. No source-specific title/DOI exception, dependency, public schema, or verdict change is introduced.

Root focused validation passes 75 tests and 34 subtests. The 83-file relevant suite passes 972 tests and 183 subtests, with the same 33 optional skips as the rejected Source067 run, actual exit 0 and zero network attempts/egress. All five frozen validation phases exit 0; all 382 runtime pins remain stable. Full18 restores 1,427 references: 17 papers are semantically identical Source065, while Fmed removes source-reviewed margin furniture from references 3/6/29. Its genuine DOI and unchanged certified claim/source fingerprint are preserved; no test fingerprint was relaxed. NASA94 and NIST58 retain all canonical semantics. Source066/067 failures remain preserved.

The eleven observed Cioffi development PDFs yield 451 references, unchanged from Source067. MUL39 has separate printed labels 1–59; EAR19 still has 13 missed author-year starts and 8 false cuts (52 records against 57 boundary annotations), and NEU41 still loses a reference continuation after a body-section column prefix. Fmed reference3 retains pre-existing author-contribution contamination. These are open tasks, not an overall extraction or identity score. The 75 reserved LinkedBooks documents remain unpredicted. Root acceptance: .t/new-corpora-20261008/continuation-002/source068-root-review001.json, SHA-256 f2ba090490b466f98d0430b3f898b00bd67808dacc7d2aa41b8680fb018ab435. Frozen manifest7f8b1e3424d53af93a3b1dc73c061dec545713815c4f13722bd008ab13e36ff8; aggregate39bbcfdafe6aad346170adbfb29e66ac177d770587417aecdfe45b6c84097e14.

The bounded historical transport experiment004 uses short per-run repository-contained TEMP/TMP/TMPDIR paths and passes13 harness controls,4 synthetic workers and2 repeatability pairs. Its actual baseline5376 worker exits0, but baselineicae138 exits3221225477 during native PDF work/trace dumping; the parent exits1 and stops before Source056. The exact native cause remains under investigation. There is no new accepted identity score: the qualified frozen056 result remains101/120 expected strong DOIs versus baseline104/120, with1307 references unscored. Failed experiments001–004 remain intact. Graphify update remains unavailable; generated graph files were not manually edited.


### Source070: later-column prefixes and bibliography style selection — 2026-10-09

Source070 addresses three linked extraction failures. On the first bibliography
page, a later column could place author contributions or other back matter before
the continuation of a real reference. Exclusion now requires multiple exact
post-bibliography headings with prose, no independent source anchor, and a
supported aligned bibliography resumption. An ambiguous or source-bearing prefix
withholds the candidate. A URL carrying only the uniquely established manuscript
DOI can be recognized within that already-proven back matter; foreign identifiers
remain protected.

A source-free, full-width mixed-case header can now seed the existing exact
whole-block repetition proof only at the top page margin and on at least three
pages with the same normalized bounds and text. Finally, a lone small numeric
locator cannot override a strong author-year prefix without a later advancing
bibliographic marker. Ordinary short numbered lists and singleton DOI entries
remain supported. These are structural heuristics, with invented regressions;
there are no document/title/author/DOI-specific production exceptions, new
dependencies, public interfaces, schema changes, or weaker identity checks.

Root's relevant 84-file suite passes 985 tests and 191 subtests, with the exact
same 33 optional skip tuples as Source068, no failures/errors and zero network
attempts or egress. All five actual validation phases exit zero, with 382 frozen
runtime files stable. The 18 historical PDFs retain 1,427 references: 17 papers
are canonically unchanged; Fmed changes only reference 3's raw text, removing
separate contribution prose while retaining the genuine source. NASA's 94 and
NIST's 58 references retain all canonical semantics.

The eleven observed Cioffi PDFs yield 477 records. Ten documents are canonically
unchanged, including EAR19's 52 and MUL39's 59 records. NEU41 changes from 37 to
63: its previously separated reference now holds the complete title, publication
year 2015 and DOI together, with no body sections or manuscript DOI mixed into
references. The ordinary parser already produces the correct list; the
column-aware adoption guard was not relaxed. Counts and TEI boundary annotations
are not work-identity accuracy measurements.

Frozen manifest: 14e46b94cf7688f5c2aaa189f40acb9058f5764f2892bd1b95c2c5d40d6efe54.
Aggregate: 3b84633627f321c56706f6663ce474f590197ddd9693befbeb0564aaff9a9f43.
Root regression review: source070-root-regression-review001.json,
SHA-256 190f8783ef1b3cb7860587e0e5bbd3dd33f0d616778afe083b3296aa5c603e1c.
Canonical review: source070-comparison-review001/source070-validation-comparison001.json.

Open work remains separate: long multiline author-run segmentation in EAR19,
its downstream field extraction, and the three additional historical Source056
DOI abstentions. A qualified four-worker strict controlled replay completes
without misses or egress, but all three targets still remain baseline-confirmed /
Source056-abstained. Native evidence localizes their failure before the Crossref
request, at host admission; no exact DOI response reached validation. This does
not replace the original 120-labelled-reference score (104 versus 101 expected
strong DOIs, zero different strong DOIs; 1,307 references unscored). Scheduler
binding is under a bounded test-harness investigation.

An existing known-furniture promotion path can also remove invented repeated
URL-bearing top-margin blocks; this was reproduced before the new header seed
and remains a separate audit item. The new seed's source veto is tested directly.
The 75 reserved LinkedBooks documents remain unpredicted for the next independent
trial. Source066/067 and all failed harness preparations remain preserved.
Graphify update is unavailable because the CLI is absent; generated graph files
were not edited manually. Overall corpus/identity objective is not complete.


### Source071: syntactic multiline author lists — 2026-10-09

Long author lists could exceed the existing date lookahead, leaving the first
author attached to the preceding reference and starting the next record at a
dated coauthor. The parser now recognizes bounded author-only runs ending in a
date. Intermediate lines must retain surname/initial syntax; title prose, bare
capitalized words, URLs and venue text cannot establish a run. Hyphenated
initials, name particles and a final conjunction are supported. This change
contains no document, author, title or DOI exceptions and does not alter
identity validation, verdicts, schemas, interfaces or dependencies.

Root focused validation passes 95 tests and 65 subtests, with one unchanged
optional skip. The relevant 85-file suite passes 992 tests and 193 subtests;
all 33 optional skip tuples match Source070. All five actual validation phases
exit zero with no network attempts or egress, 382 runtime pins and six focused
test pins unchanged. The 18 historical PDFs retain 1,427 references and all
canonical Parse semantics. NASA (94 records) and NIST (58) remain canonically
unchanged. Ten of eleven observed Cioffi documents are unchanged; EAR19 remains
at 52 records while two author heads move to their correct reference, preserving
all 35 DOI owners and the adjacent sources. The same claim sentences and 71
citation links remain; three existing author-year links gain the correct
reference endpoint (two Finné 2017 markers and one Kaniewski 2017 marker), with
no dropped endpoints. The source-PDF audit confirms both heads and their
neighboring bibliography boundaries. Counts and extraction annotations
are not measurements of work-identity accuracy.

Frozen manifest: 2c9ba249a7d28b204bc5d39cccbb5281271c0d89e2148f9f1407051ec65d64a7.
Aggregate: 6b52ab787feab2bb16e44bd8722e0635736c7aa346480e1e45db8ac1b3324f46.
Evidence is retained under .t/new-corpora-20261008/continuation-002:
source071-root-regression-review001.json and
source071-canonical-comparison-root001.json, with a separate source review for
EAR19 before root acceptance. Remaining EAR19 boundary and title-field defects
are outside this bounded change and remain open.

The historical transport experiment005 completed but did not establish the
claimed controlled-availability condition: the per-reference replay scope
replaced the startup limiter/clock, leaving its scheduler bound to stale state.
This finding supersedes the earlier availability qualification; native outcomes
and the original 120-labelled-reference comparison remain valid as observed.
Preparation006 then exposed a second test-only defect: waiting inside acquire
occurs after the ordinary admission deadline starts. It is preserved as a failed
preparation; fresh007 moves the wait before that deadline, with actual-scope and
concurrent-lock controls required before any historical replay. No production
transport limits have been enlarged and no new identity score is accepted.

The reserved 75 LinkedBooks documents remain unpredicted at this checkpoint.
Their first trial uses an independently pinned runner that preserves native
integer years, seals predictions before comparison and distinguishes conditional
field agreement from full-pipeline work identity. Graphify update is unavailable
because the CLI is absent; generated graph files were not edited. The overall
corpus/identity objective remains incomplete.

Root acceptance: source071-root-acceptance001.json, SHA-256 0792051eea01999ed60364dccf5336862624b86c284d42e01c83f90c3780eb11.


### Source072: explicitly bounded historical imprint year — 2026-10-09

Historical publication years could be dropped when an already parsed, unique
title was followed by a textual place: imprint, year tail and the imprint was
absent from the publisher allowlist. The common parser now accepts that closed
form for a sole terminal historical year. Competing dates, title ranges,
identifiers, locators and unbounded tails remain insufficient. Modern and
explicit author-date precedence are unchanged. The correction uses syntax,
with no real title, author, DOI, place or publisher exceptions.

The focused regression reproduced three failures with 18 passing controls;
all21 focused cases now pass. Root validation passes99tests and2subtests.
The86-file suite passes1013tests and193subtests, with the same33optional skip
tuples as Source071. Allfive actual phases exit0, without network attempts or
egress;382runtime files and7focused test pins remain stable. All18historical
PDFs retain1427references and exact canonical Parse semantics against071;
all11observed Cioffi documents retain477references and exact semantics.
NASA94 andNIST58 records, claims, endpoints, fulltext, ambiguity and table
projections also remain unchanged. Graphify update remains unavailable because
itsCLI is absent; generated graph files are preserved.

The same2033complete LinkedBooks spans were rerun as observed development data,
not a new independent holdout. Exactly one missing year becomes correct,
raising year agreement from1202/1900 to1203/1900; no previously exact field is
lost and all2033title fields are unchanged (49/1990exact). The other579missing
and118nonmatching years, and the title extraction debt, remain unresolved.
These comparisons do not establish work existence or identity accuracy.

Frozen072 manifest21a4a971f1b762788840cee778706e1011450b0087389531b43f602251d1451b;
aggregatebfa53b896aa04b6bf6c1a4d8cbd0ef3cbcd38ae93e6c4af595b6759699d7182e.
Rootacceptance source072-root-acceptance001.json; canonicalreviewSHA
4302777dda30f5712e3f94bc79d94e8ec24bf4b8f8885ccf6ebd53084d5f844f.
LinkedBooksdevelopment delta receiptSHA
6cb9b53efbb29b7af6cd95476de7a75fd842eb8ffe78218c3b9831698883e692.

The first75-document LinkedBooks trial on071 is preserved:2033complete spans
from70contributing documents,26malformed spans excluded;title49/1990exact,
year1202/1900exact. The75documents became observed development after that trial.
The next external conditional-field trial uses the GROBID upstream evaluation
partition acquired before072 tuning; no trial has yet run at this checkpoint.

Historical replay007 now has valid clock bindings but fails strict qualification
because51request occurrences lack captures (34exhausted,17absent exact-key
events). Original pools andfailed preparations remain intact; no new identity
score is accepted. A new coherent capture preparation is being reviewed.
The original120labelled positives remain baseline104exact/16abstentions versus
Source056101exact/19abstentions, with0different strong DOI selections;
the remaining1307historical references remain unscored.


### Source073: empty initial-author boundary guard — 2026-10-09

An empty sentence prefix after comma-separated author initials caused an
IndexError in the native title parser. One of 327 acquired GROBID citations
exposed the failure; two invented examples reproduced it. The common parser
now checks whether a preceding token exists and retains its existing
abstention for an insufficient title prefix. No corpus names, titles, authors
or identifiers were added to the rule.

Focused root checks pass 104 tests and four subtests. The unchanged 86-file
selection passes 1,015 tests and 193 subtests, with the same 33 optional skip
tuples. All five offline validation phases exit zero. Canonical Parse results
are unchanged against Source072 for the 18 historical PDFs (1,427 references),
11 observed Cioffi documents (477), NASA (94), and NIST (58). The same 2,033
LinkedBooks development spans retain all native fields and comparison outcomes:
title 49/1,990 exact; year 1,203/1,900 exact. No previously exact field is lost.

The first complete GROBID field trial on Source073 processes all 327 citations.
Of 176 eligible annotated titles, 19 match, 100 are missing, and 57 differ.
Of 286 eligible years, 280 match, four are missing, and two differ. Another
151 titles and 41 years are unscored because the annotation does not provide
a unique supported comparison value. Author extraction is not scored.
Predictions are sealed before annotation projection; network attempts are zero.
These observed development comparisons establish neither work existence nor
identity accuracy, and title extraction remains substantially incomplete.

The failed Source072 trial and every failed preparation are preserved. A
premature root Source073 invocation refused before XML access because its
acceptance receipt was missing; the subsequent accepted invocation completed.
This was an audit orchestration error, not a second parser failure.

Root acceptance: source073-root-acceptance001.json, SHA-256
8efddb8405fc49a22f72c05cbe3a026b0b4831c4071135165bf365e08803601a.
Canonical comparison: 2db1f9d6bc500d58b0117c90e95b81e27e0b61fef48aa2f3b0e22f3334b51f7b.
GROBID trial summary: 1b141839e69a3857f48383f28b8c916e5f0d24bcc2f1a741710cf5137cac2786.

Fresh historical capture remains unstarted. The root P8 timeout control failed
after six controls passed. A bounded A/B test did not support temp-path length
as the cause; inspection found that cleanup stops the parent before discovering
descendants, creating a race opportunity. A separate audit preparation is
testing cleanup ordering before any new capture. Original pools and replay
qualification remain unchanged; no new historical identity score is accepted.
Graphify update remains unavailable because the CLI is absent.


### Source074: closed labeled article-title boundary — 2026-10-09

The title parser could return a volume/page fragment for a quoted article
followed by labeled volume, pages, and a terminal month/year. It now recognizes
that complete shape with the existing initialized-author grammar and projects
only the quoted title. The same closed tail without a title abstains. Open
quotes, nested quotes, incomplete locators, competing dates, and trailing
material remain outside the new recognizer. The regression cases use invented
citations; no corpus titles, authors, DOI values, or subject acronyms enter
the rule.

Root focused validation passes 127 tests and four subtests. The unchanged
86-file broader selection passes 1,029 tests and 193 subtests, with the same
33 optional skip tuples and zero network attempts. All five validation phases
exit zero. Canonical Parse output remains unchanged for all 18 historical PDFs
(1,427 references), 11 observed Cioffi documents (477), NASA (94), and NIST (58).

On the same 327 observed GROBID TEI-stream records, seven titles gain exact
agreement and none lose it: 19 to 26 of 176 eligible titles. The remaining
150 disagreements include 98 missing titles. Years remain 280 of 286 exact;
all non-title native fields, raw inputs, annotations, and denominators remain
unchanged. On the same 2,033 LinkedBooks development spans, every native field
and comparison status remains unchanged: title 49/1,990 and year 1,203/1,900.
Predictions are sealed before gold projection. These are conditional field
comparisons, not work-existence or identity-accuracy estimates. Upstream
GROBID annotation rules support character-stream-preserving tag erasure;
fidelity to the original PDFs remains unestablished.

Root acceptance SHA-256:
546fdc90d4a546fdff693cb14fb45e0410292ab529604073545bae59f1dad8d9.
Canonical corpus comparison:
85f68fd7fb6b1101ac5f72066c538c84f6ca847011a3504bf9a2ca21e2ee9486.
Root independent development comparison:
ee44dc7ddc1cb8a8f2778310942a266c293aeefa0196c998653bbcdd2a5a6cdb.

All four fresh native historical captures completed with valid databases and
unchanged original pools; receipt:
8ee4617e023a8a89786b6fecbcc717385f7c09c3eacf4337c6eb7588bbfa29c6.
Strict replay remains unqualified. Separate preparation attempts exposed
parent-versus-paper pool-path assumptions and a relative-path assumption in
a copied transport-scope module. Each failed attempt and its output is
preserved; the next preparation corrects only those bindings and retains
the existing clock, transport, and pin-validation policy. No new historical
identity score is accepted. The citation-identity objective remains open.


### Source075: independent URI after DOI — 2026-10-09

DOI line-wrap repair could concatenate a following independent URI when that
URI contained digits and a slash. A generic scheme boundary now refuses
that continuation. The two-line production change recognizes URI syntax;
it contains no corpus titles, authors, subjects, provider hostnames, or DOI
exceptions. Existing genuine DOI wrap cases remain passing.

Root focused validation passes 339 unittest tests and five coordinate pytest
tests. The same 86-file broader selection passes 1,029 tests and 193 subtests,
with the same 33 exact optional skips and zero offline network attempts.
Canonical Parse output remains unchanged on the 18 historical PDFs (1,427
references), 11 observed Cioffi documents (477), NASA (94), and NIST (58).

On the identical 22 metadata-derived preprint citation strings (12 DOI families,
multiple source versions), correctly parsed DOI values rise from 0 to 22,
and canonical strongly confirmed preprint DOI identities rise from 0 to 22.
Titles remain 22/22 exact; no linked publication DOI is selected as an alias.
The two native Parse+Resolve pilots use zero LLM requests and 67 versus 45
HTTP attempts. Source075 independently queries providers; different live
times prevent attributing all operational differences solely to the patch.
These are generated citation renderings from official bioRxiv metadata,
not original bibliographies or a general accuracy estimate. Fetch/Verify
and content-version identity remain unscored; held-out families are untouched.

The original audit003 used nullable resolved_identifier database columns and
reported zero canonical identities despite successful direct-DOI resolution.
It is preserved. Audit004 seals both native prediction sets before expected
projection and uses RunRepository.resolve_payload_map(), independently equal
to the report export. No production identity interface was changed.

Root acceptance SHA-256: e30371f0be0f0b48f7882718826b39a93f3ece4df869e6c24ba9c1ecb94e624d.
Canonical corpus comparison SHA-256: 669fc1b63f829e52deca4ddbaf1f42dad4cfdc90c085cd748757e99645701163.
Canonical native pilot audit SHA-256: cdd6869d611bff1361bf8e5da9f85afe11a366939f6c87097d0062bb999125dc.

Historical strict replay remains unqualified: five of 17 missing HTTP keys
have explicit native pre-opener HostCooldownExceeded outcomes. Twelve more
have unchanged native/replay request fingerprints and no native HTTP attempt;
their exact admission reasons were not captured. HTTP-only recording cannot
reconstruct pre-send decisions. A separate generic admission receipt is under
investigation; do not infer rejected requests from missing keys or relax
the all-hit requirement for actual HTTP opener calls.


### Admission-aware historical replay: first qualified case — 2026-10-09

The HTTP recorder did not capture limiter decisions made before the opener.
A strict replay could therefore issue requests absent from the native HTTP
pool, despite unchanged bibliographic query inputs. The audit-only recorder
now binds ordered admission outcomes to the frozen source, input and request
intent, including the generic institutional Search GET/POST transport. Missing
HTTP keys never imply a native refusal. Production verdicts are unchanged.

The separate admission recorder passes19 offline regression tests. On one
fresh native baseline document (5376), capture and strict replay complete68
references with SQLite integrity ok and zero LLM requests. All723 admission
events are consumed and all451 HTTP events are unique replay hits backed by
validated native response/error proofs. No outside transport is attempted;
the original capture pool, tape and terminal receipt remain unchanged. All68
frozen raw-key Parse/Resolve projections and canonical identity decisions agree.

Both databases are read with their original frozen source's repository, schema109.
The current schema114 reader correctly refuses them; no migration or adapter
was added. After stable raw-key alignment, full-payload differences are confined
to run-specific reference IDs and operational/catalog timestamps with the
catalog hash derived from its timestamp.315 bounded admission timing differences
remain explicitly recorded. Failed capture/diagnostic/audit artifacts are kept.

Root pair receipt SHA-256:a04930f67d4b646768467c85b91c96ba93677b54d657bf3d06df46b20b100914.
Native admission tape SHA-256:d3d2f423988f56138d78c57649889bdade9f0e7f1f958cdc4ae15bcd5096a07a.
This establishes reproducibility for one baseline document, not identity accuracy
or qualification of the full historical corpus. The Source075 paired case,
remaining historical cases and external content-version verification are open.

### Source075 counterpart and first positive comparison — 2026-10-09

The same document's frozen Source075 capture and strict replay qualify:
68 references, SQLite integrity ok, zero LLM requests, 1,329 admission events
consumed and 788 HTTP replay hits, with an empty outside-transport ledger.
All stable raw-key Parse/Resolve projections and canonical identity decisions
agree. The 554 bounded admission timing differences remain recorded.
Root pair receipt SHA-256:
cbd21936913aab709c86116efd15fa898519e70e719614829182932a8397f914.

Both prediction sets were sealed before projecting the 11 independently
confirmed positive DOI labels. Baseline and Source075 each confirm nine and
abstain on two (references 22 and 61); none returns a different strong DOI
among these positives. The other 57 references remain unscored. The full
18-paper/120-positive comparison and specificity assessment remain open.

Across all 68 references, 24 rows and 23 statuses differ between sources,
including one newly strong match at reference 32 and no strong loss or
switch. An independent Crossref/publisher check subsequently confirmed that
reference 32 matched a review DOI rather than the cited book. That match is
a false positive, not a gain. Other cross-source changes also cannot be
attributed solely to code because the live provider epochs differ.
Canonical positive comparison SHA-256:
83dffae9d905de5a5a7ef84ce199c05bdbaf94062df97e8310922bedb7176383.


### Generic identity and preprint-revision guards — 2026-10-09

The two confirmed-positive abstentions at references22/61 arise from uppercase
X in the cited DOI versus lowercase x in the registry response. Canonical DOI
identity now uses Basic Latin A-Z equivalence only, following DOI Handbook3.3.4;
Unicode case pairs and non-DOI schemes remain distinct. Raw citation/provider
evidence is retained. The DOI-focused selection passes 14 tests/48 subtests,
and its broader selection passes 1018 tests/73 subtests.

The review/book false positive exposes a separate admission shortcut: a
validated discovered identifier and high title overlap can lock a candidate
when its author is missing, even with conflicting metadata. The generic lock
now declines that shortcut for unresolved authors with a known year or
coordinate contradiction. Missing year metadata is not treated as a year
contradiction. Declared identifiers, explicit certificates and the existing
confirmed-author/year-error control retain their paths. Two invented
negative cases reproduce the failure before the fix; 114 focused tests pass.
Readjudication of ref32's saved evidence yields not_identified, but is not a
new native corpus result. The broader book-versus-review unit problem with
otherwise matching metadata remains outside this local correction.

The 22-reference preprint Fetch pilot originally stored 22 fulltexts, with
10 source routes whose dates correspond to later revisions. Date compatibility
for the other 12 did not prove their content version. Invented bioRxiv/medRxiv
controls reproduce automatic acceptance of v2 or an unversioned candidate for
a cited v1. Storage now requires matching work and numeric revision on recognized
provider content routes, or declines automatic admission. Known contradictory
routes cannot be overridden by attestation; unknown routes retain the existing
authenticated operator path. Unversioned citations retain their behavior.
No schema, public interface or broad content_version category changed.

The combined frozen Source079 selection passes 1424 tests and 143 subtests across
67 files, with zero actual network egress. Its earlier Source078 attempt found
one regression caused by confusing an absent year with a contradiction; the
current guard fixes that distinction. Source079 manifest SHA-256:
ad13fc1beaca3d090543a334a73f74734d9b070528cd0f57d7dd0d304e33db7a.
Core aggregate SHA-256:
e0a9f5c8087726add3e25c156d1644befb35a9f83d5b6b219ff15e326d7bf790.

Source076's default parallel at017 replay remains unqualified: the same two
Fetch batch requests reach the limiter in opposite orders, and the strict
recorder refuses the mismatch. Source079's at019 capture and self-replay qualify
with the supported single-candidate-worker configuration: 1,327 admission
events consumed, 799 unique HTTP replay hits, empty outside-transport ledger,
68 aligned raw citation keys and no changes in the canonical identity decisions.
This establishes the configured control, not default parallel reproducibility.
The fresh
Source079 Fetch-only preprint child retains the original Source075 Parse/Resolve
inputs and ends at the Fetch pause: 11 fulltexts, 22 abstracts, 14 pending tasks,
SQLite integrity ok and zero LLM calls. Stored-text observations were sealed
before the new version audit; storage counts do not establish admission or
content-version correctness. Full 18-paper re-evaluation,
independent external holdout identity scoring and content-version verification
remain open.

After sealing the at019 predictions, projection of the same 11 independently
confirmed DOI positives yields 11 exact identities and zero abstentions, versus
nine exact identities and two abstentions for baseline and Source075. No different
strong identifier is returned for these positives. The other 57 citations are
unscored. Reference32 no longer returns the independently confirmed wrong review
DOI; it abstains in both capture and replay. This does not identify the cited book.
Pair receipt SHA-256:
a6669a9eb21ebb13a935ab7be4909b6abc40e7de88064bdf166305b876f4df6d.
Positive projection SHA-256:
1f8b89883908e01301f9f233c824978f90eefdbf564569ea58d5e16e294bcfb2.

### Cited preprint revision selection — 2026-10-09

The Source079 Fetch audit separates 11 missing fulltexts from three stored texts
that still require identity attestation. Ten missing texts have HTTP200 candidates
for a later revision: the bioRxiv provider receives the complete reference but
ignores its versioned URL and always selects the latest API entry. The remaining
missing text has the matching revision route and fails the existing front-matter
title check. The title check and storage admission guard are unchanged.

The provider now selects the numeric revision in a recognized bioRxiv/medRxiv
content URL. If that revision is absent, or the cited route identifies another
DOI, it returns no candidate. Unversioned citations retain latest-version behavior.
Invented regression cases fail before the correction and pass afterwards:
10 focused tests and 14 subtests. The relevant nine-file broader offline selection
passes 384 tests and 88 subtests, with zero actual network egress. Frozen Source080
differs from Source079 in this provider only; manifest SHA-256:
1e796be53a04d34f1d7909add0cedb2e07d51e11c722ed94f42a3c7ad619d043.
The original descriptor inherited stale parent/change/timestamp metadata.
A replacement bundle has identical runtime hashes and the corrected descriptor
SHA-256 a4b03360d17887fff57f04755100287e7de5641f9d8a0e78ccf2604b0148ed89.
The original bundle and its accepted test receipts remain preserved.
No per-title, per-DOI or corpus-specific selection rules were added.

The first native instrumented Source080 Fetch trial (PP012) is unqualified.
The official CLI stops at paused/fetch with exit10, SQLite integrity ok, unchanged
parent and source, but no fulltexts. The recorder captures19 PDF-signature blobs
while Fetch records transport AttributeError failures. An offline reproduction
with the actual frozen Fetch primitives identifies missing response-header
delegation in the audit cassette; the unwrapped response succeeds and the
wrapped response fails without any network call. The launcher also incorrectly
expects active rather than the actual paused Fetch state. Preserve this failed
trial and correct only the audit instrumentation before a fresh run; it is not
a production recovery result. Native recovery and body-version results remain
pending, as does the full18-paper re-evaluation.

The fresh PP014 trial, using only the audit response-API correction and isolated
state, qualifies the official Fetch pause: CLI exit10, SQLite integrity ok,
unchanged parent/source, zero LLM activity and zero Verify tasks. It recovers
21/22 fulltexts, versus Source079's 11/22; reference21 still fails the unchanged
identity check. Eight Fetch tasks remain. These counts do not establish body
revision correctness or automatic admission; that projection follows observation
sealing. The capture has105 entries,90 hash-verified bodies and29 PDF-signature
blobs. Default cookie-handshake transport may bypass this cassette seam.
Process receipt SHA-256:
3f2fa5146e7bc99f6e0e84ddb837c9602da9d9ac62010e071a872e0536ae5701.

Preparing full18 also reveals that V10 successful replay entries lack original
response headers. Strict measurement must capture and bind that evidence rather
than invent replay headers. The earlier one-paper identity projections retain
their stated scope; they do not prove faithful Fetch-header replay or full18
coverage. This correction is confined to the audit harness.

### Sealed preprint body-revision audit — 2026-10-09

PP014 observations were sealed before loading the pinned official revision
expectations. The first body reader proved two revisions and left 19 unscored:
the PDF text stream returns the publisher's physical top-page banner after the
abstract, beyond the reader's frontmatter cutoff. A read-only audit supplement
uses the first page's top 5% geometric band. It requires a labeled DOI in a
bioRxiv/medRxiv preprint banner and an explicit posting date that uniquely maps
to the official captured version history. Mixed work identifiers, conflicting
dates or versions, and disagreements with the prior reader remain unscored.
No production identity gate changed.

All 21 stored fulltexts match their cited revision; reference21 has no fulltext.
The supplement passes 11 synthetic controls, including foreign DOI, software
version, date collision and conflicting evidence negatives. Postchecks confirm
the sealed database, texts, captured bodies, source bundle and parent remain
unchanged. This is content-revision evidence, not automatic identity admission:
seven identity-attestation tasks and one missing-text task remain. It is a
22-reference development pilot built from metadata, not independent bibliography
accuracy or a full18 result.
Body audit SHA-256:
642d7326a7bc9d07ed9964e4027e2b6320ea83a3f41c377331c8e8f0fb3dd2cd.
Observation seal SHA-256:
06d0cd53848b494673c8a53ecce84fb9446d188e52c1fad6ce464999ca6b4cf1.


## Source081 formatting identity validation — 2026-10-09

The remaining preprint audit exposed a formatting-only identity false negative:
a confirmed source title with supported inline italics did not compare equal
to the same visible citation title. The exact-title key now uses the existing
validated inline-style renderer. Genuine title-word changes, unsupported or
malformed tags, identity conflicts and metadata conflicts retain their existing
rejections. A known bibliographic year error remains visible and does not erase
an otherwise established work identity. No corpus identifiers or title aliases
were added; verdict/report schemas and public interfaces are unchanged.

A separate Windows fixture defect reproduced the same four failures against
Source080 and Source081: text-mode output converted LF into CRLF while fixtures
stored an LF byte hash. Only those four fixture writers now specify LF output;
production persisted-source hash validation is unchanged.

Root offline validation passed 384 Resolve/Fetch tests plus 88 subtests
(receipt SHA256
58eefab9d493d36711ec8b4e7ab8fcfd4acd7dcdc2534a9ed86ab52e0248d423),
and 174 golden/markup/bibliographic-adjudication tests plus 12 subtests
(receipt SHA256
7f1b90d9120ec7b20099f7a29d06af9705d65cb0dd87c7cbe349a97f5538d164).
The complete identity-gate class passed 33 tests plus 55 subtests
(receipt SHA256
b8738d09f61064d7d48c9145893e934785d4e16c688cffc1a6fed48495853254).
Actual network egress was zero. The earlier Source080 384-test selection did
not include the four defective fixtures and is not evidence they previously
passed on Windows.

The fresh historical campaign now has qualified capture/self-replay pairs for
the original 5376 PDF in BOTH baseline001 and Source080: 68 stable semantic
rows each; complete AdmissionTape consumption; 413/731 HTTP entries respectively;
all normal-response header capsules restored; zero unexpected transports, LLM
ledger entries or Verify tasks. Audit SHA256 values:
cabc5ddef8a6b5386ba4fa70d7008ab9225597137d9fe91ff4c55951ef38ae9e
and f63a2b39ffa314dc65bc18674617a6a4b6e2f4865f2f252b016a510549d56b7c.
The earlier two startup-timeout runs are preserved. Their audit wrapper omitted
the bounded driver's startup-completed checkpoint; a new audit-only wrapper
restores the checkpoint with the same deadlines. This was not a citation-engine
startup failure. Grading has not been performed; the remaining 17-paper
comparison and native Source081 checks remain pending. Capture/self-replay
proves each source's repeatability, not identical live provider epochs between
the two sources or default parallel-worker scheduling.

A fresh document-split Nature corpus contains 108 physics-development references
and 81 Earth-system holdout references from original publisher PDFs/HTML.
All 107 publisher-linked DOI candidates have independently acquired registry
metadata, which alone is not identity ground truth. Before pipeline predictions,
the physics label freeze admits 74 corroborated positive identities and leaves
34 references unscored, including a Zenodo deposit with unresolved cited
version/year alignment. Physics labels SHA256:
ff1c1ec5f031a7c1c4370921d8e53d8525b926e6796a9c7f4082e111a8f61e9d.
Earth reference semantics have not informed these fixes. Neither Nature PDF
has yet supplied a scored Callimachus result.

Open audit findings remain the preprint title-change cases and one correct
revision body withheld by the dispersed-title quality guard. The Source081
formatting correction is not evidence those distinct cases are resolved.

## Source082 visible probe title and independent Parse failure — 2026-10-09

The Source081 exact-title-key correction exposed a downstream false negative:
five stored preprint documents selected their citation-owned raw HTML title as
the probe yardstick. Inline style tags then became literal document-search tokens.
The same validated inline-style renderer now supplies local visible title
candidates and the selected raw-entry span; original evidence and provenance
remain unchanged. No title alias, DOI exception, year/admission rule, dispersion
threshold, verdict schema, or public interface changed.

The regression reproduced five failures on Source081. The focused selection
passes 172 tests and 87 subtests (receipt
8aad65979abad6d9cc0b7a9dccce5351d157d2ed76df7fb08774a6aab3f48492).
Root Resolve/Fetch checks pass 384 tests and 88 subtests (receipt
38a093167fd59c9285fe2ff018f02e5b76b5a24c0d16915d36a9eba2b855d989);
root golden/markup/adjudication checks pass 174 tests and 12 subtests (receipt
e83e81aa04463edd9e3c99b8a2b4a19d873d3ebd2aa1b649bb4789a97ece12d0).
All selections ran with network blocked. These selections overlap; their counts
must not be added as a unique-suite total.

A read-only projection of the 21 stored PP014 fulltexts changes admission and
corroboration from 14/21 to 19/21: references 7, 8, 12, 13 and 18 gain, none lose.
Their document probes change from inconclusive/title_tokens_dispersed to
confirmed. Projection comparison SHA256:
9d24a2b8ab387eb87124ff0924ace1e71df704f837cf923b646574f10848d058.
This did not update databases or remove pending tasks. The two true cited-revision
title changes and the missing fulltext remain distinct unresolved cases.

The first native Nature physics capture and self-replay both completed but
extracted ZERO bibliography references, so the required 108-reference strict
pair was rejected before identity scoring. The PDF has a numbered bibliography
without a References heading. All failed artifacts are retained. This is an
upstream Parse failure, not zero recall on the 74 frozen positives. A conservative
generic headerless-boundary correction is being validated separately; the Earth
holdout has not been run and has not informed that correction.

The historical Source080 campaign remains a separate frozen-source comparison:
six of 18 documents (192 references per source) have qualified capture/self-replay
pairs. Remaining pairs are running sequentially. The 120 frozen positive labels
have not been used to grade this campaign; no updated whole-corpus accuracy is
claimed.

## Source083 headerless bibliography validation — 2026-10-10

The independent physics PDF exposed a generic boundary failure: a numbered
bibliography without a References heading was treated as article body. The
fallback now requires one unambiguous consecutive 1–5 prefix whose entries each
satisfy existing author/year grammar and independently parsed container, volume,
and page or article coordinates. Numbered prose, gaps, ambiguous starts and
footnote apparatus remain unsplit. Existing headed bibliography behavior and
identity validation are unchanged; no document identifiers or title exceptions
were added. Interposed article sections are retained as body.

Root validation passes 71 targeted tests plus 24 subtests, 945 broader parser
tests plus 126 subtests, and the certified historical fingerprint check (two
tests plus 18 document subtests). The broader selection skips 31 tests: 19 need
optional PySide6 and 12 use sample paths absent from this worktree. The separate
fingerprint check reads all 18 original documents and has no skips. All three
checks have zero actual network egress and unchanged source, test and golden
fixture hashes. Their selections overlap and are not a unique combined total.

The development PDF parse-only check now extracts all 108 distinct references
instead of zero, preserves the caption and article sections, and has no orphan
markers. This is extraction evidence; a fresh native Resolve capture and strict
self-replay, followed by sealed identity grading, are still required. A caption
math exponent remains a separate existing marker false positive and is recorded
as backlog rather than attributed to citation precision.

Nine historical documents now have qualified baseline/Source080 pairs (315
references per source). The remaining campaign and 120-positive grading are
pending. That campaign uses frozen Source080, independently of Source083's
unchanged historical Parse fingerprints. The Earth document remains unused as
a holdout. Graphify is unavailable in this environment; generated graph files
were not edited.

Audit harness failures are retained: a Source082 preflight wrote bytecode into
its initial frozen bundle, and the first PP016 postcheck used a nonexistent
ReferenceRecord.id field. A clean byte-identical replacement bundle has zero
extra files. A sealed independent read-only PP016 postcheck shows all 22 Fetch
tasks still pending, abstracts only and no captured bodies. The Fetch log and
control flow show that automatic retrieval attempted all 22 references and
returned download errors before the manual Guided Fetch prompt. The initial
inference that the prompt preceded retrieval was incorrect. PP016 is not a
qualified fulltext-retrieval result. Its transport failure remains under
investigation. The new postcheck preserves the failed process receipt, database
and parent; no engine state or integrity requirement was waived.

The subsequent Source083 native physics run confirms 108 extracted references,
then fails while finalizing Resolve: a producer metadata evidence object is
rejected by persisted evidence validation. The failed run is sealed unchanged
(SHA256 827b3daa7034c62fe2d92412b25b7119e66a393892b5c99186e58787a501f7af).
It supplies no scored identity result and the Earth holdout remains unused.
The Source083 parser correction itself and its frozen runtime remain unchanged.

The separate PP016 transport failure is isolated to execution permissions:
all 222 captured requests report DNS error 11001 before HTTP. Identical public
host checks fail in the default restricted context and succeed with the already
authorized network escalation. A fresh Fetch trial must use that context;
this result does not establish a production retrieval regression.

## Native validation checkpoint — 2026-10-10

The authorized-network preprint Fetch trial PP017 retrieves 17 of 22 fulltexts
and all 22 abstracts. Its independent read-only postcheck passes all 16 integrity
checks, verifies 39 stored texts and 67 captured response bodies, and confirms
unchanged source, parent and input. The original launcher postcheck failed on a
wrong manifest field; that failed receipt is retained separately. This qualifies
Fetch integrity only, not automatic identity admission or revision accuracy.
Independent postcheck SHA256:
cb2941baa496bbd7570dae668af4bd6a7d74951666a6af774419587ee5cf2e85.

The four additional missing texts, references 17–20, each have a recorded
per-reference deadline before requesting the very route which stored the text
in PP014. All 17 common stored texts are byte-identical. The immediate missing
text cause is established; why earlier requests exhausted the budget is not.
Reference 21 still fails the existing dispersed-title quality check. Read-only
differential SHA256:
dcdb0577e26f6d8c0448828e27774144752d0148b2b9db797d9865fa3e010391.
The captured PDF binaries differ between the trials, so previous body revision
proof is not automatically transferred to PP017.

Ten of the 18 historical documents now have qualified baseline/Source080
capture and self-replay pairs, covering 365 references per source. The queue
stopped at the ViLT baseline because its ordered AdmissionTape replay rejected
an event and left events unconsumed. That pair remains failed; no overall
120-positive score is reported. A fresh offline diagnostic preserves strict
acceptance and investigates concurrent provider completion order.

Nature Source083 extracts all 108 development references, but its Resolve
failure still prevents an identity score. Offline investigation has located
an additional registered-coordinate proof field in the discovery metadata;
the producer-to-persistence rejection is being reproduced before correction.
The 88 sealed failed-run files retain their hashes. Subsequent read-only SQLite
inspection created an empty WAL and a shared-memory sidecar; both are preserved
and are outside the earlier seal. A separate exact sealed-input copy is used
for reproduction. The Earth holdout remains unused.

An independent PP017 body audit now confirms the cited revision in all 17 stored
PDFs using the unchanged banner geometry rule and its 11 synthetic controls.
The explicit frozen-Source082 production gate admits and corroborates 15 of
those 17 texts; references 3 and 6 remain withheld because their cited revision
titles differ from the family metadata. The gate audit uses typed records from
a verified scratch database and preserves the original sealed run and frozen
source. Supported-format positive and altered-title, hard-conflict and
unsupported-markup negative controls pass. No task was answered and Verify did
not run. These are retrieval, body-version and admission outcomes, not an
independent whole-corpus identity accuracy result.
Body audit SHA256:
4e885402616649dfcab04804fd6737d11d261accc3ee476847ff1962a2891c7a.
Production gate audit SHA256:
9a23e16eb8b70b111a68146903bf8e682319e48f646bab2a6820477c53761321.

The deadline losses are recoverable with the existing official resume path.
Seventeen separate invocations on a fresh exact run copy recover four more
fulltexts, giving 21 of 22. No retrieval policy or identity gate was changed.
The endpoint was sealed before loading revision expectations. Its new independent
audit links 77 responses and 75 unique captured bodies and confirms the cited
revision in all 21 PDFs. The frozen production predicate admits 19 of them;
the same two version-specific title conflicts remain withheld. Reference 21
still has no accepted fulltext. The root qualification rechecks both complete
sealed inventories and all 11 unchanged geometry controls, with zero network,
LLM or Verify calls. Earlier import-failed audit receipts remain preserved.
Root endpoint qualification SHA256:
1e2fe31f623edfea6f8d1a5d2ae90904daeeb84188e6aaa2bd67a86c31f9dde0.

The ViLT diagnostic now reaches the actual strict replay failure: at event353,
two concurrent success reports for the same host arrive in different URL order.
This is evidence of replay completion-order drift, not a changed identity
verdict. The strict check remains enforced. A separate cassette-pool inventory
change also blocks qualification; the diagnostic has no accepted pair or score.


### Registered-coordinate proof persistence repair — 2026-10-10

The Nature producer emits a valid registered-coordinate certificate that ordinary
relational evidence metadata cannot store. The existing typed resolver attempts
already validate and retain that certificate. The repair keeps the runtime
Resolve profile unchanged, validates a unique exact binding between selected
metadata and the typed attempt before projecting ordinary storage fields, and
restores the selected and same-candidate best proof on export. Missing,
ambiguous, mismatched, malformed and Unicode-colliding bindings fail closed.
The schema, verdicts, ordinary metadata validation and golden fixtures are
unchanged. No corpus title or identifier was added to production logic.

Root validation: 61 focused tests passed; the broader selection passed 175 tests
and 12 subtests, with no skips, errors or blocked network attempts. These
selections overlap. The earlier broader failure was a real runtime-proof
regression in the first repair; the service-level stripping was reverted, and
the unchanged existing lock test now passes. Failed receipts remain preserved.
Focused receipt SHA256:
4bfbc947b21de5ddcd7198de82264b0d5119b0fbec4ffa123ea4c8ea79d00409.
Broader receipt SHA256:
e63b3e0f26ec88a2fb41ee138970f93f0ff0d4f264c613cbbcdc462e53050285.
Production changes are confined to core/infra/db/repository.py, with the focused
regression in tests/infra/test_resolve_attempt_storage.py. Fresh independent
Nature capture, strict self-replay and sealed grading remain required.

### Source084 independent validation remains incomplete — 2026-10-10

Commit 892c6e44 was published before freezing Source084. An offline check of the
original reference-3 producer payload passes actual upsert/export without
changing its selected metadata, certificate or adjudication. Eight captured
responses replay exactly, with zero misses or network calls; removing the
unique bound attempt proof rejects before any database row is written.
Exact-profile receipt SHA256:
931f39858bc313fe76f96154bfa3131ee88b4dbc89c7fbadacc24f8174b2f624.

The fresh Nature physics capture now persists reference 3 and continues, but
fails while finalizing reference 59 with another unsupported metadata variant.
It leaves 58 finalized references (55 resolved, 3 unverified). This is a partial
run, not an identity accuracy result. Its 1,401 files were sealed before
diagnosis; the frozen source inventory remains exact. No self-replay or grading
was launched, and the Earth holdout remains unused.
Failed-observation seal SHA256:
ea1ffe022cceff91037ac7819e737601543da9a949b9ad92fca98e1593a17ffc.

The separate ViLT diagnostic adds an audit-only bounded wait for an exact future
admission event while preserving the original strict tape validator. Four
controls pass: reordered concurrent calls, unknown request rejection, missing
predecessor timeout and original argument mismatch rejection. The real replay
still fails: an acquire at cursor 328 waits for 30 seconds, then rejects; only
329 of 1,187 events are consumed. All 1,120 captured pool inputs are unchanged;
the only new pool member is the expected per-run ledger with 165 replay hits.
There are zero network attempts or unexpected transports. Partial identity
differences after that strict failure are not graded as semantic drift.
Diagnostic receipt SHA256:
3eabe755bb548f9e15f36f3053dc654aca671890abce75d9b5d1ea55cb538022.
The historical campaign remains at ten qualified pairs, covering 365 references
per source. Its frozen Source080 lineage is distinct from Source084; no final
120-positive score is available.

### ViLT baseline replay qualified with lifecycle observation — 2026-10-10

A fresh offline replay adds generic executor submit/start/finish and gate-wait
observation without changing pool size, tape, captured outcomes or the original
strict validator. All five controls pass, including exact URL argument hashing,
Future result preservation and propagation of the original exception. The real
replay consumes all 1,187 events with zero failures, timeouts, unexpected
transports or network calls. Its 54 stable Parse/Resolve rows equal the capture;
both databases pass integrity checks, and no LLM or Verify task ran. All captured
pool inputs and the original case remain unchanged; the sole new pool member
is the expected audit ledger with 593 replay hits.
Diagnostic receipt SHA256:
3a4995361719db8a5458fb4df435236c88cbf51ab4c71a6844e0c2e4edeea6be.

The lifecycle trace shows both relevant URL tasks active at the first wait,
with three of four workers started. It does not prove starvation or establish
the cause of the earlier failed schedule. Instrumentation affects timing, so
this successful replay is not a production scheduling fix. Failed 004 remains
preserved. This qualifies the historical baseline ViLT capture only: its
Source080 counterpart is still required before counting an eleventh pair or
grading the historical positives. The Source084 Nature reference-59 failure
remains unresolved.

### Source085 private locator proof boundary repaired — 2026-10-10

The reference-59 failure is reproduced from the exact frozen discovery output:
the private `_crossref_generic_article_locator_certificate` leaks into ordinary
selected and best-candidate profile metadata. Those relational fields correctly
reject this unsupported variant. The generic identity relock reads the original
result and matching typed attempt; it does not require the proof in the profile.

The existing profile filter now excludes this exact private key alongside the
publisher-native private proof. Original result and attempts remain unchanged;
the typed attempt continues to preserve and validate the full certificate.
The public registered-coordinate certificate and its storage binding are unchanged.
Production scope: `core/resolve/service.py`; regression:
`tests/infra/test_resolve_attempt_storage.py`.

The actual-provider synthetic regression fails before the filter and passes
after it. Root validation passes 113 focused tests and 204 broader tests plus
20 subtests, with zero failures, errors, skips or blocked network attempts.
The selections overlap. Receipts:
`4c44a6c19cd1e2f3de9b7e1ec0ae13786cab5084bacfa01e425748db33474e99`
and `3561e40f2ab3dec97e5e4acb8da03f1531448c4117aa5dd722c07521f6d74d71`.
The exact captured reference-59 payload regenerates its profile and passes
actual scratch upsert/export: original result/attempts are immutable, typed proof
round-trips, runtime identity lock and adjudication remain equal. Original
1,401-member failed case remains unchanged. This is a unit validation, not a
strict corpus replay or accuracy result. Receipt:
`58860f87136eb9de539d5c37fa11e0c626f639ac2a8632301058af231848798e`.

The historical Source080 ViLT capture is complete and qualifies all 1,244 tape
events. Its original self-replay exits zero but qualifies only 355 events, so the
pair remains excluded. The expected first divergent event is a concurrent
`report_success` from deeplearningbook.org; the observed event is not retained
in that receipt. Input, source and captured pool pins agree. A new generic
trace/gate replay audit is being prepared; the original failed run is preserved.
BERT baseline capture/self-replay is qualified at 56 references; the Source080
counterpart and remaining queue are still in progress. No final historical
120-positive grading or Nature holdout evaluation has occurred.

### Source086 fetch-repair storage contract validated — 2026-10-10

The fresh Source085 Nature physics capture fails during inline repair of
reference52. The real fetch producer confirms the exact arXiv identifier and
emits `arxiv_id`; repository repair validation wrongly shares the abstract
producer's narrower signal enum. Its52 stored results are a partial observation,
not an accuracy score. Failed-case seal SHA256:
`5b4a6b313e84bc6e112e09d8ab3fcaacbf032940a222b39b3be78f5af07bf6dd`.

Source086 gives fetch repair its own closed enum, admitting the five additional
signals already emitted by that producer. Abstract validation stays unchanged;
unknown signals and manual operator confirmation remain rejected in automatic
repair storage. Identity scoring, admission, schemas, provenance and public
interfaces are unchanged. Production scope: `core/infra/db/repository.py`.
Regressions: `tests/infra/test_resolve_evidence_storage.py` and
`tests/test_infra.py`; the actual inline repair and typed round-trip are exercised.
There are no corpus-specific titles, identifiers or aliases in production.

Worker contract regression: six failures before the change,13 passed after it.
Root validation passes72 tests plus2 subtests in the focused selection and
222 tests plus20 subtests in the broader selection, with zero failures, errors,
skips or blocked network attempts. All382 runtime hashes and selected test-file
hashes are stable during each accepted run. Receipts:
`78904952d1434f0137e05a505a075541366e1cd2fe14a4e30b4af658623f115b`
and `67d626e610662b5467511f0934c68b99e47deff21091e1adc8f82f1598e8c9b5`.
The selections overlap and must not be added as a unique-test count.

The full focused selection exposed preexisting Windows fixture failures:
LF text was rewritten as CRLF while retaining LF hashes, and SQLite transaction
contexts did not close their connections before temporary-file cleanup.
Only the implicated test writes and four fixture connections are corrected;
the source-hash validator and production connection handling are unchanged.
Both isolated tests pass. Failed intermediate observations remain intact;
root focused003 was rejected by its test-file hash guard after a concurrent
fixture edit and is not an accepted result.

Historical BERT now has both qualified self-replays, bringing the separate
Source080 campaign to eleven pairs and421 references per source. Baseline ViT
is unqualified at0/1431 events, although its capture qualifies1431/1431. The
fresh Source080 ViLT replay remains unqualified at355/1244: three exact future
calls wait, all four pool workers are occupied, and the expected URL task is
already running without a gate-wait record. Extra capacity is an unproven
remedy; the earliest blocking path is under read-only investigation. Differences
after that replay failure are not graded as identity regressions. No final
historical120-positive grading or Nature holdout evaluation has occurred.

### Source091 identifier-boundary repairs — 2026-10-10

The three SoftwareData abstentions expose two generic defects: a DOI wrapped after its slash loses an uppercase suffix, and a whole-citation title fallback mistakes digits in DOI/URL tokens for title-version ordinals. The parser repairs only a proven empty suffix under its existing continuation grammar. Matching excludes locator tokens from ordinals only in the whole-raw-citation fallback; parsed and quoted literal titles and genuine version conflicts retain their checks. No corpus identifiers, venue aliases, admission thresholds or interfaces change.

Production scope: core/parse/parsing_common.py and core/resolve/matching.py. Focused regressions test_parse_empty_doi_suffix_wrap.py and test_fallback_title_ordinal_identifiers.py pass73 tests. Final integration passes1,013 tests/124 subtests across39 files, including the historical18-PDF parser contracts, with zero failures/errors/skips and stable source/test pins. LogSHA256: 4038a36f5a2a2fbd885b762c5f8fafb9ef116aabe6b07c32a828fbb2420c9e0a. Exact sealed residuals pass3 local recoveries/2 genuine-version negative controls without network, subprocess or original-database access. ReceiptSHA256: 7ed5885d3c29cf66ea825b443d71c6f8b956abd6e49d040bdb74fb510ef2e842.

These are regression and local evidence results. Fresh native/strict self-replay, fixed23-positive scoring, retention of all20 previously correct SoftwareData identities, Nature retention and another independent document remain pending. LiftoffTools already belongs to development in prior manifests and is excluded from a new holdout claim. Historical Source080 GroJIL has qualified its142-reference half-pair; baseline and the remaining two documents still prevent an18-paper score.

### 1.1.1 closure boundary — 2026-10-10

The requested release scope now stops corpus expansion. Final qualification is
limited to the original First/Next/Third/Modern cohort and the historical18 PDFs.
Prepared additional documents remain preserved without further campaigns.

The last complete original-cohort score, from Source035/038, is1,855 correct,
10 wrong and63 abstentions among1,928 positives (1,988 raw references):96.21%
recall and99.46% admitted precision. The older Source056 historical descriptive
score is101 correct,0 wrong and19 abstentions among120 labelled positives;
the other1,307 raw references are unscored. These are not Source091 results.
Current-source reruns and correct-identity retention remain release gates.

Release metadata prepares VERSION1.1.1 and updates the exact CI collection gate
from4,833 to the observed7,157 tests. Packaging contracts pass74 tests. The
138-file changed-test selection initially reports3,141 passed,378 subtests,
one skip and two failing subtests in one Windows fixture. Its LF/CRLF hash
mismatch is repaired by deterministic fixture writing; the focused method
passes1 test/2 subtests and its complete module passes49 tests/66 subtests.
Production integrity checks and Parse/Resolve runtime files are unchanged.
No release tag has been published.

Post-preparation packaging and complete Verify-helper regression rerun passes
123 tests and66 subtests, with stable pins. LogSHA256:
4537a874bb724dba1471edbe9c652648728bbcb400627c14850f8ca0263bc176.
