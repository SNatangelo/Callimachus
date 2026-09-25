# Desktop packages

Callimachus can run from source with `python run.py app`, from the Windows
installer, or from a standalone archive produced by GitHub Actions. The native
package includes Python, the desktop interface, the pipeline, the optional OCR
and RAG libraries, bundled documentation and report assets, and third-party
notice files. One archive is built per operating system and CPU family, with
an additional Windows installer.

## Install and start

1. Open this repository's Releases page on GitHub.
2. On Windows x64, download `Callimachus-Setup.exe` and run it. If the Microsoft
   Visual C++ x64 runtime is missing or too old, the installer asks before
   downloading and installing the official Microsoft redistributable. It checks
   the downloaded file's Microsoft signature before running it. The application
   itself is included; Python does not need to be installed separately. The
   release also provides `Callimachus-Setup.exe.sha256` to check the download.
3. For a portable Windows copy, or on Linux and macOS, download the archive
   for `windows-x64`, `linux-x64`, `macos-arm64` (Apple Silicon), or `macos-x64`
   (Intel Mac). Download the adjacent `.sha256` file if you want to verify the
   download. Extract the **whole** archive; moving only the executable breaks
   its bundled resources and libraries. The Windows ZIP requires the Visual C++
   x64 runtime to be installed already.
4. Launch `Callimachus.exe` on Windows, `Callimachus.app` on macOS, or the
   `Callimachus` executable inside its folder on Linux.

The application creates a private `.env` from the bundled template on first
launch. Configure model backends and credentials in Settings before Verify;
API keys are never included in a release. The packaged application uses its
own writable user-data directory, separate from each source checkout and Git
worktree:

| System | User-data directory |
|---|---|
| Windows | `%LOCALAPPDATA%\Callimachus` |
| macOS | `~/Library/Application Support/Callimachus` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/Callimachus` |

The `.env` and `runs/` directory live there, so replacing the application
archive does not overwrite them. Source launches still use their own
worktree's `.env` and `runs/`.

## What is external

Google Chrome is not bundled. Install it separately if you need Guided Fetch's
browser capture. If it is unavailable, the GUI should show an actionable
message; automatic retrieval and other phases do not require Chrome. Network
access and valid credentials are still required for the providers and LLMs you
select. Bundled OCR does not turn an unreadable scan into verified evidence;
unreadable content stays unresolved.

Linux still depends on operating-system graphics libraries used by Qt. If Qt
cannot start, the launcher reports the problem rather than showing a raw Python
traceback. The Windows installer is not code-signed, so Windows may warn before
launch; compare its SHA-256 checksum with the release sidecar and use only a
release you trust. The separately downloaded Microsoft runtime is checked for
a valid Microsoft signature before it is run. macOS archives are currently
**unsigned and unnotarized**. macOS may
block their launch until Apple Developer signing and notarization are added to
the release workflow. Do not treat a successful CI build as a signed Mac app.

## Licenses and release integrity

The root `LICENSE` covers Callimachus. Each dependency retains its own terms.
`THIRD-PARTY-NOTICES/` is generated from the package versions installed on the
native build runner and includes its index plus required PDFium, ONNX Runtime,
Playwright and Qt notices. It also records the bundled CPython license and, on
Linux, the distribution copyright files for system libraries copied into the
package. Qt's version-matched third-party attribution pages and their checksums
are under `Qt-PySide6/attributions/`. The build stops when it cannot identify a
bundled native binary or obtain a required notice. The same directory is
embedded as an application resource. The adjacent `.sha256` file lets you
compare the archive's SHA-256 digest after download. The release also includes
the corresponding Callimachus source archive and its digest. It also ships
separate, checksum-verified source archives for Qt 6, Qt 5 bundled by OpenCV,
PySide6, PyMuPDF, MuPDF, OpenCV Python, its FFmpeg, and the PyInstaller
bootloader. Their exact versions,
upstream URLs, and SHA-256 digests are in `Callimachus-dependency-sources.json`.
The Qt archives are large
and are separate from the application download. The source assembly step fails
the release before publication if an archive does not match its pinned digest.
The Linux release additionally includes `Callimachus-linux-system-sources.tar.gz`
with the exact Ubuntu source packages for system libraries copied into that
build. Historical versions are retrieved from Ubuntu's Launchpad source-file
archive and checked against the SHA-256 digests in their source descriptors.
Its manifest lists each source package, version, file, and digest; a missing
source package stops publication.

A `v*` tag publishes a GitHub Release only after the Windows, Linux, and two
macOS builds pass focused tests and frozen-executable smoke checks. The build
uses prebuilt dependency wheels and fails if a required wheel is absent. Its
identity record stores the source commit and whether the checkout was dirty.
It does not make a run audit-ready: the separate artifact authority described
in [Deployment](../../DEPLOYMENT.md) is still required for that designation.

This guide records what the packaging process does, not a legal opinion about
license compliance. Review the actual notices and the published dependencies
before distributing a release.
