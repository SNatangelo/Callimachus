# core/verify/claim_evidence/contracts/prompt_loader.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed loading for versioned jury system prompts."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .schema import ContractError


class PromptSpecError(ContractError):
    """A production prompt specification is missing or invalid."""


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    version: int
    system_prompt: str
    sha256: str


@dataclass(frozen=True)
class Jury1FlowPromptSpec:
    """One shared cacheable prompt plus closed, task-specific instructions."""

    prompt_id: str
    version: int
    system_prompt: str
    tasks: tuple[tuple[str, str], ...]
    sha256: str

    def task_prompt(self, task_id: str) -> str:
        for known_id, prompt in self.tasks:
            if known_id == task_id:
                return prompt
        raise PromptSpecError("Jury1 flow task is invalid")


_FIELDS = frozenset({"prompt_id", "version", "system_prompt"})
_FLOW_FIELDS = frozenset({"prompt_id", "version", "system_prompt", "tasks"})
JURY1_FLOW_TASK_IDS = (
    "support_gate",
    "full_support_gate",
    "contrary_gate",
    "topic_gate",
    "explanation_evidence",
)
_PROMPT_ID = re.compile(r"[a-z][a-z0-9_]*")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise PromptSpecError("prompt specification must not contain duplicate JSON keys")
    return dict(pairs)


def load_prompt_spec(path: Path, expected_prompt_id: str) -> PromptSpec:
    """Load one immutable production prompt specification without fallback."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PromptSpecError("prompt specification cannot be read") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, PromptSpecError) as exc:
        raise PromptSpecError("prompt specification is malformed") from exc
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise PromptSpecError("prompt specification fields must be exact")

    prompt_id = value["prompt_id"]
    if (
        not isinstance(prompt_id, str)
        or "\x00" in prompt_id
        or _PROMPT_ID.fullmatch(prompt_id) is None
        or prompt_id != expected_prompt_id
    ):
        raise PromptSpecError("prompt_id is invalid")
    version = value["version"]
    if type(version) is not int or version <= 0:
        raise PromptSpecError("version is invalid")
    paragraphs = value["system_prompt"]
    if (
        not isinstance(paragraphs, list)
        or not paragraphs
        or any(
            not isinstance(paragraph, str)
            or not paragraph.strip()
            or "\x00" in paragraph
            or re.search(r"[A-Za-z]", paragraph) is None
            for paragraph in paragraphs
        )
    ):
        raise PromptSpecError("system_prompt is invalid")

    system_prompt = "\n\n".join(paragraphs)
    return PromptSpec(prompt_id, version, system_prompt, sha256(system_prompt.encode("utf-8")).hexdigest())


def load_jury1_flow_prompt_spec(
    path: Path, expected_prompt_id: str,
) -> Jury1FlowPromptSpec:
    """Load the closed Jury1 decision-tree prompt package without fallback."""
    value = _read_prompt_object(path)
    if set(value) != _FLOW_FIELDS:
        raise PromptSpecError("Jury1 flow prompt fields must be exact")
    prompt_id = _prompt_id(value["prompt_id"], expected_prompt_id)
    version = _version(value["version"])
    system_paragraphs = _paragraphs(value["system_prompt"], "system_prompt")
    raw_tasks = value["tasks"]
    if not isinstance(raw_tasks, dict) or tuple(raw_tasks) != JURY1_FLOW_TASK_IDS:
        raise PromptSpecError("Jury1 flow tasks must be exact and ordered")
    tasks = tuple(
        (task_id, "\n\n".join(_paragraphs(raw_tasks[task_id], task_id)))
        for task_id in JURY1_FLOW_TASK_IDS
    )
    canonical = json.dumps(
        {
            "prompt_id": prompt_id,
            "version": version,
            "system_prompt": system_paragraphs,
            "tasks": {
                task_id: raw_tasks[task_id] for task_id in JURY1_FLOW_TASK_IDS
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return Jury1FlowPromptSpec(
        prompt_id,
        version,
        "\n\n".join(system_paragraphs),
        tasks,
        sha256(canonical).hexdigest(),
    )


def _read_prompt_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PromptSpecError("prompt specification cannot be read") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_object_without_duplicate_keys)
    except (json.JSONDecodeError, PromptSpecError) as exc:
        raise PromptSpecError("prompt specification is malformed") from exc
    if not isinstance(value, dict):
        raise PromptSpecError("prompt specification must be an object")
    return value


def _prompt_id(value: Any, expected: str) -> str:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or _PROMPT_ID.fullmatch(value) is None
        or value != expected
    ):
        raise PromptSpecError("prompt_id is invalid")
    return value


def _version(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise PromptSpecError("version is invalid")
    return value


def _paragraphs(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(paragraph, str)
            or not paragraph.strip()
            or "\x00" in paragraph
            or re.search(r"[A-Za-z]", paragraph) is None
            for paragraph in value
        )
    ):
        raise PromptSpecError(f"{field} is invalid")
    return value
