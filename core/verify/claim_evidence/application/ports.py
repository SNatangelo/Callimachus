# core/verify/claim_evidence/application/ports.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow injected ports for claim-evidence application coordination."""
from __future__ import annotations

from typing import Protocol, TypeAlias

from ..domain.replies import (
    SupportGateAnswer,
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    FullSupportGateAnswer,
    Jury2Answer,
    TopicGateAnswer,
)
from ..domain.types import (
    CandidateRecord,
    ControllerEvent,
    DispatchAssignment,
    LogicalRequest,
    TechnicalFailure,
)


JuryReply: TypeAlias = (
    SupportGateAnswer
    | FullSupportGateAnswer
    | ContraryGateAnswer
    | TopicGateAnswer
    | ExplanationEvidenceAnswer
    | Jury2Answer
    | TechnicalFailure
)


class FatalDispatchError(RuntimeError):
    """Audit or shared-state persistence failed around a physical dispatch."""


class RequestFactoryPort(Protocol):
    def jury1_flow_request(
        self,
        stage: str,
        candidate_cycle: int,
        *,
        determined_outcome: str | None = None,
    ) -> LogicalRequest: ...

    def prior_technical_failures(
        self,
        stage: str,
        candidate_cycle: int,
    ) -> int: ...

    def jury2_request(
        self,
        candidate: CandidateRecord,
    ) -> LogicalRequest: ...


class JuryDispatchPort(Protocol):
    def dispatch(self, request: LogicalRequest) -> JuryReply: ...


class ProviderTransportPort(Protocol):
    def dispatch(
        self,
        request: LogicalRequest,
        assignment: DispatchAssignment,
    ) -> JuryReply: ...


class AggregateLimiterPort(Protocol):
    def acquire(self, timeout: float | None = None) -> bool: ...

    def release(self) -> None: ...


class LedgerPort(Protocol):
    def append(self, event: ControllerEvent) -> None: ...


class CancellationPort(Protocol):
    def cancelled(self) -> bool: ...
