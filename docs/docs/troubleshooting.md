---
title: Troubleshooting
sidebar_position: 6
---

# Troubleshooting

## Log file

Log files are available under **Help > Open Log Folder**.

Run with `--debug` to log to the console as well. See [Settings file](configuration/settings-file.md) for the log directory on each platform.

## proxima --diagnose

The first thing to run when a console will not open at all.

```bash
proxima --diagnose
```

It reports what the installed copy can do, which video decoders it has, and whether a SPICE session can be created.

## Connection problems

**The certificate changed.** The connection stops before any credential is
sent, and reports the fingerprint it expected against the one it got. After a
genuine renewal, delete that server's entry from `trusted_certs` in the
settings file and connect again. See
[Connecting](getting-started/connecting.md).

**Login fails with the right password.** Check the realm. A `root` account is
`pam`, a Proxmox VE user is `pve`. If the account has two factor
authentication, the code goes in the TFA field.

**A server shows as failed in the tree.** Right click it and choose
**Reconnect**.

## Console problems

| Symptom                                          | Cause                                                                                                                                                              |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The console opens on VNC when you expected SPICE | The VM's display adapter is not a SPICE one, `prefer_vnc` is set globally, or the guest's Protocol setting says VNC only. The protocol label's tooltip says which. |
| Opening SPICE disconnects somebody else          | QEMU serves one SPICE client at a time. Leave **Check for other SPICE clients** on so you are asked first.                                                         |
| The check never asks                             | The account lacks `VM.Monitor`, so the check is skipped.                                                                                                           |
| The console is blank but connected               | Try **View > Refresh Framebuffer**. On a guest that has just booted, the display may not be up yet.                                                                |
| Video is corrupt or tears                        | Tick **Software video decoding only** in Preferences and restart.                                                                                                  |
| The clipboard does nothing                       | `spice-vdagent` is not running in the guest, or clipboard sharing is switched off for that guest. The status bar indicator distinguishes the two.                  |
| The guest does not resize with the window        | Auto-resize is off, or `spice-vdagent` is missing. SPICE only.                                                                                                     |
| Keys go to the host, not the guest               | Alt+Tab, Ctrl+Alt+F2 and PrintScreen never reach a guest by being pressed. Use **VM > Send Key**.                                                                  |
| Typing goes nowhere after using a menu           | Move the pointer back over the console. The keyboard grab needs the pointer over it and the window active.                                                         |

## No second monitor

Work down this list.

1. The console is SPICE, not VNC.
2. The display adapter is QXL. Everything else carries one head. See
   [Display adapters](using/display-adapters.md).
3. The QXL display has enough video memory for both screens. 16 MiB, the
   Proxmox default, is not enough for two 1080p displays.
4. `spice-vdagent` or the Windows SPICE guest tools are running in the guest.
5. **View > Use All Monitors** is ticked.

The menu entry's tooltip names the reason when it is greyed out. See
[Full screen and multiple monitors](using/display-adapters.md#full-screen-and-multiple-monitors).

## USB device not offered

The status bar indicator's tooltip names the missing piece: a VNC console, no
`usbN: spice` line on the VM, the UsbDk driver missing on Windows, or nothing
redirected yet. See [USB redirection](sharing/usb-redirection.md).

## Dropped files never arrive

The console has to be SPICE, file drag and drop has to be on for that guest, and
`spice-vdagent` has to be running in the guest with a desktop session logged
in. The status bar indicator names whichever is missing. See
[File drag and drop](sharing/file-drag-and-drop.md).

## No sound

Proxmox adds no audio device by default. The Audio indicator says whether the
VM has one and whether it is routed over SPICE, and prints the line to add when
it does not. See [Audio and microphone](sharing/audio.md).

## Interface problems

**Buttons and menu icons are blank.** The icon theme is missing. An installed
copy carries it, so reinstall from the releases page. Running from source needs
the icon theme installed separately.

**Text is fuzzy or hinting does nothing.** The Appearance page names the live
font backend. Only FreeType applies the hinting settings, and switching
backends needs a restart.

**Everything is too roomy and fewer guests fit.** **Use the system theme** is
on. It hands the interface to your desktop's theme, which is roomier than
Proxima's compact styling.

## Reporting a bug

Include the version from **Help > About**, your platform, the log file for that
run, and `--diagnose` output if a console is involved. Issues go to
[GitHub](https://github.com/andyjsmith/proxima/issues).
