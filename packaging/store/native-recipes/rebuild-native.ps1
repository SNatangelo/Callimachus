[CmdletBinding()]
param(
    [ValidateSet('All', 'Bootstrap', 'OpenSSL', 'CPython', 'Qt', 'PySide', 'NativeWheels', 'MuPDF', 'ONNXRuntime', 'PyInstaller', 'FinalInventory')]
    [string[]]$Stage = @('All'),
    [switch]$Build,
    [string]$NativeRoot,
    [string]$ToolsRoot,
    [string]$VsRoot,
    [string]$SdkRoot,
    [string]$BootstrapPython,
    [string]$Perl,
    [string]$RustRoot,
    [string]$NasmRoot,
    [string]$LLVMRoot
)

$ErrorActionPreference = 'Stop'

$frameworkRuntimeManifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'framework-runtime-manifest.json') -Raw | ConvertFrom-Json
$frameworkRuntimeExclusions = @($frameworkRuntimeManifest.dlls | ForEach-Object { [IO.Path]::GetFileName($_.appx_path) }) -join ';'

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
if (-not $NativeRoot) { $NativeRoot = Join-Path $projectRoot 'build/desktop/native-14.40' }
$NativeRoot = [IO.Path]::GetFullPath($NativeRoot)
if (-not $ToolsRoot) { $ToolsRoot = Join-Path $projectRoot 'build/store-tools' }
$ToolsRoot = [IO.Path]::GetFullPath($ToolsRoot)

if (-not $VsRoot -and (Test-Path -LiteralPath (Join-Path $ToolsRoot 'vs-build-tools/VC/Tools/MSVC/14.39.33519'))) {
    $VsRoot = Join-Path $ToolsRoot 'vs-build-tools'
}
if (-not $SdkRoot) {
    $SdkRoot = Join-Path $ToolsRoot 'windows-sdk-headers-libs/Windows Kits/10'
}
if (-not $Perl) {
    $Perl = Join-Path $ToolsRoot 'strawberry-perl/perl/bin/perl.exe'
}
if (-not $RustRoot) {
    $RustRoot = Join-Path $ToolsRoot 'rust-1.85/prefix'
}
if (-not $NasmRoot) {
    $NasmRoot = Join-Path $ToolsRoot 'nasm/nasm-3.02'
}
if (-not $LLVMRoot) {
    $LLVMRoot = Join-Path $ToolsRoot 'qt-build-tools/clang/libclang'
}
if (-not $BootstrapPython) {
    $BootstrapPython = Join-Path $ToolsRoot 'python-3.13.15/tools/python.exe'
}

$scriptRoot = $NativeRoot
$sourceRoot = Join-Path $NativeRoot 'python-sources'
$wheelsRoot = Join-Path $NativeRoot 'wheels'
$logRoot = Join-Path $NativeRoot 'logs/native-rebuild'
$pythonRoot = Join-Path $NativeRoot 'Python-3.13.15'
$bootstrapPythonExe = Join-Path $NativeRoot 'bootstrap/Scripts/python.exe'
$buildPythonExe = Join-Path $pythonRoot 'PCbuild/amd64/python.exe'
$finalPythonExe = Join-Path $NativeRoot 'final-venv/Scripts/python.exe'
$cmakeExe = Join-Path $NativeRoot 'bootstrap/Lib/site-packages/cmake/data/bin/cmake.exe'
$ninjaExe = Join-Path $NativeRoot 'bootstrap/Scripts/ninja.exe'
$delvewheelExe = Join-Path $NativeRoot 'bootstrap/Scripts/delvewheel.exe'
$frameworkStageScript = Join-Path $NativeRoot 'stage-framework-runtime.py'
$frameworkAppx = Join-Path $ToolsRoot 'vclibs-desktop-14.51/Microsoft.VCLibs.x64.14.00.Desktop.appx'
$script:createdRDrive = $false

function Get-ToolPath {
    param([string]$Name)
    $vs = $env:CALLIMACHUS_VS_ROOT
    if (-not $vs) { $vs = $VsRoot }
    if ($vs) {
        $standard = switch ($Name) {
            'cl' { Join-Path $vs 'VC/Tools/MSVC/14.39.33519/bin/Hostx64/x64/cl.exe' }
            'link' { Join-Path $vs 'VC/Tools/MSVC/14.39.33519/bin/Hostx64/x64/link.exe' }
            'nmake' { Join-Path $vs 'VC/Tools/MSVC/14.39.33519/bin/Hostx64/x64/nmake.exe' }
            'msbuild' { Join-Path $vs 'MSBuild/Current/Bin/MSBuild.exe' }
            'vctargets' { Join-Path $vs 'MSBuild/Microsoft/VC/v170' }
        }
        if (Test-Path -LiteralPath $standard) { return $standard }
    }
    $portable = switch ($Name) {
        'cl' { Join-Path $ToolsRoot 'msvc-14.39/VC/Tools/MSVC/14.39.33519/bin/Hostx64/x64/cl.exe' }
        'link' { Join-Path $ToolsRoot 'msvc-14.39/VC/Tools/MSVC/14.39.33519/bin/Hostx64/x64/link.exe' }
        'nmake' { Join-Path $ToolsRoot 'msvc-14.39/VC/Tools/MSVC/14.39.33519/bin/Hostx64/x64/nmake.exe' }
        'msbuild' { Join-Path $ToolsRoot 'msbuild-portable/packages/Microsoft.Build/Contents/MSBuild/Current/Bin/MSBuild.exe' }
        'vctargets' { Join-Path $ToolsRoot 'msbuild-portable/layout/Microsoft/VC/v170' }
    }
    return $portable
}

function Write-Plan {
    param([string]$Label, [string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory)
    $display = (@($Executable) + @($Arguments)) -join ' '
    Write-Output ("PLAN [{0}] (cwd: {1}) {2}" -f $Label, $WorkingDirectory, $display)
}

function Invoke-Logged {
    param([string]$Label, [string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory)
    Write-Plan -Label $Label -Executable $Executable -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    if (-not $Build) { return }
    if (-not (Test-Path -LiteralPath $Executable)) { throw "Required executable is missing for ${Label}: $Executable" }
    if (-not (Test-Path -LiteralPath $WorkingDirectory)) { throw "Required working directory is missing for ${Label}: $WorkingDirectory" }
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    $safeLabel = $Label -replace '[^A-Za-z0-9_.-]', '-'
    $logPath = Join-Path $logRoot ("{0}-{1:yyyyMMdd-HHmmss-fff}.log" -f $safeLabel, (Get-Date))
    Push-Location $WorkingDirectory
    try {
        & $Executable @Arguments 2>&1 | Tee-Object -FilePath $logPath
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) { $exitCode = 0 }
        if ($exitCode -ne 0) { throw "$Label failed with exit code $exitCode; see $logPath" }
    }
    finally { Pop-Location }
}

function Invoke-Python {
    param([string]$Label, [string]$Python, [string[]]$Arguments, [string]$WorkingDirectory)
    Invoke-Logged -Label $Label -Executable $Python -Arguments $Arguments -WorkingDirectory $WorkingDirectory
}

function Invoke-PowerShellScript {
    param([string]$Label, [string]$Script, [string[]]$Arguments)
    $shell = (Get-Command powershell.exe -ErrorAction Stop).Source
    Invoke-Logged -Label $Label -Executable $shell -Arguments (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Script) + $Arguments) -WorkingDirectory $projectRoot
}

function Assert-Inputs {
    param([string[]]$Paths)
    if (-not $Build) {
        foreach ($path in $Paths) { Write-Output "PLAN preflight input: $path" }
        return
    }
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Required source or generated input is missing: $path" }
    }
}

function Set-ToolEnvironment {
    if (-not $Build) { return }
    foreach ($path in @($projectRoot, $NativeRoot, $ToolsRoot)) {
        $resolved = [IO.Path]::GetFullPath($path)
        $prefix = $projectRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if (-not [string]::Equals($resolved, $projectRoot, [StringComparison]::OrdinalIgnoreCase) -and
            -not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Build paths must remain inside this checkout: $resolved"
        }
    }
    $env:CALLIMACHUS_PROJECT_ROOT = $projectRoot
    $env:CALLIMACHUS_TOOLS_ROOT = $ToolsRoot
    if ($VsRoot) { $env:CALLIMACHUS_VS_ROOT = [IO.Path]::GetFullPath($VsRoot) }
    else { Remove-Item Env:CALLIMACHUS_VS_ROOT -ErrorAction SilentlyContinue }
    if ($SdkRoot) { $env:CALLIMACHUS_SDK_ROOT = [IO.Path]::GetFullPath($SdkRoot) }
    else { Remove-Item Env:CALLIMACHUS_SDK_ROOT -ErrorAction SilentlyContinue }
    $env:CALLIMACHUS_NATIVE_ROOT = $NativeRoot
    $activation = Join-Path $scriptRoot 'activate.ps1'
    Assert-Inputs @($activation)
    . $activation -ProjectRoot $projectRoot -NativeRoot $NativeRoot -ToolsRoot $ToolsRoot -VsRoot $VsRoot -SdkRoot $SdkRoot
    $env:PATH = @(
        (Split-Path -Parent $Perl),
        (Join-Path $RustRoot 'bin'),
        $NasmRoot,
        (Join-Path $LLVMRoot 'bin'),
        $env:PATH
    ) -join ';'
    $env:RUSTUP_HOME = Join-Path $NativeRoot 'rustup-home'
    $env:CARGO_HOME = Join-Path $NativeRoot 'cargo-home'
    $env:CARGO_TARGET_DIR = Join-Path $NativeRoot 'cargo-target'
    $env:LLVM_INSTALL_DIR = $LLVMRoot
    $env:CALLIMACHUS_VCLIBS_RUNTIME = Join-Path $NativeRoot 'framework-runtime'

    $cl = Get-ToolPath 'cl'
    $link = Get-ToolPath 'link'
    $msbuild = Get-ToolPath 'msbuild'
    foreach ($tool in @(@{ Path = $cl; Version = '^19\.39\.33523(?:\.|$)' },
                         @{ Path = $link; Version = '^14\.39\.33523(?:\.|$)' })) {
        if (-not (Test-Path -LiteralPath $tool.Path)) { throw "Pinned MSVC tool is missing: $($tool.Path)" }
        $version = (Get-Item -LiteralPath $tool.Path).VersionInfo.FileVersion
        if ($version -notmatch $tool.Version) { throw "Pinned MSVC version required; expected $($tool.Version), found $version at $($tool.Path)" }
    }
    $nasmExe = Join-Path $NasmRoot 'nasm.exe'
    if (-not (Test-Path -LiteralPath $nasmExe)) { throw "Pinned NASM executable is missing: $nasmExe" }
    $nasmVersion = & $nasmExe -v 2>&1
    if ($LASTEXITCODE -ne 0 -or ($nasmVersion -join "`n") -notmatch '^NASM version 3\.02(?:\s|$)') {
        throw "NASM 3.02 is required; found '$($nasmVersion -join ' ')' at $nasmExe"
    }
    foreach ($path in @($cl, $link, $msbuild,
                        (Join-Path $NasmRoot 'nasm.exe'),
                        (Join-Path $SdkRoot 'Include/10.0.26100.0/ucrt/stdlib.h'),
                        (Join-Path $SdkRoot 'Lib/10.0.26100.0/ucrt/x64/ucrt.lib'),
                        (Join-Path $SdkRoot 'Lib/10.0.26100.0/um/x64/kernel32.lib'))) {
        if (-not (Test-Path -LiteralPath $path)) { throw "Required public build tool/input is missing: $path" }
    }
}

function Ensure-RDrive {
    if (-not $Build) {
        Write-Output "PLAN [workspace-drive] ensure R: maps to $NativeRoot before OpenCV, MuPDF, or ONNX Runtime."
        return
    }
    $entries = @(& subst.exe) | Where-Object { $_ -match '^R:' }
    if ($entries.Count -gt 1) { throw 'R: has more than one existing DOS-device mapping.' }
    if ($entries.Count -eq 1) {
        $actual = ($entries[0] -split '=>', 2)[1].Trim()
        if (-not [string]::Equals([IO.Path]::GetFullPath($actual), $NativeRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "R: already maps to another directory ($actual); refusing to overwrite it."
        }
        return
    }
    & subst.exe R: $NativeRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not map R: to $NativeRoot" }
    $script:createdRDrive = $true
}

function Stage-FrameworkRuntime {
    $manifestPath = Join-Path $scriptRoot 'framework-runtime-manifest.json'
    $toolManifestPath = Join-Path $projectRoot 'packaging/store/tool-inputs.json'
    Assert-Inputs @($frameworkStageScript, $manifestPath, $frameworkAppx, $toolManifestPath)
    if ($Build) {
        $expected = (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).appx_sha256
        $actual = (Get-FileHash -LiteralPath $frameworkAppx -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected.ToLowerInvariant()) { throw "VCLibs Appx SHA256 mismatch: expected $expected, got $actual" }
        $stageDirectory = Join-Path (Split-Path -Parent $NativeRoot) 'tools/vclibs-desktop-14.51'
        New-Item -ItemType Directory -Force -Path $stageDirectory | Out-Null
        $stagedAppx = Join-Path $stageDirectory 'Microsoft.VCLibs.x64.14.00.Desktop.appx'
        if (-not (Test-Path -LiteralPath $stagedAppx)) {
            Copy-Item -LiteralPath $frameworkAppx -Destination $stagedAppx
        }
        elseif ((Get-FileHash -LiteralPath $stagedAppx -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected.ToLowerInvariant()) {
            throw "Existing staged VCLibs Appx has the wrong SHA256: $stagedAppx"
        }
        $toolManifest = Get-Content -LiteralPath $toolManifestPath -Raw | ConvertFrom-Json
        foreach ($auditInput in $toolManifest.framework_audit_inputs) {
            $source = Join-Path (Join-Path $ToolsRoot 'vclibs-desktop-14.51') $auditInput.filename
            $target = Join-Path $stageDirectory $auditInput.filename
            if (-not (Test-Path -LiteralPath $source)) { throw "Required VCLibs audit input is missing: $source" }
            $inputHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($inputHash -ne $auditInput.sha256.ToLowerInvariant()) { throw "VCLibs audit input SHA256 mismatch: $source" }
            if (-not (Test-Path -LiteralPath $target)) { Copy-Item -LiteralPath $source -Destination $target }
            elseif ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $auditInput.sha256.ToLowerInvariant()) {
                throw "Existing staged VCLibs audit input has the wrong SHA256: $target"
            }
        }
        $appxInPlace = Join-Path $NativeRoot 'framework-runtime-manifest.json'
        if (-not (Test-Path -LiteralPath $appxInPlace)) { throw "Native recipe manifest is missing: $appxInPlace" }
        $python = if (Test-Path -LiteralPath $bootstrapPythonExe) { $bootstrapPythonExe } else { $BootstrapPython }
        Invoke-Python -Label 'stage-framework-runtime' -Python $python -Arguments @($frameworkStageScript) -WorkingDirectory $NativeRoot
    }
    else {
        Write-Output "PLAN [stage-framework-runtime] verify $frameworkAppx against $manifestPath, stage under build/desktop/tools, then copy pinned DLLs into the CPython/final environment."
    }
}

function Ensure-RustEnvironment {
    if (-not $Build) {
        Write-Output "PLAN [rust-toolchain] require rustc.exe and cargo.exe under $(Join-Path $RustRoot 'bin') (Rust 1.85.0)."
        return
    }
    $rustc = Join-Path $RustRoot 'bin/rustc.exe'
    $version = & $rustc --version
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^rustc 1\.85\.0 \(') { throw "Rust 1.85.0 is required; found '$version' at $rustc" }
}

function Ensure-BootstrapPython {
    if (-not $Build) {
        Write-Output ("PLAN [BootstrapPython] create {0} from the pinned public Python 3.13.15 host package; then install pinned native build tools" -f $bootstrapPythonExe)
        return
    }
    if (-not (Test-Path -LiteralPath $BootstrapPython)) {
        throw "Pinned public bootstrap Python is missing: $BootstrapPython. Run prepare_tools.py first."
    }
    $hostVersion = & $BootstrapPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
    if ($LASTEXITCODE -ne 0 -or $hostVersion.Trim() -ne '3.13.15') { throw "Bootstrap host must be CPython 3.13.15, found '$hostVersion'" }
    if (-not (Test-Path -LiteralPath $bootstrapPythonExe)) {
        Invoke-Logged -Label 'create-bootstrap-venv' -Executable $BootstrapPython -Arguments @('-m', 'venv', (Join-Path $NativeRoot 'bootstrap')) -WorkingDirectory $NativeRoot
    }
    $version = & $bootstrapPythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
    if ($LASTEXITCODE -ne 0 -or $version.Trim() -ne '3.13.15') { throw "Native build bootstrap Python must be 3.13.15, found '$version'" }
}

function Run-Stage {
    param([string]$Name)
    Write-Output "=== Stage: $Name ==="
    switch ($Name) {
        'Bootstrap' {
            Ensure-BootstrapPython
            $cmakeWheel = Join-Path $ToolsRoot '_downloads/cmake-4.4.3-py3-none-win_amd64.whl'
            $ninjaWheel = Join-Path $ToolsRoot '_downloads/ninja-1.13.2-py3-none-win_amd64.whl'
            Assert-Inputs @($cmakeWheel, $ninjaWheel, (Join-Path $NativeRoot 'bootstrap-requirements.txt'))
            Invoke-Python -Label 'install-hash-verified-cmake-ninja' -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-index', '--no-deps', $cmakeWheel, $ninjaWheel) -WorkingDirectory $NativeRoot
            Invoke-Python -Label 'bootstrap-pinned-native-build-tools' -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-deps', '-r', (Join-Path $NativeRoot 'bootstrap-requirements.txt')) -WorkingDirectory $NativeRoot
            foreach ($tool in @(
                @{ Name = 'cmake'; Path = $cmakeExe; Arguments = @('--version'); Pattern = '^cmake version 4\.4\.3(?:\s|$)' },
                @{ Name = 'ninja'; Path = $ninjaExe; Arguments = @('--version'); Pattern = '^1\.13\.2$' },
                @{ Name = 'maturin'; Path = (Join-Path $NativeRoot 'bootstrap/Scripts/maturin.exe'); Arguments = @('--version'); Pattern = '^maturin 1\.15\.0$' },
                @{ Name = 'swig'; Path = (Join-Path $NativeRoot 'bootstrap/Scripts/swig.exe'); Arguments = @('-version'); Pattern = 'SWIG Version 4\.4\.1' }
            )) {
                if (-not $Build) {
                    Write-Output "PLAN version gate: $($tool.Name) $($tool.Pattern) at $($tool.Path)"
                    continue
                }
                if (-not (Test-Path -LiteralPath $tool.Path)) { throw "Pinned $($tool.Name) executable is missing: $($tool.Path)" }
                $reported = & $tool.Path @($tool.Arguments) 2>&1
                if ($LASTEXITCODE -ne 0 -or ($reported -join "`n") -notmatch $tool.Pattern) { throw "Unexpected $($tool.Name) build tool version: $($reported -join ' ')" }
            }
        }
        'OpenSSL' {
            $opensslSource = Join-Path $NativeRoot 'openssl-4.0.2'
            $opensslBuild = Join-Path $NativeRoot 'openssl-build'
            $opensslInstall = Join-Path $NativeRoot 'openssl-install'
            $perlExe = $Perl
            $nmakeExe = Get-ToolPath 'nmake'
            Assert-Inputs @((Join-Path $opensslSource 'Configure'), $perlExe, $nmakeExe)
            if ($Build) { New-Item -ItemType Directory -Force -Path $opensslBuild | Out-Null }
            $prefixArgs = @("--prefix=$opensslInstall", '--libdir=.', "--openssldir=$(Join-Path $opensslInstall 'ssl')")
            Invoke-Logged -Label 'openssl-configure' -Executable $perlExe -Arguments (@((Join-Path $opensslSource 'Configure'), 'VC-WIN64A', 'no-shared', 'no-module', 'no-asm', 'no-tests') + $prefixArgs) -WorkingDirectory $opensslBuild
            Invoke-Logged -Label 'openssl-depend' -Executable $nmakeExe -Arguments @('depend') -WorkingDirectory $opensslBuild
            Invoke-Logged -Label 'openssl-build-libs' -Executable $nmakeExe -Arguments @('build_libs') -WorkingDirectory $opensslBuild
            Invoke-Logged -Label 'openssl-install-static' -Executable $nmakeExe -Arguments @('install_sw') -WorkingDirectory $opensslBuild
            Assert-Inputs @((Join-Path $opensslInstall 'include/openssl/ssl.h'), (Join-Path $opensslInstall 'libcrypto.lib'), (Join-Path $opensslInstall 'libssl.lib'))
        }
        'CPython' {
            Ensure-BootstrapPython
            $pcbuild = Join-Path $pythonRoot 'PCbuild'
            Assert-Inputs @((Join-Path $pcbuild 'get_externals.bat'), (Join-Path $pythonRoot 'PC/pyconfig.h'), (Join-Path $NativeRoot 'openssl-install/libcrypto.lib'), $frameworkStageScript, $frameworkAppx)
            Stage-FrameworkRuntime
            $previousExternals = $env:EXTERNALS_DIR
            if ($Build) { $env:EXTERNALS_DIR = Join-Path $pythonRoot 'externals' }
            try {
                Invoke-Logged -Label 'cpython-get-externals' -Executable (Join-Path $pcbuild 'get_externals.bat') -Arguments @('--python', $bootstrapPythonExe, '--no-openssl', '--no-tkinter') -WorkingDirectory $pcbuild
            }
            finally { if ($Build) { $env:EXTERNALS_DIR = $previousExternals } }
            $cpythonArgs = @('-Build', '-NativeRoot', $NativeRoot, '-ToolsRoot', $ToolsRoot, '-ProjectRoot', $projectRoot, '-SdkRoot', $SdkRoot, '-VCLibsRuntime', (Join-Path $NativeRoot 'framework-runtime'))
            if ($VsRoot) { $cpythonArgs += @('-VsRoot', $VsRoot) }
            Invoke-PowerShellScript -Label 'cpython-build' -Script (Join-Path $scriptRoot 'cpython-build.ps1') -Arguments $cpythonArgs
            if ($Build) {
                $include = Join-Path $pythonRoot 'Include'
                $libs = Join-Path $pythonRoot 'libs'
                New-Item -ItemType Directory -Force -Path $include, $libs | Out-Null
                Copy-Item -LiteralPath (Join-Path $pythonRoot 'PC/pyconfig.h') -Destination (Join-Path $include 'pyconfig.h') -Force
                foreach ($name in @('python313.lib', 'python3.lib')) {
                    $from = Join-Path $pcbuild "amd64/$name"
                    Assert-Inputs @($from)
                    Copy-Item -LiteralPath $from -Destination (Join-Path $libs $name) -Force
                }
            }
            if (-not $Build -or -not (Test-Path -LiteralPath $finalPythonExe)) {
                Invoke-Python -Label 'create-final-venv' -Python $buildPythonExe -Arguments @('-m', 'venv', (Join-Path $NativeRoot 'final-venv')) -WorkingDirectory $NativeRoot
            }
            Stage-FrameworkRuntime
            if ($Build) {
                $finalVersion = & $finalPythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
                if ($LASTEXITCODE -ne 0 -or $finalVersion.Trim() -ne '3.13.15') { throw "Final venv must use CPython 3.13.15, found '$finalVersion'" }
            }
        }
        'Qt' {
            $qtSource = Join-Path $NativeRoot 'qt-sources/qt-everywhere-src-6.11.2'
            $qtBuild = Join-Path $NativeRoot 'qt-build/physical-release'
            $qtInstall = Join-Path $NativeRoot 'qt-install'
            Assert-Inputs @((Join-Path $qtSource 'CMakeLists.txt'), $cmakeExe, $ninjaExe)
            $configureArgs = @('-S', $qtSource, '-B', $qtBuild, '-G', 'Ninja', "-DCMAKE_MAKE_PROGRAM=$ninjaExe", '-DCMAKE_BUILD_TYPE=Release', "-DCMAKE_INSTALL_PREFIX=$qtInstall", '-DBUILD_qtbase=ON', '-DBUILD_qtsvg=ON', '-DQT_BUILD_SUBMODULES=qtbase;qtsvg', '-DQT_BUILD_EXAMPLES=OFF', '-DQT_BUILD_TESTS=OFF', '-DCMAKE_C_FLAGS=/DWIN32 /D_WINDOWS', '-DCMAKE_CXX_FLAGS=/DWIN32 /D_WINDOWS', '-DCMAKE_C_FLAGS_RELEASE=/DNDEBUG -O2 -Ob3', '-DCMAKE_CXX_FLAGS_RELEASE=/DNDEBUG -O2 -Ob3', '-DCMAKE_EXE_LINKER_FLAGS=/machine:x64', '-DCMAKE_EXE_LINKER_FLAGS_RELEASE=/INCREMENTAL:NO')
            Invoke-Logged -Label 'qt-configure' -Executable $cmakeExe -Arguments $configureArgs -WorkingDirectory $projectRoot
            Invoke-Logged -Label 'qt-build-install' -Executable $cmakeExe -Arguments @('--build', $qtBuild, '--config', 'Release', '--target', 'install', '--parallel', '4') -WorkingDirectory $projectRoot
        }
        'PySide' {
            $pysideSource = Join-Path $NativeRoot 'qt-sources/pyside-setup-everywhere-src-6.11.2'
            $pysidePython = Join-Path $NativeRoot 'pyside-build/venv/Scripts/python.exe'
            $pysideSetup = Join-Path $pysideSource 'setup.py'
            Assert-Inputs @($pysideSetup, (Join-Path $NativeRoot 'qt-install/bin/qtpaths.exe'), $cmakeExe, $ninjaExe, (Join-Path $LLVMRoot 'bin/libclang.dll'))
            if (-not (Test-Path -LiteralPath $pysidePython)) {
                Invoke-Python -Label 'pyside-venv' -Python $buildPythonExe -Arguments @('-m', 'venv', (Join-Path $NativeRoot 'pyside-build/venv')) -WorkingDirectory $NativeRoot
            }
            Invoke-Python -Label 'pyside-build-tools' -Python $pysidePython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', 'setuptools==78.1.0', 'wheel==0.46.3', 'Cython==3.3.0', 'packaging==26.3') -WorkingDirectory $pysideSource
            if ($Build) { $env:LLVM_INSTALL_DIR = $LLVMRoot }
            $pysideBuildBase = Join-Path $NativeRoot 'pyside-build/build'
            $qtOptions = @("--qtpaths=$(Join-Path $NativeRoot 'qt-install/bin/qtpaths.exe')", "--cmake=$cmakeExe", '--make-spec=ninja', '--module-subset=Core,Gui,Widgets,Network,Svg,Test', '--ignore-git')
            $pysideArgs = @($pysideSetup, 'build', "--build-base=$pysideBuildBase", '--parallel=4') + $qtOptions + @('bdist_wheel', "--dist-dir=$wheelsRoot", '--skip-build')
            Invoke-Python -Label 'pyside-build-modules-and-wheels' -Python $pysidePython -Arguments $pysideArgs -WorkingDirectory $pysideSource
        }
        'NativeWheels' {
            Assert-Inputs @($bootstrapPythonExe, (Join-Path $NativeRoot 'prepare-openblas.py'))
            Ensure-RustEnvironment
            if ($Build) { New-Item -ItemType Directory -Force -Path $wheelsRoot | Out-Null }
            Invoke-Python -Label 'prepare-openblas-pkgconfig' -Python $bootstrapPythonExe -Arguments @((Join-Path $NativeRoot 'prepare-openblas.py')) -WorkingDirectory $NativeRoot
            if ($Build) { $env:PKG_CONFIG_PATH = Join-Path $NativeRoot 'openblas-pkgconfig' }

            $geosSource = Join-Path $NativeRoot 'geos-3.13.1'
            $geosBuild = Join-Path $NativeRoot 'geos-build'
            $geosInstall = Join-Path $NativeRoot 'geos-install'
            Assert-Inputs @((Join-Path $geosSource 'CMakeLists.txt'))
            Invoke-Logged -Label 'geos-configure' -Executable $cmakeExe -Arguments @('-S', $geosSource, '-B', $geosBuild, '-G', 'Ninja', "-DCMAKE_MAKE_PROGRAM=$ninjaExe", '-DCMAKE_BUILD_TYPE=Release', "-DCMAKE_INSTALL_PREFIX=$geosInstall", '-DBUILD_TESTING=OFF', '-DBUILD_SHARED_LIBS=ON') -WorkingDirectory $projectRoot
            Invoke-Logged -Label 'geos-build-install' -Executable $cmakeExe -Arguments @('--build', $geosBuild, '--config', 'Release', '--target', 'install', '--parallel', '4') -WorkingDirectory $projectRoot

            $yamlSource = Join-Path $NativeRoot 'libyaml-0.2.5'
            $yamlBuild = Join-Path $NativeRoot 'libyaml-build'
            $yamlInstall = Join-Path $NativeRoot 'libyaml-install'
            Assert-Inputs @((Join-Path $yamlSource 'CMakeLists.txt'))
            Invoke-Logged -Label 'libyaml-configure' -Executable $cmakeExe -Arguments @('-S', $yamlSource, '-B', $yamlBuild, '-G', 'Ninja', "-DCMAKE_MAKE_PROGRAM=$ninjaExe", '-DCMAKE_BUILD_TYPE=Release', "-DCMAKE_INSTALL_PREFIX=$yamlInstall", '-DBUILD_SHARED_LIBS=OFF', '-DYAML_BUILD_SHARED_LIBS=OFF') -WorkingDirectory $projectRoot
            Invoke-Logged -Label 'libyaml-build-install' -Executable $cmakeExe -Arguments @('--build', $yamlBuild, '--config', 'Release', '--target', 'install', '--parallel', '4') -WorkingDirectory $projectRoot

            $pillowSource = Join-Path $sourceRoot 'pillow-source/pillow-12.3.0'
            $pillowWinBuild = Join-Path $pillowSource 'winbuild'
            $pillowBuild = Join-Path $NativeRoot 'pillow-dependencies/build'
            $pillowDepends = Join-Path $NativeRoot 'pillow-dependencies/cache'
            Assert-Inputs @((Join-Path $pillowWinBuild 'build_prepare.py'))
            if ($Build -and (Test-Path -LiteralPath $pillowBuild)) {
                throw "Pillow's build_prepare.py removes its build directory before download; refusing to overwrite existing build artifacts: $pillowBuild"
            }
            Invoke-Python -Label 'pillow-prepare-dependencies' -Python $bootstrapPythonExe -Arguments @((Join-Path $pillowWinBuild 'build_prepare.py'), '-v', '--architecture', 'AMD64', '--build-dir', $pillowBuild, '--depends', $pillowDepends) -WorkingDirectory $pillowWinBuild
            $pillowDepsBatch = Join-Path $pillowBuild 'build_dep_all.cmd'
            if ($Build) {
                $cmdExe = (Get-Command cmd.exe -ErrorAction Stop).Source
                Invoke-Logged -Label 'pillow-build-dependencies' -Executable $cmdExe -Arguments @('/d', '/c', "`"$pillowDepsBatch`"") -WorkingDirectory $pillowWinBuild
                $env:PILLOW_BUILD = $pillowBuild
                $env:PATH = (Join-Path $pillowBuild 'bin') + ';' + $env:PATH
            }

            $sourceMap = @(
                @{ Name = 'brotli'; Dir = 'brotli-source/brotli-1.2.0' },
                @{ Name = 'cffi'; Dir = 'cffi-source/cffi-2.1.1' },
                @{ Name = 'charset-normalizer'; Dir = 'charset-normalizer-source/charset_normalizer-3.5.1' },
                @{ Name = 'cryptography'; Dir = 'cryptography-source/cryptography-50.0.1' },
                @{ Name = 'greenlet'; Dir = 'greenlet-source/greenlet-3.5.6' },
                @{ Name = 'numpy'; Dir = 'numpy-source/numpy-2.5.3' },
                @{ Name = 'pillow'; Dir = 'pillow-source/pillow-12.3.0' },
                @{ Name = 'pyclipper'; Dir = 'pyclipper-source/pyclipper-1.4.0' },
                @{ Name = 'pyyaml'; Dir = 'PyYAML-source/pyyaml-6.0.3' },
                @{ Name = 'shapely'; Dir = 'shapely-source/shapely-2.1.2' }
            )
            foreach ($entry in $sourceMap) {
                $packageSource = Join-Path $sourceRoot $entry.Dir
                Assert-Inputs @($packageSource)
                if ($entry.Name -eq 'numpy') {
                    $numpySettings = @()
                    foreach ($setting in @('-Dbuildtype=release', '-Db_ndebug=if-release', '-Db_vscrt=md', '-Dblas=scipy-openblas', '-Dlapack=scipy-openblas', '-Duse-ilp64=true', '-Dallow-noblas=false', '-Dcpu-baseline=x86-64-v2', '-Dcpu-dispatch=x86-64-v3')) { $numpySettings += @('--config-settings', "setup-args=$setting") }
                    $rawWheelRoot = Join-Path $NativeRoot 'numpy-wheel-raw'
                    if ($Build) { New-Item -ItemType Directory -Force -Path $rawWheelRoot | Out-Null }
                    Invoke-Python -Label 'wheel-numpy' -Python $bootstrapPythonExe -Arguments (@('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $rawWheelRoot) + $numpySettings + @($packageSource)) -WorkingDirectory $NativeRoot
                    if ($Build) {
                        $rawNumpyWheels = @(Get-ChildItem -LiteralPath $rawWheelRoot -Filter 'numpy-2.5.3-*.whl' -File)
                        if ($rawNumpyWheels.Count -ne 1) { throw "Expected one raw NumPy wheel in $rawWheelRoot, found $($rawNumpyWheels.Count)" }
                        $openblasLibDirOutput = @(& $bootstrapPythonExe -c 'import scipy_openblas64 as ob; print(ob.get_lib_dir())' 2>&1)
                        $openblasLibDirExitCode = $LASTEXITCODE
                        if ($openblasLibDirExitCode -ne 0) { throw "Could not locate scipy_openblas64 library directory: $($openblasLibDirOutput -join ' ')" }
                        if ($openblasLibDirOutput.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$openblasLibDirOutput[0])) { throw 'scipy_openblas64 returned no single library directory' }
                        $openblasLibDir = [string]$openblasLibDirOutput[0]
                        Invoke-Logged -Label 'repair-numpy-wheel-with-openblas' -Executable $delvewheelExe -Arguments @('repair', '--no-mangle-all', '--exclude', $frameworkRuntimeExclusions, '--add-path', $openblasLibDir, '--wheel-dir', $wheelsRoot, $rawNumpyWheels[0].FullName) -WorkingDirectory $NativeRoot
                    }
                    else {
                        Write-Plan -Label 'repair-numpy-wheel-with-openblas' -Executable $delvewheelExe -Arguments @('repair', '--no-mangle-all', '--exclude', $frameworkRuntimeExclusions, '--add-path', '<scipy_openblas64 library directory>', '--wheel-dir', $wheelsRoot, '<raw numpy wheel>') -WorkingDirectory $NativeRoot
                    }
                    Invoke-Python -Label 'install-source-built-numpy-for-opencv' -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--force-reinstall', '--no-deps', '--no-index', '--find-links', $wheelsRoot, 'numpy==2.5.3') -WorkingDirectory $NativeRoot
                }
                elseif ($entry.Name -eq 'charset-normalizer') {
                    if ($Build) { $env:CHARSET_NORMALIZER_USE_CYTHON = '1' }
                    Invoke-Python -Label 'wheel-charset-normalizer-cython' -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $wheelsRoot, $packageSource) -WorkingDirectory $NativeRoot
                }
                elseif ($entry.Name -eq 'cryptography') {
                    if ($Build) {
                        $env:OPENSSL_DIR = Join-Path $NativeRoot 'openssl-install'
                        $env:OPENSSL_STATIC = '1'
                        $env:CRYPTOGRAPHY_OPENSSL_NO_VENDOR = '1'
                    }
                    Invoke-Python -Label 'wheel-cryptography' -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $wheelsRoot, $packageSource) -WorkingDirectory $NativeRoot
                }
                elseif ($entry.Name -eq 'pyyaml') {
                    if ($Build) {
                        $env:PYYAML_FORCE_LIBYAML = '1'
                        $env:INCLUDE = (Join-Path $yamlInstall 'include') + ';' + $env:INCLUDE
                        $env:LIB = (Join-Path $yamlInstall 'lib') + ';' + $env:LIB
                    }
                    Invoke-Python -Label 'wheel-pyyaml-libyaml' -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $wheelsRoot, $packageSource) -WorkingDirectory $NativeRoot
                }
                elseif ($entry.Name -eq 'shapely') {
                    $rawWheelRoot = Join-Path $NativeRoot 'shapely-wheel-raw'
                    if ($Build) {
                        New-Item -ItemType Directory -Force -Path $rawWheelRoot | Out-Null
                        $env:GEOS_INCLUDE_PATH = Join-Path $geosInstall 'include'
                        $env:GEOS_LIBRARY_PATH = Join-Path $geosInstall 'lib'
                    }
                    Invoke-Python -Label 'wheel-shapely-geos' -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $rawWheelRoot, $packageSource) -WorkingDirectory $NativeRoot
                    if ($Build) {
                        $rawWheels = @(Get-ChildItem -LiteralPath $rawWheelRoot -Filter 'shapely-2.1.2-*.whl' -File)
                        if ($rawWheels.Count -ne 1) { throw "Expected one raw Shapely wheel in $rawWheelRoot, found $($rawWheels.Count)" }
                        Invoke-Logged -Label 'repair-shapely-wheel-with-geos' -Executable $delvewheelExe -Arguments @('repair', '--no-mangle-all', '--exclude', $frameworkRuntimeExclusions, '--add-path', (Join-Path $geosInstall 'bin'), '--wheel-dir', $wheelsRoot, $rawWheels[0].FullName) -WorkingDirectory $NativeRoot
                    }
                    else {
                        Write-Plan -Label 'repair-shapely-wheel-with-geos' -Executable $delvewheelExe -Arguments @('repair', '--no-mangle-all', '--exclude', $frameworkRuntimeExclusions, '--add-path', (Join-Path $geosInstall 'bin'), '--wheel-dir', $wheelsRoot, '<raw shapely wheel>') -WorkingDirectory $NativeRoot
                    }
                }
                else {
                    Invoke-Python -Label ("wheel-{0}" -f $entry.Name) -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $wheelsRoot, $packageSource) -WorkingDirectory $NativeRoot
                }
            }

            $opencvSource = Join-Path $sourceRoot 'opencv-python-source/opencv_python-5.0.0.93'
            Assert-Inputs @($opencvSource, (Join-Path $NativeRoot 'build-opencv-portable.py'))
            Ensure-RDrive
            Invoke-Python -Label 'wheel-opencv-portable' -Python $bootstrapPythonExe -Arguments @((Join-Path $NativeRoot 'build-opencv-portable.py'), $opencvSource) -WorkingDirectory $NativeRoot
        }
        'MuPDF' {
            $original = Join-Path $NativeRoot 'original-inputs/pymupdf/pymupdf'
            Assert-Inputs @($buildPythonExe, (Join-Path $NativeRoot 'build-mupdf-binding.py'), (Join-Path $NativeRoot 'build-pymupdf-wheel/build_pymupdf_wheel.py'), $original)
            Ensure-RDrive
            if ($Build) {
                $env:CALLIMACHUS_ORIGINAL_PYMUPDF = $original
                $env:CALLIMACHUS_NATIVE_ROOT = $NativeRoot
            }
            Invoke-Python -Label 'mupdf-binding' -Python $buildPythonExe -Arguments @((Join-Path $NativeRoot 'build-mupdf-binding.py')) -WorkingDirectory $NativeRoot
            Invoke-Python -Label 'mupdf-wheel' -Python $buildPythonExe -Arguments @((Join-Path $NativeRoot 'build-pymupdf-wheel/build_pymupdf_wheel.py')) -WorkingDirectory $NativeRoot
            if ($Build) {
                $mupdfWheels = @(Get-ChildItem -LiteralPath (Join-Path $NativeRoot 'build-pymupdf-wheel/wheels') -Filter 'pymupdf-1.28.0-cp313-abi3-win_amd64.whl' -File)
                if ($mupdfWheels.Count -ne 1) { throw "Expected one verified MuPDF wheel, found $($mupdfWheels.Count)" }
                $targetWheel = Join-Path $wheelsRoot $mupdfWheels[0].Name
                if (Test-Path -LiteralPath $targetWheel) { throw "Refusing to overwrite an existing native wheel: $targetWheel" }
                Copy-Item -LiteralPath $mupdfWheels[0].FullName -Destination $targetWheel
            }
        }
        'ONNXRuntime' {
            $revision = '2e2543fbe9fae542f921d47a72d21d5a4ef0b710'
            $onnxRoot = Join-Path $NativeRoot 'onnxruntime'
            $onnxBuild = Join-Path $NativeRoot 'onnxruntime-build'
            $onnxConfigure = Join-Path $onnxRoot 'cmake'
            $onnxBuildScript = Join-Path $onnxRoot 'tools/ci_build/build.py'
            $git = if ($Build) { (Get-Command git.exe -ErrorAction Stop).Source } else { 'git.exe' }
            Ensure-RDrive
            if ($Build -and -not [string]::Equals($NativeRoot, (Join-Path $projectRoot 'build/desktop/native-14.40'), [StringComparison]::OrdinalIgnoreCase)) {
                throw 'The checked-in ONNX helper layout currently requires NativeRoot to be the default build/desktop/native-14.40 directory.'
            }
            if (-not (Test-Path -LiteralPath $onnxRoot)) {
                Invoke-Logged -Label 'onnx-clone' -Executable $git -Arguments @('clone', 'https://github.com/microsoft/onnxruntime.git', $onnxRoot) -WorkingDirectory $NativeRoot
            }
            Invoke-Logged -Label 'onnx-fetch-pinned-revision' -Executable $git -Arguments @('-C', $onnxRoot, 'fetch', 'origin', $revision) -WorkingDirectory $NativeRoot
            Invoke-Logged -Label 'onnx-checkout-pinned-revision' -Executable $git -Arguments @('-C', $onnxRoot, 'checkout', '--detach', $revision) -WorkingDirectory $NativeRoot
            Invoke-Logged -Label 'onnx-submodules' -Executable $git -Arguments @('-C', $onnxRoot, 'submodule', 'update', '--init', '--recursive') -WorkingDirectory $NativeRoot
            Assert-Inputs @((Join-Path $onnxConfigure 'CMakeLists.txt'), $onnxBuildScript, $finalPythonExe,
                            (Join-Path $pythonRoot 'Include/Python.h'), (Join-Path $pythonRoot 'libs/python313.lib'),
                            (Join-Path $pythonRoot 'PCbuild/amd64/python313.dll'))
            if ($Build) { $env:CL = '/wd4189 /external:env:INCLUDE' }
            $cmakeDefinitions = @(
                "-DCMAKE_MAKE_PROGRAM=$ninjaExe",
                '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL',
                '-Donnxruntime_ENABLE_PYTHON=ON', '-Donnxruntime_BUILD_SHARED_LIB=ON', '-Donnxruntime_BUILD_UNIT_TESTS=ON',
                '-Donnxruntime_BUILD_JAVA=OFF', '-Donnxruntime_BUILD_MS_EXPERIMENTAL_OPS=OFF', '-Donnxruntime_BUILD_NODEJS=OFF',
                '-Donnxruntime_USE_CUDA=OFF', '-Donnxruntime_USE_CUDA_INTERFACE=OFF',
                '-Donnxruntime_USE_DNNL=OFF', '-Donnxruntime_USE_TENSORRT=OFF',
                '-Donnxruntime_USE_TENSORRT_INTERFACE=OFF', '-Donnxruntime_USE_TENSORRT_BUILTIN_PARSER=ON',
                '-Donnxruntime_ENABLE_TRAINING=OFF', '-Donnxruntime_ENABLE_TRAINING_APIS=OFF',
                '-Donnxruntime_ENABLE_TRAINING_E2E_TESTS=OFF', '-Donnxruntime_ENABLE_TRAINING_OPS=OFF',
                '-Donnxruntime_MINIMAL_BUILD=OFF', '-Donnxruntime_EXTENDED_MINIMAL_BUILD=OFF',
                '-Donnxruntime_MINIMAL_BUILD_CUSTOM_OPS=OFF', '-Donnxruntime_REDUCED_OPS_BUILD=OFF',
                '-Donnxruntime_DISABLE_CONTRIB_OPS=OFF', '-Donnxruntime_DISABLE_ML_OPS=OFF',
                '-Donnxruntime_DISABLE_SPARSE_TENSORS=OFF', '-Donnxruntime_ENABLE_CPUINFO=ON',
                '-Donnxruntime_USE_TELEMETRY=ON', '-Donnxruntime_USE_VCPKG=OFF',
                '-Donnxruntime_BUILD_CUDA_QUANT_PREPROCESS=OFF', '-Donnxruntime_CUDA_MINIMAL=OFF', '-Donnxruntime_DUMP_TENSOR=OFF',
                '-DCMAKE_C_FLAGS=/MP4', '-DCMAKE_CXX_FLAGS=/MP4',
                '-DCMAKE_C_FLAGS_RELEASE=/O2 /Ob2 /DNDEBUG', '-DCMAKE_CXX_FLAGS_RELEASE=/O2 /Ob2 /DNDEBUG',
                '-DCMAKE_EXE_LINKER_FLAGS=/machine:x64', '-DCMAKE_SHARED_LINKER_FLAGS=/machine:x64',
                '-DCMAKE_MODULE_LINKER_FLAGS=/machine:x64 delayimp.lib',
                '-DCMAKE_EXE_LINKER_FLAGS_RELEASE=/INCREMENTAL:NO',
                '-DCMAKE_SHARED_LINKER_FLAGS_RELEASE=/INCREMENTAL:NO',
                '-DCMAKE_MODULE_LINKER_FLAGS_RELEASE=/INCREMENTAL:NO',
                "-DPython_EXECUTABLE=R:/final-venv/Scripts/python.exe",
                '-DPython_INCLUDE_DIR=R:/Python-3.13.15/Include',
                '-DPython_LIBRARY=R:/Python-3.13.15/libs/python313.lib',
                '-DPython_RUNTIME_LIBRARY=R:/Python-3.13.15/PCbuild/amd64/python313.dll'
            )
            Invoke-Logged -Label 'onnx-fresh-cmake-configure' -Executable $cmakeExe -Arguments (@('-S', 'R:/onnxruntime/cmake', '-B', 'R:/onnxruntime-build/Release', '-G', 'Ninja') + $cmakeDefinitions) -WorkingDirectory $projectRoot
            Invoke-Logged -Label 'onnx-cmake-build' -Executable $cmakeExe -Arguments @('--build', 'R:/onnxruntime-build/Release', '--config', 'Release', '--parallel', '4') -WorkingDirectory $projectRoot
            Invoke-Python -Label 'onnx-runtime-python-wheel' -Python $bootstrapPythonExe -Arguments @($onnxBuildScript, '--build_dir', 'R:/onnxruntime-build', '--config', 'Release', '--build_wheel', '--skip_tests', '--skip_submodule_sync', '--skip_pip_install', '--cmake_generator', 'Ninja') -WorkingDirectory $NativeRoot
            if ($Build) {
                $onnxWheels = @(Get-ChildItem -LiteralPath $onnxRoot -Filter 'onnxruntime-1.29.0-*.whl' -File -Recurse) + @(Get-ChildItem -LiteralPath $onnxBuild -Filter 'onnxruntime-1.29.0-*.whl' -File -Recurse)
                $onnxWheels = @($onnxWheels | Sort-Object FullName -Unique)
                if ($onnxWheels.Count -ne 1) { throw "Expected one ONNX Runtime 1.29.0 wheel from the pinned source build, found $($onnxWheels.Count)" }
                $targetWheel = Join-Path $wheelsRoot $onnxWheels[0].Name
                if (Test-Path -LiteralPath $targetWheel) { throw "Refusing to overwrite an existing native wheel: $targetWheel" }
                Copy-Item -LiteralPath $onnxWheels[0].FullName -Destination $targetWheel
            }
        }
        'PyInstaller' {
            Assert-Inputs @($bootstrapPythonExe, (Join-Path $NativeRoot 'run_waf.py'), (Join-Path $sourceRoot 'pyinstaller-source/pyinstaller-6.22.3/pyproject.toml'))
            Invoke-Python -Label 'pyinstaller-bootloader' -Python $bootstrapPythonExe -Arguments @((Join-Path $NativeRoot 'run_waf.py'), 'all') -WorkingDirectory $NativeRoot
            Invoke-Python -Label 'wheel-pyinstaller' -Python $bootstrapPythonExe -Arguments @('-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', $wheelsRoot, (Join-Path $sourceRoot 'pyinstaller-source/pyinstaller-6.22.3')) -WorkingDirectory $NativeRoot
        }
        'FinalInventory' {
            Assert-Inputs @($finalPythonExe, (Join-Path $NativeRoot 'original-distributions.json'), (Join-Path $NativeRoot 'final-pure-requirements-no-omega.txt'), (Join-Path $NativeRoot 'stage-original-omegaconf.py'))
            Invoke-Python -Label 'install-pure-pins' -Python $finalPythonExe -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-deps', '-r', (Join-Path $NativeRoot 'final-pure-requirements-no-omega.txt')) -WorkingDirectory $NativeRoot
            $nativePins = @('brotli==1.2.0', 'cffi==2.1.1', 'charset-normalizer==3.5.1', 'cryptography==50.0.1', 'greenlet==3.5.6', 'numpy==2.5.3', 'onnxruntime==1.29.0', 'opencv-python==5.0.0.93', 'pillow==12.3.0', 'pyclipper==1.4.0', 'pyinstaller==6.22.3', 'pymupdf==1.28.0', 'PyYAML==6.0.3', 'shapely==2.1.2', 'PySide6==6.11.2', 'PySide6_Addons==6.11.2', 'PySide6_Essentials==6.11.2', 'shiboken6==6.11.2')
            foreach ($pin in $nativePins) {
                Invoke-Python -Label ("install-native-{0}" -f ($pin -replace '[^A-Za-z0-9.-]', '_')) -Python $finalPythonExe -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-deps', '--no-index', '--find-links', $wheelsRoot, $pin) -WorkingDirectory $NativeRoot
            }
            Invoke-Python -Label 'stage-original-omegaconf' -Python $finalPythonExe -Arguments @((Join-Path $NativeRoot 'stage-original-omegaconf.py'), '--original-site-packages', (Join-Path $NativeRoot 'original-inputs/omegaconf'), '--destination-site-packages', (Join-Path $NativeRoot 'final-venv/Lib/site-packages')) -WorkingDirectory $NativeRoot
            Stage-FrameworkRuntime
            $validation = @'
import importlib.metadata as md, json, sys
from packaging.utils import canonicalize_name
from pathlib import Path
root = Path(sys.argv[1])
expected = {canonicalize_name(k): str(v) for k, v in json.loads((root / "original-distributions.json").read_text(encoding="utf-8")).items()}
site = root / "final-venv" / "Lib" / "site-packages"
actual = {canonicalize_name(d.metadata["Name"]): d.version for d in md.distributions(path=[str(site)]) if d.metadata.get("Name")}
diff = {name: (version, actual.get(name)) for name, version in expected.items() if actual.get(name) != version}
if diff:
    raise SystemExit("47-pin inventory mismatch: " + repr(diff))
print("PINNED_DISTRIBUTIONS=" + str(len(expected)))
'@
            Invoke-Python -Label 'validate-47-version-inventory' -Python $finalPythonExe -Arguments @('-c', $validation, $NativeRoot) -WorkingDirectory $NativeRoot
        }
    }
}

$allStages = @('Bootstrap', 'OpenSSL', 'CPython', 'Qt', 'PySide', 'NativeWheels', 'MuPDF', 'ONNXRuntime', 'PyInstaller', 'FinalInventory')
$selectedStages = if ($Stage -contains 'All') { $allStages } else { $allStages | Where-Object { $Stage -contains $_ } }
if (-not $selectedStages) { throw 'No rebuild stages were selected.' }

if (-not $Build) {
    Write-Output 'PLAN ONLY: no compiler or native build command will run. Pass -Build to execute the selected stages.'
    Write-Output "Project checkout: $projectRoot"
    Write-Output "Native output root: $NativeRoot"
    Write-Output "Tools root: $ToolsRoot"
    Write-Output 'Historical inputs: CPython 3.13.15; OpenSSL 4.0.2; Qt/PySide 6.11.2; MSVC 19.39.33523; SDK 10.0.26100.0; VCLibs 14.40.33728.0.'
    Write-Output 'Tool acquisition remains an explicit prerequisite; this script does not install Visual Studio, Windows SDK, VCLibs, Perl, Rust, or LLVM.'
}
else {
    New-Item -ItemType Directory -Force -Path $NativeRoot, $logRoot, $wheelsRoot | Out-Null
    Set-ToolEnvironment
}

try {
    foreach ($selected in $selectedStages) { Run-Stage -Name $selected }
}
finally {
    if ($script:createdRDrive) {
        & subst.exe R: /D
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Could not remove the temporary R: mapping after the build stages.' }
    }
}

if ($Build) { Write-Output 'Native rebuild stages completed; this is not a claim that a fresh MSIX was built or Store-certified.' }
