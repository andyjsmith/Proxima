---
title: Display adapters
sidebar_position: 3
---

# Display adapters

Choosing a display adapter is very important, as it decides whether
you get SPICE or VNC. SPICE is what carries audio, USB redirection, clipboard sharing, folder sharing, smartcards, and auto-resizing.

Set it in Proxmox under **Hardware > Display**, or on the Hardware tab of
Proxima's [VM settings dialog](../configuration/per-guest-settings.md).

## Adapter choices

| Adapter           | Proxmox value                 | Console | Displays | 3D  | Best for                                                                   |
| ----------------- | ----------------------------- | ------- | -------- | --- | -------------------------------------------------------------------------- |
| SPICE (QXL)       | `qxl`, `qxl2`, `qxl3`, `qxl4` | SPICE   | Up to 4  | No  | Windows guests, or for multiple monitors.                                  |
| VirtIO-GPU        | `virtio`                      | SPICE   | 1        | No  | Modern Linux guests.                                                       |
| VirGL             | `virtio-gl`                   | SPICE   | 1        | Yes | Linux guests needing OpenGL. Requires a GPU and configuration on the host. |
| Standard VGA      | unset or `std`                | VNC     | 1        | No  | Servers or guests rarely interacted with by GUI.                           |
| VMware compatible | `vmware`                      | VNC     | 1        | No  | Guests imported from VMware with its driver already in place.              |
| Cirrus            | `cirrus`                      | VNC     | 1        | No  | Old guests, such as Windows XP.                                            |
| Serial terminal   | `serial0`                     | Serial  | none     | No  | Headless guests driven over a serial console.                              |

## Comparison of adapters

**SPICE (QXL)** is the best default for a desktop guest. Linux guests with `spice-vdagent` running can support up to four monitors automatically. Windows guests require changing to the `qxl2`, `qxl3` and `qxl4` options if you want multiple monitor outputs. It is a 2D adapter, so a guest doing OpenGL falls back to
software rendering.

**VirtIO-GPU** gives you SPICE with a paravirtualised adapter, which is usually faster than QXL for a single large display on a modern Linux guest. It supports only one display (custom QEMU configuration can handle more than one, but Proxmox does not easily expose this and Proxima does not currently support this). Windows needs the VirtIO display driver from the guest tools ISO.

**VirGL** is VirtIO-GPU with 3D. It needs a Linux guest with virgl support and host side OpenGL, which means the rendering happens on the Proxmox node's CPU or GPU rather than in the guest.

**Standard VGA** is the Proxmox default. You only get VNC, which means no audio, no USB redirection, no clipboard, no folder sharing and no guest resizing, and a resolution the guest picks.

**VMware compatible** and **Cirrus** exist for guests that already have those
drivers or are too old for anything else. Neither offers SPICE.

**Serial terminal** and **None** remove the graphical console. Proxima opens a
serial console for `serial0` and reports no console for `none`.

## SPICE features by adapter

|                              | QXL     | VirtIO-GPU | VirGL | std / VMware / Cirrus |
| ---------------------------- | ------- | ---------- | ----- | --------------------- |
| SPICE console                | Yes     | Yes        | Yes   | No                    |
| Multiple monitors            | Up to 4 | 1          | 1     | 1                     |
| Clipboard sharing            | Yes     | Yes        | Yes   | No                    |
| Guest resize with the window | Yes     | Yes        | Yes   | No                    |
| Audio and microphone         | Yes     | Yes        | Yes   | No                    |
| USB redirection              | Yes     | Yes        | Yes   | No                    |
| Folder sharing               | Yes     | Yes        | Yes   | No                    |
| Smartcard redirection        | Yes     | Yes        | Yes   | No                    |
| 3D acceleration              | No      | No         | Yes   | No                    |

The SPICE features need the guest configured with `spice-vdagent` on
Linux or the SPICE guest tools on Windows.

## Video memory

Video memory is the ceiling for how many pixels a guest can show at once. Proxmox defaults to 16 MiB, which is not enough for two 1080p
displays and is usually the reason a second monitor may stay blank.

| Displays   | Rough need | Set             |
| ---------- | ---------- | --------------- |
| One 1080p  | 8 MiB      | Default is fine |
| Two 1080p  | 17 MiB     | 32 MiB          |
| Four 1080p | 33 MiB     | 64 MiB          |
| Two 4K     | 66 MiB     | 128 MiB         |

Set it beside the adapter, in **Settings → Hardware → Display → Memory (MiB)**.

The same thing from the shell is `memory=` on the `vga` line:

```bash
qm set 101 --vga qxl,memory=64
```

Proxmox accepts 4 to 512 MiB.

## Full screen and multiple monitors

**Ctrl+Alt+Enter**, the toolbar button, or **View > Full Screen** puts the
console in full screen. A floating bar at the top edge is the way back
out. If captured, Ctrl+Alt hands the keyboard and mouse back to your desktop without leaving full screen.

![Fullscreen bar](/img/screenshots/fullscreen_bar.png)

### Guest requirements

The guest creates the display, not the client. There are two requirements:

- The SPICE guest agent is running. `spice-vdagent` on Linux, or SPICE guest
  tools on Windows.
- Its driver supports another head. Right now, that is only the QXL driver.

