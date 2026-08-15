---
title: Guests
sidebar_position: 5
---

# Working with guests

## Summary view

Every guest tab has a summary behind its console. The toolbar's **Console**
button flips between the two, and **View > Summary** (Ctrl+Alt+S) does the
same.

![Summary view](/img/screenshots/summary_view.png)

### Notes

The notes box shows the guest's description as saved in Proxmox. Proxima keeps its own metadata in the notes field inside a marked block. The block is never shown here: it is stripped on load and restored on save, so editing notes here cannot corrupt the metadata. See [Per guest settings](../configuration/per-guest-settings.md).

## Power action confirmations

Stop, Shutdown, and Reset ask before acting. Change any of them in **Preferences > Behaviour**. Acting on more than one guest at once always asks, regardless of what the confirmation settings are.

## Snapshots

The toolbar has Take and Revert. **VM > Manage Snapshots** opens the full list.

|                           |                                                                                                                                   |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Take Snapshot             | Name, optional description, and **Include guest RAM** for a running VM. It is unavailable for containers and for a stopped guest. |
| Revert to Latest Snapshot | Rolls back to the newest snapshot. The tooltip names it and gives its age.                                                        |
| Manage Snapshots          | The whole list with timestamps and descriptions.                                                                                  |

Proxmox reports a synthetic entry named `current` for the live state. Proxima filters it out, so the list is real snapshots only.

![Snapshots dialog](/img/screenshots/snapshots_dialog.png)

## Cloning

Right click a template and choose **Clone**. Names accept letters, digits, hyphens and dots, with no leading or trailing hyphen, which is what Proxmox accepts.

## Renaming and deleting

**Rename** edits the name in place in the tree. Only characters Proxmox allows in a guest name can be typed.

**Delete** destroys the guest and its disks. It is disabled, with the reason in its tooltip, when Proxmox would refuse: the guest is protected, a task holds a lock, or it is still running.

## Guest agent menu

**VM > Guest Agent** enables itself when `qemu-guest-agent` is running inside the guest.

|                    |                                                                                                                        |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| OS Information     | What the agent reports about the guest OS, as a table.                                                                 |
| Network Interfaces | Every interface with its MAC address and its addresses with prefixes.                                                  |
| Run Command        | Runs a command through the agent and shows its exit status and output. The agent executes it as root inside the guest. |
| Open SSH           | Looks up the guest's first IPv4 address through the agent and hands `ssh://` to your system's handler.                 |
| Open RDP           | Looks up the guest's first IPv4 address through the agent and hands your RDP client.                                   |

## Hardware and options

**Settings** on a guest, or **Edit settings** on its summary, opens a dialog with three tabs: Hardware, Options and Proxmox Manager.

Hardware and Options offer a limited subset of fields from the Proxmox web interface.

The Proxmox Manager tab is Proxima's own, and it is described in [Per guest settings](../configuration/per-guest-settings.md).
