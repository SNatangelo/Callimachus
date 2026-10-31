#!/bin/sh
# adapters/host-bridge/claude_bridge.sh
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Host bridge: routes CitationVerifier's `host` LLM backend to the `claude` CLI.
# Usage: export CITATION_VERIFIER_LLM_HOST_COMMAND="sh /abs/path/adapters/host-bridge/claude_bridge.sh"
# The model defaults to Haiku (fast, cheap — good for the per-pair Verifier);
# override per call via CITATION_VERIFIER_MODEL, or here via DEFAULT_MODEL.
set -eu

DEFAULT_MODEL="${DEFAULT_MODEL:-claude-haiku-4-5}"
PAYLOAD="$(cat)"

# Parse the stdin JSON with python3 (no jq dependency).
MODEL="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("model") or "")')"
PROMPT="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("system") or "")+"\n\n"+(d.get("user") or ""))')"
[ -n "$MODEL" ] || MODEL="$DEFAULT_MODEL"

ANSWER="$(printf '%s' "$PROMPT" | claude --append-system-prompt "" --output-format json --max-turns 1 --tools "" --model "$MODEL")"
# `claude --output-format json` returns an object with a "result" field.
printf '%s' "$ANSWER" | python3 -c 'import sys,json
raw=sys.stdin.read()
try:
    obj=json.loads(raw); text=obj.get("result", raw) if isinstance(obj,dict) else raw
except Exception:
    text=raw
print(json.dumps({"text": text}))'
