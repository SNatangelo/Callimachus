# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# This preflight runs before Callimachus files are copied by the NSIS setup.
[CmdletBinding()]
param(
    [Version]$MinimumRuntimeVersion,
    [switch]$Silent
)

$ErrorActionPreference = "Stop"
$runtimeRegistryPath = "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
$runtimeDownloadUri = "https://aka.ms/vc14/vc_redist.x64.exe"
$temporaryInstaller = $null

# Exit codes consumed by windows-installer.nsi:
# 0 success; 10 user declined; 11 download failed; 12 invalid signature;
# 13 signer is not Microsoft; 14 UAC/install failed; 15 missing in silent mode;
# 16 unsupported OS or unexpected preflight failure.
function Get-VcRuntimeVersion {
    $versions = @()
    foreach ($view in @(
        [Microsoft.Win32.RegistryView]::Registry64,
        [Microsoft.Win32.RegistryView]::Registry32
    )) {
        $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::LocalMachine,
            $view
        )
        try {
            $runtimeKey = $baseKey.OpenSubKey($runtimeRegistryPath)
            if ($null -eq $runtimeKey) {
                continue
            }
            try {
                if ([int]$runtimeKey.GetValue("Installed", 0) -ne 1) {
                    continue
                }
                $versionText = [string]$runtimeKey.GetValue("Version", "")
                if ($versionText -match '(?i)v?(?<version>\d+\.\d+\.\d+\.\d+)') {
                    $versions += [Version]::Parse($Matches.version)
                }
            }
            finally {
                $runtimeKey.Dispose()
            }
        }
        finally {
            $baseKey.Dispose()
        }
    }
    if ($versions.Count -eq 0) {
        return $null
    }
    return ($versions | Sort-Object -Descending | Select-Object -First 1)
}

function Test-VcRuntimeInstalled {
    $installedVersion = Get-VcRuntimeVersion
    return $null -ne $installedVersion -and $installedVersion -ge $MinimumRuntimeVersion
}

function Show-PreflightFailure([string]$Message) {
    [Console]::Error.WriteLine("Callimachus VC runtime preflight: $Message")
    if (-not $Silent) {
        try {
            Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
            [void][System.Windows.Forms.MessageBox]::Show(
                $Message,
                "Callimachus setup",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Error
            )
        }
        catch {
            [Console]::Error.WriteLine("Could not display the setup error dialog: $($_.Exception.Message)")
        }
    }
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        Show-PreflightFailure "This installer requires 64-bit Windows."
        exit 16
    }
    if ($null -eq $MinimumRuntimeVersion) {
        Show-PreflightFailure "The installer has no verified minimum version for the Visual C++ runtime."
        exit 16
    }

    if (Test-VcRuntimeInstalled) {
        exit 0
    }

    if ($Silent) {
        Show-PreflightFailure "The Microsoft Visual C++ 2015-2022 Redistributable (x64) is missing or older than required version $MinimumRuntimeVersion. Silent setup will not prompt, download, or install it."
        exit 15
    }

    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $answer = [System.Windows.Forms.MessageBox]::Show(
        "Callimachus requires Microsoft Visual C++ 2015-2022 Redistributable (x64) version $MinimumRuntimeVersion or later. Download the official Microsoft installer and run it with administrator permission? Choosing No cancels Callimachus setup.",
        "Microsoft Visual C++ runtime required",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )
    if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) {
        exit 10
    }

    Write-Host "Downloading the Microsoft Visual C++ runtime. Please keep this window open."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $temporaryInstaller = Join-Path $env:TEMP ("Callimachus-vc_redist-x64-{0}.exe" -f [guid]::NewGuid().ToString("N"))
    try {
        Invoke-WebRequest -Uri $runtimeDownloadUri -OutFile $temporaryInstaller -UseBasicParsing -TimeoutSec 180
    }
    catch {
        Show-PreflightFailure "The official Microsoft runtime could not be downloaded. Check the network connection and try again. Details: $($_.Exception.Message)"
        exit 11
    }

    $signature = Get-AuthenticodeSignature -FilePath $temporaryInstaller
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        Show-PreflightFailure "The downloaded runtime does not have a valid Authenticode signature. It was not run."
        exit 12
    }

    $signer = $signature.SignerCertificate
    $simpleName = if ($null -eq $signer) {
        ""
    }
    else {
        $signer.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    }
    $isMicrosoftOrganization = $null -ne $signer -and $signer.Subject -match '(?i)(?:^|,\s*)O=Microsoft Corporation(?:,|$)'
    if ($simpleName -ne "Microsoft Corporation" -or -not $isMicrosoftOrganization) {
        Show-PreflightFailure "The downloaded runtime is not signed by Microsoft Corporation. It was not run."
        exit 13
    }

    Write-Host "Download complete and Microsoft signature verified. Installing the runtime..."
    try {
        $process = Start-Process -FilePath $temporaryInstaller -ArgumentList @("/install", "/passive", "/norestart") -Verb RunAs -Wait -PassThru -ErrorAction Stop
    }
    catch {
        Show-PreflightFailure "The Microsoft runtime installer was cancelled or failed to start. Callimachus setup has stopped. Details: $($_.Exception.Message)"
        exit 14
    }

    $runtimeInstalled = Test-VcRuntimeInstalled
    if (-not $runtimeInstalled -or $process.ExitCode -notin @(0, 3010)) {
        if ($process.ExitCode -eq 1618) {
            Show-PreflightFailure "Another Windows installation is already in progress. Wait for it to finish, then run Callimachus Setup again. Callimachus has not been installed."
        }
        else {
            Show-PreflightFailure "The Microsoft runtime installer failed with exit code $($process.ExitCode). Callimachus has not been installed."
        }
        exit 14
    }
    Write-Host "Microsoft Visual C++ runtime installed. Callimachus Setup will continue."
    exit 0
}
catch {
    Show-PreflightFailure "The Visual C++ runtime check failed. Callimachus has not been installed. Details: $($_.Exception.Message)"
    exit 16
}
finally {
    if ($null -ne $temporaryInstaller -and (Test-Path -LiteralPath $temporaryInstaller)) {
        Remove-Item -LiteralPath $temporaryInstaller -Force -ErrorAction SilentlyContinue
    }
}
