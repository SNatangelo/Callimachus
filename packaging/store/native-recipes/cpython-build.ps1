[CmdletBinding()]
param(
    [switch]$Build,
    [string]$NativeRoot,
    [string]$ToolsRoot,
    [string]$ProjectRoot,
    [string]$VsRoot,
    [string]$SdkRoot,
    [string]$VCLibsRuntime
)

$ErrorActionPreference = 'Stop'

$nativeRoot = if ($NativeRoot) { [System.IO.Path]::GetFullPath($NativeRoot) } else { [System.IO.Path]::GetFullPath($PSScriptRoot) }
$worktreeRoot = if ($ProjectRoot) { [System.IO.Path]::GetFullPath($ProjectRoot) } elseif ($env:CALLIMACHUS_PROJECT_ROOT) {
    [System.IO.Path]::GetFullPath($env:CALLIMACHUS_PROJECT_ROOT)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $nativeRoot '..\..\..'))
}
$pythonRoot = Join-Path $nativeRoot 'Python-3.13.15'
$pcbuildRoot = Join-Path $pythonRoot 'PCbuild'
$solution = Join-Path $pcbuildRoot 'pcbuild.proj'
$pythonCoreProject = Join-Path $pcbuildRoot 'pythoncore.vcxproj'
$toolsRoot = if ($ToolsRoot) { [System.IO.Path]::GetFullPath($ToolsRoot) } elseif ($env:CALLIMACHUS_TOOLS_ROOT) {
    [System.IO.Path]::GetFullPath($env:CALLIMACHUS_TOOLS_ROOT)
} else {
    Join-Path $worktreeRoot 'build\store-tools'
}
if (-not $VsRoot) { $VsRoot = $env:CALLIMACHUS_VS_ROOT }
if (-not $SdkRoot) { $SdkRoot = $env:CALLIMACHUS_SDK_ROOT }
if (-not $VCLibsRuntime) { $VCLibsRuntime = $env:CALLIMACHUS_VCLIBS_RUNTIME }
if (-not $VsRoot -and (Test-Path -LiteralPath (Join-Path $toolsRoot 'vs-build-tools\VC\Tools\MSVC\14.39.33519'))) {
    $VsRoot = Join-Path $toolsRoot 'vs-build-tools'
}
$msvcRoot = if ($VsRoot -and (Test-Path -LiteralPath (Join-Path $VsRoot 'VC\Tools\MSVC\14.39.33519'))) {
    Join-Path $VsRoot 'VC\Tools\MSVC\14.39.33519'
} else {
    Join-Path $toolsRoot 'msvc-14.39\VC\Tools\MSVC\14.39.33519'
}
$sdkRoot = if ($SdkRoot) { [System.IO.Path]::GetFullPath($SdkRoot) } else {
    Join-Path $toolsRoot 'windows-sdk-headers-libs\Windows Kits\10'
}
$msbuild = if ($VsRoot -and (Test-Path -LiteralPath (Join-Path $VsRoot 'MSBuild\Current\Bin\MSBuild.exe'))) {
    Join-Path $VsRoot 'MSBuild\Current\Bin\MSBuild.exe'
} else {
    Join-Path $toolsRoot 'msbuild-portable\packages\Microsoft.Build\Contents\MSBuild\Current\Bin\MSBuild.exe'
}
$vctargetsPath = if ($VsRoot -and (Test-Path -LiteralPath (Join-Path $VsRoot 'MSBuild\Microsoft\VC\v170'))) {
    Join-Path $VsRoot 'MSBuild\Microsoft\VC\v170'
} else {
    Join-Path $toolsRoot 'msbuild-portable\layout\Microsoft\VC\v170'
}
$vcInstallDir = if ($VsRoot -and (Test-Path -LiteralPath (Join-Path $VsRoot 'VC'))) {
    Join-Path $VsRoot 'VC'
} else {
    Join-Path $toolsRoot 'msvc-14.39\VC'
}
if (-not $VCLibsRuntime) {
    $candidate = Join-Path $toolsRoot 'vclibs-desktop-14.51\dll-probe'
    if (Test-Path -LiteralPath $candidate) { $VCLibsRuntime = $candidate }
    else { $VCLibsRuntime = Join-Path (Split-Path -Parent $nativeRoot) 'tools\vclibs-desktop-14.51\dll-probe' }
}
$pythonForBuild = Join-Path $nativeRoot 'bootstrap\Scripts\python.exe'
$opensslRoot = Join-Path $nativeRoot 'openssl-install'
$intDir = Join-Path $pcbuildRoot 'obj\313amd64_Release'
$msvcBin = Join-Path $msvcRoot 'bin\Hostx64\x64'
$sdkVersion = '10.0.26100.0'
$sdkVersionWithSlash = "$sdkVersion\"
$msvcVersion = '14.39.33519'
$crtVersion = '14.40.33728'

foreach ($requiredPath in @($solution, $msbuild, $msvcBin, $sdkRoot, $vctargetsPath, $pythonForBuild, $vclibsRuntime)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required portable build path is missing: $requiredPath"
    }
}

$runtimeFiles = @(Get-ChildItem -LiteralPath $vclibsRuntime -File -Filter 'vcruntime*.dll')
foreach ($runtimeName in @('vcruntime140.dll', 'vcruntime140_1.dll')) {
    $runtimeFile = $runtimeFiles | Where-Object Name -EQ $runtimeName | Select-Object -First 1
    if ($null -eq $runtimeFile) {
        throw "Required VCLibs runtime DLL is missing: $runtimeName"
    }
    if ($runtimeFile.VersionInfo.FileVersion -ne "$crtVersion.0") {
        throw "Expected $runtimeName version $crtVersion.0, found $($runtimeFile.VersionInfo.FileVersion)"
    }
}

if (-not $Build) {
    Write-Output "Prepared CPython 3.13.15 x64 Release build recipe. Re-run with -Build after openssl-install is complete."
    exit 0
}

$opensslHeaders = Join-Path $opensslRoot 'include\openssl\ssl.h'
$opensslCryptoLib = Join-Path $opensslRoot 'libcrypto.lib'
$opensslSslLib = Join-Path $opensslRoot 'libssl.lib'
foreach ($requiredPath in @($opensslHeaders, $opensslCryptoLib, $opensslSslLib)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "OpenSSL install is not ready; required file is missing: $requiredPath"
    }
}

$buildPythonVersion = & $pythonForBuild -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0 -or $buildPythonVersion.Trim() -ne '3.13.15') {
    throw "PythonForBuild must be 3.13.15; found '$buildPythonVersion'"
}

$env:TEMP = $intDir
$env:TMP = $intDir
$env:VCToolsInstallDir = "$msvcRoot\"
$env:VCToolsVersion = $msvcVersion
$env:VCINSTALLDIR = "$vcInstallDir\"
$env:WindowsSdkDir = "$sdkRoot\"
$env:WindowsSDKVersion = $sdkVersionWithSlash
$env:UniversalCRTSdkDir = "$sdkRoot\"
$env:UCRTVersion = $sdkVersionWithSlash
$env:VCTargetsPath = "$vctargetsPath\"
$env:PATH = (@($msvcBin, (Join-Path $sdkRoot "bin\$sdkVersion\x64"), (Split-Path -Parent $pythonForBuild), $env:PATH) -join ';')
$env:INCLUDE = (@(
    (Join-Path $msvcRoot 'include'),
    (Join-Path $sdkRoot "Include\$sdkVersion\ucrt"),
    (Join-Path $sdkRoot "Include\$sdkVersion\shared"),
    (Join-Path $sdkRoot "Include\$sdkVersion\um"),
    (Join-Path $sdkRoot "Include\$sdkVersion\winrt")
) -join ';')
$env:LIB = (@(
    (Join-Path $msvcRoot 'lib\x64'),
    (Join-Path $sdkRoot "Lib\$sdkVersion\ucrt\x64"),
    (Join-Path $sdkRoot "Lib\$sdkVersion\um\x64")
) -join ';')

$logPath = Join-Path $nativeRoot ("cpython-build-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$arguments = @(
    $solution,
    '/nologo',
    '/m',
    '/verbosity:minimal',
    '/p:Configuration=Release',
    '/p:Platform=x64',
    '/p:PreferredToolArchitecture=x64',
    '/p:PlatformToolset=v143',
    '/p:VisualStudioVersion=17.0',
    "/p:VCToolsInstallDir=$env:VCToolsInstallDir",
    "/p:VCToolsVersion=$msvcVersion",
    "/p:VCToolsRedistVersion=$crtVersion",
    "/p:VCTargetsPath=$env:VCTargetsPath",
    "/p:WindowsSdkDir=$env:WindowsSdkDir",
    "/p:WindowsSDKVersion=$sdkVersionWithSlash",
    "/p:WindowsTargetPlatformVersion=$sdkVersion",
    "/p:UniversalCRTSdkDir=$env:UniversalCRTSdkDir",
    "/p:UCRTVersion=$sdkVersionWithSlash",
    '/p:UseEnv=true',
    '/p:DisableRegistryUse=true',
    '/p:TrackFileAccess=false',
    "/p:PythonForBuild=$pythonForBuild",
    '/p:KillPython=false',
    "/p:VCRuntimeDLL=$vclibsRuntime\vcruntime*.dll",
    "/p:opensslOutDir=$opensslRoot\",
    "/p:opensslIncludeDir=$opensslRoot\include\",
    '/p:IncludeExternals=true',
    '/p:IncludeSSL=true',
    '/p:IncludeCTypes=true',
    '/p:IncludeTkinter=false',
    '/p:IncludeTests=false',
    '/p:GitBranch=',
    '/p:GitVersion=',
    '/p:GitTag=',
    '/p:ForceGitTag=',
    '/p:ForceGitVersion=',
    '/p:ForceGitBranch='
)

@(
    "Source: $pythonRoot",
    "Source manifest: $(Join-Path $nativeRoot 'source-manifest.json')",
    "MSBuild: $msbuild",
    "MSVC: $msvcRoot",
    "Windows SDK: $sdkRoot ($sdkVersion)",
    "VCLibs runtime: $vclibsRuntime",
    "OpenSSL install: $opensslRoot",
    "Git ceiling: $nativeRoot (the CPython source archive has no Git metadata)",
    "Log: $logPath",
    "Arguments: $($arguments -join ' ')"
) | Set-Content -LiteralPath $logPath -Encoding utf8

$pythonExe = Join-Path $pcbuildRoot 'amd64\python.exe'
$previousGitCeiling = $env:GIT_CEILING_DIRECTORIES
$previousPythonPath = $env:PYTHONPATH
$previousDontWriteBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:GIT_CEILING_DIRECTORIES = $nativeRoot

Push-Location $pcbuildRoot
try {
    & $msbuild @arguments 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding utf8
    $buildExitCode = $LASTEXITCODE
    if ($buildExitCode -ne 0) {
        throw "CPython aggregate build failed with exit code $buildExitCode. See $logPath"
    }

    'Final metadata rebuild: direct pythoncore.vcxproj with empty GitVersion/GitTag/GitBranch globals' |
        Out-File -LiteralPath $logPath -Append -Encoding utf8
    $coreArguments = @($arguments)
    $coreArguments[0] = $pythonCoreProject
    $coreArguments += '/t:Rebuild'
    & $msbuild @coreArguments 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding utf8
    $coreBuildExitCode = $LASTEXITCODE
    if ($coreBuildExitCode -ne 0) {
        throw "Final direct pythoncore rebuild failed with exit code $coreBuildExitCode. See $logPath"
    }

    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw "MSBuild succeeded but expected output is missing: $pythonExe"
    }

    $runtimeOutput = Join-Path $pcbuildRoot 'amd64\vcruntime140.dll'
    if (-not (Test-Path -LiteralPath $runtimeOutput)) {
        throw "Built CPython output is missing vcruntime140.dll: $runtimeOutput"
    }
    $runtimeOutputVersion = (Get-Item -LiteralPath $runtimeOutput).VersionInfo.FileVersion
    if ($runtimeOutputVersion -ne "$crtVersion.0") {
        throw "Expected output vcruntime140.dll $crtVersion.0, found $runtimeOutputVersion"
    }

    $env:PYTHONPATH = Join-Path $pythonRoot 'Lib'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $pythonValidation = @'
import bz2, ctypes, lzma, os, sqlite3, ssl, sys
stale = ("codex/microsoft-store", "microsoft-store-dirty", "3485a39")
if any(marker in sys.version for marker in stale):
    raise RuntimeError(f"stale parent-worktree build metadata: {sys.version}")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
kernel32.GetModuleFileNameW.restype = ctypes.c_uint
handle = kernel32.GetModuleHandleW("vcruntime140.dll")
if not handle:
    raise RuntimeError("vcruntime140.dll was not loaded")
buffer = ctypes.create_unicode_buffer(32768)
kernel32.GetModuleFileNameW(handle, buffer, len(buffer))
runtime_path = os.path.normcase(os.path.abspath(buffer.value))
expected_runtime = os.path.normcase(os.path.abspath(os.path.join(os.path.dirname(sys.executable), "vcruntime140.dll")))
if runtime_path != expected_runtime:
    raise RuntimeError(f"unexpected VCRuntime path: {runtime_path}")
print("PYTHON_VERSION=" + sys.version)
print("IMPORTS=ssl,ctypes,sqlite3,bz2,lzma:PASS")
print("VCRUNTIME_PATH=" + runtime_path)
'@
    $pythonOutput = & $pythonExe -S -c $pythonValidation 2>&1
    $pythonExitCode = $LASTEXITCODE
    $pythonOutput | Out-File -LiteralPath $logPath -Append -Encoding utf8
    if ($pythonExitCode -ne 0) {
        throw "Fresh CPython metadata/runtime smoke failed with exit code $pythonExitCode. See $logPath"
    }
    if (-not ($pythonOutput -match '^IMPORTS=ssl,ctypes,sqlite3,bz2,lzma:PASS$')) {
        throw "Fresh CPython did not report the required import smoke as passed. See $logPath"
    }

    $dumpbin = Join-Path $msvcBin 'dumpbin.exe'
    foreach ($imageName in @('python.exe', 'python313.dll', '_ssl.pyd', '_ctypes.pyd', '_sqlite3.pyd', '_bz2.pyd', '_lzma.pyd')) {
        $imagePath = Join-Path $pcbuildRoot "amd64\$imageName"
        $linkerLine = & $dumpbin /headers $imagePath 2>$null | Select-String -Pattern 'linker version' | Select-Object -First 1
        if ($null -eq $linkerLine -or $linkerLine.Line -notmatch '14\.39 linker version') {
            throw "Expected MSVC 14.39 linker metadata in $imagePath; see $logPath"
        }
        "LINKER=$imageName $($linkerLine.Line.Trim())" | Out-File -LiteralPath $logPath -Append -Encoding utf8
    }
}
finally {
    Pop-Location
    if ($null -eq $previousGitCeiling) {
        Remove-Item Env:GIT_CEILING_DIRECTORIES -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_CEILING_DIRECTORIES = $previousGitCeiling
    }
    if ($null -eq $previousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $previousPythonPath
    }
    if ($null -eq $previousDontWriteBytecode) {
        Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONDONTWRITEBYTECODE = $previousDontWriteBytecode
    }
}

Write-Output "CPython aggregate build, final metadata rebuild, and smoke checks succeeded. Executable: $pythonExe"
Write-Output "Log: $logPath"
