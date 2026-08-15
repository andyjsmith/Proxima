---
title: Per guest settings
sidebar_position: 2
---

# Per guest settings

Open **Settings** from a guest's right click menu, or **Edit settings** on its
summary. The **Proxmox Manager** tab includes Proxima-specific settings for that guest.

- **On the server.** Stored in the guest's notes, so every machine running
  Proxima sees the same value.
- **This computer only.** Stored in this machine's settings file, because each
  one is about local hardware.

## On the server

| Setting            | Values            |                                                                                                                               |
| ------------------ | ----------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Clipboard          | Enabled, Disabled | Share the clipboard both ways. Needs a SPICE console and `spice-vdagent` in the guest.                                        |
| File drag and drop | Enabled, Disabled | Accept files dragged onto the console and send them to the guest. See [File drag and drop](../sharing/file-drag-and-drop.md). |
| Protocol           | Default, VNC only | Which console to open. Containers get Default, Serial only and VNC only instead.                                              |

Clipboard sharing is one switch rather than one per direction, because SPICE
exposes this only as a binary value: shared both ways or not at all.

**Protocol** can be changed for a guest whose SPICE display misbehaves, or a
container whose serial console does.

## This computer only

| Setting       | Default  |                                                                            |
| ------------- | -------- | -------------------------------------------------------------------------- |
| Audio         | Enabled  | Audio output. See [Audio](../sharing/audio.md).                            |
| Microphone    | Disabled | Pass this machine's microphone into the guest.                             |
| Smartcard     | Disabled | Pass this machine's card reader. See [Smartcard](../sharing/smartcard.md). |
| Shared folder | none     | Which folder to hand to this guest, and whether it is read only.           |

## Difference between status bar switches

The clipboard, file drag and drop, audio, microphone, and smartcard indicators in
the [status bar](../using/status-bar.md), and **Reopen Console with**, change the
console in front of you for as long as it is open. They do not persist the settings here.

## Notes block

Proxmox has no per guest storage for third party settings, but every guest has
a description. Proxima carves a delimited block out of it like the following:

```
=====BEGIN PROXIMA=====
{"folder": ["Production", "Customer A"], "settings": {"folder_sharing": "enabled"}}
=====END PROXIMA=====
```

The notes box on a guest's summary hides this block, but the Proxmox web interface will show this block. It is advised to not hand-edit it.

## Local storage

Under `guest_prefs` in the settings file, keyed by `<node>/<kind>/<vmid>`. See [Settings file](settings-file.md).
