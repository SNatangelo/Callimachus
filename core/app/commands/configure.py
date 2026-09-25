#!/usr/bin/env python3
# core/app/commands/configure.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
configure.py — one-stop setup for Citation Verifier ("turn the cage on", with a window).

It does what DEPLOYMENT.md describes, but from a single tool:
  1. selectively patches explicitly supplied non-secret .env fields;
  2. optionally generates a signing key file (chmod 600) — the secret the agent must not hold;
  3. optionally prepares manual Claude Code hook instructions with absolute paths,
     putting the signing key (and optional state dir) only in the hook command's environment;
  4. self-tests the selected configuration artefacts.

Two front-ends, same logic:
  • GUI (default):     python run.py configure          # a tkinter window
  • headless (CI/ssh): python run.py configure --headless --mailto you@x --accuracy standard

Claude Code integration is an explicit, separate step:
  python run.py configure --headless --prepare-claude-hooks --gen-key

The pure functions (render_env / write_env / generate_key / wire_hooks / self_test) carry no
tkinter dependency, so they are unit-tested without a display. stdlib only.
"""
from __future__ import annotations

import argparse
import json
import ntpath
import os
import posixpath
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from core.invocation import format_run_examples, run_command

try:
    from core.infra.integrity import signing as _signing
except ImportError:  # direct execution
    import signing as _signing  # type: ignore

# Kept in lockstep with core.app.run.
ACCURACY_CHOICES = ("maximum", "maximum_fallback", "standard", "abstract", "standard_web")
DEFAULT_ACCURACY = "standard"
ACCURACY_NOTES = {
    "maximum": "full text only — a source with no retrievable full text is left unchecked",
    "maximum_fallback": "full text; abstract provisional when existence is unknown",
    "standard": "full text preferred, abstract fallback on paywall  (recommended default)",
    "abstract": "abstract is the final tier",
    "standard_web": "legacy alias of standard; generic third-party web pages are not "
                    "admitted as citation evidence",
}
ENV_ACCURACY = "CITATION_VERIFIER_ACCURACY"
ENV_MAILTO = "CITATION_VERIFIER_MAILTO"
ENV_GBOOKS = "GOOGLE_BOOKS_API_KEY"
ENV_CORE_API_KEY = "CORE_API_KEY"
ENV_OPENALEX_KEY = "OPENALEX_API_KEY"
ENV_SEMANTIC_SCHOLAR_API_KEY = "SEMANTIC_SCHOLAR_API_KEY"
ENV_LENS_API_KEY = "LENS_API_KEY"
ENV_FETCH_PROVIDERS = "CITATION_VERIFIER_FETCH_PROVIDERS"
ENV_FETCH_PROVIDER_ORDER = "CITATION_VERIFIER_FETCH_PROVIDER_ORDER"
ENV_USER_AGENT = "CITATION_VERIFIER_USER_AGENT"
ENV_HTTP_PROFILE = "CITATION_VERIFIER_HTTP_PROFILE"
ENV_CHALLENGE_MODE = "CITATION_VERIFIER_FETCH_CHALLENGE_MODE"
ENV_STATE_DIR = "CITATION_VERIFIER_STATE_DIR"
ENV_VERIFY_BACKENDS = "CITATION_VERIFIER_VERIFY_BACKENDS"
ENV_VERIFY_JURY2_LEVEL = "CITATION_VERIFIER_VERIFY_JURY2_LEVEL"
ENV_VERIFY_MAX_IN_FLIGHT = "CITATION_VERIFIER_VERIFY_MAX_IN_FLIGHT"
ENV_VERIFY_MAX_TOKENS = "CITATION_VERIFIER_VERIFY_MAX_TOKENS"
ENV_REASONING = "CITATION_VERIFIER_REASONING"
ENV_REASONING_EFFORT = "CITATION_VERIFIER_REASONING_EFFORT"
ENV_VERIFY_PACING_INTERVAL_MS = "CITATION_VERIFIER_VERIFY_PACING_INTERVAL_MS"
ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"
ENV_OPENAI_MODEL = "OPENAI_MODEL"
ENV_OPENAI_KEY = "OPENAI_API_KEY"
ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
ENV_GEMINI_KEY = "GEMINI_API_KEY"
ENV_OPENROUTER_KEY = "OPENROUTER_API_KEY"
ENV_OLLAMA_HOST = "OLLAMA_HOST"
ENV_OLLAMA_KEY = "OLLAMA_API_KEY"
ENV_FREETOKEN_HOST = "FREETOKEN_HOST"
ENV_FREETOKEN_MODEL = "FREETOKEN_MODEL"
ENV_ZHIPUAI_KEY = "ZHIPUAI_API_KEY"
ENV_ZHIPUAI_MODEL = "ZHIPUAI_MODEL"
ENV_MISTRAL_KEY = "MISTRAL_API_KEY"
ENV_MISTRAL_MODEL = "MISTRAL_MODEL"
ENV_OPENCODE_KEY = "OPENCODE_API_KEY"
ENV_OPENCODE_MODEL = "OPENCODE_MODEL"
ENV_MODEL = "CITATION_VERIFIER_MODEL"
ENV_HOST_COMMAND = "CITATION_VERIFIER_LLM_HOST_COMMAND"
ENV_SIGNING_KEY_FILE = "CITATION_VERIFIER_SIGNING_KEY_FILE"

GOOGLE_BOOKS_HELP_URL = "https://console.cloud.google.com/apis/credentials"
GOOGLE_BOOKS_HELP = (
    "Optional. Unlocks the book 'preview_snippet' tier and lifts the keyless quota.\n"
    "To get one (free): open the Google Cloud Console → create/select a project →\n"
    "‘APIs & Services’ → enable the ‘Books API’ → ‘Credentials’ → ‘Create credentials’\n"
    "→ ‘API key’. Paste the key here. Leave empty to run without the book preview tier.")

ARM_SCRIPT = os.path.join("adapters", "claude-code", "hooks", "citation_arm.py")
STOP_SCRIPT = os.path.join("adapters", "claude-code", "hooks", "citation_verify_stop.py")


# --------------------------------------------------------------------------- #
#  path discovery                                                              #
# --------------------------------------------------------------------------- #

def pkg_dir(start=None):
    """Return the repository root containing ``core/`` and ``run.py``.

    The command lives below ``core/app/commands``; locating the root from its
    own path keeps hook and configuration paths independent of the caller's
    current working directory.
    """
    here = Path(__file__).resolve()
    candidates = ([Path(start)] if start else []) + list(here.parents) + [Path.cwd()]
    for base in candidates:
        if (base / "core").is_dir() and (base / "run.py").is_file():
            return str(base.resolve())
    return str(here.parents[3])


def default_env_path(pkg=None):
    """Where to write ``.env``: at the repository root beside ``.env.example``."""
    root = Path(pkg or pkg_dir())
    return str(root / ".env")


def default_key_path(*, platform_name=None, environ=None, home=None):
    """A user-writable default (no sudo). DEPLOYMENT.md shows a root-owned /etc path for a
    stronger boundary; this default at least keeps the key out of the repo."""
    environ = os.environ if environ is None else environ
    platform_name = os.name if platform_name is None else platform_name
    home = os.path.expanduser("~") if home is None else home
    if platform_name == "nt":
        base = (environ.get("LOCALAPPDATA") or environ.get("APPDATA")
                or ntpath.join(home, "AppData", "Local"))
        return ntpath.join(base, "citation-verifier", "signing.key")
    else:
        base = environ.get("XDG_CONFIG_HOME") or posixpath.join(home, ".config")
        return posixpath.join(base, "citation-verifier", "signing.key")


def default_settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _running_under_wsl(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("WSL_INTEROP") or environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  pure actions (no tkinter)                                                   #
# --------------------------------------------------------------------------- #

def render_env(mailto, google_key, accuracy):
    """The .env text. Mirrors .env.example, with the values filled in."""
    if accuracy not in ACCURACY_CHOICES:
        accuracy = DEFAULT_ACCURACY
    return (
        "# Citation Verifier configuration - generated by run.py configure.\n"
        "# Load with:  set -a; . ./.env; set +a   (the .env is git-ignored)\n\n"
        "# -- Core settings -----------------------------------------------------\n"
        "# Verification regime: " + " | ".join(ACCURACY_CHOICES) + "\n"
        f"{ENV_ACCURACY}={accuracy}\n\n"
        "# Contact email for Crossref / Europe PMC / Unpaywall / OpenAlex.\n"
        f"{ENV_MAILTO}={mailto or ''}\n\n"
        "# -- Content API keys --------------------------------------------------\n"
        f"{ENV_GBOOKS}={google_key or ''}\n"
        f"{ENV_CORE_API_KEY}=\n"
        f"# OpenAlex API key — lifts rate limits (free; register at https://openalex.org/account)\n"
        f"{ENV_OPENALEX_KEY}=\n"
        f"# Semantic Scholar API key — optional title-search resolver fallback\n"
        f"{ENV_SEMANTIC_SCHOLAR_API_KEY}=\n"
        f"# Lens.org scholarly API token — optional title-search resolver fallback\n"
        f"{ENV_LENS_API_KEY}=\n\n"
        "# -- Final Verify runtime -----------------------------------------------\n"
        "# Explicit CSV of registered providers; there is no automatic fallback.\n"
        f"{ENV_VERIFY_BACKENDS}=\n"
        "# Required: off | low | medium | high.\n"
        f"{ENV_VERIFY_JURY2_LEVEL}=\n"
        "# Aggregate cap across all providers, models, credentials and Jury roles.\n"
        f"{ENV_VERIFY_MAX_IN_FLIGHT}=4\n"
        "# Per-call output budget (or none when supported) and reasoning controls.\n"
        f"{ENV_VERIFY_MAX_TOKENS}=4000\n"
        f"{ENV_REASONING}=auto\n"
        f"{ENV_REASONING_EFFORT}=medium\n"
        "# Ordinary dispatch-start pacing; zero means no configured delay.\n"
        f"{ENV_VERIFY_PACING_INTERVAL_MS}=0\n\n"
        "# Generic model fallback. Provider-specific model variables below win.\n"
        f"{ENV_MODEL}=\n\n"
        "# -- LLM Backend - API keys / endpoints ---------------------------------\n"
        "# DeepSeek / OpenAI-compatible  (OPENAI_BASE_URL + OPENAI_API_KEY + OPENAI_MODEL)\n"
        f"{ENV_OPENAI_BASE_URL}=\n"
        f"{ENV_OPENAI_KEY}=\n"
        f"{ENV_OPENAI_MODEL}=\n\n"
        "# Anthropic / Claude CLI\n"
        f"{ENV_ANTHROPIC_KEY}=\n\n"
        "# Google Gemini\n"
        f"{ENV_GEMINI_KEY}=\n\n"
        "# OpenRouter\n"
        f"{ENV_OPENROUTER_KEY}=\n\n"
        "# GLM (ZhipuAI / z.ai)\n"
        f"{ENV_ZHIPUAI_KEY}=\n"
        f"{ENV_ZHIPUAI_MODEL}=\n\n"
        "# Mistral AI\n"
        f"{ENV_MISTRAL_KEY}=\n"
        f"{ENV_MISTRAL_MODEL}=\n\n"
        "# OpenCode Zen (modelli gratuiti: deepseek-v4-flash-free, north-mini-code-free, etc.)\n"
        f"{ENV_OPENCODE_KEY}=\n"
        f"{ENV_OPENCODE_MODEL}=\n\n"
        "# Ollama\n"
        f"{ENV_OLLAMA_HOST}=\n"
        f"{ENV_OLLAMA_KEY}=\n\n"
        "# FreeToken - credentialless local server; empty host uses http://127.0.0.1:1919\n"
        f"{ENV_FREETOKEN_HOST}=\n"
        f"{ENV_FREETOKEN_MODEL}=\n\n"
        "# Host bridge - external command handles LLM requests\n"
        f"{ENV_HOST_COMMAND}=\n\n"
        "# -- HTTP / Fetch settings ---------------------------------------------\n"
        f"{ENV_USER_AGENT}=\n"
        f"{ENV_HTTP_PROFILE}=browser_like\n"
        f"{ENV_CHALLENGE_MODE}=off\n"
        f"{ENV_FETCH_PROVIDERS}=auto\n"
        f"{ENV_FETCH_PROVIDER_ORDER}=\n"
        f"{ENV_SIGNING_KEY_FILE}=\n"
    )

def _validate_env_value(value, name):
    if not isinstance(name, str) or not _ENV_ASSIGNMENT_RE.match(f"{name}="):
        raise ValueError("environment variable name is invalid")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} must not contain a newline")


_ENV_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<separator>[ \t]*=).*$"
)


def _env_value_layout(raw):
    """Return semantic value plus formatting around one dotenv right-hand side."""
    quote = None
    escaped = False
    comment_at = None
    for index, char in enumerate(raw):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"" and not raw[:index].strip():
            quote = char
        elif char == "#" and (index == 0 or raw[index - 1].isspace()):
            comment_at = index
            break

    value_region = raw if comment_at is None else raw[:comment_at]
    value_end = len(value_region.rstrip(" \t"))
    leading_len = len(value_region) - len(value_region.lstrip(" \t"))
    leading = value_region[:leading_len]
    token = value_region[leading_len:value_end]
    suffix = raw[value_end:]
    wrapper = token[0] if len(token) >= 2 and token[0] == token[-1] \
        and token[0] in "'\"" else ""
    semantic = token[1:-1] if wrapper else token
    return semantic, leading, wrapper, suffix


def _replace_env_values(text, updates):
    """Replace only requested simple NAME=value records, preserving all other bytes."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    seen = set()
    output = []
    for line in lines:
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        match = _ENV_ASSIGNMENT_RE.match(body)
        name = match.group("name") if match else None
        if match and name in updates:
            existing, leading, wrapper, suffix = _env_value_layout(
                body[match.end("separator"):]
            )
            replacement = str(updates[name])
            if replacement == existing:
                output.append(line)
                seen.add(name)
                continue
            rendered = (
                f"{wrapper}{replacement}{wrapper}"
                if wrapper and wrapper not in replacement else replacement
            )
            output.append(
                f"{match.group('prefix')}{name}{match.group('separator')}"
                f"{leading}{rendered}{suffix}{ending}"
            )
            seen.add(name)
        else:
            output.append(line)
    missing = [(name, value) for name, value in updates.items() if name not in seen]
    if missing:
        if output and not output[-1].endswith(("\n", "\r")):
            output.append(newline)
        output.extend(f"{name}={value}{newline}" for name, value in missing)
    return "".join(output)


def _atomic_write(path, text, mode=None):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".citation-verifier-", dir=directory, text=True)
    try:
        if mode is not None:
            os.chmod(temporary, stat.S_IMODE(mode))
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            fd = None
            f.write(text)
        os.replace(temporary, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_env_updates(path, updates, *, template_path=None):
    """Atomically apply requested dotenv values while preserving unrelated bytes."""
    if not hasattr(updates, "items"):
        raise ValueError("updates must be a mapping")
    updates = dict(updates)
    for name, value in updates.items():
        _validate_env_value(value, name)
    if not updates and os.path.exists(path):
        return path
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            current = f.read()
        _atomic_write(path, _replace_env_values(current, updates), os.stat(path).st_mode)
        return path
    template_path = template_path or os.path.join(
        os.path.dirname(os.path.abspath(path)), ".env.example",
    )
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8", newline="") as f:
            current = f.read()
    else:
        current = render_env("", "", DEFAULT_ACCURACY)
    _atomic_write(path, _replace_env_values(current, updates))
    return path


def write_env(path, mailto=None, google_key=None, accuracy=None, *, template_path=None):
    """Apply legacy explicit configuration values without damaging an .env file."""
    updates = {
        ENV_MAILTO: mailto,
        ENV_GBOOKS: google_key,
        ENV_ACCURACY: accuracy,
    }
    updates = {name: value for name, value in updates.items() if value is not None}
    return write_env_updates(path, updates, template_path=template_path)


def generate_key(path, *, overwrite=False):
    """Create a 256-bit hex signing key at `path` with 0600 permissions. Returns the path.
    Refuses to clobber an existing key unless overwrite=True (a new key invalidates every
    report already sealed with the old one)."""
    path = os.path.abspath(path)
    if os.path.exists(path) and not overwrite:
        return path  # keep the existing key — re-keying is an explicit choice
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = secrets.token_hex(32)
    # O_CREAT|O_WRONLY|O_TRUNC with mode 0600 so the secret is never world/group readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (key + "\n").encode("ascii"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # enforce 0600 even if it pre-existed
    except OSError:
        pass
    return path


def _env_prefix(env: dict, *, platform_name=None) -> str:
    """A shell env-prefix for a hook command. POSIX: VAR=val VAR2=val2 ; Windows: a cmd /c
    wrapper. Values are absolute paths/dirs we control (no spaces expected); quoted anyway."""
    items = [(k, v) for k, v in env.items() if v]
    if not items:
        return ""
    if (os.name if platform_name is None else platform_name) == "nt":
        sets = " & ".join(f'set "{k}={v}"' for k, v in items)
        return sets + " & "   # caller wraps the whole thing in cmd /c
    return " ".join(f'{k}="{v}"' for k, v in items) + " "


def hook_command(pkg, script_rel, *, key_file=None, state_dir=None, executable=None,
                 platform_name=None, target="native", wsl_distro=None):
    """Absolute-path hook command, with the signing key / state dir injected into the hook's
    environment only (so it never enters the agent's interactive shell)."""
    platform_name = os.name if platform_name is None else platform_name
    if target not in ("native", "windows-wsl"):
        raise ValueError(f"unknown Claude hook target: {target}")
    if target == "windows-wsl":
        if not isinstance(wsl_distro, str) or not wsl_distro.strip():
            raise ValueError("windows-wsl Claude hook target requires a nonblank WSL distro")
        wsl_distro = wsl_distro.strip()
        script = posixpath.join(pkg, *script_rel.replace("\\", "/").split("/"))
        env = {}
        if key_file:
            env[_signing.ENV_KEY_FILE] = posixpath.abspath(key_file)
        if state_dir:
            env[ENV_STATE_DIR] = posixpath.abspath(state_dir)
        executable = posixpath.abspath(executable or sys.executable)
        argv = ["wsl.exe", "--distribution", wsl_distro, "--cd", posixpath.abspath(pkg),
                "--exec", "env"]
        argv.extend(f"{name}={value}" for name, value in env.items())
        argv.extend([executable, script])
        return subprocess.list2cmdline(argv)
    paths = ntpath if platform_name == "nt" else posixpath
    script = paths.join(pkg, *script_rel.replace("\\", "/").split("/"))
    env = {}
    if key_file:
        env[_signing.ENV_KEY_FILE] = paths.abspath(key_file)
    if state_dir:
        env[ENV_STATE_DIR] = paths.abspath(state_dir)
    executable = paths.abspath(executable or sys.executable)
    prefix = _env_prefix(env, platform_name=platform_name)
    if platform_name == "nt":
        base = subprocess.list2cmdline([executable, script])
        if prefix:
            base = f'cmd /c "{prefix}{base}"'
    else:
        base = f"{prefix}{shlex.quote(executable)} {shlex.quote(script)}"
    return base


def _env_file_values(path):
    """Read only the three GUI settings from a project .env for masked display."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            for line in f:
                match = _ENV_ASSIGNMENT_RE.match(line.rstrip("\r\n"))
                if match and match.group("name") in (
                    ENV_MAILTO, ENV_GBOOKS, ENV_ACCURACY,
                ):
                    name = match.group("name")
                    body = line.rstrip("\r\n")
                    value, _leading, _wrapper, _suffix = _env_value_layout(
                        body[match.end("separator"):]
                    )
                    values[name] = value
    except OSError:
        pass
    return values


def gui_config_values(env_path, *, environ=None):
    """Values displayed by the GUI: process environment, then project .env, then defaults."""
    environ = os.environ if environ is None else environ
    file_values = _env_file_values(env_path)
    mailto = environ.get(ENV_MAILTO, file_values.get(ENV_MAILTO, ""))
    google_key = environ.get(ENV_GBOOKS, file_values.get(ENV_GBOOKS, ""))
    accuracy = environ.get(
        ENV_ACCURACY, file_values.get(ENV_ACCURACY, DEFAULT_ACCURACY),
    )
    return mailto, google_key, accuracy if accuracy in ACCURACY_CHOICES else DEFAULT_ACCURACY


def _is_ours(cmd: str) -> bool:
    return "citation_arm.py" in cmd or "citation_verify_stop.py" in cmd


def _load_settings(settings_path):
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f:
            try:
                settings = json.load(f)
            except json.JSONDecodeError:
                raise ValueError(f"{settings_path} is not valid JSON — fix or move it first.")
    return settings


def _render_settings_with_hooks(settings_path, pkg, *, key_file=None, state_dir=None,
                                target="native", wsl_distro=None):
    """Return the Claude settings document with CitationVerifier hooks merged in.

    The project no longer writes JSON files directly; callers can inspect this payload
    or render it into human instructions for a manual patch."""
    settings = _load_settings(settings_path)
    hooks = settings.setdefault("hooks", {})

    def install(event, command):
        groups = hooks.setdefault(event, [])
        # Drop any prior citation-verifier entries (so we update, not duplicate).
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", [])
                              if not _is_ours(h.get("command", ""))]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({"hooks": [{"type": "command", "command": command}]})

    install("UserPromptSubmit",
            hook_command(pkg, ARM_SCRIPT, state_dir=state_dir, target=target,
                         wsl_distro=wsl_distro))
    install("Stop",
            hook_command(pkg, STOP_SCRIPT, key_file=key_file, state_dir=state_dir,
                         target=target, wsl_distro=wsl_distro))
    return settings


def _instructions_path(settings_path):
    return settings_path + ".citation-verifier.txt"


def wire_hooks(settings_path, pkg, *, key_file=None, state_dir=None, target="native",
               wsl_distro=None):
    """Prepare a manual Claude Code hook-instruction file beside settings.json.

    The returned text file contains the fully merged JSON document to paste into Claude
    Code. This keeps the project free of JSON-file writes while preserving a guided
    setup flow."""
    settings = _render_settings_with_hooks(
        settings_path, pkg, key_file=key_file, state_dir=state_dir, target=target,
        wsl_distro=wsl_distro,
    )
    out_path = _instructions_path(settings_path)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    rendered = json.dumps(settings, indent=2, ensure_ascii=False)
    lines = [
        "CitationVerifier Claude Code hook instructions",
        "",
        f"Target settings file: {settings_path}",
        "Apply the following JSON document manually to your Claude Code settings.",
        "It already preserves foreign hooks and updates only CitationVerifier entries.",
        "",
        rendered,
        "",
    ]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def self_test(*, key_file=None, settings_path=None, instructions_path=None):
    """Confirm the moving parts are live. Returns (ok, [lines])."""
    lines = []
    ok = True
    if key_file:
        try:
            with open(key_file, encoding="utf-8") as f:
                key = f.read().strip()
            payload = b"citation-verifier-selftest"
            seal = _signing.sign(payload, key=key)
            good = (seal["alg"] == "hmac-sha256"
                    and _signing.verify(payload, seal["alg"], seal["sig"], key=key))
            mode = stat.S_IMODE(os.stat(key_file).st_mode)
            lines.append(f"signing key: {'HMAC sign+verify OK' if good else 'FAILED'} "
                         f"(perms {oct(mode)})")
            if not good:
                ok = False
            if os.name != "nt" and mode & 0o077:
                lines.append("  ⚠ key is group/other-readable — tighten to 0600.")
        except OSError as e:
            ok = False
            lines.append(f"signing key: cannot read {key_file} ({e})")
    if instructions_path:
        if os.path.exists(instructions_path):
            lines.append(f"hook instructions: ready at {instructions_path}")
        else:
            ok = False
            lines.append(f"hook instructions: missing at {instructions_path}")
    elif settings_path:
        try:
            with open(settings_path, encoding="utf-8") as f:
                s = json.load(f)
            cmds = [h.get("command", "")
                    for ev in s.get("hooks", {}).values()
                    for g in ev for h in g.get("hooks", [])]
            arm = any("citation_arm.py" in c for c in cmds)
            stop = any("citation_verify_stop.py" in c for c in cmds)
            lines.append(f"hooks: arm {'✓' if arm else '✗'} · Stop {'✓' if stop else '✗'}")
            if not (arm and stop):
                ok = False
        except (OSError, json.JSONDecodeError) as e:
            ok = False
            lines.append(f"hooks: cannot read {settings_path} ({e})")
    if not lines:
        lines.append("nothing to test (no key / settings selected).")
    return ok, lines


def apply_all(*, env_path, mailto=None, google_key=None, accuracy=None,
              gen_key=None, prepare_claude_hooks=False, settings_path=None, state_dir=None,
              claude_hook_target="native", wsl_distro=None):
    """Run the selected steps and return a log (list of strings). Used by both front-ends."""
    pkg = pkg_dir()
    log = []
    instructions = None
    had_env = os.path.exists(env_path)
    p = write_env(env_path, mailto, google_key, accuracy,
                  template_path=os.path.join(pkg, ".env.example"))
    if mailto is None and google_key is None and accuracy is None and had_env:
        log.append(f"kept config unchanged → {p}")
    else:
        log.append(f"wrote config → {p}")
    key_file = None
    if gen_key:
        existed = os.path.exists(os.path.abspath(gen_key))
        key_file = generate_key(gen_key)
        log.append(f"signing key ({'kept existing' if existed else 'created'}) "
                   f"→ {key_file}  (chmod 600)")
    if prepare_claude_hooks:
        sp = settings_path or default_settings_path()
        instructions = wire_hooks(sp, pkg, key_file=key_file, state_dir=state_dir,
                                  target=claude_hook_target, wsl_distro=wsl_distro)
        log.append(f"prepared Claude Code arm + Stop hook instructions → {instructions}")
        if key_file:
            log.append("  signing key path is present only in trusted Hook/CI instructions, not in .env")
        if not key_file:
            log.append("  ⚠ no signing key selected — the Stop hook can seal only with a "
                       "weak sha256 (not the un-forgeable HMAC). Generate a key for full strength.")
    ok, test_lines = self_test(key_file=key_file,
                               settings_path=None,
                               instructions_path=instructions if prepare_claude_hooks else None)
    log.append("self-test: " + ("PASS" if ok else "ATTENTION"))
    log.extend("  " + l for l in test_lines)
    return ok, log


# --------------------------------------------------------------------------- #
#  GUI (tkinter, imported lazily so headless never needs a display)           #
# --------------------------------------------------------------------------- #

def launch_gui():
    import webbrowser
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    pkg = pkg_dir()
    configured_env_path = default_env_path(pkg)
    configured_mailto, configured_gkey, configured_accuracy = gui_config_values(
        configured_env_path
    )
    root = tk.Tk()
    root.title("Citation Verifier — Setup")
    root.geometry("720x680")
    pad = {"padx": 10, "pady": 4}

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    row = 0

    ttk.Label(frm, text="Citation Verifier — configuration & cage",
              font=("", 13, "bold")).grid(row=row, column=0, columnspan=3, sticky="w")
    row += 1
    ttk.Label(frm, text="Fill the fields, choose what to apply, then ‘Apply’. "
                        "The three knobs are not secret; the signing key is, and goes only "
                        "into the hook environment.",
              wraplength=680, foreground="#555").grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
    row += 1

    # --- non-secret config ---
    ttk.Label(frm, text="Contact email (Crossref/Europe PMC/Unpaywall)").grid(
        row=row, column=0, sticky="w", **pad)
    mailto = tk.StringVar(value=configured_mailto)
    ttk.Entry(frm, textvariable=mailto, width=44).grid(
        row=row, column=1, columnspan=2, sticky="we", **pad)
    row += 1

    ttk.Label(frm, text="Google Books API key (optional)").grid(
        row=row, column=0, sticky="w", **pad)
    gkey = tk.StringVar(value=configured_gkey)
    ttk.Entry(frm, textvariable=gkey, width=44, show="•").grid(
        row=row, column=1, sticky="we", **pad)
    ttk.Button(frm, text="How to get one ↗",
               command=lambda: webbrowser.open(GOOGLE_BOOKS_HELP_URL)).grid(
        row=row, column=2, sticky="e", **pad)
    row += 1
    ttk.Label(frm, text=GOOGLE_BOOKS_HELP, foreground="#555",
              font=("", 9)).grid(row=row, column=0, columnspan=3, sticky="w", padx=10)
    row += 1

    ttk.Label(frm, text="Verification regime").grid(row=row, column=0, sticky="w", **pad)
    accuracy = tk.StringVar(value=configured_accuracy)
    note = tk.StringVar(value=ACCURACY_NOTES[configured_accuracy])
    om = ttk.OptionMenu(frm, accuracy, accuracy.get(), *ACCURACY_CHOICES,
                        command=lambda v: note.set(ACCURACY_NOTES.get(v, "")))
    om.grid(row=row, column=1, sticky="w", **pad)
    row += 1
    ttk.Label(frm, textvariable=note, foreground="#555", wraplength=680).grid(
        row=row, column=0, columnspan=3, sticky="w", padx=10)
    row += 1

    ttk.Separator(frm, orient="horizontal").grid(
        row=row, column=0, columnspan=3, sticky="we", pady=8)
    row += 1
    ttk.Label(frm, text="The cage (security)", font=("", 11, "bold")).grid(
        row=row, column=0, columnspan=3, sticky="w", **pad)
    row += 1

    gen_key_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm, text="Generate a signing key (HMAC) if absent",
                    variable=gen_key_var).grid(row=row, column=0, columnspan=2, sticky="w", **pad)
    row += 1
    key_path = tk.StringVar(value=default_key_path())
    ttk.Entry(frm, textvariable=key_path, width=44).grid(
        row=row, column=0, columnspan=2, sticky="we", **pad)
    ttk.Button(frm, text="Browse…", command=lambda: key_path.set(
        filedialog.asksaveasfilename(initialfile=os.path.basename(key_path.get()))
        or key_path.get())).grid(row=row, column=2, sticky="e", **pad)
    row += 1

    hooks_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm, text="Prepare Claude Code hook instructions (arm + Stop)",
                    variable=hooks_var).grid(row=row, column=0, columnspan=2, sticky="w", **pad)
    row += 1
    settings_path = tk.StringVar(value=default_settings_path())
    ttk.Entry(frm, textvariable=settings_path, width=44).grid(
        row=row, column=0, columnspan=2, sticky="we", **pad)
    ttk.Button(frm, text="Browse…", command=lambda: settings_path.set(
        filedialog.askopenfilename() or settings_path.get())).grid(
        row=row, column=2, sticky="e", **pad)
    row += 1

    env_path = tk.StringVar(value=configured_env_path)
    ttk.Label(frm, text=".env destination").grid(row=row, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=env_path, width=44).grid(
        row=row, column=1, columnspan=2, sticky="we", **pad)
    row += 1

    log = tk.Text(frm, height=9, width=84, wrap="word")
    log.grid(row=row, column=0, columnspan=3, sticky="we", **pad)
    log.configure(state="disabled")
    row += 1

    def do_apply():
        try:
            ok, lines = apply_all(
                env_path=env_path.get(), mailto=mailto.get().strip(),
                google_key=gkey.get().strip(), accuracy=accuracy.get(),
                gen_key=key_path.get() if gen_key_var.get() else None,
                prepare_claude_hooks=hooks_var.get(), settings_path=settings_path.get())
        except Exception as e:  # surface any failure in the log, never crash the window
            ok, lines = False, [f"ERROR: {e}"]
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.insert("end", "\n".join(lines))
        log.configure(state="disabled")
        (messagebox.showinfo if ok else messagebox.showwarning)(
            "Citation Verifier", "Setup complete." if ok else
            "Setup ran with warnings — see the log.")

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=3, sticky="e", pady=8)
    ttk.Button(btns, text="Apply", command=do_apply).pack(side="right", padx=4)
    ttk.Button(btns, text="Close", command=root.destroy).pack(side="right", padx=4)
    frm.columnconfigure(1, weight=1)

    root.mainloop()


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=format_run_examples(__doc__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--headless", action="store_true",
                    help="no GUI: apply from flags (CI / ssh)")
    ap.add_argument("--mailto", default=None)
    ap.add_argument("--google-key", default=None)
    ap.add_argument("--accuracy", default=None,
                    choices=list(ACCURACY_CHOICES))
    ap.add_argument("--env-path", default=None, help="where to write .env")
    ap.add_argument(
        "--gen-key", nargs="?", const=default_key_path(), default=None, metavar="PATH",
        help="generate a signing key; omit PATH to use the native OS default",
    )
    ap.add_argument("--prepare-claude-hooks", action="store_true",
                    help="prepare manual Claude Code arm + Stop hook instructions for --settings")
    ap.add_argument("--settings", default=None, help="Claude Code settings.json path")
    ap.add_argument("--claude-hook-target", choices=("native", "windows-wsl"), default="native",
                    help="hook executor target (default: native)")
    ap.add_argument("--wsl-distro", default=None,
                    help="WSL distribution for --claude-hook-target windows-wsl")
    ap.add_argument("--state-dir", default=None,
                    help="optional: put the armed sentinel out of the agent's reach")
    args = ap.parse_args(argv)
    if args.wsl_distro is not None and args.claude_hook_target != "windows-wsl":
        ap.error("--wsl-distro requires --claude-hook-target windows-wsl")
    hook_options_used = (args.settings is not None or args.state_dir is not None
                         or args.claude_hook_target != "native")
    if hook_options_used and not args.prepare_claude_hooks:
        ap.error("--settings, --state-dir, and --claude-hook-target require --prepare-claude-hooks")
    if args.claude_hook_target == "windows-wsl":
        if not args.headless:
            ap.error("--claude-hook-target windows-wsl requires --headless")
        if not _running_under_wsl():
            ap.error("--claude-hook-target windows-wsl requires execution under WSL")
        if not args.settings or not posixpath.isabs(args.settings):
            ap.error("--claude-hook-target windows-wsl requires an explicit absolute --settings path to Windows Claude settings as visible from WSL")
        args.wsl_distro = (args.wsl_distro or "").strip() or os.environ.get(
            "WSL_DISTRO_NAME", ""
        ).strip()
        if not args.wsl_distro:
            ap.error("--claude-hook-target windows-wsl requires --wsl-distro or WSL_DISTRO_NAME")

    if not args.headless:
        try:
            launch_gui()
            return 0
        except Exception as e:  # no display / tkinter missing → guide to headless
            command = run_command(
                "configure", "--headless", "--mailto", "you@example.org",
                "--accuracy", "standard", "--gen-key",
            )
            sys.stderr.write(
                f"Cannot open the GUI ({e}).\nRun headless, e.g.:\n  {command}\n"
            )
            return 2

    ok, log = apply_all(
        env_path=args.env_path or default_env_path(),
        mailto=args.mailto, google_key=args.google_key, accuracy=args.accuracy,
        gen_key=args.gen_key, prepare_claude_hooks=args.prepare_claude_hooks,
        settings_path=args.settings, state_dir=args.state_dir,
        claude_hook_target=args.claude_hook_target, wsl_distro=args.wsl_distro)
    print("\n".join(log))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
