---
title: Inventory tree
sidebar_position: 1
---

# Inventory tree

Every connected server is a root row. Below it, guests are grouped one of three
ways. The button beside the search box cycles between them, as well as
**Preferences > Behaviour > Group guests by**.

| View   |                                                                                                               |
| ------ | ------------------------------------------------------------------------------------------------------------- |
| Node   | Server, node, guest. How Proxmox itself organises things.                                                     |
| Folder | Your own folders, stored in each guest's notes, so they span the cluster and follow a guest when it migrates. |
| Tag    | The tags Proxmox keeps.                                                                                       |

Tag view repeats guests deliberately. Tags are not a hierarchy, so a guest
tagged `prod` and `web` appears under both. Tags differing
only in case are one group, and guests with no tags collect under **Untagged** at the end.

Templates sort together at the bottom of each group. Turn that off with
**Preferences > Behaviour > Group templates at the bottom**.

## Search

The search box filters every connected server at once. A guest matches by name, VMID, node, status, tag, type or
folder path, so `prod stopped` and `101 pve2` both work. Typing a tag name
filters to that tag without switching views.

Node rows survive a search only when they either match the text themselves or
still have a guest under them.

## Names and status

**Preferences > Behaviour > Names** switches the tree between `webserver (101)`
and `101 (webserver)`. Console tab titles have their own setting as well.

## Folders

Folders are Proxima's own concept. Proxmox has none, so the path lives in each
guest's notes inside a delimited block. Folders are therefore cluster wide,
independent of the node a guest runs on, and the same for anyone else running
Proxima against that cluster. See
[Per guest settings](../configuration/per-guest-settings.md) for the format.

Two ways to organize a guest into folders:

- **Existing folders:** Drag a guest into an existing folder.
- **New folders:** Right click a guest > Move to New Subfolder

Due to the way Proxima stores folder information, empty folders cannot exist, which is why each VM has the option to move itself into a new subfolder.

## Right click menus

Delete is disabled with the reason in its tooltip when Proxmox would refuse it:
the guest is protected, a task holds a lock on it, or it is still running.

**More than one guest**, selected with Ctrl or Shift, gives a bulk menu. Each
power action shows how many of the selection it applies to. Acting on more than one guest always asks first, whatever
the confirmation settings say.

## Polling

The inventory is polled at two speeds and the window picks between them by
itself.

|               |     |                                                                        |
| ------------- | --- | ---------------------------------------------------------------------- |
| At rest       | 6s  | Nothing outstanding. Watching in case somebody else changes something. |
| While waiting | 2s  | A change performed in Proxima has not been reported yet.               |

Waiting is a state, not a countdown. A power action, a rename, a console being
restored at startup or a server still connecting each hold the faster cadence. **Keep waiting for** (15s) covers only
actions that leave nothing else to watch. A guest merely carrying a lock does
not count, because a backup can hold one for an hour.

All four intervals are in **Preferences > Polling**. The task pane polls only
while it is open.

## Hiding the tree

The inventory tree sidebar can be hidden and reopened using the tree button in the toolbar.
