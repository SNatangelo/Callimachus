# Host-bridge scripts

## Overview

The CitationVerifier tool includes a generic `host` LLM backend (`core/backends/host.py`) that routes LLM calls to an external command. This allows you to integrate any agent CLI without modifying the core CitationVerifier code.

## The Host Backend Contract

When you set `CITATION_VERIFIER_LLM_HOST_COMMAND` to a command, that command is executed once per LLM call. It receives a JSON object on STDIN containing:

| Field | Type | Description |
|-------|------|-------------|
| `host` | string | Hostname or identifier for the backend |
| `task_kind` | string | Type of task (e.g., "verification", "parsing") |
| `model` | string | Model name or empty string (use default if empty) |
| `max_tokens` | int | Maximum tokens for the response |
| `allow_tools` | bool | Whether tools are allowed in the response |
| `system` | string | System prompt / instructions |
| `user` | string | User query or input |

The command must output a JSON response with a `text` field containing the model's answer:
```json
{"text": "...model response..."}
```

Alternatively, the backend accepts `{"result": "..."}` or raw text output.

## Bridge Scripts

These scripts act as adapters between the generic `host` backend and specific agent CLIs. Each script selects a default model optimized for the use case.

| Script | CLI | Default Model | Purpose |
|--------|-----|---------------|---------|
| `claude_bridge.sh` | `claude` CLI | `claude-haiku-4-5` | Anthropic Claude (fast & cost-effective for per-citation verification) |
| `codex_bridge.sh` | `codex` CLI | `gpt-5-mini` | OpenAI Codex (GPT-based inference) |
| `gemini_bridge.sh` | `gemini` CLI | `gemini-2.5-flash` | Google Gemini (multimodal & fast) |

## Usage

### 1. Activate the Host Backend

Choose your preferred bridge and set environment variables:

```sh
export CITATION_VERIFIER_VERIFY_BACKENDS=host
export CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
export CITATION_VERIFIER_LLM_HOST_COMMAND="sh $(pwd)/adapters/host-bridge/claude_bridge.sh"
```

### 2. (Optional) Override the Model

Set `DEFAULT_MODEL` in your shell to use a different model:

```sh
export DEFAULT_MODEL="claude-opus-4-1"
export CITATION_VERIFIER_LLM_HOST_COMMAND="sh $(pwd)/adapters/host-bridge/claude_bridge.sh"
```

Or set `CITATION_VERIFIER_MODEL` to control the model globally:

```sh
export CITATION_VERIFIER_MODEL=claude-haiku-4-5
export CITATION_VERIFIER_LLM_HOST_COMMAND="sh $(pwd)/adapters/host-bridge/claude_bridge.sh"
```

### 3. Run CitationVerifier

```sh
python run.py --input paper.docx --autonomous
```

## Example: Full Setup

```sh
#!/bin/bash
cd /path/to/citation-verifier

# Use Claude Haiku (default) for fast, cost-effective verification
export CITATION_VERIFIER_VERIFY_BACKENDS=host
export CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
export CITATION_VERIFIER_LLM_HOST_COMMAND="sh $(pwd)/adapters/host-bridge/claude_bridge.sh"
export CITATION_VERIFIER_MODEL=claude-haiku-4-5

# Run the verifier
python run.py --input paper.docx --autonomous
```

## Notes

- **Generic Escape Hatch**: These bridges are the way to plug ANY future agent CLI without modifying `core/`. If a new agent CLI emerges, simply create a new bridge script following the same pattern.

- **Dedicated Backends**: The CitationVerifier also includes dedicated backends (`core/backends/claude_cli.py`, `core/backends/gemini_cli.py`, etc.) that can auto-select models within their respective hosts. These are usually simpler to use if available for your CLI. The `host` bridge is the generic fallback for custom or emerging CLIs.

- **Model Selection**: The default model in each bridge is optimized for cost and speed. Override `DEFAULT_MODEL` per script or use `CITATION_VERIFIER_MODEL` environment variable to change the model globally for all LLM calls.

- **Dependencies**: Each bridge requires its corresponding CLI installed and in your `PATH`:
  - `claude_bridge.sh` requires the `claude` CLI (Anthropic)
  - `codex_bridge.sh` requires the `codex` CLI (OpenAI / Codex)
  - `gemini_bridge.sh` requires the `gemini` CLI (Google)

- **JSON Parsing**: These scripts use Python 3's built-in `json` module to parse the incoming payload and format the response. No external dependencies like `jq` are required.
