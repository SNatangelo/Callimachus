# Microsoft Store build — Callimachus 2.0

The Store package uses the same application sources as the standalone releases,
but a separately qualified native runtime. Keep local installation signing
separate from the unsigned upload submitted to Microsoft.

## Runtime and source preparation

Follow [Desktop packaging](../maintainers/packaging/desktop-build.md), including
its Qt 6.12 Store-native build stages. The selected framework is
`Microsoft.VCLibs.140.00.UWPDesktop` x64, package `14.0.33728.0`, whose DLLs are
`14.40.33728.0`. The matching native toolset ceiling is MSVC 14.39. This is the
project's runtime compatibility choice; Microsoft Store does not generally
require rebuilding Qt. Official newer-toolset binaries require a sufficiently
new runtime and must not be substituted without requalification.

Acquire Microsoft's licensed compiler, SDK and framework separately. Start with
[tool preparation](microsoft-store-tools.md). For the Qt 6.12 source archives,
use `packaging/store/native-recipes/qt612-source-inputs.json` and
`build-qt612-msvc1439.py`. Their version/hash checks and required patch gates
must pass before compilation. Older `source-inputs.json` manifests and retained
recipes describe historical dependency inputs; they are not evidence of a
qualified Qt 6.12 application build. Keep source versions, notices and runtime
inventory aligned with the actual distributed package.

A clean rebuild must prepare CPython 3.13.15, its matching generated Windows
headers, the selected CRT/framework and required native Python dependencies,
then Qt, Qt PDF and PySide/Shiboken. Use the stage order, explicit tools and
short-path mappings in the maintainer procedure. Preserve hashes, patches,
compiler/SDK versions and build options. Never fabricate bindings or omit PDF,
OCR, codecs or plugins merely to pass packaging.

For application-only changes, reuse a matching verified native environment.
Verify platform, architecture, Python ABI, Qt/PDF/PySide versions, compiler,
framework and component hashes before reuse. Rebuild the application, run its
source and frozen tests and audit the final PE imports for every release.
Native caches contain no credentials, settings or user documents and are not
release source archives.

## Freeze and assemble

Use the qualified environment and `freeze-store-native.py` from the native
recipes to freeze the application. The Store executable must use the Windows
GUI subsystem, retain required runtime resources and exclude only the eight
DLLs supplied by the verified framework. Do not stage arbitrary host DLLs.
Validate captured output through Qt QProcess and run `packaging/smoke_desktop.py`,
including PDF, OCR, Library import, historical export and GUI initialization.

The package identity must match Partner Center:

| Field | Value |
|---|---|
| Name | `StefanoNatangelo.Callimachus` |
| Publisher | `CN=C9B83C55-1183-42F2-BE26-199806402671` |
| Publisher display name | `Stefano Natangelo` |
| Architecture | x64 |
| Application version | 2.0.0 |
| Minimum Windows | 10.0.17763.0 |

From a clean checkout with the tested onedir distribution:

```powershell
$FrameworkAppx = '<verified Microsoft.VCLibs.x64.14.00.Desktop.appx>'
$MakeAppx = '<selected Windows SDK>/x64/makeappx.exe'
python packaging/build_msix.py `
  --identity-name 'StefanoNatangelo.Callimachus' `
  --publisher 'CN=C9B83C55-1183-42F2-BE26-199806402671' `
  --publisher-display-name 'Stefano Natangelo' `
  --version '2.0.0.0' `
  --framework-appx $FrameworkAppx `
  --makeappx $MakeAppx
if ($LASTEXITCODE -ne 0) { throw 'MSIX assembly failed' }
Get-FileHash build/desktop/artifacts/Callimachus.msix -Algorithm SHA256
```

Choose the four-part package version appropriate to the Store submission;
updates must satisfy the Store's version ordering. MakePri must accompany
MakeAppx or be selected explicitly. The build verifies that the PRI contains
all required icon variants before replacing an artifact. Preserve the N1
artwork and inspect a fresh install in both Windows shell themes.

## Qualification and submission

Audit every final PE dependency against the selected framework, retain its
Microsoft signature/hash evidence, and test a fresh standard-user installation
with only the dependencies declared by the package. Exercise launch, GUI
navigation, synthetic credential storage, PDF/OCR, task handling, reporting,
redacted export and uninstall. Test the actual supported Windows baseline;
a run on a newer Windows version alone does not prove Windows 10 1809 support.

Publish corresponding application and dependency sources and notices. Keep
build receipts tied to the exact source revision and final artifact hash.
Never publish credentials, signing private keys, personal runs or memory dumps.
Follow the [submission contract and test gates](microsoft-store.md).
A successful build or local installation is not Store certification.
