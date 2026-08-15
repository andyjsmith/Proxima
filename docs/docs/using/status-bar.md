---
title: Status bar
sidebar_position: 4
---

# Status bar

The left side of the status bar shows text for the last thing that happened. The right side contains a row of indicators, the procotol the console is using, and the server you are connected to.

![Status bar](/img/screenshots/status_bar.png)

## Reading an indicator

| Look          | Meaning                                                                                |
| ------------- | -------------------------------------------------------------------------------------- |
| Regular       | Available or working                                                                   |
| Strikethrough | Turned off                                                                             |
| Dimmed        | Not applicable or unavailable. A VNC console, a container, or a VM missing the device. |

## The indicators

### SPICE agent (clipboard)

Whether the clipboard is shared between host and guest, which is a SPICE
channel handled by `spice-vdagent` inside the guest. Clicking turns sharing off
and on, and the choice is stored on the server with the guest.

### Guest agent

Whether `qemu-guest-agent` answers inside the guest. A different agent from the one above. The SPICE agent handles the clipboard and display resizing, the QEMU guest agent answers the Proxmox API for IP addresses, command execution and clean shutdown.

### Audio

Whether guest audio output is enabled. It needs a SPICE console and a SPICE audio device on the VM. Proxmox adds none by default, so a missing sound device is likely the reason for audio being unavailable.

Clicking switches playback for this guest on this machine. See
[Audio and microphone](../sharing/audio.md).

### Microphone

SPICE's record channel, carrying this machine's microphone into the guest. Off by default.

It needs Audio to be on, because SPICE builds playback and recording together or not at all, and it needs an audio device on the VM whose codec has an input.

### Smartcard

This machine's card reader can be passed through to the guest. To function, a reader must be plugged in here, a CCID device must be present on the VM, and this indicator must be enabled. Proxmox adds no CCID device by default. See [Smartcard redirection](../sharing/smartcard.md).

### USB redirection

Passthrough a USB device to the guest. Clicking opens the chooser. See [USB redirection](../sharing/usb-redirection.md).

### File drag and drop

Whether a file dragged from this machine onto the console is sent to the guest.

It needs a SPICE console and `spice-vdagent` running in the guest, which is
what receives the file. See [File drag and drop](../sharing/file-drag-and-drop.md).
