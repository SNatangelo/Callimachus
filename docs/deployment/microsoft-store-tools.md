# Recover the Microsoft Store build tools

`packaging/store/prepare_tools.py` downloads inputs from public upstream sources
and verifies them before use. Start with Python 3.13 or newer on Windows x64:

```powershell
py -3.13 packaging/store/prepare_tools.py --list
py -3.13 packaging/store/prepare_tools.py
```

`--list` performs no network or filesystem writes. The preparation command
stores downloads and extracted tool files below `build/store-tools`; it does
not depend on retained output binaries. NuGet packages and fixed public
archives use the SHA256 pins saved in the manifest; PyPI wheels use the digest
in the exact version's PyPI JSON record.

The helper prepares these paths for the native build driver:

| Input | Prepared path |
| --- | --- |
| Bootstrap Python 3.13.15 | `build/store-tools/python-3.13.15/tools/python.exe` |
| MSVC bootstrapper | `build/store-tools/vs-build-tools-bootstrap/vs_BuildTools.exe` |
| Windows SDK headers, libraries, and tools | `build/store-tools/windows-sdk-headers-libs/Windows Kits/10` |
| SDK NuGet package extracts | `build/store-tools/windows-sdk-packages/` |
| Pinned VCLibs framework Appx | `build/store-tools/vclibs-desktop-14.51/Microsoft.VCLibs.x64.14.00.Desktop.appx` |
| Framework audit MSI and cabinet | `build/store-tools/vclibs-desktop-14.51/` |
| Qt libclang 18.1.7 | `build/store-tools/qt-build-tools/clang/` |
| CMake 4.4.3 wheel | `build/store-tools/python-build-tools/cmake/cmake/data/bin/cmake.exe` |
| Ninja 1.13.2 wheel | `build/store-tools/python-build-tools/ninja/ninja/data/bin/ninja.exe` |
| Strawberry Perl portable distribution | `build/store-tools/strawberry-perl/` |
| Rust 1.85.0 usable toolchain prefix | `build/store-tools/rust-1.85/prefix/` |
| NASM 3.02 for Pillow's libjpeg-turbo | `build/store-tools/nasm/nasm-3.02/nasm.exe` |

Invoke tools by those explicit paths. The build should not resolve `cmake`,
`ninja`, `cl`, `link`, `perl`, or `cargo` from an arbitrary `PATH` entry.

## Install and verify the Microsoft compiler

The downloaded VS 17.9.7 bootstrapper is Microsoft-signed and has a fixed
manifest URL and SHA256. Install the Visual C++ tools workload plus the exact
14.39 component into the workspace tools directory:

```powershell
$tools = (Resolve-Path .).Path + '\build\store-tools'
$layout = Join-Path $tools 'vs-build-tools-layout'
$vsRoot = Join-Path $tools 'vs-build-tools'
$bootstrapper = Join-Path $tools 'vs-build-tools-bootstrap\vs_BuildTools.exe'
& $bootstrapper --layout $layout --wait `
  --add Microsoft.VisualStudio.Workload.VCTools `
  --add Microsoft.VisualStudio.Component.VC.14.39.17.9.x86.x64 `
  --includeRecommended --lang en-US
if ($LASTEXITCODE -ne 0) { throw "Visual Studio Build Tools setup failed: $LASTEXITCODE" }
$layoutBootstrapper = Join-Path $layout 'vs_BuildTools.exe'
& $layoutBootstrapper --noWeb --wait --quiet --norestart --installPath $vsRoot `
  --add Microsoft.VisualStudio.Workload.VCTools `
  --add Microsoft.VisualStudio.Component.VC.14.39.17.9.x86.x64 `
  --includeRecommended
if ($LASTEXITCODE -ne 0) { throw "Offline Visual Studio Build Tools setup failed: $LASTEXITCODE" }
```

The bootstrapper's channel can change independently of the executable. Treat
the install as usable only if the installed compiler payload passes the exact
version gate; the bootstrapper URL does not by itself prove which component
patch a future channel will serve. Confirm the following paths and file
versions before compiling:

```powershell
$cl = Join-Path $vsRoot 'VC\Tools\MSVC\14.39.33519\bin\Hostx64\x64\cl.exe'
$link = Join-Path $vsRoot 'VC\Tools\MSVC\14.39.33519\bin\Hostx64\x64\link.exe'
$msbuild = Join-Path $vsRoot 'MSBuild\Current\Bin\MSBuild.exe'
if (-not (Test-Path $cl) -or -not (Test-Path $link) -or -not (Test-Path $msbuild)) {
  throw 'Expected MSVC 14.39.33519, linker, or MSBuild path is missing'
}
if ((Get-Item $cl).VersionInfo.FileVersion -notlike '19.39.33523*') {
  throw "Unexpected compiler file version: $((Get-Item $cl).VersionInfo.FileVersion)"
}
if ((Get-Item $link).VersionInfo.FileVersion -notlike '14.39.33523*') {
  throw "Unexpected linker file version: $((Get-Item $link).VersionInfo.FileVersion)"
}
```

If the exact component is unavailable or the compiler/linker gate fails, stop:
do not substitute the generic current v143 toolset. The SDK inputs are
Microsoft NuGet packages pinned at `10.0.26100.9169`; the native include/lib
layout exposes Windows SDK files version `10.0.26100.0`.

## Candidate Qt 6.12 rebuild tools

Qt 6.12 source, matching PySide/Shiboken source, and separate Qt PDF 6.140
source pins are recorded in `packaging/store/native-recipes/qt612-source-inputs.json`.
The Qt PDF source listing publishes a SHA-1 sidecar; the pinned SHA-256 was
computed only after that sidecar matched. Verify source provenance and inspect
archives before use. The candidate native recipe requires the exact 14.39
compiler/linker and uses at most two build jobs. It remains unqualified until
native tests, final PE audit, package smoke and Sandbox checks pass.

The pinned SDK NuGet x64 payload can be merged with libraries directly under
`Windows Kits/10/ucrt/x64` and `Windows Kits/10/um/x64`; headers remain under
`Include/10.0.26100.0`. Do not assume a versioned `Lib` directory exists.
The Ninja 1.13.2 wheel can place its executable at
`python-build-tools/ninja/ninja-1.13.2.data/scripts/ninja.exe`; verify the
actual wheel layout instead of relying on the older table above. Supply the
actual executable paths explicitly to the candidate recipe.

Qt PDF's GN generation requests x86 and x64 toolchain metadata even when
`target_cpu` is x64. Preserve upstream activation for the unused x86 metadata
probe; enforce the selected SDK and MSVC toolset on the actual x64 environment.
Before compiling, verify the GN target and selected Ninja commands use
`environment.x64`, and retain the generated-environment proof. Generating
metadata does not qualify an x86 package or expand the supported architecture.

## Framework provenance

The exact VCLibs Appx is recovered from the Microsoft WinGet CLI release ZIP
and checked against the nested Appx hash in `tool-inputs.json`. The Microsoft
framework MSI and cabinet use fixed Microsoft download URLs and SHA256 pins.
The final PE audit still requires successful Authenticode evidence for the
selected MSI; a matching hash alone is not a signature result.

The Qt libclang 18.1.7 archive and standalone 7-Zip `7zr.exe` are both
downloaded from their upstream release locations and hash-checked before the
extractor is invoked. CMake and Ninja wheel files are selected by exact version
and filename from PyPI's versioned JSON response, then checked against that
response's SHA256 digest. Perl and Rust use the exact portable/archive
downloads and hashes recorded in the manifest. NASM 3.02 is recovered from its
official Windows x64 archive and added to the build PATH for Pillow's
libjpeg-turbo dependency. Its verified public download hash matches the retained upstream archive and
the independent public checksum reference recorded in `tool-inputs.json`.

The Rust helper assembles the `rustc`, `cargo`, and Windows standard-library
components described by their installer manifests into the prefix above.
It does not need Bash, WSL, a previous Rust installation, or a retained local
Callimachus build. Pass that prefix as `-RustRoot` when overriding driver paths.
