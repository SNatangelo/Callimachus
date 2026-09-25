#!/bin/sh
# adapters/host-bridge/codex_bridge.sh
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Host bridge: routes CitationVerifier's `host` LLM backend to the `codex` CLI.
# Usage: export CITATION_VERIFIER_LLM_HOST_COMMAND="sh /abs/path/adapters/host-bridge/codex_bridge.sh"
# NOTE: `codex exec` flags are version-dependent — verify with `codex exec --help`.
set -eu

DEFAULT_MODEL="${DEFAULT_MODEL:-gpt-5-mini}"
PAYLOAD="$(cat)"

MODEL="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("model") or "")')"
PROMPT="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("system") or "")+"\n\n"+(d.get("user") or ""))')"
[ -n "$MODEL" ] || MODEL="$DEFAULT_MODEL"

OUT_FILE="$(mktemp)"
trap 'rm -f "$OUT_FILE"' EXIT
printf '%s' "$PROMPT" | codex exec --skip-git-repo-check --sandbox read-only \
    --output-last-message "$OUT_FILE" --model "$MODEL" - >/dev/null 2>&1 || true
ANSWER="$(cat "$OUT_FILE" 2>/dev/null || true)"
printf '%s' "$ANSWER" | python3 -c 'import sys,json; print(json.dumps({"text": sys.stdin.read()}))'
