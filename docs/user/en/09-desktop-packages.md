# Desktop packages

Microsoft Store packaging has a separate
[submission and test contract](../../deployment/microsoft-store.md).
Read [Privacy and AI](../../legal/en/privacy.md) before starting online operations.

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

## Settings and credentials in 2.0

Compiled desktop applications (including MSIX) and source launches use different
storage. Configure model-provider credentials in the Jury chooser and other
service credentials in Settings before Verify; API keys are not bundled.

| Runtime | Saved configuration | Saved credentials | Distributed template |
|---|---|---|---|
| Compiled desktop | `settings.conf` in the user-data directory | Native OS vault | Read-only `settings.defaults` in the application resources |
| Source (`python run.py app` or CLI) | Local `.env` (or the first applicable `.env.local`) | Dotenv values remain plaintext, unless supplied separately through the process environment | Versioned `.env.example` |

At first launch, a compiled app creates `settings.conf` from `settings.defaults`
with secret assignments blank. Non-secret settings include endpoints, model IDs,
Jury selections, contact email and analysis options. These values can still be
personal or operationally sensitive. UI preferences may use separate preference
files; `settings.conf` is not a backup of all application state.

Saved API keys, tokens, passwords and the locally generated report-signing key
use Windows Credential Manager, macOS Keychain or Linux Secret Service. If the
required vault is unavailable or locked, credential access fails with an error;
there is no plaintext fallback. A Linux desktop needs an available Secret Service
session. Genuine process-environment overrides still take precedence; remove an
override before editing that setting in the GUI. Existing explicit signing-key
file references retain their file-based behavior rather than being migrated.

The compiled application uses its own writable user-data directory, separate
from each source checkout and Git worktree:

| System | User-data directory |
|---|---|
| Windows | `%LOCALAPPDATA%\Callimachus` |
| macOS | `~/Library/Application Support/Callimachus` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/Callimachus` |

The `settings.conf` file and `runs/` directory live there; replacing the extracted
application archive does not overwrite them. `CALLIMACHUS_DATA_DIR` can select a
different data directory for a compiled app and also selects a separate vault
profile. Copying `settings.conf` alone does not transfer credentials. MSIX may
virtualise application data; package replacement or uninstall must not be assumed
to preserve it. Source launches still use their worktree's `.env` and `runs/`.

### Previously configured credentials and backups

Compiled 2.0 does not load or automatically migrate old `.env` files, including
ones left in the user-data directory. It does not automatically delete them.
Re-enter the required credentials through the Jury chooser or Settings and check
the provider before Verify. A secret manually placed in `settings.conf` is
ignored by the compiled loader, not imported into the vault. Source launches
continue to use dotenv; the source-only `python run.py configure` command is not
the configuration route for compiled apps.

Settings export is a separate, explicit operation and can include credentials.
Password encryption is optional; an unencrypted export can contain plaintext
secrets even when the installed app uses a vault. Import replaces supported
settings and routes imported secrets to the vault in a compiled app; it is not
an automatic legacy-dotenv migration. External CLI logins and browser sessions
remain managed by their respective clients. See [Keys and credentials](../../reference/10-keys-and-credentials.md)
and [Privacy and AI](../../legal/en/privacy.md) for the storage and integrity boundaries.

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
a valid Microsoft signature before it is run. macOS release archives use a persistent local signing identity,
**without Apple Developer ID signing or notarization**. macOS may require
approval in **System Settings → Privacy & Security → Open Anyway** for a trusted
download. A local signature is not Apple notarization; do not disable Gatekeeper.
Disposable pull-request artifacts may instead be ad-hoc signed.

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

Release publication requires successful Windows, Linux and both native
macOS builds, frozen-executable smoke checks and verified dependency sources.
The workflow accepts a matching version tag or an explicit publication request
from the corresponding release branch. Dependencies follow the pinned build
constraints; affected macOS bindings are built from verified upstream source
to retain the supported deployment target. Its
identity record stores the source commit and whether the checkout was dirty.
It does not make a run audit-ready: the separate artifact authority described
in [Deployment](../../../DEPLOYMENT.md) is still required for that designation.

This guide records what the packaging process does, not a legal opinion about
license compliance. Review the actual notices and the published dependencies
before distributing a release.
