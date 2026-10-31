# tests/verify/claim_evidence/test_runtime.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import core.verify.claim_evidence.application.task as task_module
import core.verify.claim_evidence.config as config_module
import core.verify.claim_evidence.runtime as runtime_module
from core.infra.db import RunRepository
from core.verify.claim_evidence import ClaimEvidenceRuntime
from core.verify.claim_evidence.context_config import ContextSettings
from core.verify.claim_evidence.evidence.retrieval import (
    ALGORITHM_ID,
    Bm25DependencyUnavailable,
    Bm25RetrievalResult,
)
from core.verify.claim_evidence.domain.fingerprint import (
    FINGERPRINT_VERSION_V2, candidate_fingerprint,
)
from core.verify.claim_evidence.adapters.runtime_repository import RuntimeRepository
from core.verify.claim_evidence.application.ports import FatalDispatchError
from core.verify.claim_evidence.domain.jury1_flow import (
    SupportGateDecision,
    ContraryGateDecision,
    ExplanationEvidenceDecision,
    FullSupportGateDecision,
    TopicGateDecision,
)
from core.verify.claim_evidence.domain.replies import (
    SupportGateAnswer,
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    FullSupportGateAnswer,
    Jury2Answer,
    TopicGateAnswer,
)
from core.verify.claim_evidence.domain.types import (
    Jury2Decision,
    TechnicalFailure,
)


ENV = {
    "CITATION_VERIFIER_VERIFY_BACKENDS": "alpha",
    "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "off",
    "ALPHA_KEYS": "key-one",
    "ALPHA_MODELS": "model-one",
}
PROVIDERS = (("alpha", "ALPHA_KEYS", "ALPHA_MODELS", False),)
ENV_TWO = {
    **ENV,
    "ALPHA_KEYS": "key-one,key-two",
    "ALPHA_MODELS": "model-one,model-two",
}
ENV_JURY2 = {
    **ENV,
    "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "high",
}


class ObservedRuntimeRepository(RuntimeRepository):
    def __init__(self, run_conn, events, runtime_path=None):
        super().__init__(
            run_conn,
            runtime_conn=sqlite3.connect(
                str(runtime_path) if runtime_path is not None else ":memory:",
                check_same_thread=False,
            ),
        )
        self.events = events

    def replay_dispatch_observations(self):
        self.events.append("replay")
        return super().replay_dispatch_observations()


class ForeignQuarantineRuntimeRepository(ObservedRuntimeRepository):
    def scheduler_controls(self):
        return {
            "disabled_credentials": frozenset({("foreign", "foreign:1")}),
            "disabled_lanes": frozenset(),
            "key_eligibility_ms": (),
            "cooldowns": {},
        }


class RelatedTransport:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    def dispatch(self, request, assignment):
        self.events.append("provider")
        self.calls += 1
        if request.stage == "support_gate":
            return SupportGateAnswer(
                request.logical_request_id,
                request.payload_fingerprint,
                SupportGateDecision(False),
            )
        if request.stage == "contrary_gate":
            decision = ContraryGateDecision(False)
            answer_type = ContraryGateAnswer
        elif request.stage == "topic_gate":
            decision = TopicGateDecision(True)
            answer_type = TopicGateAnswer
        elif request.stage == "explanation_evidence":
            decision = ExplanationEvidenceDecision(
                "The source concerns the same subject but supplies no usable support.",
                None,
                None,
                None,
                (),
                None,
            )
            answer_type = ExplanationEvidenceAnswer
        else:
            raise AssertionError(f"unexpected stage: {request.stage}")
        return answer_type(
            request.logical_request_id, request.payload_fingerprint, decision
        )


class BarrierRelatedTransport(RelatedTransport):
    def __init__(self, events):
        super().__init__(events)
        self._barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def dispatch(self, request, assignment):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            self._barrier.wait(timeout=2)
            return super().dispatch(request, assignment)
        finally:
            with self._lock:
                self._active -= 1


class RateThenRelatedTransport(RelatedTransport):
    def __init__(self, events):
        super().__init__(events)
        self.assignments = []

    def dispatch(self, request, assignment):
        self.assignments.append(assignment)
        if not self.calls:
            self.events.append("provider")
            self.calls += 1
            return TechnicalFailure(
                request.stage,
                request.logical_request_id,
                request.payload_fingerprint,
                "rate_limited",
                retry_after_seconds=60,
                http_status=429,
            )
        return super().dispatch(request, assignment)


class InvalidThenRelatedTransport(RelatedTransport):
    def __init__(self, events):
        super().__init__(events)
        self.assignments = []

    def dispatch(self, request, assignment):
        self.assignments.append(assignment)
        if not self.calls:
            self.events.append("provider")
            self.calls += 1
            return TechnicalFailure(
                request.stage,
                request.logical_request_id,
                request.payload_fingerprint,
                "credential_invalid",
                http_status=401,
            )
        return super().dispatch(request, assignment)


class Jury2YesTransport:
    def __init__(self, source_text):
        self.source_text = source_text
        self.calls = 0

    def dispatch(self, request, assignment):
        self.calls += 1
        if request.stage == "support_gate":
            return SupportGateAnswer(
                request.logical_request_id,
                request.payload_fingerprint,
                SupportGateDecision(True),
            )
        if request.stage == "full_support_gate":
            return FullSupportGateAnswer(
                request.logical_request_id,
                request.payload_fingerprint,
                FullSupportGateDecision(True),
            )
        if request.stage == "explanation_evidence":
            span = json.loads(request.payload)["source_spans"][0]
            return ExplanationEvidenceAnswer(
                request.logical_request_id, request.payload_fingerprint,
                ExplanationEvidenceDecision(
                    "The passage documents the complete citation-linked use.",
                    "The source establishes its complete assigned role.",
                    None,
                    None,
                    (span["span_id"],),
                    None,
                ),
            )
        if request.stage == "jury2":
            return Jury2Answer(
                request.logical_request_id,
                request.payload_fingerprint,
                Jury2Decision(True, "The passages fit the claim."),
            )
        raise AssertionError(f"unexpected stage: {request.stage}")


class Jury1UncertainTransport:
    def __init__(self):
        self.calls = 0

    def dispatch(self, request, assignment):
        self.calls += 1
        if request.stage != "support_gate":
            raise AssertionError(f"unexpected stage: {request.stage}")
        return SupportGateAnswer(
            request.logical_request_id,
            request.payload_fingerprint,
            SupportGateDecision(True, 0.7, True),
        )


class Jury2UncertainThenRelatedTransport(Jury2YesTransport):
    """Emit one uncertain Jury2 reply, then a fresh Jury1 related outcome."""

    def __init__(self, source_text):
        super().__init__(source_text)
        self.jury2_calls = 0
        self._jury2_uncertain = False

    def dispatch(self, request, assignment):
        if request.stage == "jury2":
            self.calls += 1
            self.jury2_calls += 1
            self._jury2_uncertain = True
            return Jury2Answer(
                request.logical_request_id,
                request.payload_fingerprint,
                Jury2Decision(True, None, 0.7, True),
            )
        if self._jury2_uncertain:
            self.calls += 1
            if request.stage == "support_gate":
                return SupportGateAnswer(
                    request.logical_request_id,
                    request.payload_fingerprint,
                    SupportGateDecision(False),
                )
            if request.stage == "contrary_gate":
                return ContraryGateAnswer(
                    request.logical_request_id,
                    request.payload_fingerprint,
                    ContraryGateDecision(False),
                )
            if request.stage == "topic_gate":
                return TopicGateAnswer(
                    request.logical_request_id,
                    request.payload_fingerprint,
                    TopicGateDecision(True),
                )
            if request.stage == "explanation_evidence":
                return ExplanationEvidenceAnswer(
                    request.logical_request_id,
                    request.payload_fingerprint,
                    ExplanationEvidenceDecision(
                        "The source concerns the same subject but supplies no usable support.",
                        None,
                        None,
                        None,
                        (),
                        None,
                    ),
                )
            raise AssertionError(f"unexpected stage: {request.stage}")
        return super().dispatch(request, assignment)


class CrashControlRuntimeRepository(ObservedRuntimeRepository):
    def _apply_control(self, payload):
        raise RuntimeError("simulated shared-control crash")


class CrashAfterCompletedAnswer(ObservedRuntimeRepository):
    def __init__(self, run_conn, events, runtime_path, target):
        super().__init__(run_conn, events, runtime_path)
        self.target = target
        self.crashed = False

    def replay_dispatch_observations(self):
        result = super().replay_dispatch_observations()
        completed = self._run_conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_events "
            "WHERE event_type='completed'"
        ).fetchone()[0]
        if not self.crashed and completed >= self.target:
            self.crashed = True
            raise RuntimeError("simulated crash after completed answer")
        return result


class FailureTransport:
    def __init__(self):
        self.stages = []

    def dispatch(self, request, assignment):
        self.stages.append(request.stage)
        return TechnicalFailure(
            request.stage,
            request.logical_request_id,
            request.payload_fingerprint,
            "provider_failure",
            http_status=503,
        )


def _repo(tmp_path):
    repository = RunRepository.create(
        str(tmp_path),
        run_id="run-1",
        input_path="input.pdf",
        input_sha256="a" * 64,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile=None,
        challenge_mode=None,
        fixture_fingerprint="fixture",
    )
    repository.replace_parse_payload(
        claims=[], references=[], citations=[],
    )
    return repository


def _task(source_text):
    payload, context = ClaimEvidenceRuntime.prepare_task(
        {"sentence": "Claim", "context_window": "Manuscript context.", "marker_raw": "[1]"}, source_text,
    )
    return {
        "kind": "claim_evidence",
        "semantic_contract": ClaimEvidenceRuntime.contract_id,
        "claim_id": "claim-1",
        "ref_id": "ref-1",
        "scope": "fulltext",
        "claim_evidence_payload": payload,
        "effective_context": context,
    }


def _task_for_pair(source_text, claim_id, ref_id):
    task = _task(source_text)
    task["claim_id"] = claim_id
    task["ref_id"] = ref_id
    return task


def test_runtime_binds_frozen_output_and_reasoning_controls_to_registry(
    tmp_path, monkeypatch
):
    observed = {}

    def fake_registry(names, **controls):
        observed["names"] = names
        observed.update(controls)
        return object()

    monkeypatch.setattr(runtime_module, "provider_registry", fake_registry)
    repo = _repo(tmp_path)
    runtime = ClaimEvidenceRuntime.for_run(
        repo,
        {
            **ENV,
            "CITATION_VERIFIER_VERIFY_MAX_TOKENS": "none",
            "CITATION_VERIFIER_REASONING": "off",
            "CITATION_VERIFIER_REASONING_EFFORT": "high",
        },
        run_id="run-1",
        runtime_repository=ObservedRuntimeRepository(repo._conn, []),
        provider_metadata=PROVIDERS,
    )
    try:
        assert observed == {
            "names": ("alpha",),
            "max_tokens": None,
            "reasoning": "off",
            "reasoning_effort": "high",
        }
    finally:
        runtime.close()
        repo.close()


def test_runtime_ignores_foreign_quarantine_and_dispatches(tmp_path):
    repo = _repo(tmp_path)
    events = []
    transport = RelatedTransport(events)
    runtime = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=transport,
        runtime_repository=ForeignQuarantineRuntimeRepository(repo._conn, events),
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = runtime.execute(_task("Source text"), source_text="Source text")
        assert terminal["status"] == "accepted"
        assert transport.calls >= 1
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_attempts"
        ).fetchone()[0] >= 1
    finally:
        runtime.close()
        repo.close()


def test_resume_stops_before_dispatch_when_frozen_prompt_identity_drifts(
    tmp_path, monkeypatch,
):
    repo = _repo(tmp_path)
    first_transport = RelatedTransport([])
    first_shared = ObservedRuntimeRepository(
        repo._conn, [], runtime_path=tmp_path / "runtime.sqlite",
    )
    first = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=first_transport,
        runtime_repository=first_shared,
        provider_metadata=PROVIDERS,
    )
    first.close()

    monkeypatch.setattr(
        config_module,
        "JURY1_PROMPT_SPEC",
        replace(
            config_module.JURY1_PROMPT_SPEC,
            version=config_module.JURY1_PROMPT_SPEC.version + 1,
        ),
    )
    resumed_transport = RelatedTransport([])
    resumed_shared = ObservedRuntimeRepository(
        repo._conn, [], runtime_path=tmp_path / "runtime.sqlite",
    )
    with pytest.raises(ValueError, match="config differs from the run"):
        ClaimEvidenceRuntime.for_run(
            repo,
            ENV,
            run_id="run-1",
            transport=resumed_transport,
            runtime_repository=resumed_shared,
            provider_metadata=PROVIDERS,
        )
    assert resumed_transport.calls == 0
    resumed_shared.close()
    repo.close()


def test_shared_runtime_executes_independent_pairs_concurrently(tmp_path):
    repo = _repo(tmp_path)
    events = []
    shared = ObservedRuntimeRepository(
        repo._conn, events, runtime_path=tmp_path / "runtime.sqlite"
    )
    transport = BarrierRelatedTransport(events)
    runtime = ClaimEvidenceRuntime.for_run(
        repo,
        {**ENV, "CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT": "2"},
        run_id="run-1",
        transport=transport,
        runtime_repository=shared,
        provider_metadata=PROVIDERS,
    )
    try:
        assert runtime.aggregate_in_flight == 2
        assert (
            repo._claim_evidence_connection_lock
            is shared._run_lock
            is runtime._run_repository._lock
        )
        tasks = (
            _task_for_pair("Source one", "claim-1", "ref-1"),
            _task_for_pair("Source two", "claim-2", "ref-2"),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            terminals = list(executor.map(
                lambda task: runtime.execute(
                    task, source_text=task["effective_context"]["text"]
                ),
                tasks,
            ))
        assert transport.max_active == 2
        assert [terminal["status"] for terminal in terminals] == ["accepted", "accepted"]
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM verification_pair_state WHERE status = 'accepted'"
        ).fetchone()[0] == 2
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM verification_pair_transitions"
        ).fetchone()[0] == 2
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_attempts"
        ).fetchone()[0] == 8
    finally:
        runtime.close()
        repo.close()


def test_contract_selection_is_shared_and_persisted_run_wins():
    contract = ClaimEvidenceRuntime.contract_id
    assert ClaimEvidenceRuntime.select_contract(
        None, {"CITATION_VERIFIER_VERIFY_CONTRACT": contract}
    ) == contract
    assert ClaimEvidenceRuntime.select_contract(contract, {}) == contract
    with pytest.raises(ValueError, match="not supported"):
        ClaimEvidenceRuntime.select_contract(
            "removed-contract", {"CITATION_VERIFIER_VERIFY_CONTRACT": contract}
        )
    with pytest.raises(ValueError, match="not supported"):
        ClaimEvidenceRuntime.select_contract(
            contract, {"CITATION_VERIFIER_VERIFY_CONTRACT": "removed-contract"}
        )


def test_contract_selection_defaults_to_final_and_rejects_unknown_persistence():
    contract = ClaimEvidenceRuntime.contract_id
    assert ClaimEvidenceRuntime.select_contract(None, {}) == contract
    with pytest.raises(ValueError, match="not supported"):
        ClaimEvidenceRuntime.select_contract("unknown", {})


def test_runtime_replays_before_provider_and_publishes_one_terminal(tmp_path):
    repo = _repo(tmp_path)
    events = []
    shared = ObservedRuntimeRepository(repo._conn, events)
    transport = RelatedTransport(events)
    runtime = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=transport,
        runtime_repository=shared,
        provider_metadata=PROVIDERS,
    )
    try:
        task = _task("Source text")
        terminal = runtime.execute(task, source_text="Source text")

        assert terminal["status"] == "accepted"
        assert terminal["terminal_outcome"] == "related"
        assert transport.calls == 4
        provider_index = events.index("provider")
        assert events[provider_index - 1] == "replay"
        assert shared.replay_dispatch_observations() == 0

        resumed = runtime.execute(task, source_text="Source text")
        assert resumed["status"] == "accepted"
        assert transport.calls == 4
        transitions = repo._conn.execute(
            "SELECT COUNT(*) FROM verification_pair_transitions"
        ).fetchone()[0]
        assert transitions == 1
    finally:
        runtime.close()
        repo.close()


def test_support_gate_technical_exhaustion_stops_before_later_jury1_stages(tmp_path):
    repo = _repo(tmp_path)
    transport = FailureTransport()
    runtime = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=transport,
        runtime_repository=ObservedRuntimeRepository(repo._conn, []),
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = runtime.execute(_task("Source text"), source_text="Source text")

        assert terminal["status"] == "exhausted"
        assert terminal["terminal_cause"] == "jury1_technical"
        assert transport.stages
        assert set(transport.stages) == {"support_gate"}
        assert {
            row[0] for row in repo._conn.execute(
                "SELECT DISTINCT stage FROM llm_logical_requests"
            )
        } == {"support_gate"}
    finally:
        runtime.close()
        repo.close()


def test_support_gate_technical_cap_counts_failures_across_resume(tmp_path):
    repo = _repo(tmp_path)
    runtime_path = tmp_path / "runtime.sqlite"

    class RateLimitedTransport:
        calls = 0

        def dispatch(self, request, assignment):
            self.calls += 1
            return TechnicalFailure(
                request.stage,
                request.logical_request_id,
                request.payload_fingerprint,
                "rate_limited",
                retry_after_seconds=0,
                http_status=429,
            )

    first_transport = RateLimitedTransport()
    first = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=first_transport,
        runtime_repository=CrashControlRuntimeRepository(
            repo._conn, [], runtime_path
        ),
        provider_metadata=PROVIDERS,
    )
    try:
        with pytest.raises(FatalDispatchError, match="terminal audit"):
            first.execute(_task("Source text"), source_text="Source text")
    finally:
        first.close()
    assert first_transport.calls == 1

    resumed_transport = FailureTransport()
    resumed = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=resumed_transport,
        runtime_repository=ObservedRuntimeRepository(repo._conn, [], runtime_path),
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = resumed.execute(_task("Source text"), source_text="Source text")

        assert terminal["status"] == "exhausted"
        assert len(resumed_transport.stages) == 2
        assert set(resumed_transport.stages) == {"support_gate"}
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_attempts"
        ).fetchone()[0] == 3
    finally:
        resumed.close()
        repo.close()


def test_res_01_completed_answer_resumes_without_provider_recall(
    tmp_path, monkeypatch,
):
    run_path = tmp_path / "run"
    run_path.mkdir()
    repo = _repo(run_path)
    events = []
    runtime_path = tmp_path / "runtime.sqlite"
    transport = RelatedTransport(events)
    first = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=transport,
        runtime_repository=ObservedRuntimeRepository(
            repo._conn, events, runtime_path
        ),
        provider_metadata=PROVIDERS,
    )
    task = _task("Source text")

    def fail_after_dispatch(*_args):
        raise RuntimeError("simulated post-dispatch crash")

    try:
        with monkeypatch.context() as crash:
            crash.setattr(
                runtime_module.RunLedger,
                "append",
                fail_after_dispatch,
            )
            with pytest.raises(
                RuntimeError, match="simulated post-dispatch crash"
            ):
                first.execute(task, source_text="Source text")
    finally:
        first.close()

    assert transport.calls == 4
    assert repo.get_verification_pair_state(
        claim_id="claim-1", ref_id="ref-1", scope="fulltext"
    )["status"] == "open"

    resumed = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=transport,
        runtime_repository=ObservedRuntimeRepository(
            repo._conn, events, runtime_path
        ),
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = resumed.execute(task, source_text="Source text")
        assert terminal["status"] == "accepted"
        assert terminal["terminal_outcome"] == "related"
        assert transport.calls == 4
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_attempts"
        ).fetchone()[0] == 4
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM verification_pair_transitions"
        ).fetchone()[0] == 1
    finally:
        resumed.close()
        repo.close()


def test_jury1_provider_uncertainty_survives_completed_answer_replay_without_retry(
    tmp_path,
):
    repo = _repo(tmp_path)
    runtime_path = tmp_path / "runtime.sqlite"
    provider = Jury1UncertainTransport()
    source_text = "Source text"
    task = _task(source_text)
    first = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=provider,
        runtime_repository=CrashAfterCompletedAnswer(
            repo._conn, [], runtime_path, 1
        ),
        provider_metadata=PROVIDERS,
    )
    try:
        with pytest.raises(FatalDispatchError, match="terminal audit"):
            first.execute(task, source_text=source_text)
    finally:
        first.close()
    assert provider.calls == 1

    resumed = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=provider,
        runtime_repository=ObservedRuntimeRepository(
            repo._conn, [], runtime_path
        ),
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = resumed.execute(task, source_text=source_text)
        assert terminal["status"] == "uncertain"
        assert terminal["terminal_outcome"] is None
        assert terminal["terminal_cause"] == "jury1_provider_uncertain"
        assert provider.calls == 1
    finally:
        resumed.close()
        repo.close()


def test_jury2_provider_uncertainty_replays_to_fresh_jury1_without_second_jury2(
    tmp_path,
):
    repo = _repo(tmp_path)
    runtime_path = tmp_path / "runtime.sqlite"
    provider = Jury2UncertainThenRelatedTransport(
        "Evidence for the exact claim."
    )
    task = _task("Evidence for the exact claim.")
    first = ClaimEvidenceRuntime.for_run(
        repo,
        ENV_JURY2,
        run_id="run-1",
        transport=provider,
        runtime_repository=CrashAfterCompletedAnswer(
            repo._conn, [], runtime_path, 4
        ),
        provider_metadata=PROVIDERS,
    )
    try:
        with pytest.raises(FatalDispatchError, match="terminal audit"):
            first.execute(task, source_text="Evidence for the exact claim.")
    finally:
        first.close()
    assert provider.calls == 4
    assert provider.jury2_calls == 1

    resumed = ClaimEvidenceRuntime.for_run(
        repo,
        ENV_JURY2,
        run_id="run-1",
        transport=provider,
        runtime_repository=ObservedRuntimeRepository(
            repo._conn, [], runtime_path
        ),
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = resumed.execute(
            task, source_text="Evidence for the exact claim."
        )
        assert terminal["status"] == "accepted"
        assert terminal["terminal_outcome"] == "related"
        assert provider.calls == 8
        assert provider.jury2_calls == 1
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM verification_candidates"
        ).fetchone()[0] == 2
        assert [row[0] for row in repo._conn.execute(
            "SELECT candidate_cycle FROM verification_candidates "
            "ORDER BY candidate_cycle"
        )] == [1, 2]
        events = {
            row[0]
            for row in repo._conn.execute(
                "SELECT event_type FROM verification_candidate_events"
            )
        }
        assert "requeued" in events
        assert not events & {"jury2_yes", "jury2_no"}
    finally:
        resumed.close()
        repo.close()


@pytest.mark.parametrize(
    ("branch", "crash_after", "expected_calls", "expected_outcome"),
    [
        ("related", 1, 4, "related"),
        ("related", 2, 4, "related"),
        ("related", 3, 4, "related"),
        ("related", 4, 4, "related"),
        ("support", 1, 3, "supports"),
        ("support", 2, 3, "supports"),
        ("support", 3, 3, "supports"),
    ],
)
def test_completed_jury1_gate_resumes_without_recalling_prior_gate(
    tmp_path, branch, crash_after, expected_calls, expected_outcome,
):
    repo = _repo(tmp_path)
    events = []
    runtime_path = tmp_path / "runtime.sqlite"
    transport = (
        RelatedTransport(events)
        if branch == "related"
        else Jury2YesTransport("Evidence for the exact claim.")
    )
    source_text = (
        "Source text"
        if branch == "related"
        else "Evidence for the exact claim."
    )
    task = _task(source_text)
    first = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=transport,
        runtime_repository=CrashAfterCompletedAnswer(
            repo._conn, events, runtime_path, crash_after
        ),
        provider_metadata=PROVIDERS,
    )
    try:
        with pytest.raises(FatalDispatchError, match="terminal audit"):
            first.execute(task, source_text=source_text)
    finally:
        first.close()
    assert transport.calls == crash_after

    resumed = ClaimEvidenceRuntime.for_run(
        repo,
        ENV,
        run_id="run-1",
        transport=transport,
        runtime_repository=ObservedRuntimeRepository(
            repo._conn, events, runtime_path
        ),
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = resumed.execute(task, source_text=source_text)
        assert terminal["status"] == "accepted"
        assert terminal["terminal_outcome"] == expected_outcome
        assert transport.calls == expected_calls
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_attempts"
        ).fetchone()[0] == expected_calls
    finally:
        resumed.close()
        repo.close()


def test_res_01_missing_429_projection_replays_before_next_provider(
    tmp_path,
):
    run_path = tmp_path / "run"
    run_path.mkdir()
    repo = _repo(run_path)
    events = []
    runtime_path = tmp_path / "runtime.sqlite"
    transport = RateThenRelatedTransport(events)
    first = ClaimEvidenceRuntime.for_run(
        repo,
        ENV_TWO,
        run_id="run-1",
        transport=transport,
        runtime_repository=CrashControlRuntimeRepository(
            repo._conn, events, runtime_path
        ),
        provider_metadata=PROVIDERS,
    )
    task = _task("Source text")
    try:
        with pytest.raises(
            FatalDispatchError,
            match="terminal audit or shared replay failed",
        ):
            first.execute(task, source_text="Source text")
    finally:
        first.close()

    assert transport.calls == 1
    cooldown_status = repo._conn.execute(
        "SELECT cooldown.status "
        "FROM llm_dispatch_events AS event "
        "JOIN llm_dispatch_scheduler_controls AS control "
        "ON control.event_id = event.event_id "
        "JOIN llm_dispatch_scheduler_cooldowns AS cooldown "
        "ON cooldown.event_id = control.event_id "
        "WHERE event.event_type = 'failed'"
    ).fetchone()[0]
    assert cooldown_status == "cooldown"

    shared = ObservedRuntimeRepository(repo._conn, events, runtime_path)
    resumed = ClaimEvidenceRuntime.for_run(
        repo,
        ENV_TWO,
        run_id="run-1",
        transport=transport,
        runtime_repository=shared,
        provider_metadata=PROVIDERS,
    )
    try:
        controls = shared.scheduler_controls()
        assert dict(controls["key_eligibility_ms"])[
            ("alpha", "alpha:1")
        ] > 0
        terminal = resumed.execute(task, source_text="Source text")
        assert terminal["status"] == "accepted"
        assert [row.credential_alias for row in transport.assignments] == [
            "alpha:1",
            "alpha:2",
            "alpha:2",
            "alpha:2",
            "alpha:2",
        ]
        assert transport.calls == 5
        assert shared.replay_scheduler_controls() == 0
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_attempts"
        ).fetchone()[0] == 5
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM verification_pair_transitions"
        ).fetchone()[0] == 1
    finally:
        resumed.close()
        repo.close()


def test_credential_invalid_quarantine_stays_in_runtime_database(tmp_path):
    repo = _repo(tmp_path)
    events = []
    shared = ObservedRuntimeRepository(repo._conn, events)
    transport = InvalidThenRelatedTransport(events)

    runtime = ClaimEvidenceRuntime.for_run(
        repo,
        ENV_TWO,
        run_id="run-1",
        transport=transport,
        runtime_repository=shared,
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = runtime.execute(
            _task("Source text"), source_text="Source text"
        )
        assert terminal["status"] == "accepted"
        assert [row.credential_alias for row in transport.assignments] == [
            "alpha:1",
            "alpha:2",
            "alpha:2",
            "alpha:2",
            "alpha:2",
        ]
        assert ("alpha", "alpha:1") in shared.scheduler_controls()[
            "disabled_credentials"
        ]
    finally:
        runtime.close()
        repo.close()


def test_runtime_persists_jury2_candidate_and_terminal_once(tmp_path):
    repo = _repo(tmp_path)
    source_text = "Evidence for the exact claim."
    transport = Jury2YesTransport(source_text)
    runtime = ClaimEvidenceRuntime.for_run(
        repo,
        ENV_JURY2,
        run_id="run-1",
        transport=transport,
        runtime_repository=ObservedRuntimeRepository(repo._conn, []),
        provider_metadata=PROVIDERS,
    )
    task = _task(source_text)
    try:
        terminal = runtime.execute(task, source_text=source_text)
        assert terminal["status"] == "accepted"
        assert terminal["terminal_outcome"] == "supports"
        assert transport.calls == 4
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM verification_candidates"
        ).fetchone()[0] == 1
        candidate = repo._conn.execute(
            "SELECT fingerprint, outcome, claim_hash, source_hash FROM verification_candidates"
        ).fetchone()
        grounding = repo._conn.execute(
            "SELECT span_id, raw_start, raw_end, text FROM verification_candidate_grounding"
        ).fetchone()
        assert candidate[0] == candidate_fingerprint(
            outcome=candidate[1], evidence=(source_text,),
            outcome_fields={"claim_hash": candidate[2], "source_hash": candidate[3],
                            "supported_part": "The source establishes its complete assigned role.", "incompatible_proposition": None,
                            "reason": None, "provider_confidence": None},
            grounded=({"span_id": grounding[0], "raw_start": grounding[1],
                       "raw_end": grounding[2], "text": grounding[3],
                       "source_hash": candidate[3]},),
            version=FINGERPRINT_VERSION_V2,
        )
        events = {
            row[0] for row in repo._conn.execute(
                "SELECT event_type FROM verification_candidate_events"
            )
        }
        assert events == {"guard_accepted", "jury2_yes", "terminal"}

        assert runtime.execute(task, source_text=source_text) == terminal
        assert transport.calls == 4
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM verification_pair_transitions"
        ).fetchone()[0] == 1
    finally:
        runtime.close()
        repo.close()


def test_prepare_task_freezes_exact_claim_and_complete_source():
    payload, context = ClaimEvidenceRuntime.prepare_task(
        {"sentence": "Claim", "context_window": "Manuscript context.", "marker_raw": "[1]"}, "Source text",
    )

    assert payload == {
        "claim": "Claim",
        "claim_context": "Manuscript context.",
        "citation_marker": "[1]",
    }
    assert context["mode"] == "full_text"
    assert context["text"] == "Source text"


def test_prepare_task_resolves_medium_and_small_profiles_to_frozen_rag(monkeypatch):
    source = "Alpha evidence. Beta evidence."
    retrieval = Bm25RetrievalResult(
        ((0, len("Alpha evidence.")),),
        ALGORITHM_ID,
        (
            ("library", "bm25s-0.3.10"),
            ("neighbor_window", 1),
            ("tokenizer", "unicode-word-casefold-v1"),
            ("top_k", 6),
        ),
    )
    calls = []

    def select(catalog, query, *, budget):
        calls.append((catalog.source_hash, query, budget))
        return retrieval

    monkeypatch.setattr(task_module, "select_bm25_ranges", select)

    _, medium = ClaimEvidenceRuntime.prepare_task(
        {"sentence": "Alpha", "context_window": "Manuscript context.", "marker_raw": "[1]"},
        source,
        context_settings=ContextSettings("auto", "medium", 15),
    )
    _, small = ClaimEvidenceRuntime.prepare_task(
        {"sentence": "Alpha", "context_window": "Manuscript context.", "marker_raw": "[1]"},
        source,
        context_settings=ContextSettings("auto", "small", 15),
    )
    _, short_medium = ClaimEvidenceRuntime.prepare_task(
        {"sentence": "Alpha", "context_window": "Manuscript context.", "marker_raw": "[1]"},
        source,
        context_settings=ContextSettings("auto", "medium", len(source)),
    )

    assert medium["mode"] == small["mode"] == "extractive_rag"
    assert medium["text"] == small["text"] == "Alpha evidence."
    assert medium["retrieval_algorithm"] == small["retrieval_algorithm"] == ALGORITHM_ID
    assert medium["retrieval_config"] == small["retrieval_config"] == dict(retrieval.config)
    assert short_medium["mode"] == "full_text"
    assert len(calls) == 2


def test_prepare_task_propagates_optional_bm25_dependency_failure(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise Bm25DependencyUnavailable(
            "bm25s==0.3.10 is required; install with "
            "pip install -r requirements-rag.txt"
        )

    monkeypatch.setattr(task_module, "select_bm25_ranges", unavailable)

    with pytest.raises(Bm25DependencyUnavailable, match="requirements-rag.txt"):
        ClaimEvidenceRuntime.prepare_task(
            {
                "sentence": "Claim",
                "context_window": "Manuscript context.",
                "marker_raw": "[1]",
            },
            "Source text",
            context_settings=ContextSettings("extractive_rag", "large", 20),
        )


@pytest.mark.parametrize(
    "claim_id,ref_id,stage,error_codes",
    [
        ("claim-ac5ef49ffa", "ref-5c57f22567", "full_support_gate", ("response_content_empty",) * 3),
        ("claim-4867145c4a", "ref-5c57f22567", "support_gate", ("response_content_empty",) * 3),
        ("claim-bbf1895f59", "ref-5ccbc34d93", "full_support_gate", ("response_content_empty",) * 3),
        ("claim-1f89f83ef5", "ref-6f6cbaea76", "full_support_gate", ("response_content_empty",) * 3),
        ("claim-a5b7833c1e", "ref-1bdfecb042", "jury2", ("response_content_empty",) * 3),
        ("claim-557652c903", "ref-3e3e4018e9", "jury2", ("response_content_empty", "json_object_missing", "response_content_empty")),
    ],
)
def test_thesis_provider_protocol_failures_exhaust_one_immutable_request(
    tmp_path, claim_id, ref_id, stage, error_codes,
):
    """The thesis failures are provider exhaustion, never missing source text."""
    class EmptyStageTransport(Jury2YesTransport):
        def __init__(self):
            super().__init__("NCCN guideline passage.")
            self.failed_requests = []
            self.failed_lanes = []

        def dispatch(self, request, assignment):
            if request.stage == stage:
                error = error_codes[len(self.failed_requests)]
                self.failed_requests.append(request)
                self.failed_lanes.append(assignment.lane_id)
                return TechnicalFailure(
                    request.stage, request.logical_request_id,
                    request.payload_fingerprint, "provider_failure",
                    detail="protocol_invalid", protocol_error_code=error,
                    response_hash="a" * 64,
                )
            return super().dispatch(request, assignment)

    repo = _repo(tmp_path)
    transport = EmptyStageTransport()
    runtime = ClaimEvidenceRuntime.for_run(
        repo, ENV_JURY2, run_id="run-1", transport=transport,
        runtime_repository=ObservedRuntimeRepository(repo._conn, []),
        provider_metadata=PROVIDERS,
    )
    try:
        task = _task_for_pair("NCCN guideline passage.", claim_id, ref_id)
        terminal = runtime.execute(task, source_text="NCCN guideline passage.")
        assert terminal["status"] == "exhausted"
        assert terminal["terminal_cause"] == (
            "jury2_technical" if stage == "jury2" else "jury1_technical"
        )
        assert terminal["terminal_outcome"] is None
        assert len(transport.failed_requests) == 3
        assert len({request.logical_request_id for request in transport.failed_requests}) == 1
        assert len({request.payload for request in transport.failed_requests}) == 1
        assert len(set(transport.failed_lanes)) == 1
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM llm_dispatch_events WHERE event_type='failed'"
        ).fetchone()[0] == 3
    finally:
        runtime.close()
        repo.close()


def test_empty_provider_reply_rotates_to_another_configured_credential(tmp_path):
    class EmptyThenRelated(RelatedTransport):
        def __init__(self):
            super().__init__([])
            self.attempts = []

        def dispatch(self, request, assignment):
            self.attempts.append((request, assignment))
            if len(self.attempts) == 1:
                return TechnicalFailure(
                    request.stage, request.logical_request_id,
                    request.payload_fingerprint, "provider_failure",
                    detail="protocol_invalid", protocol_error_code="response_content_empty",
                    response_hash="a" * 64,
                )
            return super().dispatch(request, assignment)

    repo = _repo(tmp_path)
    transport = EmptyThenRelated()
    runtime = ClaimEvidenceRuntime.for_run(
        repo, ENV_TWO, run_id="run-1", transport=transport,
        runtime_repository=ObservedRuntimeRepository(repo._conn, []),
        provider_metadata=PROVIDERS,
    )
    try:
        terminal = runtime.execute(_task("Source text"), source_text="Source text")
        assert terminal["status"] == "accepted"
        first_request, first_assignment = transport.attempts[0]
        retry_request, retry_assignment = transport.attempts[1]
        assert first_request.logical_request_id == retry_request.logical_request_id
        assert first_request.payload == retry_request.payload
        assert first_assignment.credential_alias != retry_assignment.credential_alias
        assert first_assignment.model != retry_assignment.model
    finally:
        runtime.close()
        repo.close()
