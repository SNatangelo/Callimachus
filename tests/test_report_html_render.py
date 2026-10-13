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
    def test_sidebar_uses_embedded_logo_instead_of_text_wordmark(self):
        import base64
        import json

        from core.report.human.assets import default_asset_root

        root = default_asset_root()
        logo = (root / "logo.svg").read_text(encoding="utf-8")
        rendered = render_human_report(_projection())
        payload = rendered.body.split(
            '<script id="cv-data" type="application/json">', 1
        )[1].split("</script>", 1)[0]
        data = json.loads(payload)

        self.assertEqual(
            base64.b64decode(data["brand_logo"].removeprefix(
                "data:image/svg+xml;base64,"
            )).decode("utf-8"),
            logo,
        )
        self.assertIn("logo.svg", rendered.asset_sha256)
        self.assertIn("font-src data:", rendered.body)

        source = (root / "app.js").read_text(encoding="utf-8")
        styles = (root / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            'el("img", {class: "cv-brand-logo", src: state.brand_logo, '
            'alt: t("app.brand")})',
            source,
        )
        self.assertNotIn('el("strong", {}, t("app.brand"))', source)
        self.assertIn(".cv-brand-logo", styles)
        self.assertIn('el("button", {type: "button", class: "cv-brand", "aria-label": t("nav.overview")})', source)
        self.assertIn('brand.addEventListener("click", () => { setNavigationOpen(false); navigate("overview"); });', source)
        self.assertIn(".cv-brand:hover {", styles)
        self.assertIn(".cv-brand:active {", styles)

    def test_rendering_is_deterministic_and_embeds_all_assets(self):
        first = render_human_report(_projection())
        second = render_human_report(_projection())

        self.assertEqual(first, second)
        self.assertEqual(first.locale, "en")
        self.assertIn('"initial_locale":"en"', first.body)
        self.assertIn("application/json", first.body)
        self.assertIn("nav.overview", first.body)
        self.assertNotIn("src=", first.body)
        self.assertNotIn("<link", first.body)
        self.assertNotIn("@import", first.body)
        self.assertNotIn("fetch(", first.body)
        self.assertIn("locales/en.json", first.asset_sha256)
        self.assertIn("themes/high_contrast.json", first.asset_sha256)

    def test_method_and_configuration_is_only_rendered_by_its_own_route(self):
        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        overview = source.split("const overview = () => {", 1)[1].split(
            "const configuration = () => {", 1
        )[0]

        self.assertNotIn("configuration()", overview)
        self.assertIn("method: configuration", source)

    def test_elevated_issue_absence_copy_preserves_incomplete_identity_context(self):
        import json

        root = default_asset_root()
        source = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'concern.basis === "complete_issue_absence_with_incomplete_identity_checks"',
            source,
        )
        for language, phrase in (
            ("en", "identity checks remain incomplete"),
            ("it", "controlli d'identità sono incompleti"),
        ):
            catalog = json.loads(
                (root / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )
            copy = catalog["messages"][
                "attention.elevated_issue_absence_incomplete_checks_copy"
            ]
            self.assertIn(phrase, copy)
            self.assertIn("{resolver}", copy)
            self.assertIn("{coordinates}", copy)
            self.assertIn(
                "status.springer_complete_issue", catalog["messages"],
            )

    def test_review_label_copy_is_bilingual_and_non_diagnostic(self):
        import json

        root = default_asset_root()
        source = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn('label.code === "no_compatible_article_at_cited_coordinates"', source)
        self.assertIn('label.code === "incomplete_bibliographic_source"', source)
        for language, phrases in (
            ("en", ("independent identity searches", "not proof")),
            ("it", ("ricerche indipendenti dell'identità", "non prova")),
        ):
            messages = json.loads(
                (root / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )["messages"]
            for key in (
                "review_label.completed_searches_copy",
                "review_label.coordinate_copy",
            ):
                self.assertIn("{providers}", messages[key])
                self.assertTrue(any(phrase in messages[key] for phrase in phrases))
            self.assertIn("{resolver}", messages["review_label.coordinate_copy"])
            self.assertTrue(any(
                phrase in messages["review_label.incomplete_bibliographic_source_copy"]
                for phrase in ("not proof", "non prova")
            ))
            self.assertIn("not proof" if language == "en" else "non prova", messages["review_label.incomplete_bibliographic_source_copy"])

    def test_review_labels_have_clickable_metrics_filters_and_triage(self):
        import json

        root = default_asset_root()
        source = (root / "app.js").read_text(encoding="utf-8")
        for code in (
            "not_found_after_completed_searches",
            "no_compatible_article_at_cited_coordinates",
            "incomplete_bibliographic_source",
        ):
            self.assertIn(f'params: {{review: "{code}"}}', source)
            self.assertIn(f'{{value: "{code}", label: "status.{code}"}}', source)
        self.assertIn('name: "review", label: "filter.bibliographic_review", get: () => "", fixed: true', source)
        self.assertIn(
            "bibliographicReviewLabels(item).some(label => label.code === selected)",
            source,
        )
        self.assertIn("attention.bibliographic_review_title", source)
        self.assertIn("cv-review-label-badges", source)
        self.assertIn("!bibliographicReviewLabels(item).length", source)
        self.assertIn('label: "column.bibliographic_review", value: reviewLabelBadges', source)
        self.assertNotIn(
            'label: "column.bibliographic_review", value: item => bibliographicReviewLabels(item).map',
            source,
        )
        for language, expected in (
            ("en", "Bibliographic review"),
            ("it", "Revisione bibliografica"),
        ):
            messages = json.loads(
                (root / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )["messages"]
            self.assertEqual(messages["filter.bibliographic_review"], expected)
            self.assertIn("attention.bibliographic_review_title", messages)

    def test_completed_search_miss_badges_are_visually_prominent(self):
        styles = (default_asset_root() / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            ".cv-badge.cv-bibliographic_review_required, .cv-badge.cv-elevated_bibliographic_suspicion, .cv-badge.cv-not_found_after_completed_searches, .cv-badge.cv-no_compatible_article_at_cited_coordinates, .cv-badge.cv-not_found_in_completed_searches { background: var(--cv-warning-soft); border: 2px solid var(--cv-warning); color: var(--cv-negative); font-weight: 850; padding: 3px 8px; }",
            styles,
        )
        self.assertIn(
            ".cv-badge.cv-unresolved, .cv-badge.cv-no_outcome, .cv-badge.cv-non_decidable, .cv-badge.cv-incomplete_bibliographic_source, .cv-badge.cv-access_blocked_no_text, .cv-badge.cv-retrieval_blocked_identity_uncertain { background: var(--cv-purple-soft); color: var(--cv-purple); }",
            styles,
        )

    def test_review_labels_replace_the_empty_concern_summary(self):
        import json

        root = default_asset_root()
        source = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn("const bibliographicAssessmentBadge = item => {", source)
        self.assertIn('if (bibliographicReviewLabels(item).length) return badge("bibliographic_review_required")', source)
        self.assertIn('label: "column.bibliographic_concern", value: bibliographicAssessmentBadge', source)
        for language, assessment, evidence, empty in (
            ("en", "Bibliographic assessment", "Evidence requiring review", "No deterministic finding"),
            ("it", "Valutazione bibliografica", "Evidenze da riesaminare", "Nessun rilievo deterministico"),
        ):
            messages = json.loads(
                (root / "locales" / f"{language}.json").read_text(encoding="utf-8")
            )["messages"]
            self.assertEqual(messages["column.bibliographic_concern"], assessment)
            self.assertEqual(messages["column.bibliographic_review"], evidence)
            self.assertEqual(messages["status.none"], empty)
            self.assertIn("status.bibliographic_review_required", messages)

    def test_source_text_availability_uses_informational_no_text_states(self):
        import json

        root = default_asset_root()
        source = (root / "app.js").read_text(encoding="utf-8")
        styles = (root / "app.css").read_text(encoding="utf-8")
        self.assertIn("const sourceTextAvailability = item => {", source)
        self.assertIn('adjudication.outcome === "refuted"', source)
        self.assertIn('attempt.paywalled === true || attempt.challenge_blocked === true', source)
        self.assertIn('reviewCodes.has("not_found_after_completed_searches")', source)
        self.assertIn('label: "column.text_availability", value: sourceTextAvailabilityBadge', source)
        self.assertIn('["field.text_availability", sourceTextAvailabilityBadge(item)]', source)
        self.assertIn("cv-cited_reference_contradicted_no_text", styles)
        self.assertIn("cv-access_blocked_no_text", styles)
        self.assertIn("cv-known_work_no_text", styles)
        for language in ("en", "it"):
            messages = json.loads((root / "locales" / f"{language}.json").read_text(encoding="utf-8"))["messages"]
            self.assertEqual(
                messages["status.not_found_after_completed_searches"],
                messages["status.not_found_in_completed_searches"],
            )
            for availability in (
                "cited_reference_contradicted_no_text",
                "access_blocked_no_text",
                "retrieval_blocked_identity_uncertain",
                "known_work_no_text",
                "not_found_in_completed_searches",
                "identity_uncertain_no_text",
            ):
                self.assertIn(f"status.{availability}", messages)
                self.assertIn(f"availability.{availability}.detail", messages)

    def test_remediation_section_is_conditional_and_renders_auditable_labels(self):
        rendered = render_human_report(_projection())
        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        self.assertIn("if (!guidance.length && !interventions.length && !lineage) return null", source)
        self.assertIn("item.code, item.claim_id, item.ref_id, item.scope", source)
        self.assertIn("remediation.effect.${item.effect}", source)
        self.assertIn("remediation.admission.${item.admission_classification}", source)
        self.assertIn("remediation.intervention_binding", source)
        self.assertIn("task: item.task_id, answer: item.answer_id", source)
        self.assertIn("route.child_start_command", source)
        self.assertIn("cv-remediation", rendered.body)

    def test_selected_locale_theme_preview_and_hostile_data_are_safe(self):
        rendered = render_human_report(
            _projection("</script><img src=x onerror=alert(1)>"),
            locale="it",
            theme="high_contrast",
            preview=True,
            preview_failures=("gate_failed",),
        )

        self.assertEqual(rendered.locale, "it")
        self.assertEqual(rendered.theme, "high_contrast")
        self.assertIn('"initial_locale":"it"', rendered.body)
        self.assertIn('"initial_theme":"high_contrast"', rendered.body)
        self.assertIn('"preview":true', rendered.body)
        self.assertIn("gate_failed", rendered.body)
        self.assertNotIn("</script><img", rendered.body)
        self.assertIn("\\u003c/script\\u003e", rendered.body)

    def test_unknown_locale_or_theme_fails_closed(self):
        with self.assertRaises(ValueError):
            render_human_report(_projection(), locale="missing")
        with self.assertRaises(ValueError):
            render_human_report(_projection(), theme="missing")

    def test_renderer_rejects_a_manually_constructed_projection_with_local_paths(self):
        for path in (
            "/private/input.pdf", r"C:\private\input.pdf", "C:/private/input.pdf",
            r"\\host\share\input.pdf", "file:///private/input.pdf",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "local path"):
                render_human_report(_projection(path))

    def test_privacy_detects_posix_paths_after_diagnostic_punctuation(self):
        for value in (
            "diagnostic-/private/input.pdf",
            "x]/private/input.pdf",
            "x./private/input.pdf",
            "(/private/input.pdf",
            "`/private/input.pdf",
            "x{/private/input.pdf",
            "x}/private/input.pdf",
            "HTTP {success}/{calls}/private/input.pdf",
            "//private/input.pdf",
            "///private/input.pdf",
            "////host/share/private.pdf",
            "{///private/input.pdf}",
            "HTTP {success}/{calls}///private/input.pdf",
            "/",
        ):
            with self.subTest(value=value):
                self.assertTrue(contains_local_path(value))
                self.assertEqual(sanitize_local_paths(value), "[local path omitted]")

        for value in (
            "privacy and/or trade",
            "privacy and//or trade",
            "</section>",
            "HTTP 2xx {success}/{calls}",
            "sources/r1.txt",
            "https://example.test/public/path",
        ):
            with self.subTest(value=value):
                self.assertFalse(contains_local_path(value))
                self.assertEqual(sanitize_local_paths(value), value)

    def test_renderer_rejects_punctuation_prefixed_posix_paths(self):
        for value in (
            "diagnostic-/private/input.pdf",
            "x]/private/input.pdf",
            "x./private/input.pdf",
            "(/private/input.pdf",
            "`/private/input.pdf",
            "x{/private/input.pdf",
            "x}/private/input.pdf",
            "//private/input.pdf",
            "///private/input.pdf",
            "////host/share/private.pdf",
            "{///private/input.pdf}",
            "HTTP {success}/{calls}///private/input.pdf",
            "/",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "local path"):
                render_human_report(_projection(value))

    def test_renderer_omits_local_paths_from_preview_failures(self):
        rendered = render_human_report(
            _projection(),
            preview=True,
            preview_failures=(
                "Missing file `/private/input.pdf`",
                "https://example.test/public/path",
                "sources/r1.txt",
                "privacy and/or trade",
            ),
        )

        self.assertNotIn("/private/input.pdf", rendered.body)
        self.assertIn("[local path omitted]", rendered.body)
        self.assertIn("https://example.test/public/path", rendered.body)
        self.assertIn("sources/r1.txt", rendered.body)
        self.assertIn("privacy and/or trade", rendered.body)

    def test_client_source_never_uses_unsafe_dynamic_apis(self):
        from core.report.human.assets import default_asset_root

        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "insertAdjacentHTML", "eval(", "fetch(", "<script src", "<link"):
            self.assertNotIn(forbidden, source)
        self.assertIn("a11y.locale_selector", source)
        self.assertIn("document.documentElement.lang", source)
        self.assertIn("data.display_title", source)
        self.assertIn("heading.needs_review", source)
        self.assertIn("assessment.hard_findings", source)
        self.assertIn("assessment.minor_findings", source)
        self.assertIn("assessment.technical_findings", source)
        self.assertIn("sources_elevated_bibliographic_suspicion", source)
        self.assertIn("attention.reference_refuted_copy", source)
        self.assertIn("attention.refutation_complete_issue_copy", source)
        self.assertIn("attention.refutation_identifier_not_found_copy", source)
        self.assertIn("assessment.red.bibliographic_copy", source)
        self.assertIn("attention.high_suspicion_copy", source)
        self.assertIn("attention.elevated_suspicion_copy", source)
        self.assertIn('params: {concern: "strong"}', source)
        self.assertIn("execution.debug_badge", source)
        self.assertIn("cv-debug-banner", source)
        self.assertIn("cv-metric-link", source)
        self.assertIn("navigate(destination.view", source)
        self.assertNotIn('behavior: "smooth"', source)
        self.assertIn("showClaim", source)
        self.assertIn("showSource", source)
        self.assertIn("showPair", source)
        self.assertNotIn(" — ", source)
        self.assertEqual(source.count('class: "cv-eyebrow"'), 1)
        self.assertIn("const filterField", source)
        self.assertIn('class: "cv-filter-field"', source)
        self.assertIn("controls.append(filterField", source)
        self.assertNotIn("pairOutcome", source)
        self.assertIn("parser_sentence_index", source)
        self.assertIn("resolver_attempts", source)
        self.assertIn("fetch_attempts", source)
        self.assertIn("heading.models_configured", source)
        self.assertIn("heading.credentials", source)
        self.assertIn("field.credentials_present", source)
        self.assertIn("field.credentials_used", source)
        self.assertIn("credential.calls", source)
        self.assertIn("credential.channel.${channel}", source)
        self.assertIn('["resolve", "fetch", "search"]', source)
        self.assertIn("credential.warning", source)
        self.assertIn("heading.attempt_history", source)
        self.assertIn("attention.unresolved_title", source)
        self.assertIn("field.evidence_excerpts", source)
        self.assertIn("explanationParts", source)
        self.assertIn("cv-reason-block", source)
        self.assertIn("heading.source_comparison", source)
        self.assertIn("heading.parsed_search_input", source)
        self.assertIn("heading.resolver_output", source)
        self.assertIn("heading.acquired_text", source)
        self.assertIn("heading.jury2_rejected_attempts", source)
        self.assertIn("field.matched_authors", source)
        self.assertIn("field.matched_venue", source)
        self.assertIn("value.jury2_rejections", source)
        self.assertIn("throw new Error", source)
        self.assertIn("textContent", source)
        self.assertIn("claim.sentence", source)
        self.assertIn("reference.raw_entry", source)
        self.assertIn("verdict_provenance", source)
        self.assertIn("JSON.stringify(record", source)
        self.assertIn('class: "cv-audit"', source)
        self.assertIn("cv-sidebar", source)
        self.assertIn("cv-topbar", source)
        self.assertIn("cv-hero", source)
        self.assertIn("cv-drawer", source)
        self.assertIn("cv-audit", source)
        self.assertIn("assessmentState", source)
        self.assertIn("cv-assessment-", source)
        self.assertIn("summary.run_narrative", source)
        self.assertIn("summary.hero_claims", source)
        self.assertIn("attention.partial_title", source)
        self.assertIn("attention.unresolved_title", source)
        self.assertIn("attention.orphan_title", source)
        self.assertIn("attention.serious_source_title", source)
        self.assertIn("claimText", source)
        self.assertIn("sourceLabel", source)
        self.assertNotIn("cv-hero-score", source)
        self.assertNotIn("JSON.stringify(state.projection.diagnostics", source)
        self.assertNotIn("value: item => item.display_id", source)
        self.assertNotIn("value: item => item.claim_display_id", source)
        model_function = source.split("const modelsFor", 1)[1].split("const detailList", 1)[0]
        self.assertNotIn("dispatch_attempts", model_function)

    def test_reader_first_assets_have_matching_locales(self):
        from core.report.human.assets import default_asset_root
        import json
        import re

        root = default_asset_root()
        english = json.loads((root / "locales" / "en.json").read_text(encoding="utf-8"))
        italian = json.loads((root / "locales" / "it.json").read_text(encoding="utf-8"))
        source = (root / "app.js").read_text(encoding="utf-8")

        self.assertEqual(set(english["messages"]), set(italian["messages"]))
        literal_keys = set(re.findall(r'\bt\("([a-z][a-z0-9_.-]+)"', source))
        self.assertFalse(literal_keys - set(english["messages"]))
        for key in (
            "summary.hero_claims",
            "attention.unconfirmed_title",
            "attention.table_only_title",
            "regime.maximum",
            "heading.claim_considered",
            "heading.source_acquisition",
            "attention.jury1_guard",
            "attention.items_count",
            "attention.orphan_title",
            "attention.serious_source_title",
            "field.evidence_excerpts",
            "field.terminal_cause",
            "heading.attempt_history",
            "heading.credentials",
            "field.credentials_present",
            "field.credentials_used",
            "credential.calls",
            "credential.warning",
            "status.jury1_guard",
            "status.identifier_mismatch",
            "assessment.green.title",
            "assessment.yellow.title",
            "assessment.red.title",
            "assessment.minor_findings",
            "assessment.technical_findings",
            "execution.debug_badge",
            "execution.debug_title",
            "action.open_metric",
            "filter.identity_not_confirmed",
            "summary.run_narrative",
            "summary.outcome_supports",
            "summary.outcome_related",
            "attention.partial_title",
            "heading.model_timeline",
            "heading.rejected_proposals",
            "heading.jury2_rejected_attempts",
            "heading.audit",
            "heading.source_comparison",
            "heading.parsed_search_input",
            "heading.resolver_output",
            "heading.acquired_text",
            "field.parsed_first_author",
            "field.matched_authors",
            "field.matched_venue",
            "field.match_score",
            "field.title_overlap",
            "field.source_location",
            "value.first_author_only",
            "value.jury2_rejections",
        ):
            self.assertIn(key, english["messages"])

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

    def test_pair_reasons_reuse_the_overview_semantic_classes(self):
        from core.report.human.assets import default_asset_root

        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        show_pair = source.split("const explanationParts", 1)[1].split("const showClaim", 1)[0]

        self.assertIn(r"/\b(Unsupported|Supported|Reason):\s*/gi", show_pair)
        self.assertIn('reasonBlock("field.supported_content", supported, "supports")', show_pair)
        self.assertIn('reasonBlock("field.incompatible_proposition", unsupported, unsupportedTone)', show_pair)
        self.assertIn('reasonBlock("field.reason", reason, result)', show_pair)
        self.assertIn('class: `cv-card cv-verdict-card ${classFor(result)}`', show_pair)
        self.assertNotIn("evidenceEntries", show_pair)

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

    def test_source_drawer_keeps_parsed_resolved_and_acquired_facts_separate(self):
        from core.report.human.assets import default_asset_root

        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        show_source = source.split("const showSource", 1)[1].split("const table", 1)[0]

        self.assertIn('comparisonPanel("heading.parsed_search_input"', show_source)
        self.assertIn('["field.parsed_first_author", reference.ay_surname]', show_source)
        self.assertIn('comparisonPanel("heading.resolver_output"', show_source)
        self.assertIn('["field.matched_title", resolve.matched_title || candidate.title]', show_source)
        self.assertIn('["field.matched_authors", matchedAuthors]', show_source)
        self.assertIn('["field.matched_venue", match.matched_venue]', show_source)
        self.assertIn('comparisonPanel("heading.acquired_text"', show_source)
        acquired_panel = show_source.split('comparisonPanel("heading.acquired_text"', 1)[1].split('"cv-comparison-wide"', 1)[0]
        self.assertNotIn("resolve.matched_title", acquired_panel)
        self.assertNotIn("matchedAuthors", acquired_panel)
        self.assertNotIn("match.matched_venue", acquired_panel)
        self.assertIn('t("value.first_author_only"', show_source)
        self.assertIn('acquired.source_ref || acquired.source_url || acquired.url', show_source)
        self.assertNotIn("reference.authors || resolve.matched_authors", show_source)

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

    def test_identity_locked_drawer_renders_bibliographic_field_comparisons(self):
        projection = _projection()
        source = projection.value["sources"][0]
        source["reference"].update({
            "title": "Cited title", "year": 2020, "ay_surname": "Smith",
        })
        source["resolve"] = {
            "status": "resolved", "via": "crossref_metadata",
            "matched_title": "Matched title",
            "evidence_profile": {"bibliographic_adjudication": {
                "identity_status": "identified_with_errors",
                "refutations": [{"field": "issue", "source": "crossref_metadata"}],
            }},
            "metadata_match": {
                "title_overlap": 0.75, "year_match": False, "matched_year": 2021,
                "author_match": True, "cited_first_author": "Smith",
                "matched_first_author": "Smith",
                "coordinate_comparisons": [
                    {"kind": "issue", "cited_value": "1", "matched_value": "5", "status": "mismatch"},
                    {"kind": "article_page_range", "cited_value": "49-58", "matched_value": None, "status": "inconclusive"},
                ],
            },
        }

        rendered = render_human_report(projection).body
        app = (default_asset_root() / "app.js").read_text(encoding="utf-8")

        self.assertIn("Bibliographic field comparison", app)
        self.assertIn('comparison.status === "mismatch"', app)
        self.assertIn('const observedValue = observed === null || observed === undefined ? "missing from provider" : observed;', app)
        self.assertIn('"missing from provider"', app)
        self.assertIn('"unverified comparison"', app)
        self.assertIn('"title coverage"', app)
        self.assertNotIn("incomplete title", app.lower())
        self.assertIn('"identified_with_errors"', app)
        for value in ("Cited title", "Matched title", "49-58", "crossref_metadata"):
            self.assertIn(value, rendered)

    def test_overview_accounts_for_related_outcomes(self):
        from core.report.human.assets import default_asset_root

        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        outcome_order = '["supports", "partial", "unresolved", "contradicts", "related", "off_topic"]'

        self.assertEqual(source.count(outcome_order), 2)
        self.assertIn('params: {outcome: kind}', source)

    def test_pair_drawer_surfaces_each_rejected_jury2_cycle_with_jury1_outcome(self):
        from core.report.human.assets import default_asset_root

        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        show_pair = source.split("const showPair", 1)[1].split("const showClaim", 1)[0]

        self.assertIn("pair.rejected_jury2_attempts || []", show_pair)
        self.assertIn("item.jury1_outcome", show_pair)
        self.assertIn("item.proposal", show_pair)
        self.assertIn("review.event_type", show_pair)
        self.assertIn("review.reason", show_pair)
        self.assertIn('t("heading.jury2_rejected_attempts")', show_pair)
        self.assertLess(
            show_pair.index('t("heading.jury2_rejected_attempts")'),
            show_pair.index("const evidence = evidenceCard"),
        )
        pairs_view = source.split("const pairs =", 1)[1].split("const structured", 1)[0]
        self.assertIn("rejectedJury2Count(item)", pairs_view)
        self.assertIn('t("value.jury2_rejections"', pairs_view)

    def test_guard_terminated_pair_drawer_explains_rejected_proposals(self):
        from core.report.human.assets import default_asset_root

        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")
        show_pair = source.split("const showPair", 1)[1].split("const showClaim", 1)[0]

        self.assertIn('const finalizer = provenance.finalizer || {}, rejections = pair.jury1_rejections || [];', source)
        self.assertIn('const rejectedJury1Attempts = pair.rejected_jury1_attempts || [];', source)
        self.assertIn('const guardTerminated = rejectedJury1Attempts.length > 0', source)
        self.assertNotIn('else if (guardTerminated) drawer.append', show_pair)
        self.assertIn('const attemptsByRequest = new Map()', source)
        self.assertIn('rejectedJury1Attempts.forEach(item =>', source)
        self.assertIn('item.jury1_outcome', source)
        self.assertIn('attributedVerdict(item.jury1_outcome, models)', source)
        self.assertIn('["field.jury1_unaccepted_outcome", proposedOutcome]', source)
        self.assertIn('["field.rejection_cause", item.cause', source)
        self.assertIn('t("heading.rejected_proposals")', source)
        self.assertEqual(show_pair.count('t("attention.jury1_guard")'), 1)
        self.assertIn('guardTerminated ? "" : t("value.not_recorded")', show_pair)

    def test_offline_navigation_uses_fragment_routes_without_history_api(self):
        from core.report.human.assets import default_asset_root

        source = (default_asset_root() / "app.js").read_text(encoding="utf-8")

        self.assertIn("const readRoute", source)
        self.assertIn("const routeHash", source)
        self.assertIn("location.hash = nextHash", source)
        self.assertIn('setNavigationOpen(false); navigate(name)', source)
        self.assertIn("if (selected && !choices.has(selected))", source)
        self.assertNotIn("history.pushState", source)
        self.assertNotIn("history.replaceState", source)
        self.assertNotIn("location.pathname", source)

    def test_mobile_navigation_button_moves_into_open_sidebar_and_becomes_close_control(self):
        from core.report.human.assets import default_asset_root

        asset_root = default_asset_root()
        source = (asset_root / "app.js").read_text(encoding="utf-8")
        styles = (asset_root / "app.css").read_text(encoding="utf-8")

        self.assertIn('"aria-controls": "cv-sidebar", "aria-expanded": "false"', source)
        self.assertIn('const setNavigationOpen = open =>', source)
        self.assertIn('menu.textContent = open ? "×" : "☰"', source)
        self.assertIn('menu.setAttribute("aria-expanded", open ? "true" : "false")', source)
        self.assertIn('open ? t("a11y.close_navigation") : t("action.open_navigation")', source)
        self.assertIn('setNavigationOpen(false); navigate(name)', source)
        self.assertIn('.cv-menu-button { align-items: center; background: var(--cv-surface-bg); border: 1px solid var(--cv-border-strong);', styles)
        self.assertIn('.cv-shell.cv-nav-open .cv-menu-button { background: var(--cv-nav-bg-alt);', styles)
        self.assertIn('color: var(--cv-nav-text); left: calc(var(--cv-sidebar-width) - 54px);', styles)
