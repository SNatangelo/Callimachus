# core/verify/claim_evidence/runtime.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Executable composition facade for the current claim-evidence contract."""
from __future__ import annotations

from typing import Any, Mapping

from .adapters.llm.credentials import credentials_from_config
from .adapters.llm.registry import (
    provider_registry,
    registered_provider_metadata,
)
from .adapters.llm.transport import (
    AuditedTransport,
    ClaimEvidenceTransport,
    ReplayDispatch,
    RunRequestFactory,
)
from .adapters.run_repository import (
    ClaimEvidenceRunRepository,
    RunLedger,
)
from .adapters.runtime_repository import RuntimeRepository
from .application.controller import (
    ClaimEvidenceController,
    resume_controller,
    validate_dispatch_resume,
)
from .application.scheduler import ClaimEvidenceScheduler
from .application.ports import FatalDispatchError
from .application.task import RuntimeError, prepare_task as _prepare_task
from .config import (
    CONTRACT_ID,
    GENERIC_MODEL_ENV,
    ProviderEnvSpec,
    resolve_config,
    select_contract,
)
from .domain.cooldown import CooldownSettings
from .domain.fingerprint import (
    pair_fingerprint,
    policy_fingerprint,
)
from .domain.pacing import PacingPolicy
from .domain.types import StateMachinePolicy
from .evidence.context import context_from_snapshot


class ClaimEvidenceRuntime:
    """Public composition boundary shared by manual and autonomous entrypoints."""

    contract_id = CONTRACT_ID
    select_contract = staticmethod(select_contract)
    prepare_task = staticmethod(_prepare_task)

    def __init__(
        self,
        config: Any,
        scheduler: Any,
        runtime_repository: Any,
        run_repository: Any,
        cancellation: Any,
    ) -> None:
        self._config, self._scheduler = config, scheduler
        self._runtime_repository, self._run_repository = runtime_repository, run_repository
        self._cancellation = cancellation

    @classmethod
    def for_run(
        cls,
        repository: Any,
        environ: Mapping[str, str],
        *,
        run_id: str,
        cancellation: Any | None = None,
        transport: Any | None = None,
        runtime_repository: Any | None = None,
        provider_metadata: tuple[tuple[str, str, str | None, bool], ...] | None = None,
    ) -> "ClaimEvidenceRuntime":
        metadata = provider_metadata or registered_provider_metadata()
        specs = tuple(
            ProviderEnvSpec(name, key, model or GENERIC_MODEL_ENV, credentialless, capability)
            for name, key, model, credentialless, *rest in metadata
            for capability in (rest[0] if rest else None,)
        )
        config = resolve_config(environ, specs, run_id=run_id)
        cancellation = cancellation or _NeverCancelled()
        run = ClaimEvidenceRunRepository(repository)
        run.freeze_config(config.snapshot())
        shared = runtime_repository or RuntimeRepository(repository._conn)
        shared.bind_run_lock(run._lock); shared.replay_dispatch_observations()
        shared.replay_scheduler_controls()
        snapshot = run.resume_snapshot()
        validate_dispatch_resume(snapshot)
        credentials = credentials_from_config(
            config, specs, environ) if transport is None else None
        base = transport if transport is not None else ClaimEvidenceTransport(
            provider_registry(
                tuple(item.name for item in config.providers),
                max_tokens=config.max_tokens,
                reasoning=config.reasoning,
                reasoning_effort=config.reasoning_effort,
            ),
            credentials,
            provider_confidence_threshold=config.provider_confidence_threshold)
        pacing = PacingPolicy(config.global_pacing_ms, config.pacing_by_model_ms)
        cooldown = CooldownSettings(
            config.cooldown.baseline_seconds, config.cooldown.multiplier,
            config.cooldown.local_max_seconds, config.cooldown.stable_successes,
            config.cooldown.version,
        )
        audited = AuditedTransport(
            base, run, shared, selection_hash=config.execution_policy_hash,
            pacing_hash=policy_fingerprint({
                "global": config.global_pacing_ms,
                "models": config.pacing_by_model_ms,
            }, version=run.fingerprint_version()),
            control_payload=lambda assignment, result: shared.terminal_control(
                assignment, result, cooldown),
        )
        scheduler = ClaimEvidenceScheduler(
            config.providers, config.selection_seed, audited,
            aggregate_cap=config.aggregate_in_flight, pacing=pacing,
            cooldown=cooldown, cooldown_overrides=config.cooldown_overrides,
            state=shared.scheduler_state(snapshot, config.providers),
            cancelled=cancellation.cancelled)
        return cls(config, scheduler, shared, run, cancellation)

    def execute(
        self,
        task: dict[str, Any],
        *,
        source_text: str,
    ) -> Any:
        _validate_task(task)
        try:
            context = context_from_snapshot(task["effective_context"], source_text)
        except ValueError as exc:
            raise RuntimeError("persisted effective context is invalid") from exc
        claim = _validated_claim(task)
        identity = (task["claim_id"], task["ref_id"], task.get("scope", ""))
        pair = pair_fingerprint(
            claim_id=identity[0], ref_id=identity[1], scope=identity[2],
            version=self._run_repository.fingerprint_version(),
        )
        self._run_repository.ensure_pair(
            claim_id=identity[0], ref_id=identity[1], scope=identity[2]
        )
        current = self._run_repository.pair_state(
            claim_id=identity[0], ref_id=identity[1], scope=identity[2]
        )
        if current is not None and current["status"] != "open":
            return current
        self._replay_shared()
        snapshot = self.resume_snapshot(identity)
        answers = self._run_repository.completed_answers(
            snapshot, claim_id=identity[0], ref_id=identity[1], scope=identity[2])
        failure_counts = self._run_repository.technical_failure_counts(
            snapshot, claim_id=identity[0], ref_id=identity[1], scope=identity[2])
        events = self._run_repository.resume_events(
            snapshot, claim_id=identity[0], ref_id=identity[1],
            scope=identity[2]
        )
        policy = StateMachinePolicy(self._config.candidate_cap, self._config.jury1_technical_cap, self._config.jury2_technical_cap, self._config.jury2_level)
        initial = resume_controller(policy, events)
        controller = self._controller(
            task, context, source_text, pair, answers, identity, failure_counts
        )
        if controller is None:
            return self._run_repository.pair_state(
                claim_id=identity[0], ref_id=identity[1], scope=identity[2]
            )
        state = controller.run(
            policy, claim=claim, source_text=source_text, context=context,
            initial_state=initial
        )
        if state.terminal is None:
            raise RuntimeError("controller returned without a terminal")
        representative = state.terminal.representative
        self._run_repository.publish_terminal(
            claim_id=identity[0], ref_id=identity[1], scope=identity[2],
            status=_terminal_status(state.terminal.status),
            outcome=state.terminal.outcome, cause=state.terminal.resolution,
            candidate_id=(
                f"{pair}:candidate:{representative.cycle}:"
                f"{representative.fingerprint}"
                if representative else None
            ),
        )
        return self._run_repository.pair_state(claim_id=identity[0], ref_id=identity[1], scope=identity[2])

    def _controller(
        self, task: dict[str, Any], context: Any, source_text: str, pair: str,
        answers: dict[str, Any], identity: tuple[str, str, str], failure_counts: dict[tuple[str, int], int],
    ) -> ClaimEvidenceController | None:
        requests = RunRequestFactory(
            self._run_repository, self._config, task, context, source_text, pair,
            failure_counts=failure_counts)
        dispatch = ReplayDispatch(
            self._scheduler, answers,
            fingerprint_version=self._run_repository.fingerprint_version(),
        )
        return ClaimEvidenceController(
            requests, dispatch, RunLedger(self._run_repository, pair, task),
            self._cancellation,
            fingerprint_version=self._run_repository.fingerprint_version(),
        )

    @property
    def aggregate_in_flight(self) -> int:
        return self._config.aggregate_in_flight

    def resume_snapshot(
        self, identity: tuple[str, str, str] | None = None,
    ) -> dict[str, Any]:
        snapshot = self._run_repository.resume_snapshot()
        validate_dispatch_resume(snapshot, identity)
        return snapshot

    def _replay_shared(self) -> None:
        try:
            self._runtime_repository.replay_dispatch_observations()
        except Exception as exc:
            raise RuntimeError(
                "shared runtime replay failed before dispatch"
            ) from exc

    def close(self) -> None:
        close = getattr(self._runtime_repository, "close", None)
        if callable(close): close()


class _NeverCancelled:
    @staticmethod
    def cancelled() -> bool:
        return False


def _validated_claim(task: dict[str, Any]) -> str:
    payload = task["claim_evidence_payload"]
    if (set(payload) != {"claim", "claim_context", "citation_marker"}
        or any(not isinstance(payload.get(name), str) or not payload[name].strip()
               for name in payload)):
        raise RuntimeError("persisted claim payload is inconsistent")
    return payload["claim"]


def _validate_task(task: dict[str, Any]) -> None:
    if (not isinstance(task, dict) or task.get("kind") != "claim_evidence"
        or task.get("semantic_contract") != CONTRACT_ID
        or any(not isinstance(task.get(name), str) or not task[name]
               for name in ("claim_id", "ref_id"))
        or not isinstance(task.get("scope", ""), str)
        or not isinstance(task.get("claim_evidence_payload"), dict)
        or not isinstance(task.get("effective_context"), dict)):
        raise RuntimeError("claim-evidence task envelope is invalid")


def _terminal_status(status: str) -> str:
    values = {"normal": "accepted", "contested": "uncertain", "uncertain": "uncertain", "exhausted": "exhausted", "cancelled": "cancelled"}
    if status not in values:
        raise RuntimeError("controller terminal status is invalid")
    return values[status]
