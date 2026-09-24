#!/usr/bin/env python3
# tests/test_run_report.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Run-state, report, and verification-gate tests."""
from ._bootstrap import *  # noqa: F401,F403
from core.invocation import run_prefix


class TestEffectiveStyleReporting(unittest.TestCase):
    def _run(self, tmp, style=None):
        from core.infra.db import RunRepository
        return RunRepository.create(
            tmp, run_id="style-test", input_path="paper.pdf", input_sha256="sha",
            accuracy="standard", style=style, model_id=None, http_profile="default",
            challenge_mode="off", fixture_fingerprint="fixture",
        )

    def test_runtime_style_is_used_for_style_projection(self):
        from core.report.io import _load_style_projection
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._run(tmp)
            repo.replace_parse_payload(claims=[], citations=[], references=[
                {"id": "r1", "ref_number": 1, "raw_entry": "Smith, J. (2020). Title."}
            ])
            repo.set_run_setting("style", "apa7")
            repo.close()
            projection = _load_style_projection(tmp, {"references": [
                {"id": "r1", "ref_number": 1, "raw_entry": "Smith, J. (2020). Title."}
            ]})
            self.assertEqual(projection["style"], "apa7")
            self.assertEqual(len(projection["checks"]), 1)

    def test_initial_style_is_fallback_without_runtime_style(self):
        from core.report.io import _load_style_projection
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._run(tmp, style="apa7")
            repo.close()
            projection = _load_style_projection(tmp, {"references": []})
            self.assertEqual(projection["style"], "apa7")

    def test_runtime_style_is_part_of_db_projection_hash(self):
        from core.report.io import _db_projection_sha256
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._run(tmp)
            repo.close()
            first = _db_projection_sha256(tmp)
            repo = RunRepository.open(tmp)
            repo.set_run_setting("style", "apa7")
            repo.close()
            self.assertNotEqual(first, _db_projection_sha256(tmp))


class TestEvidenceExcerptRendering(unittest.TestCase):
    def test_evidence_excerpt_is_markdown_safe_and_deterministic(self):
        import importlib

        renderer = importlib.import_module("core.report.render")
        evidence = (
            "# heading\n"
            "- list item with **bold**, `code`, | pipe, and [link]\n"
            "Citation ⟦SUP:2⟧ and range ⟦SUP:11,13–15⟧. "
            + "long evidence text " * 20
        )

        first = renderer._display_evidence_excerpt(evidence)
        second = renderer._display_evidence_excerpt(evidence)
        report_line = f"        - ✓ «{first}»"

        self.assertEqual(first, second)
        self.assertNotIn("\n", first)
        self.assertNotIn("⟦SUP:", first)
        self.assertIn("\\# heading", first)
        self.assertIn("\\- list item", first)
        self.assertIn("\\*\\*bold\\*\\*", first)
        self.assertIn("\\[2\\]", first)
        self.assertIn("\\[11,13–15\\]", first)
        self.assertNotIn("\n# ", report_line)
        self.assertTrue(first.endswith("…"))


class TestVerifyRuntimeReportLabel(unittest.TestCase):
    def test_observed_dispatch_model_overrides_stale_runtime_fallback(self):
        import importlib

        renderer = importlib.import_module("core.report.render")
        labels = renderer._verify_runtime_display_labels(
            {"backend": "openai_compat", "model": "qwen3.5:9b"},
            {
                "by_provider_model_role": [
                    {"provider": "openai_compat", "model": "deepseek-v4-flash", "attempts": 2}
                ]
            },
        )

        self.assertEqual(labels, ("openai_compat", "deepseek-v4-flash"))
        self.assertNotIn("qwen3.5:9b", labels)

    def test_runtime_label_falls_back_when_no_dispatch_was_observed(self):
        import importlib

        renderer = importlib.import_module("core.report.render")
        self.assertEqual(
            renderer._verify_runtime_display_labels(
                {"backend": "openai_compat", "model": "qwen3.5:9b"},
                {"by_provider_model_role": []},
            ),
            ("openai_compat", "qwen3.5:9b"),
        )


class TestExternalSourceIdentityReportLabel(unittest.TestCase):
    def test_exact_acl_identity_gets_specific_external_label(self):
        import importlib

        renderer = importlib.import_module("core.report.render")
        label = renderer._external_identity_label(
            [{"identity_status": "exact_acl_id_confirmed"}]
        )

        self.assertEqual(label, "externally corroborated via exact ACL source")

    def test_non_exact_identity_keeps_existing_unverified_fallback(self):
        import importlib

        renderer = importlib.import_module("core.report.render")
        self.assertIsNone(
            renderer._external_identity_label(
                [{"identity_status": "externally_corroborated_text"}]
            )
        )

    def test_exact_acl_identity_replaces_stale_existence_check_reason(self):
        import importlib

        renderer = importlib.import_module("core.report.render")
        stale_reason = "no identifier (DOI/PMID/ISBN/URL): search online to verify"
        label = "externally corroborated via exact ACL source"

        self.assertEqual(
            renderer._existence_check_reason(
                status="unverified",
                reason=stale_reason,
                external_label=label,
            ),
            label,
        )
        self.assertEqual(
            renderer._existence_check_reason(
                status="unverified",
                reason=stale_reason,
                external_label=None,
            ),
            stale_reason,
        )

    def test_exact_acl_identity_replaces_only_the_stale_claim_issue(self):
        import importlib

        renderer = importlib.import_module("core.report.render")
        label = "externally corroborated via exact ACL source"
        issue = renderer._claim_issue_with_external_identity(
            "bibliographic identity not automatically confirmed",
            [{"ref_id": "r39"}],
            {"r39": {"status": "unverified"}},
            {"r39": label},
        )
        mixed_issue = renderer._claim_issue_with_external_identity(
            "bibliographic identity not automatically confirmed",
            [{"ref_id": "r39"}, {"ref_id": "r10"}],
            {"r39": {"status": "unverified"}, "r10": {"status": "unverified"}},
            {"r39": label},
        )

        self.assertEqual(issue, label)
        self.assertEqual(
            mixed_issue, "bibliographic identity not automatically confirmed"
        )


class TestGapsAccuracy(unittest.TestCase):
    """Accuracy grade -> floor tier + chase_fulltext, plus the network_blocked guard."""

    def _run_gaps(self, refs, resolves, manifest_entries, accuracy):
        import shutil
        from unittest import mock
        from core.fetch.diagnostics import gaps
        from core.infra.db import RunRepository
        run = tempfile.mkdtemp()
        try:
            repo = RunRepository.create(
                run,
                run_id="run-gaps-test",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy=accuracy,
                style="vancouver",
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.replace_parse_payload(claims=[], references=refs, citations=[])
            for rid, body in resolves.items():
                repo.upsert_resolve_result(rid, {"ref_id": rid, **body})
            for idx, entry in enumerate(manifest_entries, start=1):
                repo.store_source_text(
                    source_text_id=f"src-gap-{idx}",
                    ref_id=entry["ref_id"],
                    identity_key=f"path:{entry['ref_id']}:{entry['tier']}:{idx}",
                    tier=entry["tier"],
                    origin=entry.get("origin", "user"),
                    stored_path=f"sources/parsed/{entry['ref_id']}_{entry['tier']}_{idx}.txt",
                    sha256=f"sha-{idx}",
                    char_count=10,
                    source_ref=entry.get("source_ref"),
                    mapping=entry.get("mapping"),
                    match_signal=entry.get("match_signal"),
                    match_score=entry.get("match_score"),
                    identity_status=entry.get("identity_status"),
                    identity_note=entry.get("identity_note"),
                    content_version=entry.get("content_version"),
                    provenance_relation=entry.get("provenance_relation"),
                )
            repo.close()
            return gaps.build_gap_report(run, accuracy=accuracy)
        finally:
            shutil.rmtree(run)

    # r1: paywalled article, full text EXISTS, only abstract available
    # r2: conference abstract (full text does NOT exist) -> abstract is the ceiling
    REFS = [{"id": "r1", "ref_number": 1}, {"id": "r2", "ref_number": 2}]
    RESOLVES = {
        "r1": {"status": "resolved", "fulltext_exists": True, "oa_status": "paywalled"},
        "r2": {"status": "resolved", "fulltext_exists": False},
    }
    MANIFEST = [{"ref_id": "r1", "tier": "abstract", "origin": "crossref"},
                {"ref_id": "r2", "tier": "abstract", "origin": "crossref"}]

    def test_maximum_never_settles_for_abstract(self):
        out = self._run_gaps(self.REFS, self.RESOLVES, self.MANIFEST, "maximum")
        self.assertEqual(len(out["ready_fulltext"]), 0)   # abstracts rejected at maximum
        self.assertEqual(len(out["gaps"]), 2)

    def test_standard_chases_fulltext_but_keeps_ceiling(self):
        out = self._run_gaps(self.REFS, self.RESOLVES, self.MANIFEST, "standard")
        ready_ids = {r["ref_id"] for r in out["ready_fulltext"]}
        gap_ids = {g["ref_id"] for g in out["gaps"]}
        self.assertEqual(ready_ids, {"r2"})   # ceiling only when no fuller text exists
        self.assertEqual(gap_ids, {"r1"})     # paywalled abstract remains fetch-upgradable
        self.assertEqual(out["summary"]["ready_paywalled_abstract_fallbacks"], 1)

    def test_abstract_grade_accepts_abstract_as_final(self):
        out = self._run_gaps(self.REFS, self.RESOLVES, self.MANIFEST, "abstract")
        self.assertEqual(len(out["ready_fulltext"]), 2)   # both accepted, not chased
        self.assertEqual(len(out["gaps"]), 0)
        self.assertFalse(out["summary"]["chase_fulltext"])

    def test_network_blocked_when_nothing_usable(self):
        refs = [{"id": "r1", "ref_number": 1}, {"id": "r2", "ref_number": 2}]
        resolves = {"r1": {"status": "unresolved", "reason": "network_error"},
                    "r2": {"status": "unresolved", "reason": "network_error"}}
        out = self._run_gaps(refs, resolves, [], "standard")
        self.assertTrue(out["network_blocked"])
        self.assertEqual(out["summary"]["unresolved_network"], 2)

    def test_not_blocked_when_one_fulltext_present(self):
        refs = [{"id": "r1", "ref_number": 1}, {"id": "r2", "ref_number": 2}]
        resolves = {"r1": {"status": "resolved"},
                    "r2": {"status": "unresolved", "reason": "network_error"}}
        manifest = [{"ref_id": "r1", "tier": "fulltext", "origin": "user"}]
        out = self._run_gaps(refs, resolves, manifest, "standard")
        self.assertFalse(out["network_blocked"])   # one usable source -> proceed

    # r3: full-text EXISTENCE unknown, only the abstract available (the case where the
    # 'maximum_fallback' grade differs from strict 'maximum').
    REFS_UNKNOWN = [{"id": "r3", "ref_number": 3}]
    RESOLVES_UNKNOWN = {"r3": {"status": "resolved", "fulltext_exists": "unknown"}}
    MANIFEST_UNKNOWN = [{"ref_id": "r3", "tier": "abstract", "origin": "crossref"}]

    def test_maximum_unknown_stays_hard_gap_no_provisional(self):
        out = self._run_gaps(self.REFS_UNKNOWN, self.RESOLVES_UNKNOWN,
                             self.MANIFEST_UNKNOWN, "maximum")
        self.assertEqual(len(out["ready_fulltext"]), 0)
        g = out["gaps"][0]
        self.assertFalse(g["provisional"])
        self.assertIsNone(g["suggested_tier"])
        self.assertEqual(out["summary"]["provisional_gaps"], 0)

    def test_maximum_fallback_unknown_is_provisional_abstract(self):
        out = self._run_gaps(self.REFS_UNKNOWN, self.RESOLVES_UNKNOWN,
                             self.MANIFEST_UNKNOWN, "maximum_fallback")
        g = out["gaps"][0]
        self.assertTrue(g["provisional"])
        self.assertEqual(g["suggested_tier"], "abstract")
        self.assertIn("PROVISIONALLY", g["needs"])
        self.assertEqual(out["summary"]["provisional_gaps"], 1)
        self.assertTrue(out["summary"]["provisional_on_unknown"])

    def test_maximum_fallback_known_fulltext_stays_strict(self):
        # fulltext_exists True: a fuller text is KNOWN to exist -> no provisional abstract,
        # even under maximum_fallback (the strict rule still applies).
        refs = [{"id": "r1", "ref_number": 1}]
        resolves = {"r1": {"status": "resolved", "fulltext_exists": True,
                           "oa_status": "paywalled"}}
        manifest = [{"ref_id": "r1", "tier": "abstract", "origin": "crossref"}]
        out = self._run_gaps(refs, resolves, manifest, "maximum_fallback")
        g = out["gaps"][0]
        self.assertFalse(g["provisional"])
        self.assertIsNone(g["suggested_tier"])

    # rp: full text retrieved, but it is a PREPRINT (not the version of record).
    REFS_PREPRINT = [{"id": "rp", "ref_number": 5}]
    RESOLVES_PREPRINT = {"rp": {"status": "resolved", "fulltext_exists": True}}
    MANIFEST_PREPRINT = [{"ref_id": "rp", "tier": "fulltext", "origin": "openalex",
                          "content_version": "preprint"}]
    MANIFEST_PREPRINT_OFFICIAL = [{
        "ref_id": "rp",
        "tier": "fulltext",
        "origin": "openalex",
        "content_version": "preprint",
        "provenance_relation": sources.OFFICIALLY_SURFACED_COPY,
    }]

    def test_preprint_fulltext_does_not_close_gap_at_maximum(self):
        # maximum demands the version of record: a preprint full text is NOT enough.
        out = self._run_gaps(self.REFS_PREPRINT, self.RESOLVES_PREPRINT,
                             self.MANIFEST_PREPRINT, "maximum")
        self.assertEqual(len(out["ready_fulltext"]), 0)
        g = out["gaps"][0]
        self.assertTrue(g["provisional"])
        self.assertTrue(g["has_preprint_fulltext"])
        self.assertEqual(g["content_version"], "preprint")
        self.assertEqual(g["suggested_tier"], "fulltext")
        self.assertIn("PROVISIONALLY", g["needs"])
        self.assertEqual(out["summary"]["preprint_only_fulltext"], 1)

    def test_preprint_fulltext_ready_but_flagged_at_standard(self):
        # standard accepts the preprint as a fallback, but flags it provisional/never-green.
        out = self._run_gaps(self.REFS_PREPRINT, self.RESOLVES_PREPRINT,
                             self.MANIFEST_PREPRINT, "standard")
        self.assertEqual(len(out["gaps"]), 0)
        r = out["ready_fulltext"][0]
        self.assertEqual(r["content_version"], "preprint")
        self.assertTrue(r["provisional"])
        self.assertEqual(out["summary"]["preprint_only_fulltext"], 1)

    def test_officially_surfaced_preprint_uses_citation_path_wording(self):
        out = self._run_gaps(
            self.REFS_PREPRINT,
            self.RESOLVES_PREPRINT,
            self.MANIFEST_PREPRINT_OFFICIAL,
            "standard",
        )
        self.assertEqual(len(out["gaps"]), 0)
        r = out["ready_fulltext"][0]
        self.assertEqual(r["provenance_relation"], sources.OFFICIALLY_SURFACED_COPY)
        self.assertIn("official citation-path copy", r["note"])

    def test_preprint_counts_as_usable_for_network_guard(self):
        # A preprint full text IS usable text: the run is not network-blocked.
        refs = [{"id": "rp", "ref_number": 5}, {"id": "r2", "ref_number": 2}]
        resolves = {"rp": {"status": "resolved", "fulltext_exists": True},
                    "r2": {"status": "unresolved", "reason": "network_error"}}
        out = self._run_gaps(refs, resolves, self.MANIFEST_PREPRINT, "maximum")
        self.assertFalse(out["network_blocked"])

    def test_published_fulltext_closes_gap_normally(self):
        # Control: a normal (version-of-record) full text still closes the gap at maximum.
        manifest = [{"ref_id": "rp", "tier": "fulltext", "origin": "openalex"}]
        out = self._run_gaps(self.REFS_PREPRINT, self.RESOLVES_PREPRINT, manifest, "maximum")
        self.assertEqual(len(out["gaps"]), 0)
        self.assertEqual(out["ready_fulltext"][0]["ref_id"], "rp")
        self.assertEqual(out["summary"]["preprint_only_fulltext"], 0)

    def test_gap_records_explicit_abstract_status(self):
        out = self._run_gaps(self.REFS, self.RESOLVES, self.MANIFEST, "standard")
        self.assertEqual(out["summary"]["gaps_with_abstract_available"], 1)
        self.assertEqual(out["summary"]["gaps_with_abstract_absent"], 0)

    def test_gaps_can_read_db_without_file_inputs(self):
        from unittest import mock
        from core.fetch.diagnostics import gaps
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as run:
            repo = RunRepository.create(
                run,
                run_id="run-gaps-db-only",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id="model-x",
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.replace_parse_payload(
                claims=[],
                references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. Example. 2020."}],
                citations=[],
            )
            repo.upsert_resolve_result("r1", {
                "ref_id": "r1",
                "status": "resolved",
                "fulltext_exists": True,
                "oa_status": "paywalled",
                "abstract": "Example abstract",
            })
            repo.store_source_text(
                source_text_id="src-r1-abs",
                ref_id="r1",
                identity_key="path:parsed/1_abstract_openalex.txt",
                tier="abstract",
                origin="openalex",
                stored_path="sources/parsed/1_abstract_openalex.txt",
                sha256="sha",
                char_count=20,
            )
            repo.close()
            out = gaps.build_gap_report(run, accuracy="standard")
            self.assertFalse(os.path.exists(os.path.join(run, "gaps.json")))
            self.assertEqual(out["summary"]["references"], 1)
            self.assertEqual(out["summary"]["ready_paywalled_abstract_fallbacks"], 1)

class TestBenchmark(unittest.TestCase):
    """Cross-LLM / cross-run concordance from the append-only ledger (no gold set)."""

    def _row(self, run, model, claim, ref, outcome, *, crediting=True,
             scope="fulltext_complete"):
        return {"run_id": run, "model_id": model, "claim_id": claim, "ref_id": ref,
                "scope": scope, "outcome": outcome, "crediting": crediting}

    def _debug_run(self, root, name, *, model="model-a", snapshot="snapshot-a",
                   input_sha256="input-a", verify_table_citations=False,
                   execution_assurance=None):
        from core.infra.db import RunRepository
        from core.verify.claim_evidence.config import (
            GENERIC_MODEL_ENV,
            ProviderEnvSpec,
            resolve_config,
        )

        import hashlib

        run_dir = os.path.join(root, name)
        repo = RunRepository.create(
            run_dir, run_id=name, input_path="paper.pdf", input_sha256=input_sha256,
            accuracy="standard", style=None, model_id=model, http_profile="default",
            challenge_mode="off", fixture_fingerprint="fixture-a",
            execution_assurance=execution_assurance,
        )
        try:
            repo.replace_parse_payload(
                manuscript_text="Complete manuscript text.",
                claims=[{
                    "id": "c1",
                    "sentence": "Claim [1].",
                    "context_window": "Benchmark manuscript context.",
                    "marker_raw": "[1]",
                }],
                references=[{"id": "r1", "ref_number": 1, "raw_entry": "Reference."}],
                citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
            )
            repo.ensure_verification_pair(
                claim_id="c1", ref_id="r1", scope="fulltext_complete")
            source_text = "Claim evidence from a run-local source."
            source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            source_rel = "sources/benchmark-claim-evidence.txt"
            source_path = os.path.join(run_dir, source_rel)
            os.makedirs(os.path.dirname(source_path), exist_ok=True)
            with open(source_path, "w", encoding="utf-8") as fh:
                fh.write(source_text)
            repo.store_source_text(
                source_text_id="benchmark-claim-source", ref_id="r1",
                identity_key="benchmark-claim-source", tier="fulltext",
                origin="fixture", stored_path=source_rel, sha256=source_sha256,
                char_count=len(source_text),
            )
            from core.app.phases.verify import _claim_evidence_task
            task_payload = _claim_evidence_task(
                {
                    "id": "c1",
                    "sentence": "Claim [1].",
                    "context_window": "Benchmark manuscript context.",
                    "marker_raw": "[1]",
                },
                {"id": "r1", "ref_number": 1, "raw_entry": "Reference."},
                "fulltext_complete", source_path,
                source_text_id="benchmark-claim-source",
                source_text_sha256=source_sha256,
            )
            repo.create_task(
                task_id=f"verify-{name}", slot="verify", claim_id="c1",
                ref_id="r1", scope="fulltext_complete",
                task_payload=task_payload,
            )
            config = resolve_config({
                "CITATION_VERIFIER_VERIFY_BACKENDS": "backend-a",
                GENERIC_MODEL_ENV: model,
                "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
                "CITATION_VERIFIER_VERIFY_SELECTION_SEED": "benchmark-seed",
            }, [ProviderEnvSpec(
                "backend-a", "", GENERIC_MODEL_ENV, credentialless=True,
            )], run_id=name).snapshot()
            code_snapshot = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
            frozen_inventory = [{
                "ref_id": "r1",
                "tier": "fulltext",
                "origin": "fixture",
                "stored_path": source_rel,
                "source_ref": None,
                "sha256": source_sha256,
                "char_count": len(source_text),
            }]
            frozen_inventory_sha256 = hashlib.sha256(json.dumps(
                frozen_inventory, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            repo.update_run_settings({
                "debug_mode": True,
                "debug_labels": ["fork:frozen_fetch_verify"],
                "verify_table_citations": verify_table_citations,
                "frozen_fetch_provenance": {
                    "baseline_run_id": name + "-baseline",
                    "baseline_run_dir": run_dir,
                    "baseline_code_revision": "a" * 40,
                    "source_inventory": frozen_inventory,
                    "source_inventory_sha256": frozen_inventory_sha256,
                    "copied_assets": [source_rel],
                    "baseline_verified_clean": True,
                    "fork_code_revision": "a" * 40,
                    "fork_code_dirty": True,
                    "fork_code_diff_sha256": hashlib.sha256(
                        ("diff:" + snapshot).encode("utf-8")
                    ).hexdigest(),
                    "fork_code_snapshot_id": code_snapshot,
                },
                "verify_runtime": {
                    "code_revision": "a" * 40,
                    "code_dirty": True,
                    "code_diff_sha256": hashlib.sha256(
                        ("diff:" + snapshot).encode("utf-8")
                    ).hexdigest(),
                    "code_snapshot_id": code_snapshot,
                    "backend": "backend-a",
                    "model": model,
                    "require_fulltext": True,
                    "reasoning": "reasoning-a",
                    "reasoning_effort": "high",
                    "context_profile": "large",
                    "max_source_chars": "",
                    "semantic_contract": "verify-claim-evidence-v10",
                },
                "verify_claim_evidence_config": config,
            })
            self.assertTrue(repo.claim_verification_pair_terminal(
                claim_id="c1", ref_id="r1", scope="fulltext_complete",
                status="exhausted", outcome=None, cause="jury1_technical",
            ))
        finally:
            repo.close()
        return run_dir

    def test_debug_compare_is_opt_in_and_rejects_non_debug_run(self):
        from core.app.commands import benchmark as bm
        from core.infra.db import RunRepository

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._debug_run(tmp, "run-a")
            repo = RunRepository.open(run_dir)
            try:
                repo.set_run_setting("debug_mode", False)
            finally:
                repo.close()
            rows = bm.load_rows([run_dir])
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["crediting"])
            self.assertNotIn("attempt", rows[0])
            self.assertNotIn("passage_checks", rows[0])
            with self.assertRaisesRegex(ValueError, "debug_mode=true"):
                bm._debug_comparison_preflight([run_dir])

    def test_report_phase_preserves_claim_evidence_source_text(self):
        from unittest import mock
        from core.app.phases import report_gate
        from core.infra.db import RunRepository

        with tempfile.TemporaryDirectory() as tmp:
            run = self._debug_run(tmp, "run-report")
            repo = RunRepository.open(run)
            try:
                source_ids_before = [
                    source.source_text_id for source in repo.list_source_texts()
                ]
            finally:
                repo.close()

            gate_result = {"ok": True, "failures": [], "warnings": [], "info": {}}
            integrity = {
                "run": {"state": "clean", "audit_ready": True, "overrides": []},
                "content_store": {
                    "state": "clean",
                    "audit_ready": True,
                    "overrides": [],
                },
                "debug_mode": False,
                "debug_labels": [],
                "audit_ready": True,
            }
            report_result = {
                "audit_ready": True,
                "debug_mode": False,
                "integrity_run_state": "clean",
                "integrity_content_store_state": "clean",
            }
            integrity_gate = mock.Mock()
            with (
                mock.patch.object(
                    report_gate.RunIntegrityGate,
                    "from_environment",
                    return_value=integrity_gate,
                ),
                mock.patch.object(
                    report_gate,
                    "trusted_report_integrity",
                    return_value=integrity,
                ),
                mock.patch.object(
                    report_gate,
                    "write_report",
                    return_value=report_result,
                ),
                mock.patch.object(report_gate.verify_run, "verify", return_value=gate_result),
                mock.patch.object(report_gate.verify_run, "write_signature_status"),
                mock.patch.object(report_gate, "load_run_projection", return_value={}),
                mock.patch.object(report_gate, "build_human_report_projection", return_value=mock.sentinel.projection),
                mock.patch.object(report_gate, "render_human_report", return_value=mock.sentinel.rendered),
                mock.patch.object(report_gate, "write_html_report") as write_html,
                mock.patch.dict(os.environ, {report_gate.ENV_REPORT_HTML: "1"}),
            ):
                self.assertEqual(report_gate.phase_report({"run_dir": run}), "done")
            integrity_gate.begin_pipeline_transition.assert_not_called()
            write_html.assert_called_once_with(run, rendered=mock.sentinel.rendered, gate=None)

            repo = RunRepository.open(run)
            try:
                self.assertEqual(
                    [source.source_text_id for source in repo.list_source_texts()],
                    source_ids_before,
                )
            finally:
                repo.close()

    def test_report_phase_html_default_on_and_explicit_off(self):
        from unittest import mock
        from core.app.phases import report_gate

        result = {
            "audit_ready": True,
            "debug_mode": False,
            "integrity_run_state": "clean",
            "integrity_content_store_state": "clean",
        }
        gate_result = {"ok": True, "failures": [], "warnings": [], "info": {}}
        common = (
            mock.patch.object(report_gate, "write_report", return_value=result),
            mock.patch.object(report_gate.verify_run, "verify", return_value=gate_result),
            mock.patch.object(report_gate.verify_run, "write_signature_status"),
            mock.patch.object(report_gate, "load_run_projection", return_value={}),
            mock.patch.object(report_gate, "build_human_report_projection", return_value=mock.sentinel.projection),
            mock.patch.object(report_gate, "render_human_report", return_value=mock.sentinel.rendered),
        )
        with tempfile.TemporaryDirectory() as run, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
            os.environ.pop(report_gate.ENV_REPORT_HTML, None)
            for patch in common:
                stack.enter_context(patch)
            writer = stack.enter_context(mock.patch.object(report_gate, "write_html_report"))
            self.assertEqual(report_gate.phase_report({"run_dir": run}), "done")
            writer.assert_called_once_with(run, rendered=mock.sentinel.rendered, gate=None)

        with tempfile.TemporaryDirectory() as run, mock.patch.dict(
            os.environ, {report_gate.ENV_REPORT_HTML: "off"}
        ), contextlib.ExitStack() as stack:
            for patch in common:
                stack.enter_context(patch)
            writer = stack.enter_context(mock.patch.object(report_gate, "write_html_report"))
            self.assertEqual(report_gate.phase_report({"run_dir": run}), "done")
            writer.assert_not_called()

    def test_report_html_environment_contract(self):
        from core.app.phases import report_gate

        for value in (None, "", "1", "TRUE", "yes", "On"):
            with self.subTest(value=value):
                environment = {} if value is None else {report_gate.ENV_REPORT_HTML: value}
                self.assertTrue(report_gate._report_html_enabled(environment))
        for value in ("0", "FALSE", "no", "Off"):
            with self.subTest(value=value):
                self.assertFalse(report_gate._report_html_enabled({
                    report_gate.ENV_REPORT_HTML: value,
                }))
        with self.assertRaisesRegex(ValueError, report_gate.ENV_REPORT_HTML):
            report_gate._report_html_enabled({report_gate.ENV_REPORT_HTML: "sometimes"})

    def test_report_phase_never_generates_html_when_gate_fails(self):
        from unittest import mock
        from core.app.phases import report_gate

        result = {
            "audit_ready": True, "debug_mode": False,
            "integrity_run_state": "clean", "integrity_content_store_state": "clean",
        }
        gate_result = {"ok": False, "failures": ["incomplete"], "warnings": [], "info": {}}
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(
            os.environ, {report_gate.ENV_REPORT_HTML: "1"}
        ), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(report_gate, "write_report", return_value=result))
            stack.enter_context(mock.patch.object(report_gate.verify_run, "verify", return_value=gate_result))
            stack.enter_context(mock.patch.object(report_gate.verify_run, "write_signature_status"))
            writer = stack.enter_context(mock.patch.object(report_gate, "write_html_report"))
            stack.enter_context(mock.patch.object(report_gate, "_save_state"))
            self.assertEqual(report_gate.phase_report({"run_dir": run}), "_gate_failed")
            writer.assert_not_called()

    def test_report_phase_html_config_and_writer_failure_return_error(self):
        from unittest import mock
        from core.app.phases import report_gate

        result = {
            "audit_ready": True, "debug_mode": False,
            "integrity_run_state": "clean", "integrity_content_store_state": "clean",
        }
        gate_result = {"ok": True, "failures": [], "warnings": [], "info": {}}
        common = (
            mock.patch.object(report_gate, "write_report", return_value=result),
            mock.patch.object(report_gate.verify_run, "verify", return_value=gate_result),
            mock.patch.object(report_gate.verify_run, "write_signature_status"),
        )
        with tempfile.TemporaryDirectory() as run, mock.patch.dict(
            os.environ, {report_gate.ENV_REPORT_HTML: "sometimes"}
        ), contextlib.ExitStack() as stack:
            for patch in common:
                stack.enter_context(patch)
            self.assertEqual(report_gate.phase_report({"run_dir": run}), "error")

        with tempfile.TemporaryDirectory() as run, mock.patch.dict(
            os.environ, {report_gate.ENV_REPORT_HTML: "1"}
        ), contextlib.ExitStack() as stack:
            for patch in common:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(report_gate, "load_run_projection", return_value={}))
            stack.enter_context(mock.patch.object(report_gate, "build_human_report_projection", return_value=mock.sentinel.projection))
            stack.enter_context(mock.patch.object(report_gate, "render_human_report", return_value=mock.sentinel.rendered))
            stack.enter_context(mock.patch.object(report_gate, "write_html_report", side_effect=OSError("disk full")))
            self.assertEqual(report_gate.phase_report({"run_dir": run}), "error")

    def test_report_phase_returns_error_for_verify_or_signature_status_failure(self):
        from unittest import mock
        from core.app.phases import report_gate

        report_result = {
            "audit_ready": True,
            "debug_mode": False,
            "integrity_run_state": "clean",
            "integrity_content_store_state": "clean",
        }
        gate_result = {"ok": True, "failures": [], "warnings": [], "info": {}}
        for failing_operation in ("verify", "signature_status"):
            with self.subTest(failing_operation=failing_operation), \
                 mock.patch.object(report_gate, "write_report", return_value=report_result), \
                 mock.patch.object(
                     report_gate.verify_run,
                     "verify",
                     side_effect=(RuntimeError("verify boom") if failing_operation == "verify" else None),
                     return_value=gate_result,
                 ), \
                 mock.patch.object(
                     report_gate.verify_run,
                     "write_signature_status",
                     side_effect=(RuntimeError("status boom") if failing_operation == "signature_status" else None),
                 ):
                self.assertEqual(report_gate.phase_report({"run_dir": "run"}), "error")

    def test_debug_report_begins_with_unreliable_banner(self):
        import core.report as report

        with tempfile.TemporaryDirectory() as tmp:
            run = self._debug_run(tmp, "run-debug-report")
            md, summary = report.render(run)

        self.assertTrue(md.startswith(report.DEBUG_REPORT_BANNER + "\n\n#"))
        self.assertTrue(summary["debug_mode"])
        self.assertFalse(summary["audit_ready"])
        self.assertEqual(summary["integrity_content_store_state"], "unverifiable")

    def test_crash_recovery_is_disclosed_without_lowering_audit_readiness(self):
        import core.report as report
        from core.infra.db import ExecutionAssuranceRecord

        with tempfile.TemporaryDirectory() as tmp:
            run = self._debug_run(
                tmp,
                "run-crash-recovered",
                execution_assurance=ExecutionAssuranceRecord(
                    "agent", "agent_attested", "codex"
                ),
            )
            recovery = {
                "recovery_id": "recovery-1",
                "subject_scope": "run",
                "transition_id": "transition-1",
                "expected_checkpoint_id": "checkpoint-1",
                "last_heartbeat_at": "2026-08-22T00:00:00Z",
                "observed_manifest_sha256": "a" * 64,
                "restored_manifest_sha256": "b" * 64,
                "database_changed": True,
                "difference_count": 2,
                "action_count": 3,
                "created_at": "2026-08-22T00:00:01Z",
                "authority_id": "test-authority",
            }
            integrity = {
                "run": {
                    "state": "clean",
                    "audit_ready": True,
                    "overrides": [],
                    "crash_recoveries": [recovery],
                },
                "content_store": {
                    "state": "clean",
                    "audit_ready": True,
                    "overrides": [],
                    "crash_recoveries": [],
                },
                "debug_mode": False,
                "debug_labels": [],
                "audit_ready": True,
            }

            markdown, summary = report.render(run, integrity=integrity)

        self.assertIn("Crash recovery (run)", markdown)
        self.assertIn("audit readiness preserved", markdown)
        self.assertTrue(summary["audit_ready"])
        self.assertEqual(summary["integrity_run_crash_recoveries"], [recovery])

    def test_debug_compare_accepts_one_treatment_axis_and_surfaces_profile(self):
        from core.app.commands import benchmark as bm

        with tempfile.TemporaryDirectory() as tmp:
            runs = [self._debug_run(tmp, "run-a", model="model-a"),
                    self._debug_run(tmp, "run-b", model="model-b")]
            comparison = bm._debug_comparison_preflight(runs)
            self.assertEqual(comparison["differing_treatment_axes"],
                             ["model_backend_reasoning"])
            self.assertEqual(
                comparison["canonical_profile"]["hard_controls"]
                    ["pair_task_universe"]["verify_tasks"],
                [("c1", "r1", "fulltext_complete")],
            )
            data = bm.build(bm.load_rows(runs), debug_comparison=comparison)
            self.assertTrue(data["inputs"]["debug_comparison"]["accepted"])
            self.assertIn("debug comparison: **accepted**", bm.render_md(data))

    def test_debug_compare_rejects_hard_control_mismatch(self):
        from core.app.commands import benchmark as bm

        with tempfile.TemporaryDirectory() as tmp:
            runs = [self._debug_run(tmp, "run-a"),
                    self._debug_run(tmp, "run-b", verify_table_citations=True)]
            with self.assertRaisesRegex(ValueError, "hard-control mismatch"):
                bm._debug_comparison_preflight(runs)

    def test_debug_compare_rejects_model_and_code_changes(self):
        from core.app.commands import benchmark as bm

        with tempfile.TemporaryDirectory() as tmp:
            runs = [self._debug_run(tmp, "run-a", model="model-a", snapshot="snapshot-a"),
                    self._debug_run(tmp, "run-b", model="model-b", snapshot="snapshot-b")]
            with self.assertRaisesRegex(ValueError, "both model/backend/reasoning and code snapshot"):
                bm._debug_comparison_preflight(runs)

    def test_intrinsic_metrics_use_only_typed_terminal_facts(self):
        from core.app.commands import benchmark as bm
        rows = [
            # One crediting and one non-crediting typed terminal; no attempt facts.
            self._row("rA", "A", "c1", "r1", "supports"),
            self._row("rA", "A", "c2", "r2", None, crediting=False),
        ]
        rm, _ = bm.run_model_map(rows)
        m = bm.intrinsic_metrics(rows, rm)["A"]
        self.assertEqual(m["cells"], 2)
        self.assertEqual(m["crediting_terminal_rate"], 0.5)
        self.assertEqual(m["noncrediting_terminal_rate"], 0.5)
        self.assertNotIn("attempts_per_cell", m)
        self.assertNotIn("guard_pass_at1_rate", m)

    def test_within_model_run_to_run_concordance(self):
        from core.app.commands import benchmark as bm
        # Same model, two runs: agree on c1/r1, disagree on c2/r2.
        rows = [
            self._row("run1", "A", "c1", "r1", "supports"),
            self._row("run2", "A", "c1", "r1", "supports"),
            self._row("run1", "A", "c2", "r2", "supports"),
            self._row("run2", "A", "c2", "r2", "contradicts"),
        ]
        data = bm.build(rows)
        w = data["within_model_concordance"]["A"]
        self.assertEqual(w["raters"], 2)
        self.assertEqual(w["common_pairs"], 2)
        self.assertEqual(w["unanimity_rate"], 0.5)          # 1 of 2 pairs unanimous

    def test_between_model_discordance_listed(self):
        from core.app.commands import benchmark as bm
        rows = [
            self._row("rA", "A", "c1", "r1", "supports"),
            self._row("rB", "B", "c1", "r1", "off_topic"),      # disagreement
            self._row("rA", "A", "c2", "r2", "supports"),
            self._row("rB", "B", "c2", "r2", "supports"),       # agreement
        ]
        data = bm.build(rows)
        self.assertEqual(data["between_model_concordance"]["common_pairs"], 2)
        disc = data["discordant_pairs"]
        self.assertEqual(len(disc), 1)
        self.assertEqual(disc[0]["claim_id"], "c1")
        self.assertEqual(disc[0]["by_model"], {"A": "supports", "B": "off_topic"})

    def test_cohen_kappa_perfect_and_render(self):
        from core.app.commands import benchmark as bm
        self.assertEqual(bm.cohen_kappa([("a", "a"), ("b", "b")]), 1.0)
        self.assertIsNone(bm.cohen_kappa([]))
        rows = [self._row("rA", "A", "c1", "r1", "supports"),
                self._row("rB", "B", "c1", "r1", "supports")]
        md = bm.render_md(bm.build(rows))
        self.assertIn("Between-model concordance", md)
        self.assertIn("Intrinsic metrics per model", md)

class TestVerifyRunGate(unittest.TestCase):
    """The completion gate must catch the 'freelance' failure mode: a pair that has
    source text but no guarded verdict, and a report that was not produced by core.report."""

    def _write_report(self, run, *, sign=True):
        import core.report as report
        from core.infra.integrity import signing
        md, summary = report.render(run)
        if sign:
            body_sha256 = __import__("hashlib").sha256(
                report.strip_seal(md).encode("utf-8")).hexdigest()
            payload = report.seal_payload(
                report.provenance_fields(run, summary), body_sha256)
            content = __import__("hashlib").sha256(payload).hexdigest()
            seal = signing.sign(payload)
            md += (f"\n<!-- citation-verifier-provenance alg={seal['alg']} "
                   f"sig={seal['sig']} content={content} -->\n")
        report_path = os.path.join(run, "report.md")
        journal_path = report.journal_path(run)
        history = ""
        if os.path.exists(journal_path):
            with open(journal_path, encoding="utf-8") as f:
                history = f.read()
        separator = report.journal_separator(history)
        history_text = history + separator if history else ""
        history_sha256 = (__import__("hashlib").sha256(history_text.encode("utf-8")).hexdigest()
                          if history_text else None)
        entry = report.journal_entry(md, created_at="2026-06-18T00:00:00Z",
                                     history_sha256=history_sha256)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(md)
        with open(journal_path, "a" if history else "w", encoding="utf-8") as f:
            if separator:
                f.write(separator)
            f.write(entry)
        return md

    def _build_run(self, tmp, *, with_verdict, sign=True):
        if with_verdict:
            run = self._build_claim_evidence_run(tmp, [{
                "claim_id": "c1", "ref_id": "r1",
                "source_text": "The sky appears blue because of Rayleigh scattering.",
                "outcome": "supports", "pair_status": "accepted",
                "terminal_outcome": "supports", "terminal_cause": "jury2_accepted",
                "terminal_resolution": "jury2_accepted", "terminal_assurance": "passed",
                "evidence": ["blue because of Rayleigh scattering"],
                "grounded": [{"span_id": "span-1", "text": "blue because of Rayleigh scattering"}],
            }])
            self._write_report(run, sign=sign)
            return run
        from core.resolve import sources
        from core.infra.db import RunRepository
        run = tmp
        os.makedirs(os.path.join(run, "ledger"), exist_ok=True)
        ref = {"id": "r1", "manuscript_id": "m1", "ref_number": 1,
               "raw_entry": "Smith J. Sky. 2010.", "source_type": "article"}
        parse = {
            "manuscript": {"id": "m1", "filename": "m.txt", "sha256": "0" * 64,
                           "parser_version": "test"},
            "claims": [{"id": "c1", "manuscript_id": "m1",
                        "sentence": "The sky is blue [1].", "marker_numbers": [1],
                        "marker_raw": "[1]"}],
            "references": [ref],
            "citations": [{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
        }
        repo = RunRepository.create(
            run,
            run_id="run-verify-gate",
            input_path="C:\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style=None,
            model_id="test",
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.replace_parse_payload(
            manuscript_text="Complete manuscript text.",
            claims=parse["claims"],
            references=parse["references"],
            citations=parse["citations"],
        )
        repo.upsert_resolve_result("r1", {"ref_id": "r1", "status": "resolved", "via": "crossref"})
        repo.close()
        # The source HAS text (so the pair is checkable).
        src = "The sky appears blue because of Rayleigh scattering."
        sources.store_text(run, ref, "fulltext", "user", src, mapping="manual")
        # Produce the sealed report via core.report (the only authentic path).
        self._write_report(run, sign=sign)
        return run

    def test_claimless_link_silent_marker_is_not_a_no_text_verification_pair(self):
        from core.infra.db import RunRepository
        from core.verify import verify_run

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=True)
            repo = RunRepository.open(run)
            try:
                parse = repo.parse_payload()
                parse["references"].append({
                    "id": "r2", "ref_number": 2,
                    "raw_entry": "Jones J. Link-only coverage source. 2020.",
                    "source_type": "article",
                })
                parse["citations"].append({
                    "claim_id": None,
                    "ref_id": "r2",
                    "ref_number": 2,
                    "provenance": "link_silent_resolved",
                    "marker_raw": "2020",
                })
                repo.replace_parse_payload(
                    manuscript_text="Complete manuscript text.",
                    claims=parse["claims"],
                    references=parse["references"],
                    citations=parse["citations"],
                )
            finally:
                repo.close()
            self._write_report(run, sign=True)

            result = verify_run.verify(run)

        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["info"]["expected_pairs"], 1)
        # The real pair loses its fixture text when Parse is replaced; the
        # claimless r2 marker must not add a second no-text pair.
        self.assertEqual(result["info"]["pairs_no_text"], 1)
        self.assertTrue(any("c1·[1]" in warning for warning in result["warnings"]))
        self.assertFalse(any("[2]" in warning for warning in result["warnings"]))

    def _build_claim_evidence_run(self, tmp, pair_specs):
        from core.infra.db import RunRepository
        from core.resolve import sources
        from core.verify.claim_evidence.domain.fingerprint import (
            answer_fingerprint,
            candidate_fingerprint,
            payload_fingerprint,
            provider_prompt_fingerprint,
            FINGERPRINT_VERSION_V2,
        )
        from core.verify.claim_evidence.contracts.jury1_flow import (
            JURY1_SYSTEM_PROMPT,
            build_jury1_flow_payload,
        )

        def _hash(label):
            return __import__("hashlib").sha256(label.encode("utf-8")).hexdigest()

        run = tmp
        os.makedirs(os.path.join(run, "ledger"), exist_ok=True)
        claims = []
        references = []
        citations = []
        for index, spec in enumerate(pair_specs, start=1):
            claim_id = spec["claim_id"]
            ref_id = spec["ref_id"]
            scope = spec.get("scope", "fulltext_complete")
            sentence = spec.get("sentence", f"Claim {index} cites [{index}].")
            claims.append({
                "id": claim_id,
                "manuscript_id": "m1",
                "sentence": sentence,
                "context_window": spec.get("context_window", sentence),
                "marker_numbers": [index],
                "marker_raw": f"[{index}]",
            })
            references.append({
                "id": ref_id,
                "ref_number": index,
                "raw_entry": spec.get("raw_entry", f"Reference {index}."),
                "source_type": "article",
            })
            citations.append({"claim_id": claim_id, "ref_id": ref_id, "ref_number": index})

        repo = RunRepository.create(
            run,
            run_id="run-claim-evidence",
            input_path="C:\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style=None,
            model_id="test",
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.replace_parse_payload(
            manuscript_text="Complete manuscript text.",
            claims=claims,
            references=references,
            citations=citations,
        )
        for ref in references:
            repo.upsert_resolve_result(
                ref["id"],
                {"ref_id": ref["id"], "status": "resolved", "via": "crossref"},
            )
        repo.close()

        for index, spec in enumerate(pair_specs, start=1):
            sources.store_text(
                run,
                {"id": spec["ref_id"], "ref_number": index},
                "fulltext",
                "user",
                spec["source_text"],
                mapping="manual",
            )

        repo = RunRepository.open(run)
        adapter = repo.claim_evidence_adapter()
        for index, spec in enumerate(pair_specs, start=1):
            claim_id = spec["claim_id"]
            ref_id = spec["ref_id"]
            scope = spec.get("scope", "fulltext_complete")
            candidate_id = spec.get("candidate_id", f"candidate-{index}")
            request_id = spec.get("request_id", f"request-{index}")
            provider_id = spec.get("provider_id", "provider-a")
            model_id = spec.get("model_id", "model-a")
            credential_id = spec.get("credential_id", "provider-a:1")
            credential_fingerprint = spec.get("credential_fingerprint", _hash("secret-canary"))
            if spec.get("with_candidate", True):
                source_hash = _hash(f"{request_id}:source_hash")
                source_spans = [
                    {"span_id": item["span_id"], "text": item["text"]}
                    for item in spec.get("grounded", [])
                ] or [{
                    "span_id": f"source-span-{index}",
                    "text": spec["source_text"],
                }]
                outcome = spec["outcome"]
                stage = "explanation_evidence"
                payload = build_jury1_flow_payload(
                    task_id=stage,
                    cited_source_mode="full_text",
                    source_hash=source_hash,
                    source_spans=source_spans,
                    claim=claims[index - 1]["sentence"],
                    claim_context=claims[index - 1]["context_window"],
                    citation_marker=claims[index - 1]["marker_raw"],
                    determined_outcome=(
                        outcome
                        if outcome in {
                            "supports", "partial", "contradicts", "related", "off_topic"
                        }
                        else "supports"
                    ),
                )
                prompt_hash = provider_prompt_fingerprint(
                    JURY1_SYSTEM_PROMPT,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
                evidence = list(spec.get("evidence", []))
                outcome_fields = {
                    "claim_hash": _hash(claims[index - 1]["sentence"]),
                    "source_hash": _hash(f"{request_id}:source_hash"),
                    "supported_part": (
                        spec.get("supported_part") or evidence[0]
                        if outcome in {"supports", "partial"}
                        else None
                    ),
                    "incompatible_proposition": (
                        spec.get("incompatible_proposition")
                        or claims[index - 1]["sentence"]
                        if outcome == "contradicts"
                        else None
                    ),
                    "reason": (
                        spec.get("reason", "verification_unavailable")
                        if outcome == "non_decidable"
                        else None
                    ),
                    "provider_confidence": None,
                }
                grounded = []
                for item in spec.get("grounded", []):
                    text = item["text"]
                    raw_start = item.get(
                        "raw_start", spec["source_text"].index(text)
                    )
                    grounded.append({
                        "span_id": item["span_id"],
                        "raw_start": raw_start,
                        "raw_end": item.get("raw_end", raw_start + len(text)),
                        "text": text,
                        "source_hash": outcome_fields["source_hash"],
                        "match_mode": item.get("match_mode", "exact_raw"),
                        "score": item.get("score", 1.0),
                    })
                decision = {
                    "reason": spec.get("explanation", "because"),
                    "supported_content": (
                        outcome_fields["supported_part"]
                    if outcome in {"supports", "partial"}
                        else None
                    ),
                    "unsupported_content": (
                        "the remaining citation-linked use"
                        if outcome == "partial"
                        else None
                    ),
                    "incompatible_proposition": (
                        outcome_fields["incompatible_proposition"]
                        if outcome == "contradicts"
                        else None
                    ),
                    "evidence_span_ids": (
                        [item["span_id"] for item in spec.get("grounded", [])]
                        if outcome in {"supports", "partial", "contradicts"}
                        else []
                    ),
                    "non_decidable_reason": (
                        "verification_unavailable"
                        if outcome == "non_decidable"
                        else None
                    ),
                    "provider_confidence": None,
                }
                answer = {
                    "logical_request_id": request_id,
                    "payload_fingerprint": prompt_hash,
                    "decision": decision,
                }
                request = {
                    "logical_request_id": request_id,
                    "claim_id": claim_id,
                    "ref_id": ref_id,
                    "scope": scope,
                    "candidate_id": None,
                    "candidate_cycle": 1,
                    "stage": stage,
                    "payload": payload,
                    "payload_hash": payload_fingerprint(
                        payload, version=FINGERPRINT_VERSION_V2,
                    ),
                    "source_hash": outcome_fields["source_hash"],
                    "context_hash": _hash(f"{request_id}:context_hash"),
                    "retrieval_hash": _hash(f"{request_id}:retrieval_hash"),
                    "prompt_hash": prompt_hash,
                    "model_hash": _hash(f"{request_id}:model_hash"),
                    "policy_hash": _hash(f"{request_id}:policy_hash"),
                }
                adapter.append_logical_request(request)
                attempt = {
                    "dispatch_attempt_id": f"dispatch-{index}",
                    "logical_request_id": request_id,
                    "payload_hash": request["payload_hash"],
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "credential_id": credential_id,
                    "credential_fingerprint": credential_fingerprint,
                    "lane_id": f"{provider_id}:{model_id}:{credential_id}",
                    "credential_cursor": 0,
                    "model_draw_index": 0,
                    "selection_hash": _hash(f"{request_id}:selection"),
                    "pacing_hash": _hash(f"{request_id}:pacing"),
                    "global_interval_ms": 0,
                    "model_interval_ms": 0,
                }
                adapter.lease_dispatch(attempt)
                adapter.start_dispatch(
                    logical_request_id=request_id,
                    dispatch_attempt_id=attempt["dispatch_attempt_id"],
                )
                adapter.finish_dispatch(
                    logical_request_id=request_id,
                    dispatch_attempt_id=attempt["dispatch_attempt_id"],
                    result="completed",
                    technical_result="answer_received",
                    latency_ms=0,
                    answer_hash=answer_fingerprint(answer, version=FINGERPRINT_VERSION_V2),
                    answer=answer,
                )
                record = {
                    "outcome": outcome,
                    "explanation": spec.get("explanation", "because"),
                    "evidence": evidence,
                    "outcome_fields": outcome_fields,
                    "grounded": grounded,
                    "source_hash": request["source_hash"],
                    "context_hash": request["context_hash"],
                    "retrieval_hash": request["retrieval_hash"],
                    "prompt_hash": request["prompt_hash"],
                    "model_hash": request["model_hash"],
                    "policy_hash": request["policy_hash"],
                }
                record["fingerprint"] = candidate_fingerprint(
                    outcome=record["outcome"],
                    evidence=tuple(record["evidence"]),
                    outcome_fields=record["outcome_fields"],
                    grounded=tuple({
                        key: item[key]
                        for key in (
                            "span_id", "raw_start", "raw_end", "text",
                            "source_hash",
                        )
                    } for item in record["grounded"]),
                    version=FINGERPRINT_VERSION_V2,
                )
                adapter.append_candidate(
                    candidate_id=candidate_id,
                    claim_id=claim_id,
                    ref_id=ref_id,
                    scope=scope,
                    candidate_cycle=1,
                    origin_logical_request_id=request_id,
                    record=record,
                )
                if spec.get("terminal_event", True):
                    adapter.append_candidate_event(
                        event_id=f"{candidate_id}:terminal",
                        candidate_id=candidate_id,
                        event_type="terminal",
                        payload={
                            "resolution": spec["terminal_resolution"],
                            "assurance": spec["terminal_assurance"],
                        },
                    )
            adapter.ensure_pair(claim_id=claim_id, ref_id=ref_id, scope=scope)
            adapter.publish_terminal(
                claim_id=claim_id,
                ref_id=ref_id,
                scope=scope,
                status=spec["pair_status"],
                outcome=spec.get("terminal_outcome"),
                cause=spec["terminal_cause"],
                candidate_id=spec.get("candidate_id", f"candidate-{index}") if spec.get("with_candidate", True) else None,
            )
        repo.close()
        return run

    def test_complete_run_passes(self):
        from core.verify import verify_run
        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=True)
            res = verify_run.verify(run)
            self.assertTrue(res["ok"], res["failures"])
            self.assertEqual(res["info"]["pairs_skipped"], 0)
            self.assertTrue(res["info"]["quality"]["pipeline_complete"])
            self.assertTrue(res["info"]["quality"]["evidence_matching_complete"])
            self.assertTrue(res["info"]["quality"]["run_reliable"])

    def test_uncertain_warnings_use_authoritative_scope_selection(self):
        from core.verify.verify_run import _terminal_uncertain_by_cause

        fulltext = {
            "claim_id": "c1", "ref_id": "r1", "scope": "fulltext_complete",
            "status": "accepted", "terminal_cause": "jury2_accepted",
            "terminal_at": "2026-01-01",
        }
        web = {
            "claim_id": "c1", "ref_id": "r1", "scope": "web_secondhand",
            "status": "uncertain", "terminal_cause": "contested_negative",
            "terminal_at": "2026-01-02",
        }
        self.assertEqual(_terminal_uncertain_by_cause((fulltext, web)), {})
        self.assertEqual(_terminal_uncertain_by_cause((web, fulltext)), {})

    def test_known_deterministic_guard_is_not_a_protocol_failure(self):
        from core.report import render
        from core.verify import verify_run

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_claim_evidence_run(tmp, [{
                "claim_id": "c1",
                "ref_id": "r1",
                "source_text": "The source text is available.",
                "outcome": "supports",
                "pair_status": "uncertain",
                "terminal_outcome": None,
                "terminal_cause": "structural_claim_contamination",
                "terminal_resolution": "jury2_rejected",
                "terminal_assurance": "not_evaluated",
                "with_candidate": False,
            }])
            md, summary = render(run)
            self.assertIn("- Deterministic guard terminals: **1/1**", md)
            self.assertEqual(summary["run_health"]["deterministic_guard_pairs"], 1)
            self.assertEqual(summary["run_health"]["unclassified_terminal_pairs"], 0)

            self._write_report(run)
            result = verify_run.verify(run)

        quality = result["info"]["quality"]
        self.assertTrue(result["ok"], result["failures"])
        self.assertTrue(quality["protocol_valid"])
        self.assertFalse(quality["semantic_decision_complete"])
        self.assertFalse(quality["run_reliable"])
        self.assertEqual(quality["deterministic_guard_pairs"], 1)
        self.assertEqual(quality["unclassified_terminal_pairs"], 0)

    def test_structural_claim_contamination_is_complete_without_task_or_candidate(self):
        import core.report as report
        from core.verify import verify_run

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_claim_evidence_run(tmp, [{
                "claim_id": "c1",
                "ref_id": "r1",
                "source_text": "The source text is available.",
                "outcome": "supports",
                "pair_status": "uncertain",
                "terminal_outcome": None,
                "terminal_cause": "structural_claim_contamination",
                "terminal_resolution": "structural_claim_contamination",
                "terminal_assurance": "non_crediting",
                "with_candidate": False,
            }])
            _, summary = report.render(run)
            row = summary["verification_projection"][0]
            self.assertTrue(summary["verification_complete"])
            self.assertTrue(row["operational_complete"])
            self.assertFalse(row["crediting"])
            self.assertIsNone(row["semantic_outcome"])
            self.assertEqual(row["evidence"], [])

            self._write_report(run)
            res = verify_run.verify(run)
            self.assertTrue(res["ok"], res["failures"])
            self.assertEqual(res["info"]["pairs_skipped"], 0)

    def test_report_main_generates_a_verifiable_current_projection_without_derived_settings(self):
        import importlib
        import sys
        from unittest import mock
        from core.infra.db import RunRepository
        from core.verify import verify_run

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=True)
            module = importlib.import_module("core.report.render")
            gate = mock.Mock()
            resolver = mock.patch.object(
                module,
                "resolve_existing",
                return_value=type("_Resolved", (), {"gate": gate})(),
            )
            resolver.start()
            self.addCleanup(resolver.stop)
            gate.preflight.return_value = {
                "status": "clean",
                "audit_ready": True,
                "trust_domain_isolated": True,
                "audit_records": {"overrides": []},
            }
            gate.preflight_content_store.return_value = {
                "status": "clean",
                "audit_ready": True,
                "trust_domain_isolated": True,
                "audit_records": {"overrides": []},
            }
            gate.begin_pipeline_transition.return_value = mock.sentinel.report_lease
            argv = sys.argv
            try:
                sys.argv = ["render.py", "--run", run]
                with mock.patch.object(
                    module.RunIntegrityGate,
                    "from_environment",
                    return_value=gate,
                ):
                    module.main()
            finally:
                sys.argv = argv

            gate.begin_pipeline_transition.assert_called_once_with(
                run,
                checkpoint_kind="report",
                mutates_content_store=False,
            )
            gate.commit_pipeline_transition.assert_called_once_with(
                run, mock.sentinel.report_lease
            )
            gate.abort_pipeline_transition.assert_not_called()

            self.assertTrue(verify_run.verify(run)["ok"])
            repo = RunRepository.open(run)
            try:
                keys = set(repo.list_run_settings())
            finally:
                repo.close()
            self.assertFalse(keys.intersection({
                "report_summary", "report_signature", "signature_status", "gate_result",
            }))

    def test_report_main_aborts_integrity_transition_when_writer_fails(self):
        import importlib
        import sys
        from unittest import mock

        module = importlib.import_module("core.report.render")
        gate = mock.Mock()
        resolver = mock.patch.object(
            module,
            "resolve_existing",
            return_value=type("_Resolved", (), {"gate": gate})(),
        )
        resolver.start()
        self.addCleanup(resolver.stop)
        gate.begin_pipeline_transition.return_value = mock.sentinel.report_lease
        integrity = {
            "run": {"state": "clean", "audit_ready": True, "overrides": []},
            "content_store": {
                "state": "clean",
                "audit_ready": True,
                "overrides": [],
            },
            "debug_mode": False,
            "debug_labels": [],
            "audit_ready": True,
        }
        argv = sys.argv
        try:
            sys.argv = ["render.py", "--run", "run-that-will-not-be-written"]
            with (
                mock.patch.object(
                    module.RunIntegrityGate,
                    "from_environment",
                    return_value=gate,
                ),
                mock.patch.object(
                    module,
                    "trusted_report_integrity",
                    return_value=integrity,
                ),
                mock.patch.object(
                    module,
                    "write_report",
                    side_effect=OSError("write failed"),
                ),
                self.assertRaisesRegex(OSError, "write failed"),
            ):
                module.main()
        finally:
            sys.argv = argv

        gate.commit_pipeline_transition.assert_not_called()
        gate.abort_pipeline_transition.assert_called_once_with(
            "run-that-will-not-be-written",
            mock.sentinel.report_lease,
            reason="report generation raised OSError",
        )

    def test_content_store_debug_override_marks_report_non_audit_ready(self):
        from unittest import mock
        import core.report as report

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=True)
            gate = mock.Mock()
            gate.preflight.return_value = {
                "status": "clean",
                "audit_ready": True,
                "trust_domain_isolated": True,
                "audit_records": {"overrides": []},
            }
            gate.preflight_content_store.return_value = {
                "status": "debug_overridden",
                "audit_ready": False,
                "trust_domain_isolated": True,
                "audit_records": {
                    "overrides": [{
                        "override_id": "override-1",
                        "violation_id": "violation-1",
                        "reason": "operator accepted diagnostic input",
                        "authenticated_caller": "uid:1000",
                        "created_at": "2026-08-22T10:00:00Z",
                        "authority_id": "authority-1",
                    }],
                },
            }

            integrity = report.trusted_report_integrity(gate, run)
            md, summary = report.render(run, integrity=integrity)

        self.assertTrue(md.startswith(report.DEBUG_REPORT_BANNER + "\n\n#"))
        self.assertFalse(summary["audit_ready"])
        self.assertTrue(summary["debug_mode"])
        self.assertEqual(
            summary["integrity_content_store_state"], "debug_overridden"
        )
        self.assertIn("operator accepted diagnostic input", md)
        self.assertIn("`uid:1000`", md)

    def test_debug_flag_on_clean_authority_is_still_non_audit_ready(self):
        from unittest import mock
        import core.report as report

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=True)
            gate = mock.Mock()
            debug_clean = {
                "status": "clean",
                "audit_ready": False,
                "trust_domain_isolated": True,
                "debug_mode": True,
                "audit_records": {"overrides": []},
            }
            gate.preflight.return_value = debug_clean
            gate.preflight_content_store.return_value = debug_clean

            integrity = report.trusted_report_integrity(gate, run)

        self.assertTrue(integrity["debug_mode"])
        self.assertFalse(integrity["audit_ready"])
        self.assertEqual(integrity["run"]["state"], "clean")
        self.assertEqual(integrity["content_store"]["state"], "clean")

    def test_report_renders_claim_evidence_terminals_and_rejects_stale_seal(self):
        import core.report as report
        from core.infra.db import RunRepository
        from core.verify import verify_run

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_claim_evidence_run(tmp, [
                {
                    "claim_id": "c1",
                    "ref_id": "r1",
                    "source_text": "The source is related but does not directly support the claim.",
                    "outcome": "related",
                    "pair_status": "accepted",
                    "terminal_outcome": "related",
                    "terminal_cause": "jury2_not_eligible",
                    "terminal_resolution": "jury2_not_eligible",
                    "terminal_assurance": "not_evaluated",
                    "evidence": [],
                    "grounded": [],
                    "with_candidate": True,
                },
                {
                    "claim_id": "c2",
                    "ref_id": "r2",
                    "source_text": "The quoted passage directly supports the claim.",
                    "outcome": "supports",
                    "pair_status": "accepted",
                    "terminal_outcome": "supports",
                    "terminal_cause": "jury2_accepted",
                    "terminal_resolution": "jury2_accepted",
                    "terminal_assurance": "passed",
                    "evidence": ["directly supports the claim"],
                    "grounded": [{"span_id": "span-1", "text": "directly supports the claim"}],
                    "with_candidate": True,
                },
                {
                    "claim_id": "c3",
                    "ref_id": "r3",
                    "source_text": "The record is exhausted without a public semantic outcome.",
                    "pair_status": "exhausted",
                    "terminal_outcome": None,
                    "terminal_cause": "jury2_rejected",
                    "with_candidate": False,
                },
                {
                    "claim_id": "c4",
                    "ref_id": "r4",
                    "source_text": "The quoted passage directly contradicts the claim.",
                    "outcome": "contradicts",
                    "pair_status": "accepted",
                    "terminal_outcome": "contradicts",
                    "terminal_cause": "jury2_accepted",
                    "terminal_resolution": "jury2_accepted",
                    "terminal_assurance": "passed",
                    "evidence": ["directly contradicts the claim"],
                    "grounded": [{"span_id": "span-4", "text": "directly contradicts the claim"}],
                    "with_candidate": True,
                },
                {
                    "claim_id": "c5",
                    "ref_id": "r5",
                    "source_text": "The source is unrelated to the claim.",
                    "outcome": "off_topic",
                    "pair_status": "accepted",
                    "terminal_outcome": "off_topic",
                    "terminal_cause": "jury2_not_eligible",
                    "terminal_resolution": "jury2_not_eligible",
                    "terminal_assurance": "not_evaluated",
                    "evidence": [],
                    "grounded": [],
                    "with_candidate": True,
                },
                {
                    "claim_id": "c6",
                    "ref_id": "r6",
                    "source_text": "The quoted passage partially supports the claim.",
                    "outcome": "partial",
                    "pair_status": "accepted",
                    "terminal_outcome": "partial",
                    "terminal_cause": "jury2_accepted",
                    "terminal_resolution": "jury2_accepted",
                    "terminal_assurance": "passed",
                    "evidence": ["partially supports the claim"],
                    "grounded": [{"span_id": "span-6", "text": "partially supports the claim"}],
                    "with_candidate": True,
                },
            ])
            md, summary = report.render(run)

            self.assertIn(
                "verification outcome: **related** · result class `negative` · assurance `not_evaluated` · resolution `jury2_not_eligible`",
                md,
            )
            self.assertIn(
                "verification outcome: **supports** · result class `positive` · assurance `jury2_passed` · resolution `jury2_accepted`",
                md,
            )
            self.assertIn(
                "verification assessment: ⚪ _operational terminal without a semantic outcome · resolution `jury2_rejected` · no public semantic outcome_",
                md,
            )
            self.assertNotIn("_no verdict (source text not available)_", md)
            self.assertNotIn("missing evidence", md)
            self.assertIn("- LLM diagnostics (provider/model/credential alias/role):", md)
            self.assertIn("`provider-a/model-a/provider-a:1/jury1`", md)
            self.assertIn("transport-decodable=", md)
            self.assertIn("application-rejected=0 (schema=0)", md)
            self.assertNotIn("protocol-valid=", md)
            self.assertIn("- LLM logical requests: **5** (immutable ledger)", md)
            self.assertIn("- LLM dispatch attempts: **5** (immutable ledger)", md)
            self.assertIn(
                "- Jury1 application rejections: **0** · schema-invalid **0**",
                md,
            )
            self.assertIn("- Raw HTTP tracing: **unavailable** (debug tracing was not recorded)", md)
            self.assertNotIn("credential_fingerprint", md)
            self.assertNotIn("secret-canary", md)
            self.assertTrue(summary["verification_complete"])
            self.assertEqual(
                summary["run_health"]["mechanical_protocol_eligible_pairs"], 6
            )
            self.assertEqual(summary["claims_fail"], 2)
            self.assertEqual(summary["claims_warn"], 2)
            self.assertEqual(summary["pairs_positive"], 1)
            self.assertEqual(summary["pairs_incomplete_semantic"], 1)
            self.assertEqual(summary["pairs_negative"], 3)
            self.assertEqual(summary["pairs_unresolved_semantic"], 1)
            self.assertEqual(summary["pairs_positive_rate"], round(1 / 6, 3))
            self.assertEqual(summary["run_health"]["semantic_negative_pairs"], 3)
            self.assertTrue(any(
                row["semantic_outcome"] == "related"
                and row["result_class"] == "negative"
                and row["crediting"] is False
                for row in summary["verification_projection"]
            ))
            self.assertTrue(any(
                row["semantic_outcome"] is None and row["crediting"] is False
                for row in summary["verification_projection"]
            ))

            self._write_report(run, sign=True)
            verification = verify_run.verify(run)
            self.assertTrue(verification["ok"], verification["failures"])
            self.assertFalse(verification["info"]["quality"]["semantic_decision_complete"])
            self.assertFalse(verification["info"]["quality"]["run_reliable"])
            repo = RunRepository.open(run)
            try:
                conn = repo._conn
                conn.execute(
                    """
                    UPDATE verification_pair_state
                    SET terminal_at = '2026-07-31T00:00:00Z'
                    WHERE claim_id = 'c1' AND ref_id = 'r1' AND scope = 'fulltext_complete'
                    """
                )
                conn.commit()
            finally:
                repo.close()

            verification = verify_run.verify(run)
            self.assertFalse(verification["ok"])
            self.assertTrue(
                any("content seal does NOT match" in failure for failure in verification["failures"]),
                verification["failures"],
            )

    def test_report_keeps_contested_positive_outcome_but_does_not_credit_it(self):
        import core.report as report
        from core.verify import verify_run

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_claim_evidence_run(tmp, [{
                "claim_id": "c1",
                "ref_id": "r1",
                "source_text": "The quoted passage supports the claim.",
                "outcome": "supports",
                "pair_status": "uncertain",
                "terminal_outcome": "supports",
                "terminal_cause": "jury2_rejected_nonbinding",
                "terminal_resolution": "jury2_rejected_nonbinding",
                "terminal_assurance": "contested",
                "evidence": ["supports the claim"],
                "grounded": [{"span_id": "span-1", "text": "supports the claim"}],
                "with_candidate": True,
            }])

            md, summary = report.render(run)

            self.assertIn("assurance `contested`", md)
            self.assertIn("semantic outcome contested by Jury2", md)
            self.assertNotIn("source without text/verification", md)
            self.assertIn("strict crediting positives: **0**", md)
            self.assertEqual(summary["claims_warn"], 1)
            self.assertEqual(summary["pairs_accepted"], 0)
            self.assertEqual(summary["pairs_positive"], 1)
            self.assertEqual(summary["pairs_crediting_positive"], 0)
            self.assertFalse(summary["verification_projection"][0]["crediting"])
            self.assertTrue(summary["verification_complete"])

            self._write_report(run, sign=True)
            strict = verify_run.verify(run, strict_crediting=True)
            self.assertFalse(strict["ok"])
            self.assertIn(
                "no crediting verification result in the entire run (strict mode).",
                strict["failures"],
            )

    def test_negative_outcome_is_reported_as_model_finding_requiring_review(self):
        import core.report as report

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_claim_evidence_run(tmp, [{
                "claim_id": "c1",
                "ref_id": "r1",
                "source_text": "The source addresses a different proposition.",
                "outcome": "off_topic",
                "pair_status": "accepted",
                "terminal_outcome": "off_topic",
                "terminal_cause": "jury2_not_eligible",
                "terminal_resolution": "jury2_not_eligible",
                "terminal_assurance": "not_evaluated",
                "evidence": [],
                "grounded": [],
                "with_candidate": True,
            }])

            md, _summary = report.render(run)

        self.assertIn("model finding requiring review", md)
        self.assertNotIn("real manuscript problem", md)
        self.assertNotIn("operationally accepted negative outcome**", md)

    def test_skipped_verification_fails(self):
        """A pair with text but NO ledger verdict = the Haiku failure -> gate FAILS."""
        from core.verify import verify_run
        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=False, sign=True)
            res = verify_run.verify(run)
            self.assertFalse(res["ok"])
            self.assertTrue(any("skipped" in f or "NO verdict" in f
                                for f in res["failures"]))
            self.assertEqual(res["info"]["pairs_skipped"], 1)
            self.assertFalse(res["info"]["quality"]["semantic_decision_complete"])

    def test_handwritten_report_fails(self):
        """A report with no provenance signature is rejected as inauthentic."""
        from core.verify import verify_run
        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=True)
            with open(os.path.join(run, "report.md"), "w", encoding="utf-8") as f:
                f.write("# Report\n\nAll citations valid. ACCEPTED.\n")
            res = verify_run.verify(run)
            self.assertFalse(res["ok"])
            self.assertTrue(any("provenance" in f for f in res["failures"]))


    def test_edited_report_body_fails(self):
        """The seal binds the report BODY, not only parse+ledger: editing the prose after
        signing (seal comment left intact) is caught as a content mismatch."""
        from core.verify import verify_run
        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=True)
            self.assertTrue(verify_run.verify(run)["ok"])   # authentic to start
            with open(os.path.join(run, "report.md"), encoding="utf-8") as f:
                text = f.read()
            # Tamper with the prose but keep the trailing provenance seal comment.
            tampered = text.replace("\n<!--", "\nAll citations are perfectly valid.\n\n<!--", 1)
            self.assertNotEqual(tampered, text)
            with open(os.path.join(run, "report.md"), "w", encoding="utf-8") as f:
                f.write(tampered)
            res = verify_run.verify(run)
            self.assertFalse(res["ok"])
            self.assertTrue(any("content seal does NOT match" in f for f in res["failures"]))

    def test_append_only_history_chain_detects_rewrite(self):
        import core.report as report; from core.verify import verify_run
        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=True)
            self._write_report(run, sign=True)
            self.assertTrue(verify_run.verify(run)["ok"])
            with open(report.journal_path(run), encoding="utf-8") as f:
                text = f.read()
            latest = report.latest_report_entry(text)
            self.assertIsNotNone(latest)
            tampered = (text[:latest["start"]].replace(
                "Citation Verification Report", "Rewritten history", 1)
                + text[latest["start"]:])
            with open(report.journal_path(run), "w", encoding="utf-8") as f:
                f.write(tampered)
            res = verify_run.verify(run)
            self.assertFalse(res["ok"])
            self.assertTrue(any("report.journal.md append-only history hash" in f
                                for f in res["failures"]))









    def test_execution_assurance_warning_and_summary_are_reported(self):
        import importlib
        from core.infra.db import ExecutionAssuranceRecord, RunRepository

        report = importlib.import_module("core.report.render")

        with tempfile.TemporaryDirectory() as tmp:
            run = self._build_run(tmp, with_verdict=True, sign=False)

            standalone_md, standalone_summary = report.render(run)
            self.assertNotIn(report.UNPROTECTED_AGENT_REPORT_BANNER, standalone_md)
            self.assertEqual(
                standalone_summary["execution_protection"],
                "standalone_unattested",
            )
            self.assertFalse(standalone_summary["audit_ready"])

            repository = RunRepository.open(run)
            repository.downgrade_execution_assurance(
                ExecutionAssuranceRecord(
                    initial_origin="standalone",
                    protection="agent_unprotected_acknowledged",
                    agent_identity="codex",
                    failure_reason="authority socket unavailable",
                    acknowledged_at="2026-08-24T12:00:00+00:00",
                )
            )
            repository.close()

            unprotected_md, unprotected_summary = report.render(run)
            self.assertTrue(
                unprotected_md.startswith(
                    report.UNPROTECTED_AGENT_REPORT_BANNER + "\n\n#"
                )
            )
            self.assertFalse(unprotected_summary["audit_ready"])
            self.assertEqual(
                unprotected_summary["execution_protection"],
                "agent_unprotected_acknowledged",
            )
            self.assertEqual(unprotected_summary["execution_agent_identity"], "codex")
            self.assertIn("authority socket unavailable", unprotected_md)

            written_summary = report.write_report(run, integrity=None)
            with open(os.path.join(run, "report.md"), encoding="utf-8") as handle:
                written_report = handle.read()
            self.assertTrue(
                written_report.startswith(
                    report.UNPROTECTED_AGENT_REPORT_BANNER + "\n\n#"
                )
            )
            self.assertEqual(
                written_summary["execution_protection"],
                "agent_unprotected_acknowledged",
            )

            repository = RunRepository.open(run)
            repository.set_run_setting("debug_mode", True)
            repository.close()
            combined_md, combined_summary = report.render(run)
            self.assertTrue(
                combined_md.startswith(
                    report.DEBUG_REPORT_BANNER
                    + "\n\n"
                    + report.UNPROTECTED_AGENT_REPORT_BANNER
                    + "\n\n#"
                )
            )
            self.assertTrue(combined_summary["debug_mode"])
            self.assertFalse(combined_summary["audit_ready"])


class TestPresent(unittest.TestCase):
    """The report is shown by the deterministic tool, verbatim, only if verified."""

    def test_presents_verified_report_verbatim(self):
        from core.app.commands import present
        with tempfile.TemporaryDirectory() as tmp:
            run = TestVerifyRunGate()._build_run(tmp, with_verdict=True, sign=True)
            with open(os.path.join(run, "report.md"), encoding="utf-8") as f:
                body = f.read()
            ok, text = present.present(run)
            self.assertTrue(ok)
            self.assertIn("VERIFIED CITATION REPORT", text)
            self.assertIn("END OF VERIFIED REPORT", text)
            # The actual report body is reproduced verbatim (not summarised).
            self.assertIn(body.strip().splitlines()[0], text)

    def test_refuses_unverified_report(self):
        from core.app.commands import present
        with tempfile.TemporaryDirectory() as tmp:
            # A pair has text but no verdict â†’ gate fails â†’ present must withhold.
            run = TestVerifyRunGate()._build_run(tmp, with_verdict=False, sign=True)
            ok, text = present.present(run)
            self.assertFalse(ok)
            self.assertIn("WITHHELD", text)


def test_authority_projection_requires_isolated_trust_domain():
    import importlib

    report = importlib.import_module("core.report.render")

    checked = {"status": "clean", "audit_ready": True, "audit_records": {"overrides": []}}
    assert report._authority_subject_projection("run", checked) == {
        "state": "unverifiable",
        "audit_ready": False,
        "trust_domain_isolated": False,
        "overrides": [],
        "crash_recoveries": [],
    }

    isolated = {**checked, "trust_domain_isolated": True}
    assert report._authority_subject_projection("run", isolated)["state"] == "clean"
    assert report._authority_subject_projection("run", isolated)["audit_ready"] is True


def test_unattested_debug_authority_keeps_report_debug_mode():
    import importlib
    from unittest import mock

    report = importlib.import_module("core.report.render")
    gate = mock.Mock()
    unchecked_debug = {
        "status": "debug_overridden",
        "audit_ready": False,
        "audit_records": {"overrides": []},
    }
    gate.preflight.return_value = unchecked_debug
    gate.preflight_content_store.return_value = unchecked_debug
    with mock.patch.object(report, "_run_debug_settings", return_value=(False, [])):
        integrity = report.trusted_report_integrity(gate, "unused-run")

    assert integrity["run"]["state"] == "unverifiable"
    assert integrity["content_store"]["state"] == "unverifiable"
    assert integrity["debug_mode"] is True
    assert integrity["audit_ready"] is False


class TestReportAbstractAvailability(unittest.TestCase):
    def test_signature_status_lines_are_explicit(self):
        from unittest.mock import patch
        from core.report.render import _report_signature_line

        with patch("core.report.render._signing.key_present", return_value=True):
            self.assertIn("HMAC-signed", _report_signature_line())
        with patch("core.report.render._signing.key_present", return_value=False):
            weak = _report_signature_line()
        self.assertIn("content seal only", weak)
        self.assertIn("NOT signed", weak)

    def _render_for(self, resolve_payload):
        import core.report as report
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as run:
            os.makedirs(os.path.join(run, "ledger"), exist_ok=True)
            repo = RunRepository.create(
                run,
                run_id="run-report-abstract",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.replace_parse_payload(
                claims=[],
                references=[{"id": "r1", "ref_number": 1,
                             "raw_entry": "Smith J. Example article. 2020.",
                             "source_type": "article"}],
                citations=[],
            )
            repo.upsert_resolve_result("r1", {"ref_id": "r1", **resolve_payload})
            repo.close()
            md, _summary = report.render(run)
            return md

    def test_report_shows_abstract_available_from_metadata(self):
        md = self._render_for({
            "status": "resolved",
            "via": "crossref",
            "fulltext_exists": True,
            "abstract": "Example abstract text",
        })
        self.assertIn("- abstract: _available via crossref metadata_", md)

    def test_report_shows_abstract_not_available(self):
        md = self._render_for({
            "status": "resolved",
            "via": "crossref",
            "fulltext_exists": True,
        })
        self.assertIn("- abstract: _not available_", md)

    def test_report_shows_paywalled_abstract_fallback(self):
        md = self._render_for({
            "status": "resolved",
            "via": "crossref",
            "fulltext_exists": True,
            "oa_status": "paywalled",
            "abstract": "Example abstract text",
        })
        self.assertIn("standard fallback", md)

class TestNoClaimsStop(unittest.TestCase):
    def test_bibliography_only_has_zero_claims(self):
        """A reference list with no in-context citations yields n_claims == 0,
        which main() turns into an explicit stop (exit 4)."""
        from core.parse.parse_manuscript import parse
        doc = (
            "References\n"
            "[1] Smith J. A study of things. J Med. 2020;1:1-5.\n"
            "[2] Doe A. Another study. J Sci. 2019;2:6-9.\n"
        )
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write(doc)
            path = f.name
        try:
            result, _debug = parse(path, window=1)
            self.assertEqual(result["_debug"]["n_claims"], 0)
            self.assertGreaterEqual(result["_debug"]["n_references"], 1)
        finally:
            os.unlink(path)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Tests: unreadable-PDF OCR queue (fetch / provide map / sources)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestRunStatus(unittest.TestCase):
    @staticmethod
    def _fetch_task_payload(repo):
        repo.upsert_resolve_result("r1", {
            "status": "resolved", "fulltext_exists": "unknown",
            "reference_status_tag": "confirmed", "fabrication_risk": "low",
            "tag_reason": "fixture",
        })
        row = repo._conn.execute(
            "SELECT * FROM reference_entries WHERE ref_id='r1'"
        ).fetchone()
        reference = {field: row[field] for field in (
            "ref_number", "raw_entry", "title", "doi", "pmid", "url", "isbn", "year",
            "source_type", "source_kind", "indexability", "source_type_confidence",
        )}
        return {
            "kind": "fetch", "status": "pending", "answer": None, "ref_id": "r1",
            "ref_number": reference["ref_number"], "reference": reference,
            "source_identity": {
                "reference_status_tag": "confirmed", "fabrication_risk": "low",
                "tag_reason": "fixture", "matched_title": None, "metadata_match": None,
            },
            "instructions": "Retrieve an auditable source.",
        }

    def test_status_snapshot_for_interrupted_run(self):
        from core.app import run as driver
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.abspath(tmp)
            repo = RunRepository.create(
                run,
                run_id="run-status-interrupted",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.update_run_phase("fetch")
            repo.update_run_settings({"fetch_paused": True, "autonomous": False})
            repo.replace_parse_payload(
                claims=[],
                references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. Example. 2020."}],
                citations=[],
            )
            repo.update_run_phase("fetch")
            repo.create_task(
                task_id="fetch:ref-1",
                slot="fetch",
                ref_id="r1",
                claim_id=None,
                scope=None,
                task_payload=self._fetch_task_payload(repo),
            )
            repo.close()

            snap = driver.status_snapshot(run)
            self.assertFalse(snap["done"])
            self.assertEqual(snap["phase"], "fetch")
            self.assertEqual(snap["pending_tasks_total"], 1)
            self.assertEqual(snap["pending_tasks_by_slot"], {"fetch": 1})
            self.assertIn("--resume", snap["resume_command"])
            self.assertIn("fill pending task answers", snap["next_action"])

    def test_status_snapshot_for_completed_run(self):
        from core.app import run as driver
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.abspath(tmp)
            repo = RunRepository.create(
                run,
                run_id="run-status-completed",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.update_run_phase("done")
            repo.update_run_status("completed")
            repo.update_run_settings({
                "autonomous": False,
            })
            repo.close()
            with open(os.path.join(run, "report.md"), "w", encoding="utf-8") as f:
                f.write("# report\n")

            snap = driver.status_snapshot(run)
            self.assertTrue(snap["done"])
            self.assertEqual(snap["phase"], "done")
            self.assertIsNone(snap["gate_ok"])
            self.assertIsNone(snap["signature_verdict"])
            self.assertEqual(snap["fetch_attempt_summary"]["attempt_rows_total"], 0)
            self.assertEqual(snap["next_action"], "none")

    def test_status_snapshot_recomputes_status_artifact_without_reading_its_text(self):
        from core.app import run as driver
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.abspath(tmp)
            repo = RunRepository.create(
                run,
                run_id="run-status-report-failed",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.update_run_phase("report")
            repo.close()
            with open(os.path.join(run, "report.md"), "w", encoding="utf-8") as f:
                f.write("# report\n")
            with open(os.path.join(run, "report.signature_status.md"), "w", encoding="utf-8") as f:
                f.write("Verdict: **SIGNED_OK**\n")

            snap = driver.status_snapshot(run)
            self.assertFalse(snap["gate_ok"])
            self.assertEqual(snap["signature_verdict"], "INVALID")

    def test_status_snapshot_includes_fetch_attempt_summary(self):
        from core.app import run as driver
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.abspath(tmp)
            repo = RunRepository.create(
                run,
                run_id="run-status-fetch-summary",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.replace_parse_payload(
                claims=[],
                references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. Example. 2020."}],
                citations=[],
            )
            repo.append_fetch_attempt(
                "r1",
                method="openalex",
                url="https://example.org/x",
                kind="summary",
                final_url="https://example.org/x",
                status_code=403,
                content_type="text/html",
                outcome="challenge_blocked",
                reason="challenge",
                challenge_blocked=True,
                paywalled=False,
                trace={"summary_only": True},
            )
            repo.close()

            snap = driver.status_snapshot(run)
            fetch = snap["fetch_attempt_summary"]
            self.assertEqual(fetch["attempt_rows_total"], 1)
            self.assertEqual(fetch["references_with_attempts"], 1)
            self.assertEqual(fetch["challenge_blocked_references"], 1)
            self.assertEqual(fetch["outcomes"]["challenge_blocked"], 1)

    def test_status_json_only_outputs_machine_readable_json(self):
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.abspath(tmp)
            repo = RunRepository.create(
                run,
                run_id="run-status-json-only",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.update_run_phase("fetch")
            repo.update_run_settings({"fetch_paused": True, "autonomous": False})
            repo.replace_parse_payload(
                claims=[],
                references=[{"id": "r1", "ref_number": 1, "raw_entry": "Smith. Example. 2020."}],
                citations=[],
            )
            # Phase transitions belong to the driver.  Parse persistence sets the
            # run to resolve; prepare the fetch-state fixture only after that write.
            repo.update_run_phase("fetch")
            repo.append_fetch_attempt(
                "r1",
                method="reference_url",
                url="https://example.org/x.pdf",
                kind="summary",
                final_url="https://example.org/x.pdf",
                status_code=200,
                content_type="application/pdf",
                outcome="identity_mismatch",
                reason="mismatch",
                challenge_blocked=False,
                paywalled=True,
                trace={"summary_only": True},
            )
            repo.close()
            pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env = os.environ.copy()
            env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
            cmd = [sys.executable, os.path.join(pkg_root, "run.py"), "--run", run, "--status", "--json-only"]
            p = __import__("subprocess").run(cmd, cwd=os.getcwd(), env=env,
                                             capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn("RUN STATUS:", p.stdout)
            data = json.loads(p.stdout)
            self.assertEqual(data["phase"], "fetch")
            self.assertEqual(data["fetch_attempt_summary"]["attempt_rows_total"], 1)
            self.assertEqual(data["fetch_attempt_summary"]["paywalled_references"], 1)
            self.assertEqual(data["next_action"], "resume")

    def test_status_default_is_human_only_not_json_dump(self):
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.abspath(tmp)
            repo = RunRepository.create(
                run,
                run_id="run-status-human-only",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.update_run_phase("fetch")
            repo.update_run_settings({"fetch_paused": True, "autonomous": False})
            repo.close()
            pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env = os.environ.copy()
            env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
            cmd = [sys.executable, os.path.join(pkg_root, "run.py"), "--run", run, "--status"]
            p = __import__("subprocess").run(cmd, cwd=os.getcwd(), env=env,
                                             capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("RUN STATUS:", p.stdout)
            self.assertNotIn('"schema": "citation-verifier.run-status.v1"', p.stdout)


class TestFetchCompletionGate(unittest.TestCase):
    @staticmethod
    def _fetch_task_payload(repo):
        repo.upsert_resolve_result("r1", {
            "status": "resolved", "fulltext_exists": "unknown",
            "reference_status_tag": "confirmed", "fabrication_risk": "low",
            "tag_reason": "fixture",
        })
        row = repo._conn.execute(
            "SELECT * FROM reference_entries WHERE ref_id='r1'"
        ).fetchone()
        reference = {field: row[field] for field in (
            "ref_number", "raw_entry", "title", "doi", "pmid", "url", "isbn", "year",
            "source_type", "source_kind", "indexability", "source_type_confidence",
        )}
        return {
            "kind": "fetch", "status": "pending", "answer": None, "ref_id": "r1",
            "ref_number": reference["ref_number"], "reference": reference,
            "source_identity": {
                "reference_status_tag": "confirmed", "fabrication_risk": "low",
                "tag_reason": "fixture", "matched_title": None, "metadata_match": None,
            },
            "instructions": "Retrieve an auditable source.",
        }

    def _make_fetch_run(self):
        from core.infra.db import RunRepository

        tmp = tempfile.TemporaryDirectory()
        run = os.path.abspath(tmp.name)
        repo = RunRepository.create(
            run,
            run_id="run-fetch-gate",
            input_path="C:\\paper.pdf",
            input_sha256="abc123",
            accuracy="standard",
            style="vancouver",
            model_id=None,
            http_profile="default",
            challenge_mode="off",
            fixture_fingerprint="fixture-1",
        )
        repo.update_run_phase("fetch")
        repo.update_run_settings({"fetch_paused": True, "autonomous": False})
        repo.replace_parse_payload(
            claims=[{"id": "c1", "sentence": "Claim [1]."}],
            references=[{
                "id": "r1",
                "ref_number": 1,
                "raw_entry": "Smith. Example. 2020.",
                "title": "Example paper",
                "source_type": "article",
                "source_kind": "article_like",
                "indexability": "high",
                "source_type_confidence": "high",
            }],
            citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
        )
        repo.close()
        return tmp, run

    def test_phase_fetch_stays_paused_when_fetch_task_is_unanswered(self):
        from core.app import run as driver
        from core.infra.db import RunRepository

        tmp, run = self._make_fetch_run()
        with tmp:
            repo = RunRepository.open(run)
            try:
                repo.create_task(
                    task_id="fetch:r1",
                    slot="fetch",
                    ref_id="r1",
                    claim_id=None,
                    scope=None,
                    task_payload=self._fetch_task_payload(repo),
                )
            finally:
                repo.close()

            rc = driver.phase_fetch({"run_dir": run, "accuracy": "standard", "fetch_paused": True})
            self.assertEqual(rc, driver.ACTION_REQUIRED)

            repo = RunRepository.open(run)
            try:
                task = repo.get_task("fetch:r1")
                self.assertEqual(task.status, "pending")
            finally:
                repo.close()

    def test_phase_fetch_stays_paused_when_blocking_ref_has_no_trace(self):
        from core.app import run as driver

        tmp, run = self._make_fetch_run()
        with tmp:
            rc = driver.phase_fetch({"run_dir": run, "accuracy": "standard", "fetch_paused": True})
            self.assertEqual(rc, driver.ACTION_REQUIRED)

    def test_phase_fetch_can_continue_after_explicit_not_found_answer(self):
        from core.app import run as driver
        from core.infra.db import RunRepository

        tmp, run = self._make_fetch_run()
        with tmp:
            repo = RunRepository.open(run)
            try:
                repo.create_task(
                    task_id="fetch:r1",
                    slot="fetch",
                    ref_id="r1",
                    claim_id=None,
                    scope=None,
                    task_payload=self._fetch_task_payload(repo),
                )
                repo.submit_task_answer(
                    task_id="fetch:r1",
                    actor_type="user",
                    raw_payload={"found": False},
                )
            finally:
                repo.close()

            rc = driver.phase_fetch({"run_dir": run, "accuracy": "standard", "fetch_paused": True})
            self.assertEqual(rc, "gaps")

    def test_phase_fetch_standard_still_emits_fetch_task_when_only_abstract_is_present(self):
        from core.app import run as driver
        from core.resolve import sources
        from core.infra.db import RunRepository

        tmp, run = self._make_fetch_run()
        orig_auto_fetch = driver._auto_fetch_fulltexts
        orig_auto_ocr = driver._auto_run_source_ocr
        with tmp:
            repo = RunRepository.open(run)
            try:
                repo.upsert_resolve_result("r1", {
                    "ref_id": "r1",
                    "status": "resolved",
                    "via": "crossref",
                    "matched_title": "Example paper",
                    "abstract": "Stored abstract text with enough distinctive tokens.",
                    "fulltext_exists": True,
                    "oa_status": "paywalled",
                })
            finally:
                repo.close()
            sources.store_text(
                run,
                {
                    "id": "r1",
                    "ref_number": 1,
                    "raw_entry": "Smith. Example. 2020.",
                    "title": "Example paper",
                    "source_type": "article",
                },
                "abstract",
                "openalex",
                "Stored abstract text with enough distinctive tokens.",
                source_ref="resolve:openalex",
                mapping="tokens",
                signal="tokens",
                score=0.9,
            )
            try:
                driver._auto_fetch_fulltexts = lambda st, need, refs: []
                driver._auto_run_source_ocr = (
                    lambda st, refs, accuracy="standard": {
                        "attempted": 0,
                        "stored": 0,
                        "failed": 0,
                        "skipped": 0,
                        "errors": [],
                    }
                )
                rc = driver.phase_fetch({
                    "run_dir": run,
                    "accuracy": "standard",
                    "fetch_paused": False,
                    "challenge_mode": "off",
                })
                self.assertEqual(rc, driver.ACTION_REQUIRED)
            finally:
                driver._auto_fetch_fulltexts = orig_auto_fetch
                driver._auto_run_source_ocr = orig_auto_ocr

            repo = RunRepository.open(run)
            try:
                task = repo.get_task("fetch:r1")
                self.assertIsNotNone(task)
                self.assertEqual(task.status, "pending")
            finally:
                repo.close()

    def test_phase_fetch_auto_ocr_recovers_fulltext_without_manual_task(self):
        from core.fetch.extraction import ocr as ocr_mod
        from core.app import run as driver
        from core.resolve import sources
        from core.infra.db import RunRepository

        tmp, run = self._make_fetch_run()
        orig_auto_fetch = driver._auto_fetch_fulltexts
        orig_ocr = ocr_mod.ocr_pdf
        with tmp:
            repo = RunRepository.open(run)
            try:
                repo.replace_parse_payload(
                    claims=[{"id": "c1", "sentence": "Claim [1]."}],
                    references=[{
                        "id": "r1",
                        "ref_number": 1,
                        "raw_entry": ("Smith. Transit Infrastructure and the Public "
                                      "Utility. 2020. doi:10.1234/example"),
                        "title": "Transit Infrastructure and the Public Utility",
                        "doi": "10.1234/example",
                        "source_type": "article",
                        "source_kind": "article_like",
                        "indexability": "high",
                        "source_type_confidence": "high",
                    }],
                    citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
                )
                repo.upsert_resolve_result("r1", {
                    "ref_id": "r1",
                    "status": "resolved",
                    "via": "crossref",
                    "matched_title": "Transit Infrastructure and the Public Utility",
                    "fulltext_exists": True,
                    "oa_status": "paywalled",
                })
            finally:
                repo.close()

            scan = os.path.join(run, "scan.pdf")
            with open(scan, "wb") as handle:
                handle.write(b"%PDF-1.4\n% scanned\n")
            sources.park_unreadable(
                run,
                {"id": "r1", "ref_number": 1, "doi": "10.1234/example"},
                scan,
                origin="user",
                reason="unreadable",
                move=False,
            )

            try:
                driver._auto_fetch_fulltexts = lambda st, need, refs: []
                # A real scan carries its own title in the front matter: the
                # automatic-OCR identity probe now corroborates the scan against the
                # reference before promoting it to full text, so generic filler
                # (however long) is honestly left parked.
                ocr_mod.ocr_pdf = (
                    lambda path, lang="eng", dpi=300, **kwargs: (
                        "Transit Infrastructure and the Public Utility\n\n"
                        + ("This recovered full text has enough real words to clear the "
                           "quality gate comfortably. " * 8) + "doi:10.1234/example",
                        "fake",
                    )
                )
                rc = driver.phase_fetch({
                    "run_dir": run,
                    "accuracy": "maximum",
                    "fetch_paused": False,
                    "challenge_mode": "off",
                    "ocr_lang": "eng",
                })
                self.assertEqual(rc, "gaps")
            finally:
                driver._auto_fetch_fulltexts = orig_auto_fetch
                ocr_mod.ocr_pdf = orig_ocr

            repo = RunRepository.open(run)
            try:
                self.assertTrue(repo.source_text_exists("r1", tier="fulltext"))
                self.assertIsNone(repo.get_task("fetch:r1"))
            finally:
                repo.close()

    def test_report_keeps_ocr_pending_when_alternative_fulltext_is_unavailable(self):
        from core.resolve import sources

        for damage in ("missing", "tampered"):
            with self.subTest(damage=damage):
                tmp, run = self._make_fetch_run()
                with tmp:
                    scan = os.path.join(run, "scan.pdf")
                    with open(scan, "wb") as handle:
                        handle.write(b"%PDF-1.4\n% scanned\n")
                    sources.park_unreadable(
                        run,
                        {"id": "r1", "ref_number": 1},
                        scan,
                        origin="user",
                        reason="unreadable",
                        move=False,
                    )
                    entry = sources.store_text(
                        run,
                        {"id": "r1", "ref_number": 1},
                        "fulltext",
                        "user",
                        "Alternative text recorded before the file was damaged.",
                    )
                    alternative = os.path.join(run, "sources", entry["stored_as"])
                    if damage == "missing":
                        os.unlink(alternative)
                    else:
                        with open(alternative, "w", encoding="utf-8") as handle:
                            handle.write("tampered")

                    markdown, _summary = report.render(run)

                    self.assertIn(
                        "alternative full text used: **0**; pending OCR: **1**",
                        markdown,
                    )
                    self.assertIn(
                        f"(no alternative text — OCR pending, run `{run_prefix()} ocr`)",
                        markdown,
                    )
                    self.assertNotIn("fulltext (user) ✓ — used", markdown)


class TestTerminalHealthDimensions(unittest.TestCase):
    """The report used to call a reference cited only in a table "never cited in body" —
    an accusation against the parser for a marker it read correctly, and one that buried
    the list that matters: the references the document names nowhere at all."""

    def _render(self, *, table_only):
        import core.report as report
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as run:
            os.makedirs(os.path.join(run, "sources"), exist_ok=True)
            repo = RunRepository.create(
                run,
                run_id="run-table-only",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            refs = [{"id": f"r{i}", "ref_number": i, "raw_entry": f"Author {i}. T. J. 2020."}
                    for i in (1, 2, 3)]
            claims = [{"id": "c1", "manuscript_id": "m", "sentence": "As shown [1].",
                       "context_window": "As shown [1].", "marker_raw": "[1]",
                       "marker_numbers": [1], "is_multisource": False,
                       "claim_scope": "sentence"}]
            repo.replace_parse_payload(
                claims=claims, references=refs,
                citations=[{"claim_id": "c1", "ref_id": "r1", "ref_number": 1}],
                table_only_citations=table_only,
            )
            repo.close()
            md, _summary = report.render(run)
        return md

    def test_a_table_only_reference_is_cited_not_missing(self):
        md = self._render(table_only=[{"ref_number": 2, "ref_id": "r2",
                                       "marker_raw": "[2]", "raw_entry": "Author 2. T. J. 2020."}])
        self.assertIn("only inside a table", md)
        self.assertIn("--verify-table-citations", md)
        # [2] is cited (in a table) and [3] is not cited at all: the report must say
        # exactly that, and coverage must count both [1] and [2].
        nowhere = next(l for l in md.splitlines() if "cited nowhere" in l)
        self.assertIn("[3]", nowhere)
        self.assertNotIn("[2]", nowhere)
        self.assertIn("**2/3 (67%)**", md)

    def test_without_table_citations_the_uncited_list_is_the_old_one(self):
        md = self._render(table_only=[])
        nowhere = next(l for l in md.splitlines() if "cited nowhere" in l)
        self.assertIn("[2]", nowhere)
        self.assertIn("[3]", nowhere)
        self.assertNotIn("only inside a table", md)
        self.assertIn("**1/3 (33%)**", md)
