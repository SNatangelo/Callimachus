#!/usr/bin/env python3
# tests/test_report_html_render.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure rendering checks for the self-contained human HTML report."""

from tests._bootstrap import *  # noqa: F401,F403
from core.report.human import HumanReportProjection, render_human_report
from core.report.human.assets import default_asset_root
from core.report.human.privacy import contains_local_path, sanitize_local_paths
def _projection(text="A claim"):
    return HumanReportProjection({
        "schema_version": 1,
        "manuscript": {"title": "Test manuscript"},
        "execution": {"debug_mode": False, "debug_labels": []},
        "overview": {
            "claims_with_crediting_support": 1,
            "claims_total": 1,
            "references_cited": 1,
            "references_total": 1,
            "pairs_completed": 1,
            "pairs_total": 1,
            "pairs_by_semantic_outcome": {"supports": 1},
            "assessment": {
                "state": "green", "hard_findings": 0,
                "review_findings": 0, "minor_findings": 0,
                "technical_findings": 0, "reason_codes": [],
            },
        },
        "attention": [],
        "configuration": {"verify_runtime": {}},
        "models": {"by_provider_model_role": [], "totals": {}},
        "claims": [{"claim": {"id": "C1", "sentence": text, "context_window": "Context"}, "ref_ids": ["R1"], "pair_ids": []}],
        "sources": [{"reference": {"id": "R1", "ref_number": 1, "raw_entry": "Reference", "doi": "10.1000/example"}, "resolve": {"status": "resolved"}, "manifest_entries": [{"tier": "full_text", "selected_for_verification": True}], "claim_ids": ["C1"]}],
        "pairs": [{"claim_id": "C1", "ref_id": "R1", "verification": {"semantic_outcome": "supports", "assurance": "jury2_accepted"}, "dispatch_attempts": [{"provider_id": "provider", "model_id": "model"}]}],
        "diagnostics": {},
    })
class TestHumanHtmlRender(unittest.TestCase):
    def test_orphan_citations_are_distinguished_from_claim_outcome_and_other_verdicts(self):
        import json

        root = default_asset_root()
        source = (root / "app.js").read_text(encoding="utf-8")
        styles = (root / "app.css").read_text(encoding="utf-8")
        claims_view = source.split("const claims = () => {", 1)[1].split(
            "const sources = () => {", 1
        )[0]
        show_claim = source.split("const showClaim =", 1)[1].split(
            "const resolveText =", 1
        )[0]
        triage = source.split("const orphans = attention.filter", 1)[1].split(
            "const sourceAttention =", 1
        )[0]

        self.assertIn(
            "const isOrphanCitation = citation => citation && citation.ref_id == null",
            source,
        )
        self.assertIn(
            "const orphanCitations = item => (item.citations || []).filter(isOrphanCitation);",
            source,
        )
        self.assertIn('orphanCitations(item).length ? "orphan" : ""', claims_view)
        self.assertIn('badge(worst(byClaim.get(item.claim.id) || []))', claims_view)
        self.assertIn('orphanCitations(item).length ? badge("orphan") : null', claims_view)
        self.assertIn("const orphanCitationLabel = citation =>", source)
        self.assertIn("${surname[0].toLocaleUpperCase()}${surname.slice(1)} · ${year}", source)
        self.assertIn("if (orphans.length) drawer.append(orphanCard(orphans, pairs.length > 0));", show_claim)
        self.assertLess(
            show_claim.index("orphanCard(orphans, pairs.length > 0)"),
            show_claim.index('detailCard("heading.claim_considered"'),
        )
        self.assertIn('t("attention.orphan_marker_copy", {marker: citationMarker(citation)})', source)
        self.assertIn('t("attention.orphan_verdict_scope")', source)
        self.assertIn('hasLinkedPairs ? el("p", {class: "cv-orphan-verdict-scope"}', source)
        self.assertIn(
            't("attention.orphan_copy", {citation: orphanCitationLabel(citation)})',
            triage,
        )
        self.assertIn(".cv-badge.cv-orphan {", styles)
        self.assertIn(".cv-orphan-card {", styles)

        english = json.loads((root / "locales" / "en.json").read_text(encoding="utf-8"))["messages"]
        italian = json.loads((root / "locales" / "it.json").read_text(encoding="utf-8"))["messages"]
        self.assertEqual(set(english), set(italian))
        self.assertIn("{citation}", english["attention.orphan_copy"])
        self.assertIn("marker", english["attention.orphan_marker_copy"])
        self.assertIn("altre citazioni", italian["attention.orphan_verdict_scope"])


    def test_claim_pair_verdict_buttons_use_outcome_semantics_and_interaction_states(self):
        root = default_asset_root()
        source = (root / "app.js").read_text(encoding="utf-8")
        show_claim = source.split("const showClaim", 1)[1].split("const resolveText", 1)[0]
        styles = (root / "app.css").read_text(encoding="utf-8")

        self.assertIn(
            'class: `cv-structured-item cv-verdict-card cv-pair-verdict ${classFor(outcome(pair))}`',
            show_claim,
        )
        self.assertIn(
            ".cv-pair-verdict { appearance: none; background: var(--cv-semantic-soft, var(--cv-surface-bg));",
            styles,
        )
        self.assertIn(".cv-pair-verdict:hover {", styles)
        self.assertIn(".cv-pair-verdict:active {", styles)
        self.assertIn(".cv-pair-verdict:focus-visible { outline: 3px solid var(--cv-focus);", styles)
        self.assertIn(
            ".cv-supports { --cv-semantic-color: var(--cv-positive); --cv-semantic-soft: var(--cv-positive-soft); }",
            styles,
        )


    def test_operator_attestation_is_primary_identity_and_search_miss_remains_auditable(self):
        import json

        projection = _projection()
        source_item = projection.value["sources"][0]
        source_item["resolve"] = {
            "status": "not_found_after_completed_searches",
            "evidence_profile": {
                "bibliographic_adjudication": {
                    "identity_status": "not_identified",
                    "check_status": "complete",
                    "outcome": "not_corroborated",
                },
            },
        }
        source_item["bibliographic_review_labels"] = [{
            "code": "not_found_after_completed_searches",
            "providers": ["crossref"],
        }]
        source_item["manifest_entries"][0]["identity_status"] = "operator_attested"

        rendered = render_human_report(projection)
        payload = rendered.body.split(
            '<script id="cv-data" type="application/json">', 1
        )[1].split("</script>", 1)[0]
        data = json.loads(payload)
        projected_source = data["projection"]["sources"][0]
        app = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        styles = (default_asset_root() / "app.css").read_text(encoding="utf-8")
        show_source = app.split("const showSource", 1)[1].split("const table", 1)[0]

        self.assertEqual(
            projected_source["manifest_entries"][0]["identity_status"],
            "operator_attested",
        )
        self.assertEqual(
            projected_source["resolve"]["status"],
            "not_found_after_completed_searches",
        )
        self.assertEqual(
            projected_source["bibliographic_review_labels"][0]["code"],
            "not_found_after_completed_searches",
        )
        self.assertIn(
            'const operatorAttestedManifestEntry = item => (item.manifest_entries || []).find(entry => entry && entry.identity_status === "operator_attested");',
            app,
        )
        self.assertIn(
            'const sourceIdentityStatus = item => operatorAttestedManifestEntry(item) ? "operator_attested" : (item.resolve || {}).status;',
            app,
        )
        self.assertIn(
            'label: "column.identity", value: item => add(badge(sourceIdentityStatus(item)),',
            app,
        )
        self.assertIn('"field.identity", add(badge(resolve.status)', show_source)
        self.assertIn("reviewLabels.map(reviewLabelText)", show_source)
        self.assertIn(
            "audit({ref_id: item.reference.id, resolve, manifest_entries: item.manifest_entries, fetch_attempts: item.fetch_attempts})",
            show_source,
        )
        self.assertIn(
            ".cv-badge.cv-operator_attested { background: var(--cv-positive-soft); color: var(--cv-positive); }",
            styles,
        )

        for language, expected in (
            ("en", "Identity confirmed by operator"),
            ("it", "Identità confermata dall'operatore"),
        ):
            messages = json.loads(
                (default_asset_root() / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )["messages"]
            self.assertEqual(messages["status.operator_attested"], expected)


    def test_uncited_source_use_is_explicit_in_table_and_drawer(self):
        import json

        root = default_asset_root()
        script = (root / "app.js").read_text(encoding="utf-8")
        styles = (root / "app.css").read_text(encoding="utf-8")
        self.assertIn('new Set((state.projection.table_only_citations || []).map(item => item.ref_id))', script)
        self.assertIn('tableOnlySourceIds.has(item.reference.id) ? "table_only" : "uncited"', script)
        self.assertIn('const sourceClaimCount = item => (item.claim_ids || []).length', script)
        self.assertIn('el("span", {}, "0"), badge(sourceUsage(item))', script)
        self.assertIn('label: "column.source_count", value: sourceClaimCount', script)
        self.assertIn('get: sourceUsage, status: true', script)
        self.assertIn('usage === "table_only" ? t("usage.table_only_explanation")', script)
        self.assertIn('t("usage.uncited_explanation")', script)
        self.assertIn('.cv-badge.cv-uncited {', styles)
        self.assertIn('.cv-badge.cv-table_only {', styles)
        for language, text in (
            ("en", "No citation to this bibliography entry was detected"),
            ("it", "Nel manoscritto non è stata rilevata alcuna citazione"),
        ):
            messages = json.loads(
                (root / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )["messages"]
            self.assertIn(text, messages["usage.uncited_explanation"])
            self.assertIn("status.table_only", messages)
            self.assertIn("usage.table_only_explanation", messages)
