# core/report/credential_metrics_projection.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pure, secret-free summary of credential-bound HTTP attempts."""

from __future__ import annotations

import re
from typing import Any

_OBSERVATION_FIELDS = {
    "observation_id", "provider", "env_name", "channel", "outcome",
    "http_status", "created_at",
}
_COUNT_FIELDS = (
    "calls", "successful_http_responses", "http_401", "http_403",
    "http_429", "other_http_errors", "network_errors",
)
_CHANNELS = ("resolve", "fetch", "search")
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def _counts(records: list[dict[str, Any]]) -> dict[str, int]:
    statuses = [
        row["http_status"]
        for row in records
        if row["http_status"] is not None
    ]
    return {
        "calls": len(records),
        "successful_http_responses": sum(
            200 <= status <= 299 for status in statuses
        ),
        "http_401": statuses.count(401),
        "http_403": statuses.count(403),
        "http_429": statuses.count(429),
        "other_http_errors": sum(
            not (200 <= status <= 299 or status in {401, 403, 429})
            for status in statuses
        ),
        "network_errors": sum(
            row["outcome"] == "network_error" for row in records
        ),
    }


def _validated_inventory(inventory: Any) -> tuple[str, dict[str, dict[str, Any]]]:
    if (
        type(inventory) is not dict
        or set(inventory) != {"recorded_at", "entries"}
        or type(inventory["recorded_at"]) is not str
        or not inventory["recorded_at"]
    ):
        raise ValueError("credential inventory is invalid")
    entries = inventory["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("credential inventory is incomplete")
    by_env: dict[str, dict[str, Any]] = {}
    for item in entries:
        if (
            type(item) is not dict
            or set(item) != {"provider", "env_name", "present_at_start"}
            or type(item["provider"]) is not str
            or _PROVIDER.fullmatch(item["provider"]) is None
            or type(item["env_name"]) is not str
            or _ENV_NAME.fullmatch(item["env_name"]) is None
            or type(item["present_at_start"]) is not bool
            or item["env_name"] in by_env
        ):
            raise ValueError("credential inventory is invalid")
        by_env[item["env_name"]] = item
    return inventory["recorded_at"], by_env


def _validated_observations(
    observations: Any,
    *,
    inventory_by_env: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(observations, (list, tuple)):
        raise ValueError("credential observations must be a list")
    grouped: dict[str, list[dict[str, Any]]] = {
        env_name: [] for env_name in inventory_by_env
    }
    seen: dict[str, dict[str, Any]] = {}
    for item in observations:
        if type(item) is not dict or set(item) != _OBSERVATION_FIELDS:
            raise ValueError("credential observation is invalid")
        observation_id = item["observation_id"]
        if type(observation_id) is not str or not observation_id:
            raise ValueError("credential observation id is invalid")
        previous = seen.get(observation_id)
        if previous is not None:
            if previous != item:
                raise ValueError("credential observation is divergent")
            continue
        seen[observation_id] = item
        env_name = item["env_name"]
        provider = item["provider"]
        outcome = item["outcome"]
        status = item["http_status"]
        if (
            env_name not in inventory_by_env
            or inventory_by_env[env_name]["provider"] != provider
            or item["channel"] not in {"resolve", "fetch", "search"}
            or outcome not in {"response", "http_error", "network_error"}
            or type(item["created_at"]) is not str
            or not item["created_at"]
        ):
            raise ValueError("credential observation is invalid")
        if outcome == "network_error":
            if status is not None:
                raise ValueError("credential observation is invalid")
        elif type(status) is not int or not 100 <= status <= 599:
            raise ValueError("credential observation is invalid")
        grouped[env_name].append(item)
    return grouped


def project_credential_metrics(inventory: Any, observations: Any) -> dict[str, Any]:
    """Project environment names and physical-call outcomes, never key values."""
    if inventory is None:
        if observations:
            raise ValueError("credential observations exist without inventory")
        return {
            "recorded": False,
            "rows": [],
            "totals": {
                **{field: 0 for field in _COUNT_FIELDS},
                "keys_present_at_start": 0,
                "keys_used": 0,
            },
        }

    recorded_at, by_env = _validated_inventory(inventory)
    grouped = _validated_observations(
        observations, inventory_by_env=by_env
    )
    rows = []
    for provider, env_name in sorted((row["provider"], row["env_name"]) for row in by_env.values()):
        records = grouped[env_name]
        counts = _counts(records)
        rows.append(
            {
                "provider": provider,
                "env_name": env_name,
                "present_at_start": by_env[env_name]["present_at_start"],
                "used": bool(counts["calls"]),
                **counts,
                "by_channel": {
                    channel: _counts([
                        row for row in records if row["channel"] == channel
                    ])
                    for channel in _CHANNELS
                },
                "warning_code": (
                    "credential_rejected_or_not_entitled"
                    if counts["calls"]
                    and counts["http_401"] + counts["http_403"]
                    == counts["calls"]
                    else None
                ),
            }
        )
    totals = {
        field: sum(row[field] for row in rows) for field in _COUNT_FIELDS
    }
    totals.update(
        {
            "keys_present_at_start": sum(
                row["present_at_start"] for row in rows
            ),
            "keys_used": sum(row["used"] for row in rows),
        }
    )
    return {
        "recorded": True,
        "recorded_at": recorded_at,
        "rows": rows,
        "totals": totals,
    }
