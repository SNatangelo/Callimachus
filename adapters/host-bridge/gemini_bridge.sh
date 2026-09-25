#!/bin/sh
# adapters/host-bridge/gemini_bridge.sh
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Host bridge: routes CitationVerifier's `host` LLM backend to the `gemini` CLI.
# Usage: export CITATION_VERIFIER_LLM_HOST_COMMAND="sh /abs/path/adapters/host-bridge/gemini_bridge.sh"
# NOTE: gemini CLI flags vary by version — verify with `gemini --help`.
set -eu

DEFAULT_MODEL="${DEFAULT_MODEL:-gemini-2.5-flash}"
PAYLOAD="$(cat)"

MODEL="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("model") or "")')"
PROMPT="$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("system") or "")+"\n\n"+(d.get("user") or ""))')"
[ -n "$MODEL" ] || MODEL="$DEFAULT_MODEL"

ANSWER="$(gemini -m "$MODEL" -p "$PROMPT" 2>/dev/null || true)"
printf '%s' "$ANSWER" | python3 -c 'import sys,json; print(json.dumps({"text": sys.stdin.read()}))'
