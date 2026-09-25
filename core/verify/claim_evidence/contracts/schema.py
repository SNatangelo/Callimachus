# core/verify/claim_evidence/contracts/schema.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared fail-closed JSON-shape checks."""
import json
from typing import Any

from ..domain.types import PROTOCOL_ERROR_CODES


class ContractError(ValueError):
    """A model response cannot enter the claim-evidence contract."""

    def __init__(self, message: str, *, code: str = "contract_invalid") -> None:
        if code not in PROTOCOL_ERROR_CODES:
            raise ValueError("protocol error code is invalid")
        super().__init__(message)
        self.code = code


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ContractError(
            "response must not contain duplicate JSON keys",
            code="json_duplicate_keys",
        )
    return dict(pairs)


def _decode_single_object(text: str) -> Any:
    """Decode one object while rejecting ambiguous wrapper structure."""
    decoder = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys)
    stripped = text.strip()
    if not stripped:
        raise ContractError("response content must not be empty", code="response_content_empty")
    try:
        value, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        pass
    else:
        if not stripped[end:].strip():
            return value

    candidates: list[tuple[dict[str, Any], int, int]] = []
    index = 0
    while index < len(stripped):
        start = stripped.find("{", index)
        if start < 0:
            break
        try:
            candidate, end = decoder.raw_decode(stripped, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(candidate, dict):
            candidates.append((candidate, start, end))
            index = end
        else:
            index = start + 1

    if not candidates:
        raise ContractError(
            "response must contain a JSON object",
            code="json_object_missing",
        )
    if len(candidates) > 1:
        raise ContractError(
            "response must contain one unambiguous JSON object",
            code="json_object_ambiguous",
        )
    value, start, end = candidates[0]
    before, after = stripped[:start].strip(), stripped[end:].strip()
    wrapper = before + after
    if any(character in wrapper for character in "{}[]"):
        raise ContractError(
            "response wrapper contains competing JSON structure",
            code="json_wrapper_competing",
        )
    for segment in (before, after):
        if not segment:
            continue
        try:
            _extra, extra_end = decoder.raw_decode(segment)
        except json.JSONDecodeError:
            continue
        if not segment[extra_end:].strip():
            raise ContractError(
                "response wrapper contains another JSON value",
                code="json_wrapper_extra_value",
            )
    return value


def exact_object(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if isinstance(value, str):
        value = _decode_single_object(value)
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(
            "response fields must be exact",
            code="response_fields_invalid",
        )
    return value


def nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(
            f"{field} must be a non-empty string",
            code="response_string_invalid",
        )
    return value


def null(value: Any, field: str) -> None:
    if value is not None:
        raise ContractError(
            f"{field} must be JSON null",
            code="response_expected_null",
        )
    return None


def unique_nonempty_strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(
            f"{field} must be a JSON list of non-empty strings",
            code="response_string_list_invalid",
        )
    if len(value) != len(set(value)):
        raise ContractError(
            f"{field} must not contain duplicates",
            code="response_list_duplicate",
        )
    return tuple(value)
