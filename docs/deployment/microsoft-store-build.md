# Recorded Microsoft Store build — Callimachus 1.1.0

This records the Windows x64 build used for the first Microsoft Store submission.
The application is still version 1.1.0. It does not describe an ARM build, an
NSIS installer, or a new release. Run builds locally only when authorised.

The shipped application code is commit
`6b15e98f613fa4a38a9194f3e6bca3c42a761d11` on `codex/microsoft-store`.
Later documentation commits on that branch preserve the build procedure.
Build timestamps and tool outputs can change: this is not a promise of a
byte-identical MSIX rebuild.

## Recorded inputs and output

| Input | Selected value |
|---|---|
| Python | CPython 3.13.15, rebuilt for Windows x64 |
| Compiler | MSVC 19.39.33523.0; toolset directory 14.39.33519 |
| Linker | 14.39.33523.0 |
| SDK packages | Microsoft.Windows.SDK.CPP / CPP.x64 10.0.26100.9169 |
| SDK headers/libraries/tool version | 10.0.26100.0 |
| Microsoft framework | Microsoft.VCLibs.140.00.UWPDesktop, x64, package version 14.0.33728.0 |
| Framework DLL file version | 14.40.33728.0 |
| Package name | StefanoNatangelo.Callimachus |
| Publisher | CN=C9B83C55-1183-42F2-BE26-199806402671 |
| Publisher display name | Stefano Natangelo |
| MSIX version / architecture | 1.1.0.0 / x64 |
| Minimum Windows version | 10.0.17763.0 |

Selected framework Appx SHA256:
`077a3d1a5d0622bd3004dca85f5e192d6e98ec79b83d4aa06766759ea6c09c3d`.

Uploaded unsigned `Callimachus.msix`: 222,698,865 bytes, SHA256
`b74b7155fcefa0dd675357b2fd371de038d1eb9c8b557bb925534d1871325312`.

Compiler, framework package, and framework DLL versions use different version
spaces. A directory named `vclibs-desktop-14.51` in the historical build does
**not** identify the actual selected framework version; inspect its manifest,
DLL versions, and hash. Do not replace a framework with a different download
just because both files are named `Microsoft.VCLibs.x64.14.00.Desktop.appx`.

## Material preserved in Git and in the release

- [`packaging/store/`](../../packaging/store/): input recovery, native build recipes,
  dependency versions, source provenance, runtime hash inventory and build receipt.
- [`packaging/build_desktop.py`](../../packaging/build_desktop.py): application
  freeze and third-party notice assembly.
- [`packaging/build_msix.py`](../../packaging/build_msix.py): MSIX assembly,
  selected-framework validation, manifest and Store assets.
- [`packaging/smoke_desktop.py`](../../packaging/smoke_desktop.py): frozen runtime
  checks, including actual OCR.
- [`source-inputs.json`](../../packaging/store/source-inputs.json): public source
  archive URLs and hashes on the existing
  [v1.1.0 release](https://github.com/SNatangelo/Callimachus/releases/tag/v1.1.0).

The supplementary archive
`Callimachus-microsoft-store-build-sources.tar.gz` has SHA256
`f66a2225f636155367dbff8eb4ffbe876c73d1dd114cab3ac8d92cb1972a107d`.
It holds the additional native source archives. Eight large upstream archives
are separate assets on the same release; their hashes and URLs are listed in
`source-inputs.json`. The application source snapshot is also preserved there.

Acquire Microsoft's licensed SDK, compiler, framework and build tools separately.
They are not stored in Git. Neither signing keys nor provider credentials belong
in the build instructions, source archives, package or repository.

## Build layout and preparation

Use a clean Windows x64 checkout. Prepare inputs in the build directory before
running the native recipes; executing them directly from `packaging/store/`
is incorrect. `prepare_sources.py` recovers the pinned inputs and copies the
recipes into that directory. An installed Git and a build-host Python are normal
prerequisites; no saved Callimachus build directory is a prerequisite.

From the checkout root, inspect or recover the source inputs:

```powershell
python packaging/store/prepare_sources.py --list
if ($LASTEXITCODE -ne 0) { throw 'Source input plan failed' }
python packaging/store/prepare_sources.py
if ($LASTEXITCODE -ne 0) { throw 'Source recovery failed' }
```

Use Python 3.13 or newer for this preparation helper. It validates SHA256 before
extraction, refuses to overwrite extracted source trees, and does not compile.
The original OmegaConf and MuPDF wheels in its plan are public upstream inputs;
they are not wheels from the old local Callimachus build.

Recover tools with `python packaging/store/prepare_tools.py`, then follow
[the compiler installation and version gates](microsoft-store-tools.md).
That document lists every prepared tool path and the exact Microsoft component.
Neither tool preparation nor source preparation compiles Callimachus.

After preparation, inspect the native build plan:

```powershell
$NativeRoot = Join-Path (Get-Location) 'build/desktop/native-14.40'
$ToolsRoot = Join-Path (Get-Location) 'build/store-tools'
$VsRoot = Join-Path $ToolsRoot 'vs-build-tools'
$BootstrapPython = Join-Path $ToolsRoot 'python-3.13.15/tools/python.exe'
& "$NativeRoot/rebuild-native.ps1" -ToolsRoot $ToolsRoot -VsRoot $VsRoot `
  -BootstrapPython $BootstrapPython
if (-not $?) { throw 'Native build plan failed' }
```

To execute that same plan, add `-Build`. The stages run in dependency order:
Bootstrap, OpenSSL, CPython, Qt, PySide, NativeWheels, MuPDF, ONNXRuntime,
PyInstaller, FinalInventory. `-Stage` can select a stage for a deliberate retry;
its prerequisites must already be prepared. This creates the final Python
environment and 18 native wheels instead of copying an old binary baseline.
The final environment is checked against all 47 application package versions.
`bootstrap-distributions.json` and `bootstrap-requirements.txt` preserve the
separate build-tool environment, including the source-build backends.

After explicitly authorising a build, execute:

```powershell
& "$NativeRoot/rebuild-native.ps1" -ToolsRoot $ToolsRoot -VsRoot $VsRoot `
  -BootstrapPython $BootstrapPython -Build
if (-not $?) { throw 'Native source rebuild failed' }
```

```text
<checkout>/
  build/store-tools/                  # recovered tools; installed VS Build Tools
  build/desktop/
    native-14.40/                     # source preparation copies recipes here
      Python-3.13.15/
      bootstrap/                     # Python build utilities
      final-venv/                    # application build environment
      python-sources/
      wheels/
      framework-runtime/
    tools/
      vclibs-desktop-14.51/           # selected Appx, MSI and cabinet for audit
```

The new driver and activation/CPython helpers accept explicit tool paths.
Their default tool root is inside the checkout. The short `R:` mapping used by
the original dependency helpers must point to this checkout's native directory.

### Optional reuse of an existing native baseline

For an application-code rebuild using the same native versions, the workspace's
`output/microsoft-store-1.1.0/` also retains 18 built native/Qt wheels and 93
CPython runtime/import-library files. Their hashes are preserved in
`retained-native-wheels.json` and `retained-native-python.json` on this branch.
These local binary inputs are not committed to Git or uploaded as source code.
They are an optional accelerator, not a prerequisite for the source rebuild.

Restore `Python-3.13.15.tar.xz` from the pinned public source bundle into the
native directory, then overlay `native-python/Python-3.13.15/` from that retained
artifact directory. The original source supplies the standard library and
headers; the overlay supplies the tested Windows binaries. Validate its hashes
before use. Create `final-venv` with this Python, install the 18 retained wheels
with `--no-deps`, install `final-pure-requirements-no-omega.txt`, and restore the
original OmegaConf wheel as described below. Check all 47 installed versions
against `original-distributions.json`. Then stage the selected framework and
freeze/package as below. This avoids recompiling the native dependencies when
only application code changes. It still needs the selected framework and MakeAppx.

The full native-source rebuild procedure below recovers upstream inputs and
recreates the binary baseline. No retained local wheel or Python overlay is
required by that procedure. A fresh build was not performed for this update.

1. Restore the pinned archives from `source-inputs.json`. Check every downloaded
   SHA256 before extraction. Extract source archives under `native-14.40` using
   the directory names referenced by the recipes.
2. Copy `packaging/store/native-recipes/` into `build/desktop/native-14.40/`.
   Restore the licensed tools under `build/store-tools/` and the selected Appx
   under `build/desktop/tools/vclibs-desktop-14.51/`.
3. Activate the x64 compiler/SDK environment. Confirm compiler and linker
   versions against the table above before building.
4. Some native recipes use `R:` as a short path for Windows path-length limits.
   Map it to **this** checkout's `build/desktop/native-14.40`. Check an existing
   mapping before using it; never repoint another checkout's drive.
5. Build the native Python and dependency baseline below. Do not substitute
   current PyPI wheel versions: the original Store packaging failure came from
   newer native binaries requiring a newer CRT than the available framework.

## Native environment and package versions

`native-recipes/original-distributions.json` records all 47 installed package
versions. `source-manifest.json` pins CPython and ONNX Runtime's source revision;
`python-sources/manifest.json` pins native PyPI source URLs and hashes.

The order is: compiler/SDK and build utilities → OpenSSL and CPython → Qt and
PySide/Shiboken → native dependencies → final application environment → freeze.
The recorded helpers include:

| Stage | Recipe / record |
|---|---|
| Compiler environment | `activate.ps1` |
| CPython | `cpython-build.ps1 -Build`; OpenSSL must already be installed in `openssl-install` |
| ONNX Runtime | `resume-onnxruntime*.ps1`, `finish-onnxruntime.ps1`; use the pinned revision |
| OpenCV | `build-opencv-portable.py <OpenCV-source-directory>` |
| NumPy OpenBLAS configuration | `prepare-openblas.py` |
| MuPDF Python binding | `build-mupdf-binding.py`; original engine input is explicit |
| PyInstaller bootloader | `run_waf.py` |
| Original non-rebuilt versions | `prepare-final-requirements.py` |
| Original OmegaConf | `stage-original-omegaconf.py` with explicit source/destination site-packages |
| Selected framework DLLs | `stage-framework-runtime.py` |

The surviving Qt cache records Qt 6.11.2, Ninja, Release, MSVC 19.39.33523,
`qtbase;qtsvg`, release C/C++ flags `/DNDEBUG -O2 -Ob3` and release linker flag
`/INCREMENTAL:NO`. `OPENSSL_INCLUDE_DIR` was not found in that Qt configuration.
Later successful PySide logs record Core, Gui, Widgets, Network, Svg and QtTest,
successful generator/module builds, wheel creation and four 6.11.2 package
installations. They do not retain the successful `setup.py` command line.
Earlier logged command lines failed and must not be presented as the successful
recipe. OpenSSL 4.0.2 used `VC-WIN64A`; NumPy's build identified Meson 1.11.1
and Cython 3.3.0. Exact successful shell invocations for those native dependency
builds were not recovered.

The branch now includes a native rebuild driver that reconstructs these commands
from the source entry points and surviving build configuration. The retained
local binary baseline is optional. The new driver has not been executed through
a fresh complete native build; historical successful logs do not validate every
reconstructed command. A clean rebuild remains the practical end-to-end check.

Qt/PySide and native Python packages must retain the versions in the recorded
inventory. Preserve the upstream features used by the original packages.
Compiler/CRT substitution is not an excuse to remove PDF, OCR or codec support.

OmegaConf 2.0.6 has dependency metadata rejected by modern pip. The recorded
build kept the original package and METADATA; it did not upgrade the application
dependency. Restore its pinned wheel directly, without asking current pip to
resolve the invalid historical dependency specifier. The exact wheel URL and
SHA256 are in `packaging/store/original-wheel-inputs.json`. Its 11 package files
and METADATA were compared with the installed build environment: 12/12 match.
Installation bookkeeping files may differ from the wheel's original dist-info.

The MuPDF binding recipe requires the compatible engine/development files,
supplied through `CALLIMACHUS_ORIGINAL_PYMUPDF`. They come from the pinned upstream
`pymupdf-1.28.0-cp310-abi3-win_amd64.whl` in the same input manifest. The engine
DLL and two import libraries were compared with the actual build inputs: 3/3
match. `original-input-verification.json` records their component hashes. The
binding and `_extra` extension were rebuilt for CPython 3.13; the original wheel
is an engine input, not the final installed PyMuPDF wheel.

Restore the two wheels into separate input directories with `python -m zipfile
-e <wheel> <input-directory>`. Stage OmegaConf with
`stage-original-omegaconf.py --original-site-packages <OmegaConf-input-directory>
--destination-site-packages <final-venv/Lib/site-packages>`. Set
`CALLIMACHUS_ORIGINAL_PYMUPDF` to `<PyMuPDF-input-directory>/pymupdf`, run
`build-mupdf-binding.py`, then `build-pymupdf-wheel/build_pymupdf_wheel.py` with
`CALLIMACHUS_NATIVE_ROOT` pointing to the native build directory.

## Freeze and assemble the unsigned MSIX

Run from the checkout root, after preparing the final environment and framework:

```powershell
$NativeRoot = Join-Path (Get-Location) 'build/desktop/native-14.40'
$BuildPython = Join-Path $NativeRoot 'final-venv/Scripts/python.exe'
& $BuildPython "$NativeRoot/stage-framework-runtime.py"
if ($LASTEXITCODE -ne 0) { throw 'Framework staging failed' }
& $BuildPython "$NativeRoot/freeze-store-native.py" 2>&1 |
  Tee-Object "$NativeRoot/freeze-store-native-4.log"
if ($LASTEXITCODE -ne 0) { throw 'Application freeze failed' }
```

The freeze extends the external CRT set to the eight selected framework DLLs
and checks their actual bytes before excluding them from the payload.
Keep that driver: a generic application freeze alone is not the Store procedure.
The Store driver also selects `--windowed` instead of the common Windows
`--hide-console hide-early` options. Its executable must have the Windows GUI
subsystem so Store activation does not allocate a terminal window. Validate
stdout and stderr capture through a real Qt `QProcess`, then run the frozen
PDF/OCR smoke checks before assembling the MSIX.
The standard fresh build output is `build/desktop/dist/`.

```powershell
$FrameworkAppx = Join-Path (Get-Location) 'build/desktop/tools/vclibs-desktop-14.51/Microsoft.VCLibs.x64.14.00.Desktop.appx'
$MakeAppx = Join-Path (Get-Location) 'build/store-tools/windows-sdk-headers-libs/Windows Kits/10/bin/10.0.26100.0/x64/makeappx.exe'
& $BuildPython packaging/build_msix.py `
  --identity-name 'StefanoNatangelo.Callimachus' `
  --publisher 'CN=C9B83C55-1183-42F2-BE26-199806402671' `
  --publisher-display-name 'Stefano Natangelo' `
  --version '1.1.0.0' `
  --framework-appx $FrameworkAppx `
  --makeappx $MakeAppx
if ($LASTEXITCODE -ne 0) { throw 'MSIX assembly failed' }
Get-FileHash build/desktop/artifacts/Callimachus.msix -Algorithm SHA256
```

For an actual Store update, choose the appropriate higher four-part package
version; the command above records the package already built. Microsoft signs
Store submissions. Keep the upload MSIX unsigned; use a separate signed test
copy and a test certificate for local installation. Do not run the NSIS builder
or include its prerequisite installer in the MSIX.

## Validation and cleanup boundary

Before shipping another build, validate the selected framework dependency,
native PE imports and notices, real frozen OCR, GUI, writable Verify state,
and installed standard-user behavior. The preserved PE audit also checks the
selected Microsoft MSI's signature. Generate fresh evidence for the actual MSI:

```powershell
$Msi = Join-Path (Get-Location) 'build/desktop/tools/vclibs-desktop-14.51/VC_CRT.Desktop.Appx.140.msi'
$Signature = Get-AuthenticodeSignature -LiteralPath $Msi
if ($Signature.Status -ne 'Valid' -or
    $Signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
  throw 'Selected framework MSI signature is not valid Microsoft evidence'
}
$AuditDir = Join-Path $NativeRoot 'final-pe-audit'
$SignatureProof = @{
  path = (Resolve-Path -LiteralPath $Msi).Path
  sha256 = (Get-FileHash -LiteralPath $Msi -Algorithm SHA256).Hash.ToLowerInvariant()
  status = [string]$Signature.Status
  signer = $Signature.SignerCertificate.Subject
} | ConvertTo-Json
[IO.File]::WriteAllText("$AuditDir/msi-signature.json", $SignatureProof,
  [Text.UTF8Encoding]::new($false))
& $BuildPython "$AuditDir/audit_final_pe.py"
if ($LASTEXITCODE -ne 0) { throw 'Native PE audit failed' }
$AuditReport = Get-Content "$AuditDir/report.json" -Raw | ConvertFrom-Json
if (-not $AuditReport.audit_pass) { throw 'Native PE audit reported violations' }
& $BuildPython "$NativeRoot/validate-frozen-runtime.py"
if ($LASTEXITCODE -ne 0) { throw 'Frozen runtime validation failed' }
```

The historical audit uses `freeze-store-native-4.log` for its freeze receipt;
the command above captures that log. The recorded final build results were:

- PE audit: 96 x64 PE files, zero violations.
- Frozen runtime: 6 passed, including actual OCR with an empty PATH.
- Disclosure tests: 24 passed.
- GUI document navigation/theme: 3 passed, 127 deselected.
- Sandbox kit: 23 hashes checked; the publisher confirmed OCR/Verify working.
- MakeAppx and ZIP integrity passed. WACK was not run; it is an optional local
  check. Store certification is a separate process.

The first submitted package was frozen into an isolated
`submission-release-20261003` directory to protect existing artifacts. A fresh
checkout can use the standard output directory shown above. Do not overwrite
existing run artifacts or packages while rebuilding in a populated checkout.

The original public build-source archive accidentally assigned the MSIX hash
to `framework-runtime-manifest.json`'s `appx_sha256`. The recipe preserved on
this branch restores the actual framework hash verified from the selected input.
Use the branch's manifest after restoring the public archive.

Git, the public source assets and the pinned upstream tool downloads preserve
the inputs needed by the rebuild procedure. No saved local build file is a
required input. The submitted MSIX, exact selected framework Appx, its Microsoft
MSI and cabinet were copied
to the workspace's `output/microsoft-store-1.1.0/` before any cleanup. Their four
hashes were verified and are recorded in `retained-artifact-inputs.json`; binaries
are not committed to Git. The native wheels and Python runtime are preserved
there too, with per-file hashes; these copies are optional historical evidence.
The original OmegaConf/MuPDF wheel inputs have verified
public recovery URLs and hashes. Source compilation has not been repeated solely
to validate this documentation. The retained Python was restored against the
original source standard library and passed version 3.13.15 and imports of
`ssl`, `ctypes`, `sqlite3`, `bz2`, `lzma`, `venv`, and `ensurepip`.
A fresh complete rebuild is the final reproducibility check.

The source comparison recorded in
`packaging/store/qt-pyside-source-verification.json` checked 23,512 Qt root,
qtbase and qtsvg files and all 4,602 PySide files against their pinned archives.
No checked files were missing or modified. Generated extracted-only files and
unused Qt modules were excluded. No source patch is needed for that scope;
this comparison does not replace a fresh native build.
