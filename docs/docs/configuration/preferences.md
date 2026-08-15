---
title: Preferences
sidebar_position: 1
---

# Preferences

Change global Proxima preferences under **File > Preferences**. Per guest settings live in
[their own dialog](per-guest-settings.md).

## Appearance

### Window

|                                      |                                                                                                              |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| Use the system theme                 | Use the native system theme, useful if you're on Linux and already have a GTK theme.                         |
| Colours                              | Follow system, Light, or Dark.                                                                               |
| Draw the titlebar in the application | Replaces the system titlebar with one Proxima draws, so it matches the rest of the window. Restart required. |

### Text

|                |                                                   |
| -------------- | ------------------------------------------------- |
| Font backend   | FreeType or the platform's own. Restart required. |
| Interface font | The theme's font, or one you pick.                |
| Antialiasing   | Grayscale, subpixel, none, or the default.        |
| Hinting        | Slight, full, medium or none.                     |
| Hint metrics   | Snap glyph advances to whole pixels.              |

FreeType is the default because it is the only backend that applies the hinting
settings. Windows own text renderer ignores them and hints its own way.

## Console

|                                     |                                                                        |
| ----------------------------------- | ---------------------------------------------------------------------- |
| Enable audio                        | SPICE audio playback, for every console.                               |
| Software video decoding only        | Disables D3D, NVDEC and Vulkan decoders. Restart required.             |
| Auto-resize guest                   | Asks the guest to match the window. SPICE only, needs `spice-vdagent`. |
| Scale console to fit                | Fit the picture to the tab instead of scrolling it.                    |
| Display scaling                     | The default for guests with no answer of their own. 100% to 200%.      |
| Always use VNC                      | Ignore SPICE even where it is available.                               |
| Ask when a USB device is plugged in | Automatically offer newly pluigged in devices to the guest.            |
| Check for other SPICE clients       | Ask before disconnecting somebody. Needs `VM.Monitor`.                 |

Display scaling means different things on SPICE and VNC. See
[Consoles](../using/consoles.md).

## Behaviour

### Ask before

Configure whether Stop, Shutdown, Reset and Pause ask for confirmation before performing the action.

### Startup

|                                 |                                                                                                           |
| ------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Automatically check for updates | Ask GitHub for the latest release a few seconds after the window opens. Packaged builds only.             |
| Restore the last session        | Reopen the consoles that were open when the window closed, and put the tree back the way it was expanded. |

### Inventory tree

|                 |                                                                                     |
| --------------- | ----------------------------------------------------------------------------------- |
| Group guests by | Node, folder or tag. The same three shapes the button beside the search box cycles. |
| Names           | `webserver (101)` or `101 (webserver)`.                                             |

### Names

|                               |                                                         |
| ----------------------------- | ------------------------------------------------------- |
| Console tabs                  | Name, ID, or both.                                      |
| Group templates at the bottom | Templates sort together below the guests in each group. |

## Polling

|                          | Default |                                                                                   |
| ------------------------ | ------- | --------------------------------------------------------------------------------- |
| Inventory, at rest       | 6s      | With nothing outstanding.                                                         |
| Inventory, while waiting | 2s      | While a change this window asked for has not been reported.                       |
| Keep waiting for         | 15s     | How long the faster cadence outlives an action that leaves nothing else to watch. |
| Task list                | 5s      | Only while the task pane is open.                                                 |

The window switches between the two cadences by itself. Start a guest, rename
one, or reconnect a server and it watches closely until the cluster reports the
change, then drops back to the resting interval.
