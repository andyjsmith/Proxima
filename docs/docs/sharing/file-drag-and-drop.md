---
title: File drag and drop
sidebar_position: 1
---

# File drag and drop

Drag a file from this machine onto a guest's console and it is sent to the
guest over SPICE.

Nothing is configured on the VM for this. Unlike folder sharing, USB
redirection or audio, there is no device to add in Proxmox: the transfer rides
on the same agent channel the clipboard uses.

## Requirements

|                              |                                                                                                      |
| ---------------------------- | ---------------------------------------------------------------------------------------------------- |
| A SPICE console              | VNC has no channel to carry a file.                                                                  |
| `spice-vdagent` in the guest | The agent is what receives the file and writes it. On Windows this is part of the SPICE guest tools. |
| The switch on                | On by default. See below.                                                                            |

## Destination folder

`spice-vdagent` decides, not Proxima. The default is the desktop of the user
running the agent, so a file dropped on a Linux guest appears on that user's
Desktop, and the same on Windows.

On Linux the destination is `spice-vdagent`'s `--file-xfer-save-dir` option,
which takes `xdg-desktop`, `xdg-download` or a path. Set it in the agent's
service configuration.

A guest with no graphical session logged in has no desktop to write to, and the
transfer fails there.

## Turning it off

**Settings > Proxmox Manager > File drag and drop**, under **On the server**. It is stored server-side, so the setting persists. See [Per guest settings](../configuration/per-guest-settings.md).

The **File drag and drop** indicator in the [status bar](../using/status-bar.md)
reports the state and toggles it for the current console.

With it off, the console will not transfer a file, however teh drop cursor may still show if you drag a file over the console.

## Troubleshooting

| Symptom                                  | Cause                                                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| The console refuses the drop             | The switch is off, or the console is VNC.                                                                      |
| The drop is accepted but no file appears | `spice-vdagent` is not running in the guest, or no desktop session is logged in for it to write to.            |
| It worked before and stopped             | The console reconnected and the agent has not come back yet. The clipboard indicator tells you the same thing. |
| Files land somewhere unexpected          | `--file-xfer-save-dir` in the guest's agent configuration.                                                     |
