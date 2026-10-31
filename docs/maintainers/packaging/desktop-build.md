# Desktop packaging

## Maintainer build procedure

Update this procedure in the same change as a packaging fix. Use the current
versioned scripts and constraints; a successful old binary or a private temporary
build directory is not a substitute for the recipe. PyInstaller remains the
desktop packager.

### Reuse native dependencies before rebuilding them

Keep verified native artifacts in a persistent workspace `build-cache` outside
throwaway source checkouts. Save the installed Qt development prefix, completed
Shiboken/PySide wheels, original source/tool archives, source-patch hashes,
dependency freeze, compiler/SDK information and qualification receipts. Record
per-file and archive hashes and preserve any original paths or drive aliases
needed by generated build trees. Cache snapshots must exclude credentials,
personal settings, `.env` files and run data.

Reuse requires matching platform and architecture, supported OS baseline,
Python ABI, Qt/PDF/PySide versions, build options, compiler/SDK, source patches
and selected runtime framework. Verify the recorded hashes before installation
or reuse. An application-only Python or GUI change does not invalidate those
unchanged native dependencies: freeze the new application against the preserved
runtime instead of rebuilding Qt and PySide. Run the relevant application tests,
frozen smoke tests and final binary/runtime audit for every new package.

Keep a completed Qt-only prefix separate from pending PDF/binding components;
label it built rather than fully runtime-qualified until qualification passes.
Preserve candidate wheels in a separate pending area until their metadata,
contents and runtime checks pass; only then mark them qualified for release
reuse. Never merge incomplete build outputs into a qualified environment.
If inputs change or relocation cannot be verified, create a new cache generation
without overwriting the earlier known-good artifacts. Routine cleanup of old
application packages must preserve the native cache and its protected inputs.

### Source, tools and supported systems

Build a committed revision in a clean Git checkout, keeping a separate checkout
for tests that create local artifacts. Retain `git rev-parse HEAD`, the dependency
freeze, build metadata, test results and artifact hashes. Do not copy a personal
`.env`, credentials, user settings or real run data into either the bundle or a
source archive used for a build. Set the application and package versions for
the release being prepared, keeping their metadata consistent.

Derive transferred-source hashes from the committed Git contents, not a Windows
working copy whose text files may still contain CRLF. Verify the complete file
set, revision and clean native checkout before the build.

| Target | Required qualification |
|---|---|
| Windows x64 | Windows 10 1809/build 17763 or newer; CPython 3.13.15; qualified native CRT |
| Linux x64 | Build and test on Ubuntu 22.04/glibc 2.35, or a demonstrated equivalent |
| macOS | macOS 14.4 minimum; build natively for the package architecture; audit every Mach-O dependency |

Use the Qt/PySide/Shiboken and separate PDF/WebEngine versions selected by
`packaging/release-constraints.txt`. Qt 6.12 is approved on all three systems.
Do not reintroduce 6.11.3 to address a build inconvenience: its nested-layout
ownership defect can leave destroyed layout items registered in Shiboken
(upstream PYSIDE-3469). Do not qualify a package using tests that monkeypatch
Qt's virtual `itemAt` methods: that instrumentation can itself create stale
wrappers during native layout removal.

Create a dedicated environment with the qualified CPython, then install from
the repository root:

```sh
python -m pip install --only-binary=:all: -r requirements.txt -r requirements-gui.txt -r requirements-ocr.txt -r requirements-rag.txt -c packaging/release-constraints.txt
python -m pip install --only-binary=:all: pyinstaller==6.22.3 pytest -c packaging/release-constraints.txt
python -m pip check
python -m pip freeze --all
```

On a Windows Store builder, use the retained native Python/CRT procedure under
`packaging/store/native-recipes/` and the
[Store submission contract](../../deployment/microsoft-store.md). Do not substitute
DLLs from the developer PC for the selected Microsoft VCLibs framework. Audit
the actual packaged PE imports against that framework after a Qt update.

For local builds, require at least 7 GiB of free host RAM before starting and
stop the owned build process group if available RAM falls below 4 GiB. Keep
temporary files and caches inside the task workspace. Build sequentially:
macOS, shut down its VM, Linux, shut down its VM/WSL, then Windows. Do not shut
down an environment while its tests or artifact transfer are still running.

### Windows Sandbox crash evidence

Before launching a test package in Windows Sandbox, configure guest-only WER
`LocalDumps/Callimachus.exe` to write full user-mode dumps (`DumpType=2`) into
a writable host-mapped evidence folder. Use a unique session folder, verify
write access as the interactive guest user, and keep the host's WER settings
unchanged. The host kernel/VM dump collector does not collect application crashes.

Wait until each dump is no longer open for writing before preserving its original
bytes and recording its size and SHA-256. Retain a verified copy outside any
WER rotation folder; a guest-local or temporary WER file may disappear before
collection. Verify this mechanism with a separately named synthetic crash
fixture and label those results separately from real application crashes. Do not
upload dumps or include them in release artifacts: they can contain process memory.

### Persistent macOS signing and Keychain access

Use one persistent signing certificate and private key across updates. Set
`CALLIMACHUS_MACOS_SIGNING_IDENTITY` to the certificate's 40-digit SHA-1
fingerprint. PyInstaller signs nested code with that identity; the final reseal
regenerates the designated requirement rather than retaining an ad-hoc hash.
Keep `science.callimachus.desktop` stable. Back up the signing identity securely:
recreating a certificate on every build loses continuity even if its name matches.

A Developer ID subscription is not required to publish archives on GitHub.
A persistent self-signed identity keeps the certificate-based designated requirement
stable, but does not guarantee prompt-free Keychain updates: the legacy Keychain
also partitions self-signed clients by their changing code hash. Do not promise
that a one-time grant survives every rebuild. Self-signing does not satisfy
Gatekeeper or provide Apple notarization. Document the initial **System
Settings â†’ Privacy & Security â†’ Open Anyway** approval for a downloaded,
non-notarized build. Do not instruct users to disable Gatekeeper or give every
application access to their Keychain. Only the build machine needs its local
certificate approved for code signing; do not install private keys on user Macs.

Unsigned/ad-hoc diagnostic builds require the explicit opt-in
`CALLIMACHUS_MACOS_ALLOW_ADHOC=1`. They cannot qualify Keychain access after an
update. The workflow uses this opt-in only for disposable pull-request artifacts.
For a self-signed certificate also set `CALLIMACHUS_MACOS_SELF_SIGNED=1` on the
builder. That explicit mode supplies only the library-validation exception needed
to load the bundled Python/Qt libraries without an Apple Team ID. It does not
change Keychain authorization or system-wide security policy. Native qualification
must still execute the complete frozen smoke suite; ad-hoc success is insufficient.

Resign only `Contents/Frameworks/playwright/driver/node` with
`packaging/assets/macos-playwright-node.entitlements.plist` before the final root
reseal. Hardened Node/V8 needs `allow-jit` and `allow-unsigned-executable-memory`;
`allow-jit` alone failed the native JavaScript probe. Do not grant these exceptions
to the main app or Qt. Node does not need the library-validation exception.
The seventh smoke executes the actual bundled CLI JavaScript, not just
`node --version`, so it detects V8 startup failures before distribution.

For signed manual/tagged GitHub builds, configure the encrypted PKCS#12 identity
in `CALLIMACHUS_MACOS_CERTIFICATE_BASE64`, its password in
`CALLIMACHUS_MACOS_CERTIFICATE_PASSWORD`, and its fingerprint in
`CALLIMACHUS_MACOS_SIGNING_IDENTITY` repository secrets. For a self-signed
certificate, additionally set the repository variable
`CALLIMACHUS_MACOS_SELF_SIGNED=1`. The runner imports it into a temporary build
keychain, approves only code-signing policy there, and removes the keychain after
packaging. No private identity belongs in Git or a distributed app.

Qualify upgrades with two separately launched frozen bundles whose binary/signature
hashes differ and whose complete designated requirements match without `cdhash`.
The existing package self-test has an opt-in synthetic Keychain fixture controlled
by `CALLIMACHUS_PACKAGE_KEYCHAIN_PHASE=create|check|recover|delete` and a fresh UUID in
`CALLIMACHUS_PACKAGE_KEYCHAIN_ID`. It uses only `Callimachus.PackageSmoke`, never
real Callimachus credential items. Run create in A, check with a cold process in
A and B. If B needs authorization, `check` must fail promptly without UI;
only an explicitly initiated `recover` phase may request native consent. Then
repeat cold `check` in B, and delete using the authorized app. `check` uses a native read that
forbids authentication UI: a permission prompt requirement fails the test rather
than being hidden or automatically approved. Also qualify refusal/Cancel with a
usable GUI and no repeated background authorization attempts. Normal smoke runs
leave this fixture off and never contact a personal Keychain.

Follow the [synthetic A/B and desktop recovery procedure](macos-keychain-qualification.md)
for the exact build pair being distributed. Preserve the outcomes and limits
with its build receipts; an older pair does not qualify a new binary.

Ordinary reads suppress both the modern authentication-UI flag and the legacy
Keychain interaction flag. The latter is process-wide: restore its previous value
in `finally` and serialize quiet reads with interactive reads and writes. GUI workers
share authorized values for that desktop lifetime; denial never triggers automatic
retries. Recovery is optional and starts only after the main window is visible.

macOS uses one `Callimachus / <profile>:CREDENTIALS_V1` item per profile, containing
only classified secrets. The JSON binds its schema, service and profile. Legacy
`<profile>:<setting>` entries are read only as needed and consolidated only after
explicit recovery consent. Preserve newer aggregate values and deletion tombstones;
import fills only missing names under the writer lock. Verify the new item before
removing any old one. Reads, updates, rollback and cleanup bind to persistent item
references: class/service/account matching alone must never update or delete every
namesake across Keychains. No label-based scanning or cleanup is permitted.

Native tests require `CALLIMACHUS_RUN_NATIVE_KEYCHAIN_TESTS=1` and an explicitly
selected owned keychain in `CALLIMACHUS_NATIVE_KEYCHAIN_PATH`. Test queries are pinned
to that keychain without changing the system search list; fixture names are fixed to
`Callimachus.PackageSmoke` and UUID profiles. No personal login items are accessed.

For native SSH builds, unlock the dedicated build keychain in the same foreground
security session that runs codesign/PyInstaller. Unlocking in a previous SSH session
and then launching a detached build can fail with `errSecInternalComponent`. Keep
its password in protected local storage, transmit it only through stdin, and never
write a private identity or password to logs, source manifests or app resources.

### macOS 14.4 binding preparation

The release workflow prepares the macOS application runtime on its ephemeral
GitHub-hosted runner with `packaging/prepare_macos_runtime.sh`. The helper is
CI-only: it downloads the fixed-hash Python.org universal2 CPython 3.13.15
installer, checks the package signature, installs it on that temporary runner,
and audits the native interpreter and framework slices against the 14.4 minimum
before creating a virtual environment under `RUNNER_TEMP`. Local Mac machines
are never modified by this workflow helper. The runtime and framework remain
subject to the 14.4 minimum checks in the binding builder and frozen-package
smoke test. Each native package artifact includes the installer URL and hash,
Python version, runner architecture, and inspected interpreter/framework minima.

For a fresh native CI binding build, use
`packaging/prepare_macos_bindings.sh` after installing the application
dependencies. Set `BUILD_PYTHON` to the native CPython 3.10.11 executable on
Apple Silicon or CPython 3.10.22 on Intel, and set `RUNTIME_PYTHON` to the
verified application CPython 3.13.15 executable, then run:

```sh
BUILD_PYTHON="/path/to/python3.10" RUNTIME_PYTHON="/path/to/python3.13" \
  bash packaging/prepare_macos_bindings.sh
```

The helper selects the native `arm64` or `x86_64` host architecture, verifies
pinned source/tool inputs, builds only the affected bindings, relocates a
ten-file overlay, preserves the PDF binaries and records a provenance manifest.
The resulting package still needs native runtime/import checks, the full
frozen smoke suite and a final Mach-O/signature audit. Parameterizing the Intel
recipe does not by itself qualify Apple Silicon or a new build. Preserve the
actual architecture-specific outcomes with the release artifacts.

Installing the official 6.12 wheels alone did not qualify the Intel 14.4 target:
the affected binding binaries declared a newer macOS minimum. Reuse the already
audited 14.4-compatible environment when its versions and hashes still match.
If rebuilding it, compile the affected bindings from the unmodified official
`pyside-setup-everywhere-src-6.12.0.tar.xz` archive, SHA-256:

```text
099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751
```

The qualified Intel binding build uses an isolated CPython 3.10.22 environment
for the stable ABI. The native arm64 CI runner uses CPython 3.10.11 because
3.10.22 has no published arm64 runner archive; both build interpreters target
the stable ABI, and the application itself still uses CPython 3.13.15. The
helper rejects a build interpreter whose exact version or native architecture
does not match its runner. This Python is a build tool and is not shipped, so
only its architecture is checked; the application runtime, overlay, and Qt
frameworks retain the strict 14.4 minimum. Stage a Qt 6.12.0 macOS SDK, static
libclang with its headers/CMake configuration, CMake 3.22 or later and Ninja.
Verify the SDK frameworks also meet 14.4. Set
`LLVM_INSTALL_DIR`, `MACOS_SDK`, `QTPATHS_BIN`, and `CMAKE_BIN` to these staged
tools, then build from the extracted source directory:

```sh
export MACOSX_DEPLOYMENT_TARGET=14.4
python setup.py install \
  --qtpaths="$QTPATHS_BIN" --cmake="$CMAKE_BIN" --ignore-git \
  --qt-conf-prefix='"Qt"' --rpath=@loader_path/Qt/lib \
  --skip-docs --no-qt-tools --verbose-build --parallel=2 \
  --module-subset=Core,Gui,Widgets,Network,DBus,Svg,Test \
  --limited-api=yes --macos-arch=x86_64 \
  --macos-sysroot="$MACOS_SDK" --macos-deployment-target=14.4
```

For an incremental rebuild of that same verified build directory, add
`--reuse-build`; omit it for a fresh build. Preserve the quotes around `Qt` in
`--qt-conf-prefix`. Keep build-only Python/sysconfig paths out of the application
runtime. This Intel recipe does not qualify a new Apple Silicon binding build.

Stage copies of the seven rebuilt Qt bindings, `libpyside6`, the Shiboken module
and `libshiboken6` as a ten-file overlay. Record their SHA-256 digests. Preserve
the qualified `QtPdf`/`QtPdfWidgets` bindings and verify their hashes did not
change. On the staged copies, remove SDK/build-directory RPATHs and use
`@loader_path`; PySide bindings additionally use
`@loader_path/../shiboken6` and `@loader_path/Qt/lib`. Re-sign those copies ad hoc.
Never patch a higher Mach-O deployment target downward to claim compatibility.

Before replacing the corresponding files in the fresh application environment:

- Check architecture and `LC_BUILD_VERSION`/`LC_VERSION_MIN_MACOSX` for every
  overlay file with `lipo` and `otool`/`vtool`; every minimum must be at most 14.4.
- Verify the overlay hashes and the unchanged PDF hashes.
- Import Qt/PySide and open a synthetic PDF with the **application's Python
  3.13.15**, including `QPdfView`.
- Check the actual loaded Qt frameworks resolve inside that environment's
  `PySide6/Qt/lib`, without an SDK `DYLD_FRAMEWORK_PATH` override.

Repeat this qualification whenever Qt, the compiler, source bindings, PDF
bindings or application Python change; do not silently reuse an old overlay.

### Tests, freeze and artifact checks

For binary-inspection commands whose output is parsed, explicitly set
`LC_ALL=C` (for example, when reading `readelf -h`). Do not classify an unknown
architecture from a translated field label as a binary incompatibility; retain
the failed audit and repeat it with a fixed output locale.

Run the package contracts selected in
`.github/workflows/desktop-release.yml`, the owners of changed behavior and
the relevant desktop GUI owners. Run GUI owners in fresh processes. Use
synthetic PDF/DOCX inputs and private settings/data directories. A native crash
or a memory-guard interruption is a failed qualification, even when other tests
or frozen smoke checks pass. Record skipped tests and unavailable native hosts.

When a GUI owner exceeds a local process memory cap, retain the cap notification
and process identity before classifying a native abort. Collect the complete
test-node list once and run batches of at most 40 cases in fresh processes under
the same limits. Keep a manifest proving that every collected case was executed
exactly once; batching must not omit failing cases or replace behavioral checks.

From the clean build checkout:

```sh
python packaging/build_desktop.py
python packaging/smoke_desktop.py
python packaging/archive_desktop.py linux-x64
```

Use `windows-x64`, `macos-x64` or `macos-arm64` instead of `linux-x64` for the
corresponding native build. The smoke suite must pass all seven checks: Qt startup,
packaged self-test including Library PDF/DOCX import/review/save/reopen, CLI help,
OCR help, synthetic PDF parse, image-only PDF OCR, and execution of the bundled
Playwright JavaScript driver with `--version` (without opening a browser). Preserve the fail-closed
license/source-inventory checks.

The existing `--package-self-test` also exercises historical HTML export with
synthetic sealed reports and absent or corrupt run databases. It checks History
discovery, SHA checksum versus authenticated HMAC labels, preserved decisions,
redaction and manifest integrity, and rejection of tampered or unauthenticatable
reports on the strict path. The same probe also calls the human Desktop adapter
without a signing key, checking its unverified-signature notice and manifest while
preserving redaction and decisions. This permission is confined to historical
artifact-only export; live run and default CLI integrity gates remain strict.
Signing-key substitution is confined to these temporary fixtures; the
probe must not read real signing keys. The frozen smoke runner requires the
explicit historical-export success marker as well as the Library marker. Source
test success alone, or an older binary lacking this probe, does not qualify the
compiled historical-export path. No public `report-export --historical` option
is introduced for testing.

The same internal self-test constructs a disposable offscreen desktop and exercises
compact navigation, Settings search and synthetic saving, a temporary theme
preference, the cancelled settings-transfer dialog, and the return to a wide
layout. It also embeds a synthetic assisted-Fetch view, switches its detail tabs,
and restores Analysis while checking that moved layout items are invalidated
and the shared phase rail remains alive. This initializes `core/gui/shell/`,
`core/gui/settings/` and the assisted-Fetch view, including lazy imports. All data callbacks are synthetic and every QSettings access is
redirected to a temporary INI with fallbacks disabled; no user credentials or
external service is involved. Temporary state is removed on success and failure.
An initialization or Qt callback exception fails the self-test. The smoke runner
also requires the `GUI shell/settings probe passed` marker, so an older executable
cannot qualify by returning zero without running the probe. The ordinary
`--qt-probe` remains lightweight; no new public CLI command is added.

Run the corresponding source regressions before freezing, then run the existing
smoke suite against each newly built native distribution. Source/offscreen test
success is not evidence that an earlier executable contains the reorganized
modules. Do not bypass a platform runtime/license gate or resume a paused MSIX
build merely to obtain this qualification.

Every frozen smoke child process receives empty values for the secret settings
declared by `.env.example`, and the signing-key file override is removed. This
keeps inherited developer credentials out of package probes without changing
the build process environment or reading local `.env` values.

Linux's public `Callimachus` entry is a shell launcher which checks GNU glibc
before loading the internal `Callimachus.bin`. Its system helpers use the standard
`/usr/bin:/bin` search path when the caller has an empty `PATH`, without changing
the environment passed to the application. Ship both with the complete
`_internal` directory. Test the wrapper on the qualification baseline, including
arguments, paths with spaces and symlinked launch commands. Audit the final ELF
dependencies against the glibc 2.35 baseline; passing on a newer WSL distribution
alone is insufficient. Before distribution, also run:

```sh
python packaging/collect_ubuntu_sources.py
```

For macOS, verify `LSMinimumSystemVersion=14.4`, every final Mach-O minimum and
architecture, and absence of absolute non-system library dependencies/RPATHs.
Run `codesign --verify --deep --strict` on the final `.app` after all changes.
An ad-hoc signature is not Developer ID signing or notarization, and checking
binary minimums on a newer Mac is not a test on macOS 14.4 itself.

### Qt 6.12 Store-native build

The retained official Qt 6.12 binaries report linker 14.44, while the selected
Microsoft-signed VCLibs framework (`14.0.33728.0`) contains runtime DLLs
`14.40.33728.0`. The project chooses a 14.39 toolset ceiling for that runtime;
this is a compatibility policy, not a Store requirement to compile Qt from
source. Microsoft's [runtime compatibility rule](https://learn.microsoft.com/en-us/cpp/porting/binary-compat-2015-2017)
requires a runtime at least as recent as the newest component toolset. The
source-build route is recorded in
`packaging/store/native-recipes/build-qt612-msvc1439.py` and
`qt612-source-inputs.json`. Qualify its build, PE audit, frozen smokes and fresh
Windows Sandbox installation; Store certification and the actual Windows 10
1809 baseline remain separate checks. The recipe requires explicit
paths to the verified Qt 6.12, PySide/Shiboken 6.12 and separate Qt PDF source
6.140, the exact MSVC 14.39 toolchain, SDK, Python 3.13.15, CMake, Ninja,
Perl and libclang. Run each named stage under the existing Windows RAM and
owned-process guard; the recipe itself is not a guard. Configure and build Qt,
install to a private prefix, then configure/build/install Qt PDF with full
WebEngine disabled, followed by the matching PySide build. Two build jobs are
used. Verify PDF and PDF Widgets, the PDF image plugin, wheel metadata and ABI,
and the final frozen imports before packaging. The Windows target macros are
not proof of Windows 10 1809 compatibility; test that baseline separately.
Do not mix old Qt wheels or unverified binaries into the new prefix.

The source-built CPython must expose its matching Windows `pyconfig.h` in the
public include directory before configuring Shiboken. For a GIL-enabled Windows
build, verify the CPython build procedure and source hash before staging its
matching generated header; never substitute headers from another interpreter.
For a monolithic PySide subset, the candidate recipe contains a hash-verified
Windows packaging correction for a missing Designer plugin only when Designer
is explicitly excluded from the requested subset. It does not fabricate the
plugin or waive checks for required bindings, Qt DLLs, PDF plugins, or wheel
metadata. `--no-qt-tools` limits tool executables separately. Reuse existing
Shiboken/PySide build outputs only after verifying their cache, compiler, Python,
Qt prefix and selected modules; qualify every resulting application package separately.

Invoke the recipe once per stage, under the external guard, in this order:
`configure-qt`, `build-qt`, `install-qt`, `configure-pdf`, `build-pdf`,
`install-pdf`, `build-pyside`. Each invocation requires `--vs`, `--sdk`,
`--cmake`, `--ninja`, `--python`, `--perl`, `--clang`, `--qt`, `--pdf`,
`--pyside`, `--build` and `--prefix` paths. Use short, private source/build
paths, preserve source checksums and stage logs, and stop if a configure
summary disables a required module. For this subset, produce the upstream
monolithic `PySide6==6.12.0` and `shiboken6==6.12.0` wheels with
`setup.py bdist_wheel --reuse-build`, retaining the verified build flags,
module subset and explicit tool/prefix paths. Inspect the actual compatible
wheel tags, metadata and Qt DLL hashes before installation. PDF/PDF Widgets
must be present as real bindings; the optional official split-wheel constraint
`PySide6_Pdf==6.12.0.140` does not require inventing split metadata for the
monolithic wheel. Do not install the generator into the application runtime or
emit empty Essentials/Addons/WebEngine distributions. The development tool
copy can be disabled with `--no-qt-tools`; required runtime plugins remain
subject to the normal presence and hash checks.

The Windows PDF build also requires the recipe's hash-verified patch to the
extracted `qwe_setup_toolchain.py`. Upstream's script otherwise reloads the
host SDK even when GN has a different `windows_sdk_path`. The patch accepts
only the recorded original or patched source hash, retains the real Visual
Studio installation, and pins the generated x64 environment to the selected
SDK and MSVC 14.39. Propagate `QWE_PINNED_SDK` to every configure/build child;
missing or mismatched SDK input must stop the build. Validate the generated
`environment.x64` before reusing it and after a successful build. Preserve the
original archive and patch hashes in release evidence. After an SDK mismatch,
use a fresh PDF build directory rather than reusing its object files.

Set `NINJAFLAGS=-j2` before configuring Qt PDF: its nested Ninja command does
not inherit CMake's job limit automatically. Keep the external RAM/process
guard. Do not change Visual Studio installation ACLs or register a system SDK
just to make the private build inputs visible.

### Windows installer and MSIX

With NSIS **3.10** on `PATH`, generate and test the standalone installer:

```sh
python packaging/build_windows_installer.py
python packaging/smoke_windows_installer.py
```

Its OS gate must reject unsupported Windows before staging the VC-runtime
preflight or installing application files. The standalone application also
checks its OS before loading settings, credentials or Qt. Microsoft Store/MSIX
processes do **not** execute that additional OS-version check; their existing
manifest minimum is enforced by Windows.

Create the MSIX with the reserved Store identity, publisher/display name, the
qualified x64 Microsoft VCLibs APPX and MakeAppx/MakePri from the selected Windows SDK:

```sh
python packaging/build_msix.py --identity-name "$IDENTITY_NAME" --publisher "$PUBLISHER" --publisher-display-name "$PUBLISHER_DISPLAY_NAME" --version 2.0.0.0 --framework-appx "$VCLIBS_APPX" --makeappx "$MAKEAPPX"
```

The builder selects MakePri beside MakeAppx, then on PATH; use `--makepri` to
select it explicitly. It preserves the original N1 artwork and generates exact
pixel-size taskbar variants for default, light and dark shell surfaces. A
transparent manifest background alone does not suppress Windows' icon plate.
MakePri indexes only staged visual assets, retaining their `Assets/` paths and
the actual package identity. All variants stay in one `resources.pri`; a detailed
index check must find every expected taskbar candidate before MakeAppx runs.
A missing tool, empty index or missing candidate stops packaging without replacing
the previous artifact. Inspect the icon after a fresh installation in both shell
themes; PNG alpha checks alone do not qualify Windows' final rendering.

Do not invent a publisher or use a different CRT to make packaging pass. Keep
local test signing separate from Store submission. Hash and inspect each final
archive/MSIX, its source revision/dirty metadata, notices and source inventory
before replacing the previous VM copy. Replace the application only after those
checks pass; keep settings, credentials, runs and test papers separate from the
application directory. Store the receipts beside the new artifact, not over an
older build's evidence.


### Local CLI discovery qualification

Run `tests/test_local_executables.py` with the catalog and backend regressions
when changing local command lookup. Check a minimal desktop `PATH`, standard
installer locations, and identical executable selection for catalog checks and
verification on Windows, macOS and Linux. Windows npm launchers must preserve
argument boundaries without an arbitrary command shell; Unix fallback locations
must reject executables or directories writable by other users or groups.
Explicit user PATH entries remain the user's chosen installation locations.

Presence, authentication and service reachability are separate results. In
particular, an Ollama executable is not required for an already running Ollama
HTTP service. Real-account authentication/catalog probes require explicit user
scope and must not run generation tasks or record tokens. Normal package smoke
tests remain isolated from personal credentials.

FreeToken Desktop on Windows installs its managed engine at
`%LOCALAPPDATA%/FreeToken/venv/Scripts/ft.exe`; it need not be on PATH. Probe that
exact installer location rather than recursively searching application folders
or invoking PowerShell's unrelated `ft` alias. The native FreeToken engine is
not yet supported on macOS; its Jury entry remains visible but disabled there.

For authorized real service checks, retain the small test model's origin,
revision and SHA-256 and exercise Ollama's `/api/tags` and FreeToken's
`/v1/models` through the application's catalog checker. Record service absent,
empty catalog and populated catalog separately, plus missing/wrong-key refusal
when testing authentication. A catalog check is not an inference-quality test.
Stop only the temporary server processes owned by the test; leave personal
settings, credentials and existing services untouched.

### Replacing VM application copies and archives

Replace both the application and its archive at every known test-package
location. Updating a `.app` while leaving an older TAR beside it allows the old
application to be restored accidentally. Match each copied archive's SHA-256 to
the qualified artifact and inspect the embedded build revision before handing
it to the tester; the shared display version `2.0.0` does not distinguish builds.
Keep the deployment receipt with the new qualification evidence and preserve
settings, credentials, runs and test papers outside application replacement.
