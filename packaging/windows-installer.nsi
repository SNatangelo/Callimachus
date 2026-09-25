; Copyright (C) 2026 Stefano Natangelo
; SPDX-License-Identifier: AGPL-3.0-only

Unicode true
RequestExecutionLevel user
Name "Callimachus"
InstallDir "$LOCALAPPDATA\Programs\Callimachus"
OutFile "${OUTPUT_FILE}"
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show

!include "MUI2.nsh"
!include "LogicLib.nsh"

; NSIS_VERSION includes the leading "v" (for example, "v3.10").
!if "${NSIS_VERSION}" != "v3.10"
  !error "The installer license and smoke contract target NSIS 3.10"
!endif

!ifndef DIST_DIR
  !error "DIST_DIR must point to build/desktop/dist"
!endif
!ifndef PACKAGING_DIR
  !error "PACKAGING_DIR must point to packaging"
!endif
!ifndef NSIS_LICENSE_FILE
  !error "NSIS_LICENSE_FILE must point to the NSIS license and attribution"
!endif
!ifndef VC_RUNTIME_MINIMUM_VERSION
  !error "VC_RUNTIME_MINIMUM_VERSION must be derived from the Windows build runtime"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE must name the generated setup executable"
!endif

!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Callimachus"
!define MUI_ABORTWARNING
!define MUI_ICON "${PACKAGING_DIR}\assets\Callimachus.ico"
!define MUI_UNICON "${PACKAGING_DIR}\assets\Callimachus.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\Callimachus\Callimachus.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start Callimachus"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Var PowerShellPath
Var SilentArgument
Var WindowStyleArgument
Var PreflightExitCode

Function .onInit
  ; Keep the mutex handle open for the lifetime of this setup process.
  System::Call 'kernel32::CreateMutex(i 0, i 0, t "Local\CallimachusSetup-7B99D628-D948-41E9-96C5-8EB5A06FB2AE") i .r1 ?e'
  Pop $R0
  ${If} $R0 == 183
    IfSilent setup_already_running_silent setup_already_running_interactive
setup_already_running_interactive:
    MessageBox MB_ICONEXCLAMATION|MB_OK "Another Callimachus installation is already in progress. Finish or close it before starting Setup again."
setup_already_running_silent:
    DetailPrint "Another Callimachus Setup instance is already running."
    SetErrorLevel 17
    Abort
  ${EndIf}

  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=install_vc_runtime.ps1 "${PACKAGING_DIR}\install_vc_runtime.ps1"

  ; NSIS is a 32-bit process on the hosted runner. Sysnative selects native
  ; 64-bit Windows PowerShell; System32 is the fallback for a 64-bit process.
  StrCpy $PowerShellPath "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  IfFileExists "$PowerShellPath" powershell_found 0
  StrCpy $PowerShellPath "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
  IfFileExists "$PowerShellPath" powershell_found powershell_missing

powershell_missing:
  IfSilent powershell_missing_silent powershell_missing_interactive
powershell_missing_interactive:
  MessageBox MB_ICONSTOP|MB_OK "Windows PowerShell is required to check the Microsoft Visual C++ runtime. Callimachus has not been installed."
powershell_missing_silent:
  DetailPrint "VC runtime preflight could not start: Windows PowerShell is unavailable."
  SetErrorLevel 3
  Abort

powershell_found:
  IfSilent silent_install interactive_install
silent_install:
  StrCpy $SilentArgument " -Silent"
  StrCpy $WindowStyleArgument "Hidden"
  Goto run_preflight
interactive_install:
  StrCpy $SilentArgument ""
  StrCpy $WindowStyleArgument "Normal"
run_preflight:
  ClearErrors
  ExecWait '"$PowerShellPath" -NoLogo -NoProfile -WindowStyle $WindowStyleArgument -ExecutionPolicy Bypass -File "$PLUGINSDIR\install_vc_runtime.ps1" -MinimumRuntimeVersion "${VC_RUNTIME_MINIMUM_VERSION}"$SilentArgument' $PreflightExitCode
  IfErrors powershell_launch_failed
  ${If} $PreflightExitCode != 0
    DetailPrint "Microsoft Visual C++ runtime preflight failed with exit code $PreflightExitCode."
    ${If} $PreflightExitCode == 15
      DetailPrint "The runtime is missing or older than required; silent setup does not download or install prerequisites."
    ${EndIf}
    SetErrorLevel $PreflightExitCode
    Abort
  ${EndIf}
  Return

powershell_launch_failed:
  IfSilent powershell_launch_failed_silent powershell_launch_failed_interactive
powershell_launch_failed_interactive:
  MessageBox MB_ICONSTOP|MB_OK "Callimachus could not run the Visual C++ runtime check. The application has not been installed."
powershell_launch_failed_silent:
  DetailPrint "VC runtime preflight could not start: Windows PowerShell failed to launch."
  SetErrorLevel 4
  Abort
FunctionEnd

Section "Callimachus" SEC_APP
  SectionIn RO
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File /r "${DIST_DIR}\*"

  ; Keep the NSIS stub's zlib/libpng license beside the installed notices.
  ; This does not change the generated notice index from the PyInstaller dist.
  SetOutPath "$INSTDIR\THIRD-PARTY-NOTICES"
  File /oname=NSIS-3.10.txt "${NSIS_LICENSE_FILE}"

  CreateDirectory "$SMPROGRAMS\Callimachus"
  CreateShortcut "$SMPROGRAMS\Callimachus\Callimachus.lnk" "$INSTDIR\Callimachus\Callimachus.exe"
  CreateShortcut "$DESKTOP\Callimachus.lnk" "$INSTDIR\Callimachus\Callimachus.exe"
  CreateShortcut "$SMPROGRAMS\Callimachus\Uninstall Callimachus.lnk" "$INSTDIR\Uninstall.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Callimachus" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "Callimachus"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Callimachus contributors"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$SMPROGRAMS\Callimachus\Callimachus.lnk"
  Delete "$DESKTOP\Callimachus.lnk"
  Delete "$SMPROGRAMS\Callimachus\Uninstall Callimachus.lnk"
  RMDir "$SMPROGRAMS\Callimachus"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "Software\Callimachus"
  RMDir /r "$INSTDIR\Callimachus"
  RMDir /r "$INSTDIR\THIRD-PARTY-NOTICES"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
