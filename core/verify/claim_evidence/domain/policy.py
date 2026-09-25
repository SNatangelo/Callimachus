# core/verify/claim_evidence/domain/policy.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure retry, Jury2 enforcement, and exhaustion policy."""
from collections import Counter
from dataclasses import dataclass

from .types import CandidateRecord, EVIDENCE_OUTCOMES, StateMachinePolicy


JURY2_LEVELS = frozenset({"off", "low", "medium", "high"})


class PolicyError(ValueError):
    """A state-machine policy or transition request is invalid."""


@dataclass(frozen=True, slots=True)
class MajorityResult:
    winner: str | None
    representative: CandidateRecord | None
    tally: tuple[tuple[str, int], ...]


def validate_policy(policy: StateMachinePolicy) -> StateMachinePolicy:
    if not isinstance(policy, StateMachinePolicy):
        raise PolicyError("state-machine policy is required")
    counts = (
        policy.candidate_cap,
        policy.jury1_technical_cap,
        policy.jury2_technical_cap,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in counts):
        raise PolicyError("all state-machine caps must be positive integers")
    if policy.jury2_level not in JURY2_LEVELS:
        raise PolicyError("Jury2 level is invalid")
    return policy


def jury2_eligible(outcome: str) -> bool:
    return outcome in EVIDENCE_OUTCOMES


def jury2_no_action(*, level: str, cycle: int, candidate_cap: int) -> str:
    if level not in JURY2_LEVELS - {"off"}:
        raise PolicyError("Jury2 no is invalid for this enforcement level")
    if (
        isinstance(cycle, bool)
        or isinstance(candidate_cap, bool)
        or not isinstance(cycle, int)
        or not isinstance(candidate_cap, int)
        or cycle <= 0
        or candidate_cap <= 0
        or cycle > candidate_cap
    ):
        raise PolicyError("candidate-cycle state is invalid")
    if level == "low":
        return "contested"
    return "requeue" if cycle < candidate_cap else "exhausted"


def strict_majority(candidates: tuple[CandidateRecord, ...]) -> MajorityResult:
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, CandidateRecord)
        or candidate.decision.outcome not in EVIDENCE_OUTCOMES
        for candidate in candidates
    ):
        raise PolicyError("majority candidates are invalid")
    counts = Counter(candidate.decision.outcome for candidate in candidates)
    tally = tuple(sorted(counts.items()))
    winners = [
        outcome
        for outcome, count in tally
        if count > len(candidates) / 2
    ]
    if len(winners) != 1:
        return MajorityResult(None, None, tally)
    winner = winners[0]
    representative = next(
        candidate
        for candidate in reversed(candidates)
        if candidate.decision.outcome == winner
    )
    return MajorityResult(winner, representative, tally)
