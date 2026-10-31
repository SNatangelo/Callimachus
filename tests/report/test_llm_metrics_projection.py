# tests/report/test_llm_metrics_projection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from tests._bootstrap import *  # noqa: F401,F403
import json


class TestLlmMetricsProjection(unittest.TestCase):
    def test_terminal_resolution_counts_use_authoritative_pair_states(self):
        from core.report.llm_metrics_projection import project_llm_metrics

        pair_states = [
            {
                "claim_id": "c1",
                "ref_id": "r1",
                "scope": "",
                "status": "accepted",
                "terminal_cause": "jury2_accepted",
            },
            {
                "claim_id": "c2",
                "ref_id": "r2",
                "scope": "",
                "status": "uncertain",
                "terminal_cause": "majority_fallback",
            },
            {
                "claim_id": "c3",
                "ref_id": "r3",
                "scope": "",
                "status": "exhausted",
                "terminal_cause": "jury1_guard",
            },
        ]
        metrics = project_llm_metrics(
            [], [], [], pair_states=pair_states + [dict(pair_states[0])]
        )

        self.assertEqual(
            metrics["terminal_resolutions"],
            {
                "jury1_guard": 1,
                "jury2_accepted": 1,
                "majority_fallback": 1,
            },
        )

    def test_application_schema_rejections_are_separate_from_transport_validity(self):
        from core.report.llm_metrics_projection import project_llm_metrics

        request = {
            "logical_request_id": "q1",
            "stage": "explanation_evidence",
        }
        attempt = {
            "dispatch_attempt_id": "a1",
            "logical_request_id": "q1",
            "provider_id": "provider",
            "model_id": "model",
            "credential_id": "provider:1",
        }
        event = {
            "event_id": "a1:terminal",
            "dispatch_attempt_id": "a1",
            "event_type": "completed",
            "payload": {
                "technical_result": "answer_received",
                "latency_ms": 1,
            },
        }
        rejection = {
            "event_id": "q1:rejected",
            "logical_request_id": "q1",
            "state_cause": "schema_invalid",
            "cause": "schema_invalid",
        }

        metrics = project_llm_metrics(
            [request],
            [attempt],
            [event],
            jury1_rejections=[rejection, dict(rejection)],
        )

        self.assertEqual(metrics["totals"]["protocol_valid"], 1)
        self.assertEqual(metrics["totals"]["application_rejections"], 1)
        self.assertEqual(metrics["totals"]["application_schema_rejections"], 1)
        self.assertEqual(
            metrics["application_rejection_causes"], {"schema_invalid": 1}
        )

    def test_met_01_counts_unique_observations_and_replay_duplicates(self):
        from core.report.llm_metrics_projection import project_llm_metrics

        requests = [
            {"logical_request_id": f"q{i}", "stage": "support_gate"}
            for i in range(1, 6)
        ] + [
            {
                "logical_request_id": "q6",
                "stage": "jury2",
                "candidate_id": "c1",
            },
            {
                "logical_request_id": "q7",
                "stage": "jury2",
                "candidate_id": "c2",
            },
        ]
        requests = requests + [dict(requests[0])]
        attempts = [
            {
                "dispatch_attempt_id": f"a{i}",
                "logical_request_id": f"q{i}",
                "provider_id": "p",
                "model_id": "m",
                "credential_id": "p:1",
            }
            for i in range(1, 8)
        ]
        attempts = attempts + [dict(attempts[4])]
        events = [
            {
                "event_id": f"a{i}:terminal",
                "dispatch_attempt_id": f"a{i}",
                "event_type": "failed" if i == 5 else "completed",
                "payload": {
                    "technical_result": "protocol_invalid" if i == 5 else "response",
                    "answer_hash": "a" * 64,
                },
            }
            for i in range(1, 8)
        ]
        events = events + [dict(events[4])]
        candidates = [
            {
                "candidate_id": f"c{i}",
                "origin_logical_request_id": f"q{i}",
                "outcome": "supports",
            }
            for i in range(1, 4)
        ]
        candidates = candidates + [dict(candidates[0])]
        candidate_events = [
            {
                "event_id": "e1",
                "candidate_id": "c1",
                "event_type": "jury2_yes",
                "payload": {"logical_request_id": "q6"},
            },
            {
                "event_id": "e2",
                "candidate_id": "c2",
                "event_type": "jury2_no",
                "payload": {"logical_request_id": "q7"},
            },
        ]
        candidate_events = candidate_events + [dict(candidate_events[0])]

        metrics = project_llm_metrics(requests, attempts, events, candidates, candidate_events)
        row = metrics["totals"]
        self.assertEqual((row["attempts"], row["answers_received"], row["protocol_valid"],
                          row["jury1_admissible"], row["jury2_evaluated"], row["jury2_yes"]),
                         (7, 7, 6, 3, 2, 1))
        self.assertEqual(row["logical_requests"], 7)
        self.assertEqual(len(metrics["by_provider_model_role"]), 2)
        for row in metrics["by_provider_model_role"]:
            self.assertEqual(row["provider"], "p")
            self.assertEqual(row["model"], "m")
            self.assertIn(row["role"], {"jury1", "jury2"})
            self.assertEqual(row["credential_alias"], "p:1")
            self.assertNotIn("credential_fingerprint", row)
        self.assertNotIn("secret-canary", json.dumps(metrics))

    def test_met_01_retry_admits_origin_and_jury2_to_completed_attempt(self):
        from core.report.llm_metrics_projection import project_llm_metrics

        requests = [
            {"logical_request_id": "rq-origin", "stage": "full_support_gate"},
            {
                "logical_request_id": "rq-judge",
                "stage": "jury2",
                "candidate_id": "cand-1",
            },
        ]
        attempts = [
            {
                "dispatch_attempt_id": "a-origin-a",
                "logical_request_id": "rq-origin",
                "provider_id": "p",
                "model_id": "model-a",
                "credential_id": "p:1",
            },
            {
                "dispatch_attempt_id": "a-origin-b",
                "logical_request_id": "rq-origin",
                "provider_id": "p",
                "model_id": "model-b",
                "credential_id": "p:1",
            },
            {
                "dispatch_attempt_id": "a-judge-b",
                "logical_request_id": "rq-judge",
                "provider_id": "p",
                "model_id": "model-b",
                "credential_id": "p:1",
            },
        ]
        events = [
            {
                "event_id": "a-origin-a:terminal",
                "dispatch_attempt_id": "a-origin-a",
                "event_type": "failed",
                "payload": {"technical_result": "provider_failure"},
            },
            {
                "event_id": "a-origin-b:terminal",
                "dispatch_attempt_id": "a-origin-b",
                "event_type": "completed",
                "payload": {
                    "technical_result": "response",
                    "answer_hash": "b" * 64,
                },
            },
            {
                "event_id": "a-judge-b:terminal",
                "dispatch_attempt_id": "a-judge-b",
                "event_type": "completed",
                "payload": {
                    "technical_result": "response",
                    "answer_hash": "c" * 64,
                },
            },
        ]
        candidates = [
            {
                "candidate_id": "cand-1",
                "origin_logical_request_id": "rq-origin",
                "outcome": "supports",
            }
        ]
        candidate_events = [
            {
                "event_id": "cand-1:terminal",
                "candidate_id": "cand-1",
                "event_type": "terminal",
                "payload": {"resolution": "jury2_accepted"},
            },
            {
                "event_id": "cand-1:yes",
                "candidate_id": "cand-1",
                "event_type": "jury2_yes",
                "payload": {"logical_request_id": "rq-judge"},
            },
        ]

        metrics = project_llm_metrics(requests, attempts, events, candidates, candidate_events)
        rows = {
            (row["role"], row["model"]): row
            for row in metrics["by_provider_model_role"]
        }
        origin_failed = rows[("jury1", "model-a")]
        origin = rows[("jury1", "model-b")]
        judge = rows[("jury2", "model-b")]
        self.assertEqual(
            (
                origin_failed["attempts"],
                origin_failed["answers_received"],
                origin_failed["protocol_valid"],
                origin_failed["jury1_admissible"],
            ),
            (1, 0, 0, 0),
        )
        self.assertEqual(
            (
                origin["attempts"],
                origin["answers_received"],
                origin["protocol_valid"],
                origin["jury1_admissible"],
                origin["jury2_evaluated_origin"],
                origin["jury2_yes_origin"],
            ),
            (1, 1, 1, 1, 1, 1),
        )
        self.assertEqual(
            (
                judge["attempts"],
                judge["answers_received"],
                judge["protocol_valid"],
                judge["jury2_evaluated"],
                judge["jury2_yes"],
            ),
            (1, 1, 1, 1, 1),
        )
        self.assertNotIn("credential_fingerprint", origin)
        self.assertNotIn("credential_fingerprint", judge)
        self.assertNotIn("secret-canary", json.dumps(metrics))

    def test_met_01_rejects_divergent_same_id_replays(self):
        from core.report.llm_metrics_projection import project_llm_metrics

        base_request = {"logical_request_id": "rq", "stage": "support_gate"}
        base_attempt = {
            "dispatch_attempt_id": "attempt",
            "logical_request_id": "rq",
            "provider_id": "p",
            "model_id": "m",
            "credential_id": "p:1",
        }
        base_event = {
            "event_id": "attempt:terminal",
            "dispatch_attempt_id": "attempt",
            "event_type": "completed",
            "payload": {"technical_result": "response", "answer_hash": "a" * 64},
        }
        base_candidate = {
            "candidate_id": "cand",
            "origin_logical_request_id": "rq",
            "outcome": "supports",
        }
        base_candidate_event = {
            "event_id": "cand:terminal",
            "candidate_id": "cand",
            "event_type": "terminal",
            "payload": {"resolution": "jury2_accepted"},
        }

        cases = [
            (
                "request",
                [base_request, {**base_request, "stage": "jury2"}],
                [base_attempt],
                [base_event],
                [base_candidate],
                [base_candidate_event],
            ),
            (
                "attempt",
                [base_request],
                [base_attempt, {**base_attempt, "model_id": "other"}],
                [base_event],
                [base_candidate],
                [base_candidate_event],
            ),
            (
                "event",
                [base_request],
                [base_attempt],
                [base_event, {**base_event, "payload": {"technical_result": "response", "answer_hash": "b" * 64}}],
                [base_candidate],
                [base_candidate_event],
            ),
            (
                "candidate",
                [base_request],
                [base_attempt],
                [base_event],
                [base_candidate, {**base_candidate, "outcome": "partial"}],
                [base_candidate_event],
            ),
            (
                "candidate_event",
                [base_request],
                [base_attempt],
                [base_event],
                [base_candidate],
                [base_candidate_event, {**base_candidate_event, "payload": {"resolution": "jury2_rejected"}}],
            ),
        ]

        for label, requests, attempts, events, candidates, candidate_events in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    project_llm_metrics(
                        requests,
                        attempts,
                        events,
                        candidates,
                        candidate_events,
                    )
