param(
    [string]$ToolsRoot,
    [string]$NativeRoot,
    [string]$ProjectRoot,
    [string]$VsRoot,
    [string]$SdkRoot
)

$taskNativeRoot = if ($NativeRoot) { [IO.Path]::GetFullPath($NativeRoot) } else { [IO.Path]::GetFullPath($PSScriptRoot) }
$taskProjectRoot = if ($ProjectRoot) { [IO.Path]::GetFullPath($ProjectRoot) } elseif ($env:CALLIMACHUS_PROJECT_ROOT) {
    [IO.Path]::GetFullPath($env:CALLIMACHUS_PROJECT_ROOT)
} else {
    [IO.Path]::GetFullPath((Join-Path $taskNativeRoot '../../..'))
}
$taskToolsRoot = if ($ToolsRoot) { [IO.Path]::GetFullPath($ToolsRoot) } elseif ($env:CALLIMACHUS_TOOLS_ROOT) {
    [IO.Path]::GetFullPath($env:CALLIMACHUS_TOOLS_ROOT)
} else {
    Join-Path $taskProjectRoot 'build/store-tools'
}
if (-not $VsRoot) { $VsRoot = $env:CALLIMACHUS_VS_ROOT }
if (-not $SdkRoot) { $SdkRoot = $env:CALLIMACHUS_SDK_ROOT }
if (-not $VsRoot -and (Test-Path -LiteralPath (Join-Path $taskToolsRoot 'vs-build-tools/VC/Tools/MSVC/14.39.33519'))) {
    $VsRoot = Join-Path $taskToolsRoot 'vs-build-tools'
}
$taskVC = if ($VsRoot -and (Test-Path -LiteralPath (Join-Path $VsRoot 'VC/Tools/MSVC/14.39.33519'))) {
    Join-Path $VsRoot 'VC/Tools/MSVC/14.39.33519'
} else {
    Join-Path $taskToolsRoot 'msvc-14.39/VC/Tools/MSVC/14.39.33519'
}
$taskVCInstall = if ($VsRoot -and (Test-Path -LiteralPath (Join-Path $VsRoot 'VC'))) {
    Join-Path $VsRoot 'VC'
} else {
    Join-Path $taskToolsRoot 'msvc-14.39/VC'
}
$taskSDK = if ($SdkRoot) { [IO.Path]::GetFullPath($SdkRoot) } else {
    Join-Path $taskToolsRoot 'windows-sdk-headers-libs/Windows Kits/10'
}
$taskSdkVersion = '10.0.26100.0'
$env:PATH = (Join-Path $taskVC 'bin/Hostx64/x64') + ';' + (Join-Path $taskSDK "bin/$taskSdkVersion/x64") + ';' + (Join-Path $taskNativeRoot 'bootstrap/Scripts') + ';' + $env:PATH
$env:INCLUDE = @((Join-Path $taskVC 'include'), (Join-Path $taskSDK "Include/$taskSdkVersion/ucrt"), (Join-Path $taskSDK "Include/$taskSdkVersion/shared"), (Join-Path $taskSDK "Include/$taskSdkVersion/um"), (Join-Path $taskSDK "Include/$taskSdkVersion/winrt"), (Join-Path $taskSDK "Include/$taskSdkVersion/cppwinrt")) -join ';'
$env:LIB = @((Join-Path $taskVC 'lib/x64'), (Join-Path $taskSDK "Lib/$taskSdkVersion/ucrt/x64"), (Join-Path $taskSDK "Lib/$taskSdkVersion/um/x64")) -join ';'
$env:VCINSTALLDIR = $taskVCInstall + '\'
$env:VCTargetsPath = if ($VsRoot -and (Test-Path -LiteralPath (Join-Path $VsRoot 'MSBuild/Microsoft/VC/v170'))) {
    (Join-Path $VsRoot 'MSBuild/Microsoft/VC/v170') + '\'
} else {
    (Join-Path $taskToolsRoot 'msbuild-portable/layout/Microsoft/VC/v170') + '\'
}
$env:VCToolsInstallDir = $taskVC + '\'
$env:VCToolsVersion = '14.39.33519'
$env:WindowsSdkDir = $taskSDK + '\'
$env:WindowsSDKVersion = $taskSdkVersion + '\'
$env:UniversalCRTSdkDir = $taskSDK + '\'
$env:UCRTVersion = $taskSdkVersion
$env:VSCMD_ARG_TGT_ARCH = 'x64'
$env:VSCMD_ARG_HOST_ARCH = 'x64'
$env:Platform = 'x64'
$env:DISTUTILS_USE_SDK = '1'
$env:MSSdk = '1'
$env:CC = 'cl'
$env:CXX = 'cl'
$env:TEMP = Join-Path $taskNativeRoot 'tmp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PIP_CACHE_DIR = Join-Path $taskNativeRoot 'pip-cache'
$env:CARGO_HOME = Join-Path $taskNativeRoot 'cargo-home'
$env:CARGO_TARGET_DIR = Join-Path $taskNativeRoot 'cargo-target'
