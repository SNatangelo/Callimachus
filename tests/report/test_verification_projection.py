# tests/report/test_verification_projection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from tests._bootstrap import *  # noqa: F401,F403


class TestVerificationProjection(unittest.TestCase):
    def test_preview_snippet_reliability_is_always_low(self):
        from core.report.verification_projection import verification_reliability

        self.assertEqual(
            verification_reliability({
                "scope": "preview_snippet",
                "assurance": "jury2_passed",
            }),
            "low",
        )
        self.assertEqual(
            verification_reliability({
                "scope": "fulltext_complete",
                "assurance": "jury2_passed",
            }),
            "high",
        )

    def _candidate(self, candidate_id, outcome, *, grounded=(), evidence=()):
        return {
            "candidate_id": candidate_id,
            "claim_id": "c",
            "ref_id": "r",
            "scope": "",
            "outcome": outcome,
            "grounded": list(grounded),
            "evidence": list(evidence),
        }

    def _state(self, *, status, terminal_outcome, terminal_cause, winner_call_id):
        return {
            "claim_id": "c",
            "ref_id": "r",
            "scope": "",
            "status": status,
            "terminal_outcome": terminal_outcome,
            "terminal_cause": terminal_cause,
            "winner_call_id": winner_call_id,
        }

    def _terminal(self, resolution, assurance):
        return {
            "candidate_id": "winner",
            "event_type": "terminal",
            "payload": {"resolution": resolution, "assurance": assurance},
        }

    def _project(self, state, candidate=None, terminal=None):
        from core.report.verification_projection import project_verification_pairs

        candidates = [candidate] if candidate is not None else []
        events = [terminal] if terminal is not None else []
        return project_verification_pairs([state], candidates, events)[0]

    def test_frozen_terminal_projection_matrix(self):
        cases = [
            (
                "jury2_accepted",
                self._state(
                    status="accepted",
                    terminal_outcome="supports",
                    terminal_cause="jury2_accepted",
                    winner_call_id="winner",
                ),
                self._candidate("winner", "supports", grounded=("E1", "E2")),
                self._terminal("jury2_accepted", "passed"),
                ("jury2_passed", True, True, "supports", "positive"),
            ),
            (
                "jury2_off",
                self._state(
                    status="accepted",
                    terminal_outcome="supports",
                    terminal_cause="jury2_off",
                    winner_call_id="winner",
                ),
                self._candidate("winner", "supports", grounded=("E1", "E2")),
                self._terminal("jury2_off", "not_evaluated"),
                ("not_evaluated", None, True, "supports", "positive"),
            ),
            (
                "jury2_not_eligible_related",
                self._state(
                    status="accepted",
                    terminal_outcome="related",
                    terminal_cause="jury2_not_eligible",
                    winner_call_id="winner",
                ),
                self._candidate("winner", "related"),
                self._terminal("jury2_not_eligible", "not_evaluated"),
                ("not_evaluated", None, False, "related", "negative"),
            ),
            (
                "jury2_not_eligible_off_topic",
                self._state(
                    status="accepted",
                    terminal_outcome="off_topic",
                    terminal_cause="jury2_not_eligible",
                    winner_call_id="winner",
                ),
                self._candidate("winner", "off_topic"),
                self._terminal("jury2_not_eligible", "not_evaluated"),
                ("not_evaluated", None, False, "off_topic", "negative"),
            ),
            (
                "jury2_not_eligible_non_decidable",
                self._state(
                    status="accepted",
                    terminal_outcome="non_decidable",
                    terminal_cause="jury2_not_eligible",
                    winner_call_id="winner",
                ),
                self._candidate("winner", "non_decidable"),
                self._terminal("jury2_not_eligible", "not_evaluated"),
                ("not_evaluated", None, False, "non_decidable", "unresolved"),
            ),
            (
                "jury2_rejected_nonbinding",
                self._state(
                    status="uncertain",
                    terminal_outcome="supports",
                    terminal_cause="jury2_rejected_nonbinding",
                    winner_call_id="winner",
                ),
                self._candidate("winner", "supports", grounded=("E1",)),
                self._terminal("jury2_rejected_nonbinding", "contested"),
                ("contested", False, False, "supports", "positive"),
            ),
            (
                "majority_fallback",
                self._state(
                    status="uncertain",
                    terminal_outcome="partial",
                    terminal_cause="majority_fallback",
                    winner_call_id="winner",
                ),
                self._candidate("winner", "partial", grounded=("E1",)),
                self._terminal("majority_fallback", "contested"),
                ("contested", False, False, "partial", "incomplete"),
            ),
            (
                "jury1_guard",
                self._state(
                    status="exhausted",
                    terminal_outcome=None,
                    terminal_cause="jury1_guard",
                    winner_call_id=None,
                ),
                None,
                self._terminal("jury1_guard", "non_crediting"),
                ("non_crediting", None, False, None, "unresolved"),
            ),
            (
                "jury1_technical",
                self._state(
                    status="exhausted",
                    terminal_outcome=None,
                    terminal_cause="jury1_technical",
                    winner_call_id=None,
                ),
                None,
                self._terminal("jury1_technical", "non_crediting"),
                ("non_crediting", None, False, None, "unresolved"),
            ),
            (
                "jury2_technical",
                self._state(
                    status="exhausted",
                    terminal_outcome=None,
                    terminal_cause="jury2_technical",
                    winner_call_id=None,
                ),
                None,
                self._terminal("jury2_technical", "non_crediting"),
                ("non_crediting", None, False, None, "unresolved"),
            ),
            (
                "no_consensus",
                self._state(
                    status="exhausted",
                    terminal_outcome=None,
                    terminal_cause="no_consensus",
                    winner_call_id=None,
                ),
                None,
                self._terminal("no_consensus", "non_crediting"),
                ("non_crediting", False, False, None, "unresolved"),
            ),
            (
                "jury2_rejected",
                self._state(
                    status="exhausted",
                    terminal_outcome=None,
                    terminal_cause="jury2_rejected",
                    winner_call_id=None,
                ),
                None,
                self._terminal("jury2_rejected", "non_crediting"),
                ("non_crediting", False, False, None, "unresolved"),
            ),
            (
                "jury1_provider_uncertain",
                self._state(
                    status="uncertain",
                    terminal_outcome=None,
                    terminal_cause="jury1_provider_uncertain",
                    winner_call_id=None,
                ),
                None,
                self._terminal("jury1_provider_uncertain", "non_crediting"),
                ("non_crediting", None, False, None, "unresolved"),
            ),
            (
                "jury2_provider_uncertain",
                self._state(
                    status="uncertain",
                    terminal_outcome=None,
                    terminal_cause="jury2_provider_uncertain",
                    winner_call_id=None,
                ),
                self._candidate("winner", "supports", grounded=("E1",)),
                self._terminal("jury2_provider_uncertain", "non_crediting"),
                ("non_crediting", None, False, None, "unresolved"),
            ),
            (
                "cancelled",
                self._state(
                    status="cancelled",
                    terminal_outcome=None,
                    terminal_cause="cancelled",
                    winner_call_id=None,
                ),
                None,
                self._terminal("cancelled", "non_crediting"),
                ("non_crediting", None, False, None, "unresolved"),
            ),
        ]

        for label, state, candidate, terminal, expected in cases:
            with self.subTest(label=label):
                row = self._project(state, candidate, terminal)
                self.assertEqual(
                    (
                        row["assurance"],
                        row["jury2_passed"],
                        row["crediting"],
                        row["semantic_outcome"],
                        row["result_class"],
                    ),
                    expected,
                )
                self.assertTrue(row["operational_terminal"])
                self.assertTrue(row["operational_complete"])

    def test_verification_projection_rejects_frozen_inconsistencies(self):
        from core.report.verification_projection import project_verification_pairs

        winner = self._candidate("winner", "supports", grounded=("E1",))
        related = self._candidate("winner", "related")
        terminal = self._terminal("jury2_accepted", "passed")

        bad_cases = [
            (
                "invalid status-resolution",
                self._state(
                    status="accepted",
                    terminal_outcome=None,
                    terminal_cause="jury1_guard",
                    winner_call_id=None,
                ),
                [],
                [],
                "terminal status contradicts its resolution",
            ),
            (
                "related with jury2 accepted",
                self._state(
                    status="accepted",
                    terminal_outcome="related",
                    terminal_cause="jury2_accepted",
                    winner_call_id="winner",
                ),
                [related],
                [terminal],
                "related cannot be represented as Jury2 accepted",
            ),
            (
                "missing winner",
                self._state(
                    status="accepted",
                    terminal_outcome="supports",
                    terminal_cause="jury2_accepted",
                    winner_call_id=None,
                ),
                [winner],
                [terminal],
                "crediting terminal lacks its winner candidate",
            ),
            (
                "mismatched winner",
                self._state(
                    status="accepted",
                    terminal_outcome="supports",
                    terminal_cause="jury2_accepted",
                    winner_call_id="different",
                ),
                [winner],
                [terminal],
                "terminal winner does not match pair state",
            ),
            (
                "missing terminal event",
                self._state(
                    status="accepted",
                    terminal_outcome="supports",
                    terminal_cause="jury2_accepted",
                    winner_call_id="winner",
                ),
                [winner],
                [],
                "terminal assurance contradicts pair state",
            ),
            (
                "mismatched assurance/outcome",
                self._state(
                    status="accepted",
                    terminal_outcome="supports",
                    terminal_cause="jury2_accepted",
                    winner_call_id="winner",
                ),
                [winner],
                [self._terminal("jury2_accepted", "contested")],
                "terminal assurance contradicts pair state",
            ),
            (
                "grounded empty but evidence text present",
                self._state(
                    status="accepted",
                    terminal_outcome="supports",
                    terminal_cause="jury2_accepted",
                    winner_call_id="winner",
                ),
                [self._candidate("winner", "supports", grounded=(), evidence=("text",))],
                [terminal],
                "evidence outcome lacks representative evidence",
            ),
        ]

        for label, state, candidates, events, message in bad_cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError, msg=message):
                    project_verification_pairs([state], candidates, events)

    def test_related_is_negative_and_never_requires_evidence(self):
        from core.report.verification_projection import project_verification_pairs

        candidate = {"candidate_id": "winner", "claim_id": "c", "ref_id": "r", "scope": "",
                     "outcome": "related", "grounded": []}
        rows = project_verification_pairs(
            [{"claim_id": "c", "ref_id": "r", "scope": "", "status": "accepted",
              "terminal_outcome": "related", "terminal_cause": "jury2_not_eligible",
              "winner_call_id": "winner"}], [candidate],
            [{"candidate_id": "winner", "event_type": "terminal",
              "payload": {"resolution": "jury2_not_eligible", "assurance": "not_evaluated"}}])

        self.assertEqual(rows[0]["semantic_outcome"], "related")
        self.assertEqual(rows[0]["evidence"], [])
        self.assertFalse(rows[0]["crediting"])
        self.assertEqual(rows[0]["result_class"], "negative")
        self.assertEqual(rows[0]["assurance"], "not_evaluated")

    def test_non_positive_evidence_outcomes_keep_candidate_and_evidence(self):
        for outcome, result_class in (
            ("partial", "incomplete"),
            ("contradicts", "negative"),
        ):
            with self.subTest(outcome=outcome):
                row = self._project(
                    self._state(
                        status="accepted",
                        terminal_outcome=outcome,
                        terminal_cause="jury2_accepted",
                        winner_call_id="winner",
                    ),
                    self._candidate("winner", outcome, grounded=("E1",)),
                    self._terminal("jury2_accepted", "passed"),
                )

                self.assertFalse(row["crediting"])
                self.assertEqual(row["result_class"], result_class)
                self.assertEqual(row["representative_candidate_id"], "winner")
                self.assertEqual(row["evidence"], ["E1"])

    def test_exhaustion_is_complete_but_non_crediting(self):
        from core.report.verification_projection import project_verification_pairs

        row = project_verification_pairs(
            [{"claim_id": "c", "ref_id": "r", "scope": "", "status": "exhausted",
              "terminal_outcome": None, "terminal_cause": "jury2_rejected"}], [], [])[0]

        self.assertTrue(row["operational_complete"])
        self.assertFalse(row["crediting"])
        self.assertIsNone(row["semantic_outcome"])

    def test_uncorroborated_bibliographic_identity_is_complete_nonsemantic_terminal(self):
        from core.report.verification_projection import project_verification_pairs

        row = project_verification_pairs(
            [{
                "claim_id": "c",
                "ref_id": "r",
                "scope": "",
                "status": "uncertain",
                "terminal_outcome": None,
                "terminal_cause": "bibliographic_identity_not_corroborated",
                "winner_call_id": None,
            }],
            [],
            [],
        )[0]

        self.assertEqual(row["assurance"], "non_crediting")
        self.assertIsNone(row["jury2_passed"])
        self.assertEqual(row["result_class"], "unresolved")
        self.assertFalse(row["crediting"])
        self.assertIsNone(row["semantic_outcome"])
        self.assertIsNone(row["representative_candidate_id"])
        self.assertEqual(row["evidence"], [])

    def test_uncorroborated_bibliographic_identity_rejects_semantic_data(self):
        from core.report.verification_projection import project_verification_pairs

        cases = (
            ("outcome", "supports", None, []),
            ("candidate", None, None, [self._candidate("winner", "supports")]),
        )
        for label, outcome, winner_call_id, candidates in cases:
            with self.subTest(label=label):
                state = self._state(
                    status="uncertain",
                    terminal_outcome=outcome,
                    terminal_cause="bibliographic_identity_not_corroborated",
                    winner_call_id=winner_call_id,
                )
                with self.assertRaisesRegex(ValueError, "must not expose LLM candidates or outcomes"):
                    project_verification_pairs([state], candidates, [])

    def test_exhaustion_never_projects_rejected_candidate_evidence(self):
        from core.report.verification_projection import project_verification_pairs

        row = project_verification_pairs(
            [{
                "claim_id": "c",
                "ref_id": "r",
                "scope": "",
                "status": "exhausted",
                "terminal_outcome": None,
                "terminal_cause": "jury2_rejected",
                "winner_call_id": None,
            }],
            [self._candidate("winner", "supports", grounded=("rejected",))],
            [self._terminal("jury2_rejected", "non_crediting")],
        )[0]

        self.assertFalse(row["crediting"])
        self.assertIsNone(row["semantic_outcome"])
        self.assertIsNone(row["representative_candidate_id"])
        self.assertEqual(row["evidence"], [])

    def test_structural_contamination_is_complete_nonsemantic_terminal(self):
        from core.report.verification_projection import project_verification_pairs

        state = self._state(
            status="uncertain",
            terminal_outcome=None,
            terminal_cause="structural_claim_contamination",
            winner_call_id=None,
        )
        row = project_verification_pairs([state], [], [])[0]
        self.assertTrue(row["operational_complete"])
        self.assertEqual(row["assurance"], "non_crediting")
        self.assertEqual(row["result_class"], "unresolved")
        self.assertFalse(row["crediting"])
        self.assertIsNone(row["semantic_outcome"])
        self.assertIsNone(row["representative_candidate_id"])
        self.assertEqual(row["evidence"], [])
        contradictions = (
            ("outcome", {**state, "terminal_outcome": "supports"}, [], []),
            ("winner", {**state, "winner_call_id": "winner"}, [], []),
            ("candidate", state, [self._candidate("winner", "supports")], []),
            (
                "terminal_candidate",
                state,
                [self._candidate("winner", "supports")],
                [self._terminal("structural_claim_contamination", "non_crediting")],
            ),
        )
        for label, bad_state, candidates, events in contradictions:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "nonsemantic uncertain"):
                    project_verification_pairs([bad_state], candidates, events)

    def test_identity_anomalies_are_complete_nonsemantic_terminals(self):
        from core.report.verification_projection import project_verification_pairs

        for cause in (
            "source_integrity_unavailable",
            "source_identity_target_unavailable",
            "source_identity_check_unavailable",
        ):
            with self.subTest(cause=cause):
                state = self._state(
                    status="uncertain",
                    terminal_outcome=None,
                    terminal_cause=cause,
                    winner_call_id=None,
                )
                row = project_verification_pairs([state], [], [])[0]
                self.assertTrue(row["operational_complete"])
                self.assertEqual(row["assurance"], "non_crediting")
                self.assertEqual(row["result_class"], "unresolved")
                self.assertFalse(row["crediting"])
                self.assertIsNone(row["semantic_outcome"])
                self.assertIsNone(row["representative_candidate_id"])
                self.assertEqual(row["evidence"], [])

    def test_pair_scope_selector_is_semantic_and_fail_closed(self):
        from core.report.verification_projection import select_verification_pair_rows

        def row(scope, outcome="supports", **extra):
            return {
                "claim_id": "c",
                "ref_id": "r",
                "scope": scope,
                "semantic_outcome": outcome,
                "result_class": "positive",
                "assurance": "jury2_passed",
                **extra,
            }

        fulltext = row("fulltext_complete")
        web = row("web_secondhand", "contradicts")
        for rows in ((fulltext, web), (web, fulltext)):
            self.assertEqual(select_verification_pair_rows(rows), [fulltext])

        abstract = row("abstract_only", "partial", result_class="incomplete")
        self.assertEqual(select_verification_pair_rows((abstract, web)), [abstract])
        conclusive_abstract = row("abstract_only")
        self.assertEqual(
            select_verification_pair_rows((conclusive_abstract, web)),
            [conclusive_abstract],
        )
        preview = row("preview_snippet", "partial", result_class="incomplete")
        self.assertEqual(select_verification_pair_rows((abstract, preview)), [preview])
        self.assertEqual(
            select_verification_pair_rows((conclusive_abstract, preview)),
            [conclusive_abstract],
        )
        with self.assertRaisesRegex(ValueError, "incompatible abstract"):
            select_verification_pair_rows((row("abstract_only"), row("abstract_fallback")))
        with self.assertRaisesRegex(ValueError, "scope is missing or invalid"):
            select_verification_pair_rows((row("unknown"),))
        with self.assertRaisesRegex(ValueError, "duplicate scope"):
            select_verification_pair_rows((fulltext, dict(fulltext)))
