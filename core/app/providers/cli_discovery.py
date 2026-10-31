# core/app/providers/cli_discovery.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Read CLI authentication and catalogs without submitting a generation task."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping

from core.infra._local_executables import find_cli, cli_environment, cli_argv

_TIMEOUT = 8.0
_MAX_BYTES = 512 * 1024
_MAX_MODELS = 500
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-\[\]()]{0,199}\Z")


class _CatalogError(Exception):
    pass


def _close_pipe(stream):
    try:
        stream.close()
    except OSError:
        pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _CatalogError("The CLI returned an invalid catalog response.")
        result[key] = value
    return result


def _decode(raw: bytes) -> dict:
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError):
        raise _CatalogError("The CLI returned an invalid catalog response.") from None
    if not isinstance(result, dict):
        raise _CatalogError("The CLI returned an invalid catalog response.")
    return result


class _Session:
    """Bound both process lifetime and stdout, including a CLI that never exits."""

    def __init__(self, argv: list[str], environ: Mapping[str, str], *, cwd: Path | None = None,
                 timeout: float | None = None):
        self.deadline = time.monotonic() + (_TIMEOUT if timeout is None else timeout)
        self.events = queue.SimpleQueue()
        self.buffer = b""
        self.diagnostics = ""
        self.closing = threading.Event()
        self.writers = []
        child_env = cli_environment(Path(argv[0]).stem, environ)
        self.process = subprocess.Popen(
            cli_argv(argv, child_env), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False,
            cwd=Path(__file__).resolve().parents[3] if cwd is None else cwd,
            env=child_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        self.error_reader.start()

    def _read_errors(self):
        total = 0
        retained = b""
        try:
            while chunk := self.process.stderr.read1(4096):
                total += len(chunk)
                retained = (retained + chunk)[:16384]
                self.diagnostics = retained.decode("utf-8", errors="replace")
                if total > _MAX_BYTES:
                    self.events.put(_CatalogError("The CLI diagnostics exceeded the safe size limit."))
                    return
        except (OSError, ValueError):
            return
        finally:
            _close_pipe(self.process.stderr)

    def _read(self):
        total = 0
        try:
            while chunk := self.process.stdout.read1(4096):
                total += len(chunk)
                if total > _MAX_BYTES:
                    self.events.put(_CatalogError("The CLI catalog exceeded the safe size limit."))
                    return
                self.events.put(chunk)
        except (OSError, ValueError):
            self.events.put(_CatalogError("The CLI catalog check could not be completed."))
        finally:
            self.events.put(None)
            _close_pipe(self.process.stdout)

    def __enter__(self):
        return self

    def _signal_process_group(self, signum):
        try:
            os.killpg(self.process.pid, signum)
        except ProcessLookupError:
            return
        except PermissionError:
            # Darwin may report EPERM for a group whose only member is an
            # exited, unreaped child. Reap it, then suppress the error only
            # when a zero-signal probe confirms that the group is gone.
            if self.process.poll() is None:
                raise
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                return
            raise

    def __exit__(self, exc_type=None, *_args):
        self.closing.set()
        cleanup_error = None
        if os.name != "nt":
            # Reap an already-finished launcher before signaling its group.
            # A surviving descendant remains in the group and is still stopped.
            self.process.poll()
            # A launcher can exit before its descendants release inherited
            # pipes. The isolated group still belongs to this catalog check.
            try:
                self._signal_process_group(signal.SIGTERM)
            except PermissionError as exc:
                cleanup_error = exc
        if self.process.poll() is None:
            if os.name == "nt":
                # npm's .cmd launcher owns child processes. Stop only the tree
                # started for this check so its inherited pipe cannot hang Qt.
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=1, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except (OSError, subprocess.SubprocessError):
                    self.process.kill()
            try:
                self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.3)
        if os.name != "nt":
            try:
                self._signal_process_group(signal.SIGKILL)
            except PermissionError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        # Once the child has exited, drain its last diagnostics before closing
        # the pipe; a fast native-login failure may have no stdout at all.
        self.reader.join(timeout=0.3)
        self.error_reader.join(timeout=0.3)
        for writer in self.writers:
            writer.join(timeout=0.3)
        # Buffered stream.close() waits on an active reader/writer's lock.
        # Let that thread close its own pipe if a descendant still holds it.
        if not any(writer.is_alive() for writer in self.writers):
            _close_pipe(self.process.stdin)
        if cleanup_error is not None and exc_type is None:
            raise cleanup_error

    def _chunk(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _CatalogError("The CLI catalog check timed out.")
        try:
            result = self.events.get(timeout=remaining)
        except queue.Empty:
            raise _CatalogError("The CLI catalog check timed out.") from None
        if isinstance(result, Exception):
            raise result
        return result

    def send(self, value: dict):
        self.send_text(json.dumps(value) + "\n")

    def send_text(self, value: str):
        payload = value.encode("utf-8")
        if len(payload) > 2 * 1024 * 1024:
            raise _CatalogError("The CLI request exceeded the safe size limit.")
        completed = queue.SimpleQueue()

        def write():
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
                completed.put(None)
            except (OSError, ValueError):
                completed.put(_CatalogError("The CLI closed before accepting its request."))
            finally:
                if self.closing.is_set():
                    _close_pipe(self.process.stdin)

        writer = threading.Thread(target=write, daemon=True)
        self.writers.append(writer)
        writer.start()
        try:
            result = completed.get(timeout=max(0.001, self.deadline - time.monotonic()))
        except queue.Empty:
            raise _CatalogError("The CLI request timed out.") from None
        if isinstance(result, Exception):
            raise result

    def frame(self) -> dict:
        while b"\n" not in self.buffer:
            chunk = self._chunk()
            if chunk is None:
                raise _CatalogError("The CLI closed before returning its catalog.")
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return _decode(line)

    def document(self) -> dict:
        return _decode(self.text_document().encode("utf-8"))

    def text_document(self) -> str:
        result = self.buffer
        self.buffer = b""
        while (chunk := self._chunk()) is not None:
            result += chunk
        try:
            return result.decode("utf-8")
        except UnicodeError:
            raise _CatalogError("The CLI returned invalid text.") from None


def _models(rows: object, field: str) -> list[str]:
    if not isinstance(rows, list) or len(rows) > _MAX_MODELS:
        raise _CatalogError("The CLI returned an invalid or oversized model catalog.")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise _CatalogError("The CLI returned an invalid model catalog.")
        model = row.get(field)
        if row.get("hidden") is True:
            continue
        if isinstance(model, str) and _MODEL.fullmatch(model) and model not in result:
            result.append(model)
    if not result:
        raise _CatalogError("The CLI did not return any usable models.")
    return result


def _rpc(session: _Session, request_id: int, method: str, params: dict) -> dict:
    session.send({"id": request_id, "method": method, "params": params})
    for _ in range(128):
        frame = session.frame()
        if frame.get("id") != request_id:
            if "id" in frame:
                raise _CatalogError("The CLI returned an unexpected catalog response.")
            continue
        if "error" in frame or not isinstance(frame.get("result"), dict):
            raise _CatalogError("The CLI rejected the catalog check; update or sign in to the CLI.")
        return frame["result"]
    raise _CatalogError("The CLI returned too many catalog notifications.")


def _codex(path: str, environ: Mapping[str, str], result: dict):
    # Official app-server stdio protocol. Never create a thread or submit a turn.
    with _Session([path, "app-server"], environ) as session:
        _rpc(session, 0, "initialize", {"clientInfo": {
            "name": "callimachus", "title": "Callimachus", "version": "1.2.0",
        }})
        session.send({"method": "initialized", "params": {}})
        auth = _rpc(session, 1, "account/read", {"refreshToken": False})
        account = auth.get("account")
        result["authenticated"] = isinstance(account, dict) and bool(account.get("type"))
        if not result["authenticated"] and auth.get("requiresOpenaiAuth") is not False:
            raise _CatalogError("Sign in to Codex before checking its models.")
        models = []
        cursor = None
        cursors = set()
        for page in range(3):
            params = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            catalog = _rpc(session, page + 2, "model/list", params)
            rows = catalog.get("data")
            if rows != []:
                models.extend(_models(rows, "model"))
            if len(models) > _MAX_MODELS:
                raise _CatalogError("The CLI model catalog exceeded the safe limit.")
            cursor = catalog.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in cursors:
                raise _CatalogError("The CLI returned invalid catalog pagination.")
            cursors.add(cursor)
        else:
            raise _CatalogError("The CLI model catalog exceeded the safe page limit.")
        if not models:
            raise _CatalogError("The CLI did not return any usable models.")
        result.update(ok=True, models=list(dict.fromkeys(models)), message=(
            "Codex model catalog detected; model access is confirmed when used."
        ))


def _claude(path: str, environ: Mapping[str, str], result: dict):
    with _Session([path, "auth", "status"], environ) as session:
        auth = session.document()
        if session.process.wait(timeout=max(0.01, session.deadline - time.monotonic())) != 0:
            raise _CatalogError("Sign in to Claude Code before checking its models.")
    result["authenticated"] = auth.get("authMethod") in {
        "claude.ai", "oauth_token", "api_key", "api_key_helper", "third_party",
    }
    if not result["authenticated"]:
        raise _CatalogError("Sign in to Claude Code before checking its models.")
    # Same initialize-only control handshake as the official Agent SDK. No user
    # message, generation, workspace tools, hooks or external MCP servers.
    argv = [path, "--print", "--input-format", "stream-json", "--output-format",
            "stream-json", "--verbose", "--tools", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--settings",
            '{"disableAllHooks":true}', "--no-session-persistence"]
    with _Session(argv, environ) as session:
        session.send({"type": "control_request", "request_id": "catalog_init",
                      "request": {"subtype": "initialize", "hooks": None}})
        for _ in range(128):
            frame = session.frame()
            if frame.get("type") == "control_response":
                response = frame.get("response")
                if not isinstance(response, dict) or response.get("request_id") != "catalog_init":
                    raise _CatalogError("The CLI returned an unexpected catalog response.")
                if response.get("subtype") != "success" or not isinstance(response.get("response"), dict):
                    raise _CatalogError("Claude Code does not expose a usable model catalog; update the CLI.")
                result.update(ok=True, models=_models(response["response"].get("models"), "value"),
                              message="Claude Code login and model catalog detected; access is confirmed when used.")
                return
            if frame.get("type") == "control_request":
                raise _CatalogError("Claude Code requested interaction during the catalog check.")
        raise _CatalogError("The CLI returned too many catalog notifications.")


def cli_catalog(provider: str, environ: Mapping[str, str]) -> dict:
    """Return only authentication state and safe model IDs, never CLI raw output."""
    result = {"provider": provider, "ok": False, "installed": False,
              "authenticated": False, "models": [], "message": "The CLI is not installed or is not on PATH."}
    command = {"codex_cli": "codex", "claude_cli": "claude",
               "gemini_cli": "gemini", "antigravity_cli": "agy",
               "opencode_cli": "opencode"}.get(provider)
    if command is None:
        return result
    path = find_cli(command, environ)
    if path is None:
        return result
    result["installed"] = True
    try:
        if provider == "codex_cli":
            _codex(path, environ, result)
        elif provider == "claude_cli":
            _claude(path, environ, result)
        else:
            if provider == "gemini_cli":
                from core.verify.backends._gemini_cli_runtime import check_catalog
            elif provider == "opencode_cli":
                from core.verify.backends.opencode_cli import check_catalog
            else:
                from core.verify.backends.antigravity_cli import check_catalog
            native = check_catalog(path, environ)
            result.update(native)
            result.update(provider=provider, installed=True)
    except _CatalogError as exc:
        result.update(ok=False, models=[], message=str(exc))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        result.update(ok=False, models=[], message="The CLI catalog check could not be completed safely.")
    return result
