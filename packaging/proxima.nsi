; Proxima installer.
;
; Offers the choice between installing for everyone (needs an administrator)
; and installing for the current user only (does not). MultiUser.nsh is what
; keeps the two apart: it points SHCTX, $INSTDIR and the uninstall registry
; entries at the right hive for whichever was chosen.
;
; Built from a finished Nuitka bundle, not from source:
;
;   makensis -DVERSION=1.2.3 -DDIST=../build/proxima.dist \
;            -DOUTFILE=proxima-1.2.3-windows-x86_64-setup.exe packaging/proxima.nsi

Unicode true

!define APPNAME "Proxima"
!define PUBLISHER "Proxima"
!define APPEXE "proxima.exe"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef DIST
  !define DIST "..\build\proxima.dist"
!endif
!ifndef OUTFILE
  !define OUTFILE "proxima-setup.exe"
!endif

; 64 MB of GTK compresses a long way, and an installer is downloaded far more
; often than it is built.
SetCompressor /SOLID lzma

!define MULTIUSER_EXECUTIONLEVEL Highest
!define MULTIUSER_MUI
!define MULTIUSER_INSTALLMODE_COMMANDLINE
!define MULTIUSER_INSTALLMODE_INSTDIR "${APPNAME}"
!define MULTIUSER_INSTALLMODE_DEFAULT_CURRENTUSER
!define MULTIUSER_INSTALLMODE_INSTDIR_REGISTRY_KEY \
  "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
!define MULTIUSER_INSTALLMODE_INSTDIR_REGISTRY_VALUENAME "InstallLocation"
!include MultiUser.nsh

!include MUI2.nsh
!include FileFunc.nsh

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "FileDescription" "${APPNAME} installer"
VIAddVersionKey "LegalCopyright" ""

!define MUI_ICON "proxima.ico"
!define MUI_UNICON "proxima.ico"
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MULTIUSER_PAGE_INSTALLMODE
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APPEXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APPNAME}"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Function .onInit
  !insertmacro MULTIUSER_INIT
FunctionEnd

Function un.onInit
  !insertmacro MULTIUSER_UNINIT
FunctionEnd

Section "Proxima" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  ; The whole bundle: the executable, the GTK libraries, the typelibs, the
  ; pixbuf loaders, the GStreamer plugins and the icon themes.
  File /r "${DIST}\*.*"

  CreateShortCut "$SMPROGRAMS\${APPNAME}.lnk" "$INSTDIR\${APPEXE}"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; SHCTX is HKLM for an all-users install and HKCU for a per-user one, so
  ; the entry lands in the list the user can actually see.
  !define UNINST_KEY \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
  WriteRegStr SHCTX "${UNINST_KEY}" "DisplayName" "${APPNAME}"
  WriteRegStr SHCTX "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr SHCTX "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APPEXE}"
  WriteRegStr SHCTX "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr SHCTX "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr SHCTX "${UNINST_KEY}" "UninstallString" \
    '"$INSTDIR\uninstall.exe" /$MultiUser.InstallMode'
  WriteRegStr SHCTX "${UNINST_KEY}" "QuietUninstallString" \
    '"$INSTDIR\uninstall.exe" /$MultiUser.InstallMode /S'
  WriteRegDWORD SHCTX "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD SHCTX "${UNINST_KEY}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD SHCTX "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  Delete "$INSTDIR\uninstall.exe"
  ; Everything else came out of the bundle, so the install directory goes as
  ; a whole. Settings live in %APPDATA%\Proxima and are deliberately kept.
  RMDir /r "$INSTDIR"
  DeleteRegKey SHCTX \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
SectionEnd
