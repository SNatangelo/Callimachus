#!/usr/bin/env python3
# run.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Citation Verifier — single entry point.

Run the full verification pipeline (the common case):

    python run.py --input manuscript.pdf [pipeline flags ...]

Or invoke a sub-tool:

    python run.py <command> [args ...]

Commands:
    parse          extract claims / refs / citations       (core.parse.parse_manuscript)
    preprocess     preprocess a source's text              (core.parse.preprocess)
    authoryear     resolve author-year citations           (core.parse.authoryear)
    resolve        axis 1: existence + text retrieval      (core.resolve)
    provide        feed a source's text into a run         (core.resolve.provide)
    fetch          fetch a reference's full text           (core.fetch)
    ocr            OCR a PDF                                (core.fetch.extraction.ocr)
    verify         re-run verification on a prepared run   (core.verify.verify_run)
    report         render the final report                 (core.report)
    report-bibliography  export bibliographic findings without Fetch/Verify
    report-html    render an optional human HTML companion (core.report.human.cli)
    present        project a run into presentation form    (core.app.commands.present)
    preview        inspect Google Books preview evidence   (core.fetch.fallbacks.preview)
    gaps           report unresolved gaps                  (core.fetch.diagnostics.gaps)
    style-check    check citation style                    (core.style.check)
    style-detect   detect citation style                   (core.style.detect)
    tasks          inspect / manage run tasks              (core.app.commands.tasks)
    configure      setup (GUI or --headless)               (core.app.commands.configure)
    app            launch the Callimachus desktop app      (core.app.commands.desktop)
    benchmark      performance benchmark                   (core.app.commands.benchmark)
    journal-catalog  create or refresh journal authority   (core.app.commands.journal_catalog)

`python run.py <command> --help` shows that command's own options.
"""
import importlib
import importlib.util
import json
import multiprocessing
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

try:
    from core.app.runtime_paths import (
        application_argv,
        environment_file,
        is_frozen,
        load_environment_file,
        read_build_metadata,
        resource_root,
        record_startup_diagnostic,
        show_startup_error,
        user_data_root,
    )
except ModuleNotFoundError as exc:
    # Keep the source entry point's missing-requirement diagnosis usable even
    # when copied or launched before the project package is importable.
    if exc.name != "core" or getattr(sys, "frozen", False):
        raise

    def is_frozen() -> bool:
        return False

    def resource_root() -> Path:
        return Path(__file__).resolve().parent

    def user_data_root(*, source_root=None) -> Path:
        return Path(source_root).resolve() if source_root is not None else resource_root()

    def environment_file(*, source_root=None) -> Path:
        return user_data_root(source_root=source_root) / ".env"

    def application_argv(*args: str, source_root=None) -> list[str]:
        root = Path(source_root).resolve() if source_root is not None else resource_root()
        return [sys.executable, str(root / "run.py"), *map(str, args)]

    def load_environment_file(_path) -> None:
        return None

    def read_build_metadata(*, root=None):
        return None

    def record_startup_diagnostic(_message):
        return None

    def show_startup_error(message: str, *, title: str = "Callimachus") -> None:
        sys.stderr.write(f"{title}: {message}\n")

# subcommand -> dotted module path (each module exposes a ``main()`` callable)
_COMMANDS = {
    "parse": "core.parse.parse_manuscript",
    "preprocess": "core.parse.preprocess",
    "authoryear": "core.parse.authoryear",
    "resolve": "core.resolve",
    "provide": "core.resolve.provide",
    "fetch": "core.fetch",
    "ocr": "core.fetch.extraction.ocr",
    "verify": "core.verify.verify_run",
    "report": "core.report",
    "report-bibliography": "core.app.commands.report_bibliography",
    "report-html": "core.report.human.cli",
    "present": "core.app.commands.present",
    "preview": "core.fetch.fallbacks.preview",
    "gaps": "core.fetch.diagnostics.gaps",
    "style-check": "core.style.check",
    "style-detect": "core.style.detect",
    "tasks": "core.app.commands.tasks",
    "configure": "core.app.commands.configure",
    "app": "core.app.commands.desktop",
    "benchmark": "core.app.commands.benchmark",
    "journal-catalog": "core.app.commands.journal_catalog",
}

# no subcommand -> run the full verification pipeline
_PIPELINE = "core.app.run"

_MANDATORY_RUNTIME_DEPENDENCIES = {
    "certifi": "certifi",
    "Brotli": "brotli",
}
_PDF_BACKEND_MODULES = ("pymupdf", "fitz", "pdfminer")


def _shell_join(argv: list[str], platform_name: str) -> str:
    if platform_name.startswith("win"):
        return " ".join(_windows_argument(value) for value in argv)
    return shlex.join(argv)


def _windows_argument(value: str) -> str:
    """Quote one argument for an operator-facing PowerShell command."""
    rendered = subprocess.list2cmdline([value])
    if ("<" in value or ">" in value) and rendered == value:
        return f'"{value}"'
    return rendered


def _shell_command(argv: list[str], platform_name: str) -> str:
    """Render an executable command for the operator's shell."""
    rendered = _shell_join(argv, platform_name)
    if platform_name.startswith("win") and _windows_argument(argv[0]).startswith('"'):
        return f"& {rendered}"
    return rendered


def _pip_install_command(
    platform_name: str | None = None, *, executable: str | None = None,
    requirements: str = "requirements.txt",
) -> str:
    """Return the shell command appropriate for the current operator OS."""
    platform_name = sys.platform if platform_name is None else platform_name
    executable = sys.executable if executable is None else executable
    return _shell_command(
        [executable, "-m", "pip", "install", "-r", requirements],
        platform_name,
    )


def _project_venv_python(platform_name: str) -> str:
    if platform_name.startswith("win"):
        return r".\.venv\Scripts\python.exe"
    return ".venv/bin/python"


def _project_venv_path(
    platform_name: str, *, root: Path | None = None,
) -> Path:
    root = Path(__file__).resolve().parent if root is None else Path(root)
    parts = (
        (".venv", "Scripts", "python.exe")
        if platform_name.startswith("win")
        else (".venv", "bin", "python")
    )
    return root.joinpath(*parts)


def _project_venv_is_ready(
    platform_name: str | None = None, *, root: Path | None = None,
) -> bool:
    """Probe the project environment without importing its packages here."""
    platform_name = sys.platform if platform_name is None else platform_name
    venv_python = _project_venv_path(platform_name, root=root)
    if not venv_python.is_file():
        return False
    probe = (
        "import importlib.util, shutil, sys\n"
        f"required = {tuple(_MANDATORY_RUNTIME_DEPENDENCIES.values())!r}\n"
        f"pdf_modules = {_PDF_BACKEND_MODULES!r}\n"
        "base_ok = all(importlib.util.find_spec(name) is not None "
        "for name in required)\n"
        "pdf_ok = any(importlib.util.find_spec(name) is not None "
        "for name in pdf_modules) or shutil.which('pdftotext') is not None\n"
        "raise SystemExit(0 if base_ok and pdf_ok else 1)\n"
    )
    try:
        completed = subprocess.run(
            [str(venv_python), "-c", probe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _is_externally_managed_python() -> bool:
    """Return whether pip must not modify the current base interpreter."""
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return False
    stdlib = sysconfig.get_path("stdlib")
    return bool(stdlib and os.path.isfile(os.path.join(stdlib, "EXTERNALLY-MANAGED")))


def _runtime_install_commands(
    platform_name: str | None = None,
    *,
    executable: str | None = None,
    externally_managed: bool | None = None,
    venv_exists: bool | None = None,
    root: Path | None = None,
    cwd: Path | None = None,
) -> list[str]:
    """Return commands that install dependencies without modifying managed Python."""
    platform_name = sys.platform if platform_name is None else platform_name
    executable = sys.executable if executable is None else executable
    root = Path(__file__).resolve().parent if root is None else Path(root)
    cwd = Path.cwd() if cwd is None else Path(cwd)
    requirements = "requirements.txt" if cwd.resolve() == root.resolve() else str(root / "requirements.txt")
    if externally_managed is None:
        externally_managed = _is_externally_managed_python()
    if not externally_managed:
        return [_pip_install_command(
            platform_name, executable=executable, requirements=requirements,
        )]

    in_project_root = cwd.resolve() == root.resolve()
    venv_path = _project_venv_path(platform_name, root=root)
    venv_python = _project_venv_python(platform_name) if in_project_root else str(venv_path)
    venv_directory = ".venv" if in_project_root else str(root / ".venv")
    if venv_exists is None:
        venv_exists = venv_path.is_file()
    commands = []
    if not venv_exists:
        commands.append(
            _shell_command([executable, "-m", "venv", venv_directory], platform_name)
        )
    commands.append(
        _shell_command(
            [venv_python, "-m", "pip", "install", "-r", requirements],
            platform_name,
        )
    )
    return commands


def _project_run_command(
    args: list[str], platform_name: str | None = None, *, root: Path | None = None,
    cwd: Path | None = None,
) -> str:
    """Render a runnable project-venv command from the current directory."""
    platform_name = sys.platform if platform_name is None else platform_name
    root = Path(__file__).resolve().parent if root is None else Path(root)
    cwd = Path.cwd() if cwd is None else Path(cwd)
    if cwd.resolve() == root.resolve():
        argv = [_project_venv_python(platform_name), "run.py", *args]
    else:
        argv = [str(_project_venv_path(platform_name, root=root)), str(root / "run.py"), *args]
    return _shell_command(argv, platform_name)


def _ensure_local_env(*, root: Path | None = None) -> bool:
    """Create first-run local configuration, or report why startup must stop."""
    root = resource_root() if root is None else Path(root)
    state_root = user_data_root(source_root=root)
    if is_frozen():
        candidates = (state_root / ".env", state_root / ".env.local")
    else:
        candidates = (
            root / "core" / "verify" / ".env",
            root / "core" / "verify" / ".env.local",
            root / "core" / ".env",
            root / "core" / ".env.local",
            root / ".env",
            root / ".env.local",
        )
    if any(candidate.is_file() for candidate in candidates):
        return False
    template_path = root / ".env.example"
    env_path = environment_file(source_root=root)
    if not template_path.is_file():
        message = (
            "Callimachus could not find its configuration template. "
            "Repair or reinstall the current release, then try again."
        )
        if is_frozen():
            show_startup_error(message)
        else:
            sys.stderr.write(
                "Callimachus configuration was not found: both .env and "
                ".env.example are missing; recover them before starting.\n"
            )
        return True
    try:
        template_bytes = template_path.read_bytes()
    except OSError as exc:
        if is_frozen():
            show_startup_error(
                "Callimachus could not read its bundled configuration template. "
                "Repair or reinstall the current release and try again."
            )
        else:
            sys.stderr.write(
                "Callimachus could not read .env.example; recover a readable template "
                f"before starting: {exc}\n"
            )
        return True

    created = False
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        destination = env_path.open("xb")
        created = True
        with destination:
            destination.write(template_bytes)
    except FileExistsError:
        return False
    except OSError as exc:
        if created:
            try:
                env_path.unlink()
            except OSError:
                pass
        if is_frozen():
            show_startup_error(
                "Callimachus could not create its per-user configuration file. "
                "Check your account's folder permissions and try again."
            )
        else:
            sys.stderr.write(
                "Callimachus could not create .env from .env.example; fix the "
                f"workspace and retry: {exc}\n"
            )
        return True

    sys.stderr.write(
        "Created .env from .env.example for this first run.\n"
    )
    return False


def _missing_base_runtime_dependencies() -> list[str]:
    """List required, non-optional runtime distributions whose modules are absent."""
    return [
        distribution
        for distribution, module in _MANDATORY_RUNTIME_DEPENDENCIES.items()
        if importlib.util.find_spec(module) is None
    ]


def _has_usable_pdf_backend() -> bool:
    """Match the runtime PDF backend contract without importing pipeline modules."""
    for module in _PDF_BACKEND_MODULES:
        try:
            if importlib.util.find_spec(module) is not None:
                return True
        except BaseException:
            pass
    return shutil.which("pdftotext") is not None


def _print_missing_runtime_dependencies(
    missing: list[str], *, missing_pdf_backend: bool,
) -> None:
    if missing:
        sys.stderr.write(
            "Callimachus cannot start: missing mandatory runtime dependencies: "
            f"{', '.join(missing)}.\n"
        )
    if missing_pdf_backend:
        sys.stderr.write(
            "Callimachus cannot start: no usable PDF backend "
            "(install pymupdf or pdfminer.six, or put pdftotext on PATH).\n"
        )
    if _project_venv_is_ready():
        sys.stderr.write(
            "The project virtual environment already has the required dependencies.\n"
        )
        sys.stderr.write("Run Callimachus with:\n")
        sys.stderr.write(
            f"  {_project_run_command(sys.argv[1:])}\n"
        )
        return
    commands = _runtime_install_commands()
    if len(commands) > 1:
        sys.stderr.write("Create a project virtual environment and install them with:\n")
    elif _is_externally_managed_python():
        sys.stderr.write("Install them in the project virtual environment with:\n")
    else:
        sys.stderr.write("Install them with:\n")
    for command in commands:
        sys.stderr.write(f"  {command}\n")
    if _is_externally_managed_python():
        sys.stderr.write("Then run Callimachus with:\n")
        sys.stderr.write(
            f"  {_project_run_command(sys.argv[1:])}\n"
        )


def _check_runtime_dependencies() -> int | None:
    missing = _missing_base_runtime_dependencies()
    missing_pdf_backend = not _has_usable_pdf_backend()
    if not missing and not missing_pdf_backend:
        return None
    if is_frozen():
        missing_items = list(missing)
        if missing_pdf_backend:
            missing_items.append("a PDF extraction backend")
        show_startup_error(
            "Callimachus is missing a required packaged component"
            + (f" ({', '.join(missing_items)})" if missing_items else "")
            + ". Repair or reinstall the current release and try again."
        )
    else:
        _print_missing_runtime_dependencies(missing, missing_pdf_backend=missing_pdf_backend)
    return 2


def _dispatch(module_path: str, argv: list[str], prog: str) -> int:
    """Import ``module_path`` and call its ``main()`` with ``sys.argv`` set to
    ``[prog, *argv]`` so the module's own argparse sees the right program name
    and arguments. Returns the exit code (``main()`` may return ``None`` -> 0)."""
    try:
        module = importlib.import_module(module_path)
    except (ImportError, OSError) as exc:
        if not is_frozen():
            raise
        missing = getattr(exc, "name", None)
        component = f" ({missing})" if missing else ""
        show_startup_error(
            "Callimachus could not load a required packaged component"
            f"{component}. Repair or reinstall the current release and try again."
        )
        return 2
    sys.argv = [prog, *argv]
    try:
        rc = module.main()
    except (ImportError, OSError) as exc:
        if not is_frozen():
            raise
        missing = getattr(exc, "name", None)
        component = f" ({missing})" if missing else ""
        show_startup_error(
            "Callimachus could not load a required packaged component"
            f"{component}. Repair or reinstall the current release and try again."
        )
        return 2
    return rc if isinstance(rc, int) else 0


def main() -> int:
    _repair_frozen_stdio()
    try:
        return _main()
    except KeyboardInterrupt:
        return 130


def _qt_probe() -> int:
    """Initialize Qt in the disposable child process used by GUI preflight."""
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        app.quit()
        return 0
    except BaseException as exc:
        record_startup_diagnostic(
            "Qt startup probe failed in the packaged runtime."
        )
        print(f"Qt startup probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


_PACKAGE_SELF_TEST_RESOURCES = (
    ".env.example",
    "LICENSE",
    "DEPLOYMENT.md",
    "build-metadata.json",
    "THIRD-PARTY-NOTICES/index.json",
    "docs/guide/README.md",
    "docs/guide/01-quickstart.md",
    "docs/guide/02-cli-reference.md",
    "docs/guide/03-configuration.md",
    "docs/guide/04-pipeline.md",
    "docs/guide/05-tasks-and-recovery.md",
    "docs/guide/06-verification-and-evidence.md",
    "docs/guide/07-artifacts-and-provenance.md",
    "docs/guide/08-capabilities-and-limits.md",
    "docs/guide/09-troubleshooting.md",
    "docs/guide/10-keys-and-credentials.md",
    "docs/guide/11-environment-reference.md",
    "docs/guide/12-desktop-packages.md",
    "docs/deployment/agent-guide.md",
    "docs/deployment/administrator-guide.md",
    "docs/architecture/artifact-integrity.md",
    "core/parse/parsers.json",
    "core/parse/config/bibliography_headings.json",
    "core/resolve/providers.json",
    "core/resolve/providers/publisher_routes.json",
    "core/resolve/providers/config/institutional_sources.json",
    "core/verify/claim_evidence/contracts/prompts/jury1.json",
    "core/verify/claim_evidence/contracts/prompts/jury2.json",
    "core/gui/assets/locales/en.json",
    "core/gui/assets/locales/it.json",
    "core/report/human/assets/app.css",
    "core/report/human/assets/app.js",
    "core/report/human/assets/manifest.json",
    "core/report/human/assets/logo.svg",
    "core/report/human/assets/locales/en.json",
    "core/report/human/assets/locales/it.json",
)


def _probe_ocr_import() -> None:
    importlib.import_module("core.fetch.extraction.ocr")
    model_files = []
    for package_name in ("rapidocr", "rapidocr_onnxruntime"):
        try:
            spec = importlib.util.find_spec(package_name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            continue
        for location in spec.submodule_search_locations or ():
            model_dir = Path(location) / "models"
            model_files.extend(model_dir.glob("*.onnx"))
    if len(model_files) < 2:
        raise RuntimeError("RapidOCR detector and recognizer model assets are not bundled")


def _package_self_test() -> int:
    """Check the frozen distribution without starting the desktop window."""
    root = resource_root()
    failures = [
        relative for relative in _PACKAGE_SELF_TEST_RESOURCES
        if not (root / relative).is_file()
    ]
    metadata = read_build_metadata(root=root)
    if metadata is None or not isinstance(metadata.get("dirty"), bool):
        failures.append("valid build metadata")
    try:
        notice_index = json.loads(
            (root / "THIRD-PARTY-NOTICES" / "index.json").read_text(encoding="utf-8")
        )
        if not isinstance(notice_index, list) or not notice_index or any(
            not isinstance(item, dict) or not item.get("name") or not item.get("files")
            for item in notice_index
        ):
            failures.append("third-party notice inventory")
    except (OSError, json.JSONDecodeError):
        failures.append("third-party notice inventory")

    try:
        state_root = user_data_root()
        state_root.mkdir(parents=True, exist_ok=True)
        probe = state_root / f".callimachus-self-test-{os.getpid()}"
        with probe.open("xb") as target:
            target.write(b"ok")
        probe.unlink()
    except OSError:
        failures.append("writable per-user data directory")
        try:
            probe.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass

    expected_argv = [sys.executable, "--package-self-test"]
    if application_argv("--package-self-test") != expected_argv:
        failures.append("frozen executable argument handling")

    for label, module_path in (
        ("desktop entry point", _COMMANDS["app"]),
        ("analysis pipeline", _PIPELINE),
    ):
        try:
            importlib.import_module(module_path)
        except Exception:
            failures.append(label)

    try:
        from core.gui.launcher import _probe_qapplication_startup

        if _probe_qapplication_startup() is not None:
            failures.append("Qt startup probe")
    except Exception:
        failures.append("Qt startup probe")

    try:
        _probe_ocr_import()
    except Exception:
        failures.append("OCR runtime import or bundled model assets")

    if failures:
        unique = list(dict.fromkeys(failures))
        message = "Packaged self-test failed: " + ", ".join(unique) + "."
        record_startup_diagnostic(message)
        stream = getattr(sys, "stderr", None)
        if stream is not None:
            try:
                stream.write(message + "\n")
            except (OSError, ValueError):
                pass
        return 2

    stream = getattr(sys, "stdout", None)
    if stream is not None:
        try:
            stream.write(
                "Packaged self-test passed. OCR dependencies and model assets were found; "
                "PDF inference was not exercised.\n"
            )
        except (OSError, ValueError):
            pass
    return 0


def _prepare_frozen_desktop() -> bool:
    try:
        qt_available = importlib.util.find_spec("PySide6") is not None
    except (ImportError, ValueError):
        qt_available = False
    if not qt_available:
        show_startup_error(
            "The desktop interface is missing from this Callimachus release. "
            "Repair or reinstall the current release and try again."
        )
        return False

    try:
        from core.gui.launcher import _probe_qapplication_startup

        reason = _probe_qapplication_startup()
    except Exception:
        reason = "Qt could not initialize its graphical components."
    if reason is not None:
        show_startup_error(
            "Callimachus could not open its desktop interface. "
            "Check that a graphical desktop session is available, then repair "
            "or reinstall the current release if the problem continues."
        )
        return False
    return True


def _repair_frozen_stdio() -> None:
    """Reconnect inherited pipes or use the null device for windowed builds."""
    if not is_frozen():
        return
    for name, descriptor, mode in (
        ("stdin", 0, "r"),
        ("stdout", 1, "w"),
        ("stderr", 2, "w"),
    ):
        stream = getattr(sys, name, None)
        if stream is not None:
            if name != "stdin":
                reconfigure = getattr(stream, "reconfigure", None)
                if callable(reconfigure):
                    try:
                        reconfigure(encoding="utf-8", errors="replace")
                    except (OSError, ValueError):
                        pass
            continue
        try:
            stream = os.fdopen(
                descriptor, mode, closefd=False,
                encoding="utf-8", errors="replace",
            )
        except (OSError, ValueError):
            stream = open(os.devnull, mode, encoding="utf-8", errors="replace")
        setattr(sys, name, stream)


def _load_frozen_environment() -> bool:
    env_path = environment_file()
    if not env_path.is_file():
        local_path = env_path.with_name(".env.local")
        if local_path.is_file():
            env_path = local_path
        else:
            if _ensure_local_env():
                return False
            env_path = environment_file()
    try:
        load_environment_file(env_path)
    except OSError:
        show_startup_error(
            "Callimachus could not read its per-user configuration file. "
            "Check the file permissions and try again."
        )
        return False
    return True


def _main() -> int:
    argv = sys.argv[1:]
    program = Path(sys.executable).name if is_frozen() else "run.py"
    if is_frozen() and argv == ["--qt-probe"]:
        return _qt_probe()
    if is_frozen() and argv == ["--package-self-test"]:
        return _package_self_test()
    if is_frozen() and not _load_frozen_environment():
        return 2
    if is_frozen() and not argv:
        argv = ["app"]
    if argv in (["--help"], ["-h"]):
        from core.app.cli import build_parser

        build_parser(prog=program).print_help()
        return 0
    if argv and not argv[0].startswith("-"):
        cmd = argv[0]
        module_path = _COMMANDS.get(cmd)
        if module_path is None:
            sys.stderr.write(f"{program}: unknown command {cmd!r}\n\n")
            sys.stderr.write("Available commands: " + ", ".join(_COMMANDS) + "\n")
            sys.stderr.write(
                "Run the pipeline directly with:  "
                f"{_shell_command(application_argv('--input', '<file>'), sys.platform)}\n"
            )
            return 2
        if is_frozen() and cmd == "app" and not _prepare_frozen_desktop():
            return 2
        dependency_error = _check_runtime_dependencies()
        if dependency_error is not None:
            return dependency_error
        return _dispatch(module_path, argv[1:], f"{program} {cmd}")
    # no command (or leading flag): the full verification pipeline
    if _ensure_local_env():
        return 2
    dependency_error = _check_runtime_dependencies()
    if dependency_error is not None:
        return dependency_error
    return _dispatch(_PIPELINE, argv, program)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
