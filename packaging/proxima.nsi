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
;
; Optionally carries the UsbDk driver, which SPICE USB redirection needs on
; Windows. Fetch it first with tools/fetch_usbdk.py and point -DUSBDK at the
; MSI; without that define the installer simply does not offer it, so a
; build with no network still produces a working installer.

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
!include LogicLib.nsh
!include Sections.nsh

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
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APPEXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APPNAME}"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

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

!ifdef USBDK
; SPICE can hand a USB device on this computer to the VM, but on Windows
; that needs a kernel driver -- spice-gtk lists devices without it and then
; fails at the moment one is claimed, which is a miserable way to find out.
;
; Unselected when UsbDk is already installed (see .onInit): reinstalling a
; driver somebody else's software may be using is not a favour.
;
; /passive rather than /qn: installing a driver needs administrator rights,
; and a per-user install of Proxima does not have them. Windows Installer
; raises its own elevation prompt, which it can only do if it is allowed to
; show UI at all. The uninstaller deliberately leaves the driver alone --
; virt-viewer and friends use the same one.
Section "USB redirection driver (UsbDk)" SecUsbDk
  DetailPrint "Installing the UsbDk USB redirection driver..."
  SetOutPath "$PLUGINSDIR"
  File "/oname=UsbDk.msi" "${USBDK}"
  ExecWait '"$SYSDIR\msiexec.exe" /i "$PLUGINSDIR\UsbDk.msi" /passive /norestart' $0
  ${If} $0 == 0
    DetailPrint "UsbDk installed."
  ${ElseIf} $0 == 3010
    DetailPrint "UsbDk installed; a restart will finish it."
  ${ElseIf} $0 == 1602
    DetailPrint "UsbDk installation cancelled. USB redirection will not work."
  ${Else}
    DetailPrint "UsbDk installation failed (code $0). USB redirection will not work."
  ${EndIf}
  Delete "$PLUGINSDIR\UsbDk.msi"
SectionEnd
!endif

; Below the sections deliberately: ${SecUsbDk} is only defined once the
; section it names has been seen, and NSIS resolves that where it is used.
Function .onInit
  !insertmacro MULTIUSER_INIT
!ifdef USBDK
  ; Already there: offer it, but do not tick it. The file is the one the
  ; driver's own installer writes, and it is what the app checks for too.
  ${If} ${FileExists} "$SYSDIR\drivers\UsbDk.sys"
    SectionSetText ${SecUsbDk} "USB redirection driver (UsbDk, already installed)"
    !insertmacro UnselectSection ${SecUsbDk}
  ${EndIf}
!endif
FunctionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} \
    "${APPNAME} itself, with the GTK, SPICE and VNC libraries it needs."
!ifdef USBDK
  !insertmacro MUI_DESCRIPTION_TEXT ${SecUsbDk} \
    "The driver that lets ${APPNAME} pass a USB device on this computer \
     through to a virtual machine over SPICE. Needs administrator rights. \
     Leave this unticked if you do not want USB redirection, or if you \
     already have UsbDk."
!endif
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  Delete "$INSTDIR\uninstall.exe"
  ; Everything else came out of the bundle, so the install directory goes as
  ; a whole. Settings live in %APPDATA%\Proxima and are deliberately kept.
  RMDir /r "$INSTDIR"
  DeleteRegKey SHCTX \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
SectionEnd
