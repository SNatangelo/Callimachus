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
    report-export  export a public redacted copy of a completed Verify report
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
import re
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
        settings_template_file,
        show_startup_error,
        user_data_root,
    )
    from core.app.platform_support import startup_compatibility_error
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

    def settings_template_file(*, source_root=None) -> Path:
        root = Path(source_root).resolve() if source_root is not None else resource_root()
        return root / ".env.example"

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

    def startup_compatibility_error() -> str | None:
        return None

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
    "report-export": "core.report.human.public_export",
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


_FROZEN_SETTING_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*="
)


def _blank_frozen_secret_assignments(template: bytes) -> bytes:
    """Keep the bundled template's non-secret defaults, blanking credential fields."""
    from core.app.settings.credentials import is_secret_setting

    text = template.decode("utf-8")
    output = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        match = _FROZEN_SETTING_ASSIGNMENT.match(body)
        if match and is_secret_setting(match.group("name")):
            equals = body.find("=")
            line = body[:equals + 1] + ending
        output.append(line)
    return "".join(output).encode("utf-8")


def _ensure_local_env(*, root: Path | None = None) -> bool:
    """Create first-run local configuration, or report why startup must stop."""
    root = resource_root() if root is None else Path(root)
    state_root = user_data_root(source_root=root)
    if is_frozen():
        candidates = (state_root / "settings.conf",)
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
    template_path = settings_template_file(source_root=root)
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
        if is_frozen():
            template_bytes = _blank_frozen_secret_assignments(template_bytes)
    except (OSError, UnicodeDecodeError) as exc:
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


def _launch_desktop(
    module_path: str, argv: list[str], prog: str, *,
    startup_secret_snapshot=None,
) -> int:
    """Show the desktop splash while the GUI command's imports complete."""
    from core.gui.startup_splash import run_with_splash

    def start_desktop() -> int:
        if startup_secret_snapshot is None:
            return _dispatch(module_path, argv, prog)
        from core.app.settings.credentials import _use_startup_secret_snapshot

        try:
            with _use_startup_secret_snapshot(startup_secret_snapshot):
                return _dispatch(module_path, argv, prog)
        finally:
            startup_secret_snapshot.values.clear()

    try:
        return run_with_splash(
            prepare=lambda: importlib.import_module(module_path),
            start=start_desktop,
        )
    except ModuleNotFoundError as exc:
        if exc.name != "PySide6":
            raise
        # Preserve the desktop command's existing missing-GUI diagnosis.
        return start_desktop()


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
    "settings.defaults",
    "LICENSE",
    "DEPLOYMENT.md",
    "build-metadata.json",
    "THIRD-PARTY-NOTICES/index.json",
    "docs/README.md",
    "docs/reference/README.md",
    "docs/reference/01-quickstart.md",
    "docs/reference/02-cli-reference.md",
    "docs/reference/03-configuration.md",
    "docs/reference/04-pipeline.md",
    "docs/reference/05-tasks-and-recovery.md",
    "docs/reference/06-verification-and-evidence.md",
    "docs/reference/07-artifacts-and-provenance.md",
    "docs/reference/08-capabilities-and-limits.md",
    "docs/reference/09-troubleshooting.md",
    "docs/reference/10-keys-and-credentials.md",
    "docs/reference/11-environment-reference.md",
    "docs/user/en/09-desktop-packages.md",
    "docs/legal/en/privacy.md",
    "docs/legal/it/privacy.md",
    "docs/user/en/README.md",
    "docs/user/en/01-quickstart.md",
    "docs/user/en/02-analysis.md",
    "docs/user/en/03-settings.md",
    "docs/user/en/04-reviews-and-sources.md",
    "docs/user/en/05-results-and-history.md",
    "docs/user/en/06-library-and-cache.md",
    "docs/user/en/07-capabilities-and-limits.md",
    "docs/user/en/08-troubleshooting.md",
    "docs/user/it/README.md",
    "docs/user/it/01-quickstart.md",
    "docs/user/it/02-analysis.md",
    "docs/user/it/03-settings.md",
    "docs/user/it/04-reviews-and-sources.md",
    "docs/user/it/05-results-and-history.md",
    "docs/user/it/06-library-and-cache.md",
    "docs/user/it/07-capabilities-and-limits.md",
    "docs/user/it/08-troubleshooting.md",
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


def _probe_library_import(parent_dir: Path) -> None:
    """Exercise the packaged Library importer using isolated synthetic papers."""
    import hashlib
    import tempfile
    import zipfile
    from xml.sax.saxutils import escape

    import fitz

    from core.app.library.importer import LibraryImporter
    from core.fetch.storage import content_store
    from core.parse import extract as parse_extract

    parent = Path(parent_dir)
    parent.mkdir(parents=True, exist_ok=True)

    def write_pdf(path: Path, title: str, body: str) -> None:
        document = fitz.open()
        page = document.new_page(width=612, height=792)
        page.insert_text((54, 72), title, fontsize=18)
        page.insert_text((54, 104), "Jane Doe 2024", fontsize=11)
        page.insert_textbox(
            fitz.Rect(54, 132, 558, 760), body, fontsize=10, lineheight=1.35,
        )
        document.set_metadata({"title": title})
        path.parent.mkdir(parents=True, exist_ok=True)
        document.save(path)
        document.close()

    def write_docx(path: Path, title: str, body: str) -> None:
        word_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        paragraphs = (title, "Jane Doe 2024", body)
        paragraph_xml = "".join(
            "<w:p><w:r><w:t xml:space=\"preserve\">"
            f"{escape(paragraph)}</w:t></w:r></w:p>"
            for paragraph in paragraphs
        )
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:document xmlns:w="{word_ns}"><w:body>{paragraph_xml}'
            "<w:sectPr/></w:body></w:document>"
        )
        content_types_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            archive.writestr("word/document.xml", document_xml)

    with tempfile.TemporaryDirectory(
        prefix=".callimachus-library-smoke-", dir=parent,
    ) as temporary:
        scratch = Path(temporary)
        source_dir = scratch / "sources"
        body = (
            "This synthetic scholarly paper contains enough readable text for "
            "local identity review and a deterministic storage round trip. "
        ) * 12
        documents = (
            (source_dir / "library-roundtrip.pdf", ".pdf", "pdf", "pdf",
             "Callimachus Library PDF Roundtrip"),
            (source_dir / "library-roundtrip.docx", ".docx", "docx", "docx",
             "Callimachus Library DOCX Roundtrip"),
        )
        supported = {
            str(extension).casefold()
            for extension in parse_extract.supported_extensions()
        }
        required = {item[1] for item in documents}
        if not required.issubset(supported):
            raise RuntimeError("PDF or DOCX is disabled in the packaged extractor registry")

        for path, extension, _family, _fmt, title in documents:
            if extension == ".pdf":
                write_pdf(path, title, body)
            else:
                write_docx(path, title, body)

        original_bytes = {path: path.read_bytes() for path, *_ in documents}
        original_hashes = {
            path: hashlib.sha256(data).hexdigest()
            for path, data in original_bytes.items()
        }
        anchor = str(scratch / "runs" / "anchor")
        environ = {"CITATION_VERIFIER_STATE_DIR": str(scratch / "state")}
        importer = LibraryImporter(anchor, environ)
        inspected = importer.inspect([str(path) for path, *_ in documents])
        by_extension = {
            str(row.get("source_extension") or "").casefold(): row
            for row in inspected
        }
        if len(inspected) != len(documents) or set(by_extension) != required:
            raise RuntimeError("packaged Library importer did not discover PDF and DOCX")

        expected_by_extension = {item[1]: item for item in documents}
        for extension, row in by_extension.items():
            path, _extension, family, fmt, title = expected_by_extension[extension]
            digest = original_hashes[path]
            method = row.get("extraction_method")
            if (
                row.get("status") != "needs_review"
                or row.get("reason_code") != "missing_author_year"
                or row.get("title") != title
                or row.get("sha256") != digest
                or row.get("_file_id") != digest
                or row.get("content_family") != family
                or row.get("extraction_format") != fmt
                or not isinstance(method, str)
                or not method.strip()
            ):
                raise RuntimeError("packaged Library import extraction or identity review failed")
            record = importer._load_session(row["_session_id"])["files"][digest]
            staged = importer._staged_path(row["_session_id"], digest, record)
            if staged.read_bytes() != original_bytes[path]:
                raise RuntimeError("packaged Library staging changed the original paper bytes")
            if hashlib.sha256(staged.read_bytes()).hexdigest() != digest:
                raise RuntimeError("packaged Library staged-paper SHA-256 did not match")

        reopened = LibraryImporter(anchor, environ)
        pending = reopened.pending_rows()
        pending_by_extension = {
            str(row.get("source_extension") or "").casefold(): row
            for row in pending
        }
        if len(pending) != len(documents) or set(pending_by_extension) != required:
            raise RuntimeError("packaged Library pending review did not survive reopening")

        for extension, pending_row in pending_by_extension.items():
            path, _extension, _family, fmt, title = expected_by_extension[extension]
            digest = original_hashes[path]
            method = by_extension[extension]["extraction_method"]
            candidate = dict(
                pending_row, title=title, authors="Jane Doe", year=2024,
            )
            reviewed = reopened.review_identity(candidate)
            if reviewed.get("status") != "identified" or reviewed.get("reason_code"):
                raise RuntimeError("packaged Library local identity review failed")
            saved_rows = reopened.save([reviewed])
            if len(saved_rows) != 1 or saved_rows[0].get("status") != "stored":
                raise RuntimeError("packaged Library reviewed paper could not be saved")
            saved = saved_rows[0]
            if (
                path.read_bytes() != original_bytes[path]
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                raise RuntimeError("packaged Library save changed the source paper")
            stored_original = Path(str(saved.get("stored_path") or ""))
            if (
                not stored_original.is_file()
                or stored_original.read_bytes() != original_bytes[path]
                or hashlib.sha256(stored_original.read_bytes()).hexdigest() != digest
            ):
                raise RuntimeError("packaged Library stored paper bytes or SHA-256 did not match")

            record = reopened._load_session(reviewed["_session_id"])["files"][digest]
            revisions = record.get("revisions") or []
            if (
                record.get("saved") is not True
                or record.get("accepted_identity", {}).get("source") != "operator_review"
                or record.get("identity_review", {}).get("accepted") is not True
                or not any(
                    revision.get("decision") == "identity_review_accepted"
                    for revision in revisions
                )
                or not revisions
                or revisions[-1].get("decision") != "saved"
                or revisions[-1].get("evidence", {}).get("extraction_format") != fmt
                or revisions[-1].get("evidence", {}).get("extraction_method") != method
            ):
                raise RuntimeError("packaged Library review provenance was not persisted")

            reusable = content_store.find_reusable_parsed_text(
                anchor,
                {"title": title, "ay_surname": "Doe", "year": 2024},
                tiers=("fulltext",),
                environ=environ,
            )
            if reusable is None:
                raise RuntimeError("packaged Library saved content could not be reopened")
            if (
                reusable.get("file_format") != fmt
                or reusable.get("extraction_method") != method
                or reusable.get("supplied_via") != "user_library_review"
                or reusable.get("identity_status") != "user_library_reviewed"
                or reusable.get("source_ref") != str(path.resolve())
                or f"staged_sha256={digest}" not in str(reusable.get("identity_note") or "")
            ):
                raise RuntimeError("packaged Library saved-content provenance or format did not match")
            parsed_text_path = Path(str(reusable.get("stored_path") or ""))
            parsed_text = parsed_text_path.read_text(encoding="utf-8")
            if hashlib.sha256(parsed_text.encode("utf-8")).hexdigest() != reusable.get("sha256"):
                raise RuntimeError("packaged Library parsed-text SHA-256 did not match")

        if LibraryImporter(anchor, environ).pending_rows():
            raise RuntimeError("packaged Library saved reviews remained pending after reopening")

        quality_parent = scratch / "quality"
        quality_source = quality_parent / "short-quality-probe.pdf"
        write_pdf(quality_source, "Short Quality Gate Probe", "Short readable quality probe.")
        quality_probe = parse_extract.probe_file(str(quality_source))
        with fitz.open(quality_source) as quality_document:
            quality_text = "\n".join(page.get_text() for page in quality_document)
        alpha_count = sum(character.isalpha() for character in quality_text)
        if (
            quality_probe.get("detected_format") != "pdf"
            or not quality_text.strip()
            or len(quality_text) >= 200
            or alpha_count / len(quality_text) < 0.4
        ):
            raise RuntimeError("packaged Library quality-gate fixture was not a short readable PDF")
        quality_environ = {
            "CITATION_VERIFIER_STATE_DIR": str(scratch / "quality-state"),
        }
        quality_importer = LibraryImporter(
            str(scratch / "quality-runs" / "anchor"), quality_environ,
        )
        bad_row = quality_importer.inspect([str(quality_source)])[0]
        if bad_row.get("status") != "unreadable" or bad_row.get("reason_code") != "unreadable_pdf":
            raise RuntimeError("packaged Library accepted a PDF below the text-quality gate")
        forged = dict(bad_row, title="Short Quality Gate Probe", authors="Jane Doe", year=2024)
        rejected_review = quality_importer.review_identity(forged)
        rejected_save = quality_importer.save([rejected_review])[0]
        if (
            rejected_review.get("status") == "identified"
            or rejected_review.get("reason_code") != "unreadable_pdf"
            or rejected_save.get("status") == "stored"
            or rejected_save.get("reason_code") != "identity_review_required"
        ):
            raise RuntimeError("packaged Library quality failure could be reviewed or saved")


def _probe_historical_export(state_root: Path) -> None:
    """Exercise artifact-only report export using isolated synthetic fixtures."""
    import hashlib
    import json
    import tempfile
    from unittest import mock

    from core.infra.integrity import signing
    from core.report.human import HumanReportProjection, render_human_report
    from core.report.human.public_export import export_public_html
    from core.report.human.sealing import seal_html_body

    state_root = Path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    fulltext_sentinel = "CALLIMACHUS_HISTORICAL_SMOKE_FULLTEXT_SENTINEL"
    synthetic_key = "CALLIMACHUS_HISTORICAL_SMOKE_SYNTHETIC_KEY"
    long_text = (
        fulltext_sentinel
        + " supporting material should be removed from a public historical export. "
    ) * 8
    expected_decision = {
        "verdict": "supports",
        "status": "accepted",
        "explanation": "The cited source supports the claim.",
    }
    projection_value = {
        "schema_version": 1,
        "execution": {"debug_mode": False, "debug_labels": []},
        "manuscript": {"title": "Historical artifact"},
        "overview": {},
        "attention": [],
        "configuration": {},
        "models": {},
        "claims": [{
            "claim": {"context_window": long_text, "sentence": "Claim [1]."},
            "focus_text": "Claim [1].",
        }],
        "sources": [{
            "reference": {"id": "r1", "title": "Source title"},
            "resolve": {"abstract": long_text},
        }],
        "pairs": [{
            "ref_id": "r1",
            "status": "accepted",
            "decision": dict(expected_decision),
        }],
        "diagnostics": {},
    }
    rendered = render_human_report(HumanReportProjection(projection_value))
    report_markdown = b"Synthetic historical report for the packaged export probe."
    fulltext_bytes = fulltext_sentinel.encode("utf-8")
    synthetic_key_bytes = synthetic_key.encode("utf-8")
    wrong_key = "CALLIMACHUS_HISTORICAL_SMOKE_WRONG_KEY"
    wrong_key_bytes = wrong_key.encode("utf-8")
    data_open = '<script id="cv-data" type="application/json">'

    def make_sealed_report(key: str | None) -> bytes:
        with mock.patch.object(signing, "load_key", return_value=key):
            return seal_html_body(rendered, report_markdown).encode("utf-8")

    def create_run(
        name: str,
        report_bytes: bytes,
        database_bytes: bytes | None = None,
    ) -> tuple[Path, Path, Path]:
        run_dir = scratch / name
        run_dir.mkdir()
        report_path = run_dir / "report.html"
        report_path.write_bytes(report_bytes)
        database_path = run_dir / "run.sqlite"
        if database_bytes is not None:
            database_path.write_bytes(database_bytes)
        return run_dir, report_path, database_path

    def validate_history_recognition(run_dir: Path, report_path: Path) -> None:
        from core.app.desktop import load_run_summary

        summary = load_run_summary(str(run_dir))
        if (
            summary.get("available") is not False
            or summary.get("report_html_present") is not True
            or Path(str(summary.get("report_html_path") or "")).resolve()
            != report_path.resolve()
        ):
            raise RuntimeError("History did not recognize the historical report artifact")

    def validate_export(
        report_path: Path,
        report_bytes: bytes,
        database_path: Path,
        database_bytes: bytes | None,
        output_path: Path,
        *,
        algorithm: str,
        authentication: str,
    ) -> None:
        public_bytes = output_path.read_bytes()
        sidecar = output_path.with_suffix(output_path.suffix + ".redaction.json")
        manifest_bytes = sidecar.read_bytes()
        public_html = public_bytes.decode("utf-8")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        historical = manifest.get("historical_export")
        if not isinstance(historical, dict):
            raise RuntimeError("historical export manifest classification is missing")
        expected_seal = {
            "algorithm": algorithm,
            "authentication": authentication,
        }
        if (
            historical.get("kind") != "historical_artifact_only"
            or historical.get("source_html_seal") != expected_seal
            or historical.get("current_run_database_verified") is not False
            or historical.get("completion_status_verified") is not False
            or historical.get("verification_mode_verified") is not False
            or historical.get("report_markdown_binding_verified") is not False
            or manifest.get("public_html_sha256")
            != hashlib.sha256(public_bytes).hexdigest()
        ):
            raise RuntimeError("historical export manifest did not preserve its verification limits")
        if fulltext_bytes in public_bytes or fulltext_bytes in manifest_bytes:
            raise RuntimeError("historical public export retained the synthetic full-text sentinel")
        if any(
            key_bytes in public_bytes or key_bytes in manifest_bytes
            for key_bytes in (synthetic_key_bytes, wrong_key_bytes)
        ):
            raise RuntimeError("historical public export exposed synthetic signing key material")
        if "citation-verifier-human-report-seal" in public_html:
            raise RuntimeError("historical public export retained the source report seal")
        expected_notice = {
            "hmac_authenticated": "source HMAC was authenticated",
            "hmac_not_authenticated": "source signature was not authenticated",
            "checksum_only_unauthenticated": "does not authenticate the report's author",
        }[authentication]
        if expected_notice not in public_html:
            raise RuntimeError("historical public export omitted its authentication notice")

        try:
            encoded_projection = public_html.split(data_open, 1)[1].split("</script>", 1)[0]
            public_projection = json.loads(encoded_projection)["projection"]
            public_pair = public_projection["pairs"][0]
        except (IndexError, KeyError, json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("historical public export projection could not be inspected") from exc
        if (
            len(public_projection["pairs"]) != 1
            or public_pair.get("decision") != expected_decision
            or public_pair.get("status") != "accepted"
        ):
            raise RuntimeError("historical public export changed the serialized verification result")
        if report_path.read_bytes() != report_bytes:
            raise RuntimeError("historical export changed its source report artifact")
        if database_bytes is None:
            if database_path.exists():
                raise RuntimeError("historical export created a run database")
        elif not database_path.is_file() or database_path.read_bytes() != database_bytes:
            raise RuntimeError("historical export changed the synthetic run database")

    def require_rejected(
        report_path: Path,
        report_bytes: bytes,
        run_dir: Path,
        output_path: Path,
        key: str | None,
        expected_error: str,
    ) -> None:
        database_path = run_dir / "run.sqlite"
        database_existed = database_path.exists()
        database_bytes = database_path.read_bytes() if database_existed else None
        rejected = False
        with mock.patch.object(signing, "load_key", return_value=key):
            try:
                export_public_html(
                    str(run_dir), str(output_path), historical=True,
                )
            except ValueError as exc:
                if expected_error not in str(exc):
                    raise RuntimeError("historical export rejected the fixture for an unrelated reason") from exc
                rejected = True
        sidecar = output_path.with_suffix(output_path.suffix + ".redaction.json")
        if not rejected:
            raise RuntimeError("historical export accepted a tampered or unauthenticated report")
        if output_path.exists() or sidecar.exists():
            raise RuntimeError("rejected historical export left public output or manifest files")
        if report_path.read_bytes() != report_bytes:
            raise RuntimeError("rejected historical export changed its source report artifact")
        if database_path.exists() != database_existed or (
            database_existed and database_path.read_bytes() != database_bytes
        ):
            raise RuntimeError("rejected historical export changed its synthetic run database")

    with tempfile.TemporaryDirectory(
        prefix=".callimachus-historical-smoke-", dir=state_root,
    ) as temporary:
        scratch = Path(temporary)
        public_dir = scratch / "public"
        public_dir.mkdir()
        corrupt_database = b"synthetic corrupt database fixture"

        valid_sha = make_sealed_report(None)
        if fulltext_bytes not in valid_sha:
            raise RuntimeError("historical export fixture did not contain the full-text sentinel")
        for name, database_bytes in (
            ("sha-database-absent", None),
            ("sha-database-corrupt", corrupt_database),
        ):
            run_dir, report_path, database_path = create_run(
                name, valid_sha, database_bytes,
            )
            original_report = report_path.read_bytes()
            output_path = public_dir / f"{name}.html"
            with mock.patch.object(signing, "load_key", return_value=None):
                result = export_public_html(
                    str(run_dir), str(output_path), historical=True,
                )
            if result != str(output_path):
                raise RuntimeError("historical SHA-256 export returned an unexpected output path")
            validate_export(
                report_path, original_report, database_path, database_bytes,
                output_path,
                algorithm="sha256",
                authentication="checksum_only_unauthenticated",
            )
            validate_history_recognition(run_dir, report_path)

        valid_hmac = make_sealed_report(synthetic_key)
        hmac_run, hmac_report, hmac_database = create_run(
            "hmac-database-corrupt", valid_hmac, corrupt_database,
        )
        original_hmac = hmac_report.read_bytes()
        hmac_output = public_dir / "hmac-database-corrupt.html"
        with mock.patch.object(signing, "load_key", return_value=synthetic_key):
            result = export_public_html(
                str(hmac_run), str(hmac_output), historical=True,
            )
        if result != str(hmac_output):
            raise RuntimeError("historical HMAC export returned an unexpected output path")
        validate_export(
            hmac_report, original_hmac, hmac_database, corrupt_database,
            hmac_output,
            algorithm="hmac-sha256",
            authentication="hmac_authenticated",
        )

        # Exercise the actual human Desktop adapter too: historical redaction
        # works without opening the old installation's private signing key.
        from core.app.commands.desktop import _export_desktop_report
        desktop_output = public_dir / "desktop-hmac-without-key.html"
        with mock.patch.object(signing, "load_key", side_effect=RuntimeError("probe must not read any signing key")):
            _export_desktop_report(str(hmac_run), str(desktop_output), historical=True)
        validate_export(
            hmac_report, original_hmac, hmac_database, corrupt_database,
            desktop_output,
            algorithm="hmac-sha256",
            authentication="hmac_not_authenticated",
        )

        tampered_run, tampered_report, _ = create_run(
            "tampered-sha", valid_sha,
        )
        untampered = tampered_report.read_bytes()
        tampered = untampered.replace(b"<!doctype html>", b"<!DOCTYPE html>", 1)
        if tampered == untampered:
            raise RuntimeError("historical tamper fixture did not change the report body")
        tampered_report.write_bytes(tampered)
        require_rejected(
            tampered_report, tampered, tampered_run,
            public_dir / "tampered-sha.html", None,
            "body does not match its seal",
        )

        for name, key, expected_error in (
            ("hmac-key-missing", None, "HMAC cannot be authenticated"),
            ("hmac-key-wrong", wrong_key, "HMAC signature is invalid"),
        ):
            require_rejected(
                hmac_report, original_hmac, hmac_run,
                public_dir / f"{name}.html", key, expected_error,
            )



def _probe_desktop_gui(state_root: Path) -> None:
    """Initialize the shipped GUI against synthetic data and disposable preferences."""
    import tempfile
    from unittest import mock

    state_root = Path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".callimachus-gui-smoke-", dir=state_root) as temporary:
        with mock.patch.dict(os.environ, {
            "QT_QPA_PLATFORM": "offscreen", "CALLIMACHUS_DATA_DIR": temporary,
        }):
            from PySide6 import QtCore, QtWidgets
            from core.gui.desktop import create_desktop_window, load_translations
            from core.gui.settings import preferences, services, transfer
            from core.gui.shell import chrome, navigation, resize, scroll

            app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
            if app.platformName() != "offscreen":
                raise RuntimeError("GUI probe requires the offscreen platform")
            settings_class = QtCore.QSettings
            settings_path = str(Path(temporary) / "preferences.ini")

            def isolated_settings(*_args, **_kwargs):
                settings = settings_class(settings_path, settings_class.Format.IniFormat)
                settings.setFallbacksEnabled(False)
                return settings

            isolated_settings.Status = settings_class.Status
            saved = []
            callback_errors = []
            window = None
            guided = None

            def require(condition, label):
                if not condition:
                    raise RuntimeError("GUI probe failed: " + label)

            def settle(predicate, label):
                timer = QtCore.QElapsedTimer()
                timer.start()
                while not predicate() and timer.elapsed() < 3000:
                    app.processEvents()
                    QtCore.QThread.msleep(10)
                require(predicate(), label)

            def callback_failed(kind, _error, _traceback):
                callback_errors.append(kind.__name__)

            with mock.patch.object(QtCore, "QSettings", isolated_settings), \
                    mock.patch.object(sys, "excepthook", callback_failed):
                try:
                    values = preferences.validate_preferences(preferences.default_preferences())
                    settings = isolated_settings()
                    for key in ("font_size", "dark_theme", "browser_cache_policy"):
                        settings.setValue(key, values[key])
                    settings.sync()
                    window = create_desktop_window(
                        language="en", preference_loader=lambda: "en",
                        preference_saver=lambda _value: None,
                        settings_loader=lambda: [{
                            "name": "CITATION_VERIFIER_MAILTO", "group": "general",
                            "control": "text", "value": "probe@example.invalid",
                            "configured": True,
                        }],
                        settings_saver=lambda updates: saved.append(dict(updates)),
                        docs_loader=lambda: {"README.md": "# Synthetic GUI probe"},
                        open_path_callback=lambda _path: require(False, "unexpected external open"),
                    )
                    window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
                    window.resize(600, 600)
                    window.show()
                    controller = window._atlas_navigation_controller
                    settle(lambda: controller.is_overlay, "compact navigation initialized")
                    window.navigation_toggle.click()
                    settle(lambda: controller.overlay.isVisible(), "navigation opened")
                    window.navigation.setCurrentRow(3)
                    settle(lambda: window.pages.currentIndex() == 3, "settings navigation")
                    settle(lambda: not controller.overlay.isVisible(), "navigation dismissed")
                    require(type(controller).__module__ == navigation.__name__, "navigation module")
                    require(type(window._atlas_window_chrome).__module__ == chrome.__name__,
                            "window chrome initialized")
                    require(type(window._settings_edge_scrollbar).__module__ == scroll.__name__,
                            "edge scrollbar initialized")
                    require(getattr(window.history_table, "_atlas_resize_filter", None) is not None
                            or getattr(window.history_table, "_atlas_resize_installed", False),
                            "table resize initialized")
                    require(resize.install_table_resize.__module__ == resize.__name__, "resize module")
                    window.settings_search.setText("contact")
                    settle(lambda: window.settings_search_results.count() > 0, "settings search")
                    require(services.valid_email("probe@example.invalid"), "settings services")
                    window.settings_search.clear()
                    editor = window._settings_editors["CITATION_VERIFIER_MAILTO"]
                    editor.setText("updated@example.invalid")
                    editor.textEdited.emit(editor.text())
                    window.settings_save_button.click()
                    require(saved == [{"CITATION_VERIFIER_MAILTO": "updated@example.invalid"}],
                            "synthetic settings saved")
                    before = window._dark
                    window.theme_toggle.click()
                    require(window._dark != before, "theme preference applied")
                    require(isolated_settings().value("dark_theme", before, type=bool) != before,
                            "preference persisted in temporary INI")

                    # Reuse the existing settings-dialog cancellation regression.
                    # No export, credential load or filesystem chooser is invoked.
                    dismissed = []
                    timed_out = []
                    timer = QtCore.QTimer(window)
                    timer.setInterval(10)
                    def dismiss_transfer():
                        dialog = app.activeModalWidget()
                        if dialog is not None and dialog.objectName() == "settingsTransferDialog":
                            dismissed.append(True)
                            dialog.reject()
                    timer.timeout.connect(dismiss_transfer)
                    watchdog = QtCore.QTimer(window)
                    watchdog.setSingleShot(True)
                    def transfer_timed_out():
                        timed_out.append(True)
                        app.exit(2)
                    watchdog.timeout.connect(transfer_timed_out)
                    timer.start()
                    watchdog.start(3000)
                    try:
                        require(transfer.options(window, QtWidgets, load_translations("en"), "export") is None,
                                "settings transfer cancelled")
                    finally:
                        timer.stop()
                        watchdog.stop()
                    require(not timed_out, "settings transfer timed out")
                    require(dismissed == [True], "settings transfer initialized")
                    window.navigation.setCurrentRow(0)
                    require(window.pages.currentIndex() == 0, "return to analysis")

                    # Exercise the real embedded Fetch transfer too: a stale
                    # QWidgetItem can survive simple navigation and fail later
                    # while native objects are allocated on a detail-tab change.
                    import shiboken6
                    from core.gui.guided_fetch import create_guided_fetch_window
                    guided = create_guided_fetch_window(
                        temporary, submit_source=lambda *_args: None, proceed=lambda: None,
                        inventory_loader=lambda _run: {"references": [{
                            "ref_id": "synthetic-source", "ref_number": 1,
                            "parsed": {"title": "Synthetic source awaiting full text"},
                            "resolve": {"status": "resolved"},
                            "fetch": {"tier": None, "pending_tasks": [{"task_kind": "fetch"}]},
                        }]},
                    )
                    rail = window._atlas_steps
                    heading = window._atlas_heading_row
                    old_item = heading.itemAt(heading.indexOf(rail))
                    window._embed_guided_fetch(guided)
                    require(not shiboken6.isValid(old_item), "Fetch rail item detached")
                    guided.show()
                    for _cycle in range(2):
                        for index in range(guided.tabs.count()):
                            if not guided.tabs.isTabEnabled(index):
                                continue
                            guided.tabs.setCurrentIndex(index)
                            app.processEvents()
                            require(guided.tabs.currentIndex() == index,
                                    "Fetch detail tab selected")
                            require(shiboken6.isValid(rail), "Fetch rail survives tab changes")
                    embedded_layout = rail.parentWidget().layout()
                    embedded_item = embedded_layout.itemAt(embedded_layout.indexOf(rail))
                    window._restore_analysis_workspace(guided)
                    require(not shiboken6.isValid(embedded_item), "Fetch rail item restored")
                    require(shiboken6.isValid(rail) and heading.indexOf(rail) >= 0,
                            "analysis rail restored after Fetch")
                    window.resize(1600, 900)
                    settle(lambda: not controller.is_overlay, "wide navigation restored")
                finally:
                    if guided is not None:
                        guided.close()
                        guided.deleteLater()
                    if window is not None:
                        # Also avoid a dirty-settings prompt if an earlier check failed.
                        window._settings_dirty.clear()
                        app.processEvents()
                        window.close()
                        app.processEvents()
                        window.deleteLater()
                    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                    app.processEvents()
            require(not callback_errors, "uncaught Qt callback errors")



def _probe_macos_keychain_gui(values: dict[str, str]) -> None:
    """Operator-driven desktop, with only the caller's synthetic vault callbacks."""
    import tempfile
    from unittest import mock
    from PySide6 import QtCore, QtWidgets
    from core.app.settings import credentials as store
    from core.gui.desktop import create_desktop_window

    # Keep CALLIMACHUS_DATA_DIR unchanged: it identifies the UUID Keychain item.
    # QSettings is redirected; bundled UI defaults may be read, but personal
    # settings and template-driven vault loading are not used.
    with tempfile.TemporaryDirectory(prefix=".callimachus-keychain-gui-") as temporary:
        settings_class = QtCore.QSettings
        class IsolatedSettings(settings_class):
            def __init__(self, *_args, **_kwargs):
                super().__init__(str(Path(temporary) / "preferences.ini"),
                                 settings_class.Format.IniFormat)
                self.setFallbacksEnabled(False)

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = None
        with mock.patch.object(QtCore, "QSettings", IsolatedSettings), \
                store._macos_desktop_secret_session():
            store.load_secret_values(values)  # Quiet startup; mark any blocked items.
            try:
                window = create_desktop_window(
                    language="en", preference_loader=lambda: "en",
                    preference_saver=lambda _language: None,
                    settings_loader=lambda: [], settings_saver=lambda _updates: None,
                    docs_loader=lambda: {"README.md": "Synthetic PackageSmoke credential qualification."},
                    credential_recovery_state_loader=store.macos_credential_recovery_state,
                    credential_recovery_callback=store.recover_macos_credentials,
                )
                window.setWindowTitle("Callimachus — synthetic Keychain qualification")
                window.show()
                app.exec()  # App and native consent buttons belong to the operator.
                if store.load_secret_values(values) != values:
                    raise store.CredentialStoreError("Synthetic GUI recovery is incomplete.")
            finally:
                if window is not None:
                    window._settings_dirty.clear()
                    window.close()
                    window.deleteLater()
                    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def _probe_macos_keychain_update(phase: str) -> int:
    """Opt-in frozen qualification; its fixed service can never address user items."""
    import uuid
    from core.app.settings import credentials as store

    try:
        probe_id = str(uuid.UUID(os.environ.get("CALLIMACHUS_PACKAGE_KEYCHAIN_ID", "")))
    except ValueError:
        sys.stderr.write("Invalid synthetic Keychain probe identifier.\n")
        return 2
    if sys.platform != "darwin" or phase not in {"create", "check", "recover", "gui", "delete"}:
        sys.stderr.write("Invalid synthetic Keychain probe phase or platform.\n")
        return 2
    previous_service = store._SERVICE_NAME
    previous_profile = os.environ.get("CALLIMACHUS_DATA_DIR")
    store._SERVICE_NAME = "Callimachus.PackageSmoke"
    # This profile string is only an account namespace; no file is read or created.
    os.environ["CALLIMACHUS_DATA_DIR"] = "/CallimachusPackageSmoke/" + probe_id
    values = {name: "synthetic-package-key-" + probe_id for name in (
        "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CITATION_VERIFIER_SIGNING_KEY",
    )}
    try:
        if phase == "create":
            with store.update_secret_values(values):
                pass
        elif phase in {"check", "recover"}:
            with store._macos_desktop_secret_session():
                loaded = store.load_secret_values(values)
                if phase == "recover" and loaded != values:
                    store.recover_macos_credentials()
                    loaded = store.load_secret_values(values)
                if loaded != values:
                    raise store.CredentialStoreError("Synthetic Keychain upgrade check failed.")
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool:
                    if any(item != values for item in pool.map(store.load_secret_values, [values] * 6)):
                        raise store.CredentialStoreError("Synthetic Keychain worker check failed.")
        elif phase == "gui":
            _probe_macos_keychain_gui(values)
        else:
            from core.app.settings import macos_vault
            from core.app.settings.macos_keychain import read_item, delete_item
            backend = store._trusted_backend()
            account = macos_vault.account(store._profile_scope())
            raw, reference = read_item(store._SERVICE_NAME, account)
            if reference is not None:
                if macos_vault._decode(raw, store._SERVICE_NAME, store._profile_scope()) != values:
                    raise store.CredentialStoreError("Synthetic fixture identity did not match.")
                delete_item(store._SERVICE_NAME, account, reference)
            for name, expected in values.items():
                from core.app.settings.macos_keychain import delete_if_matches
                delete_if_matches(store._SERVICE_NAME, store._username(name), expected)
    except Exception:
        sys.stderr.write("Synthetic Keychain update probe failed; no user credentials were accessed.\n")
        return 1
    finally:
        store._SERVICE_NAME = previous_service
        if previous_profile is None:
            os.environ.pop("CALLIMACHUS_DATA_DIR", None)
        else:
            os.environ["CALLIMACHUS_DATA_DIR"] = previous_profile
    print("macOS Keychain update probe passed: " + phase)
    return 0


def _package_self_test() -> int:
    """Check the frozen distribution, including a disposable offscreen desktop."""
    keychain_phase = os.environ.get("CALLIMACHUS_PACKAGE_KEYCHAIN_PHASE", "")
    if keychain_phase:
        return _probe_macos_keychain_update(keychain_phase)
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

    if "writable per-user data directory" not in failures:
        for probe, label in (
            (_probe_library_import, "Library PDF/DOCX round-trip"),
            (_probe_historical_export, "Historical report export"),
            (_probe_desktop_gui, "GUI shell/settings initialization"),
        ):
            try:
                probe(state_root)
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
                "Library PDF/DOCX round-trip passed; Historical report export probe passed; "
                "GUI shell/settings probe passed; "
                "OCR inference was not exercised.\n"
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
    except Exception as exc:
        from core.app.settings.credentials import CredentialStoreError

        if not isinstance(exc, CredentialStoreError):
            raise
        show_startup_error(str(exc))
        return False
    return True


def _main() -> int:
    argv = sys.argv[1:]
    desktop = not argv or (argv[0] == "app" and not {"-h", "--help"}.intersection(argv[1:]))
    if is_frozen() and sys.platform == "darwin" and desktop:
        from core.app.settings.credentials import _macos_desktop_secret_session
        with _macos_desktop_secret_session():
            return _main_impl()
    return _main_impl()


def _main_impl() -> int:
    argv = sys.argv[1:]
    program = Path(sys.executable).name if is_frozen() else "run.py"
    if is_frozen():
        if sys.platform == "linux" and program == "Callimachus.bin":
            program = "Callimachus"
        compatibility_error = startup_compatibility_error()
        if compatibility_error:
            show_startup_error(compatibility_error)
            return 2
    if is_frozen() and argv == ["--qt-probe"]:
        return _qt_probe()
    if is_frozen() and argv == ["--package-self-test"]:
        return _package_self_test()
    if argv == ["--jury-readiness-probe"]:
        from core.app.providers.readiness import main as readiness_main

        return readiness_main()
    startup_secret_snapshot = None
    capture_startup_secrets = is_frozen() and (
        not argv
        or (
            argv[0] == "app"
            and not {"-h", "--help"}.intersection(argv[1:])
        )
    )
    if capture_startup_secrets:
        from core.app.settings.credentials import _capture_startup_secret_snapshot

        with _capture_startup_secret_snapshot() as snapshot:
            if not _load_frozen_environment():
                return 2
        if snapshot.values:
            startup_secret_snapshot = snapshot
    elif is_frozen() and not _load_frozen_environment():
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
        if cmd == "app" and not {"-h", "--help"}.intersection(argv[1:]):
            if startup_secret_snapshot is not None:
                return _launch_desktop(
                    module_path, argv[1:], f"{program} {cmd}",
                    startup_secret_snapshot=startup_secret_snapshot,
                )
            return _launch_desktop(module_path, argv[1:], f"{program} {cmd}")
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
