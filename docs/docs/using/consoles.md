---
title: Consoles
sidebar_position: 2
---

# Consoles

Double click a guest, or use **VM > Open Console**. Each guest gets one tab
holding both the console and its summary. The **Console** button on the toolbar
flips between them, as does **View > Summary** (Ctrl+Alt+S).

The console view can be split horizontally or vertically to display two guests at once.

## Protocol selection

| Guest                   | Default | Alternative |
| ----------------------- | ------- | ----------- |
| VM with a SPICE display | SPICE   | VNC         |
| VM without one          | VNC     | none        |
| Container               | Serial  | VNC         |

A VM has a SPICE display when its display (`vga`) is one of the SPICE (`qxl`) values or VirtIO-GPU.
Anything else gets VNC through Proxmox's `vncproxy` websocket. The adapter
therefore decides which features a guest can have, such as auto-resize. See [Display adapters](display-adapters.md) for further information.

**VM > Reopen Console with** lets you switch between protocols, such as SPICE and VNC. To
make the choice stick for everyone, set **Protocol** in the guest's
[per guest settings](../configuration/per-guest-settings.md). To force VNC on
this machine, tick **Always use VNC** in **Preferences > Console**.

### SPICE is single client

QEMU serves one SPICE client at a time, so connecting to a VM already in use by someone else will disconnect them. If permitted, Proxima asks QEMU whether anyone else is attached and then offers the choice to take over the session, or open VNC instead, which is multi client.

The check reads the VM status through `VM.Monitor`. An account without that
privilege skips it silently, and connecting then behaves like any other SPICE
client. Turn the check off in **Preferences > Console > Check for other SPICE
clients**.

![Already in use dialog](/img/screenshots/already_in_use.png)

## Container serial console

The container default is Proxmox's `termproxy`, what the web interface opens
with xterm.js.

|            |                                                                                                                                 |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Selection  | Select text with the mouse. **Ctrl+Shift+C** copies, **Ctrl+Shift+V** pastes. Middle click pastes the primary selection on X11. |
| Size       | The terminal is as wide as the tab, not a fixed 80x24, and the container is told when that changes.                             |
| Scrollback | The wheel, the scrollbar, or Shift+Page Up.                                                                                     |
| Font size  | **Ctrl+plus** and **Ctrl+minus**, remembered per container. **Ctrl+0** returns to the default.                                  |
| Cost       | A few hundred bytes a screen rather than a framebuffer.                                                                         |

If the serial console misbehaves, the protocol can be switch to VNC.

## Display scaling

**View > Display Scaling** draws the guest larger, from 100% to 200%, remembered per guest. **Preferences > Console** sets the default for
guests with no setting saved already.

What it does depends on the protocol.

**On SPICE** it changes what the guest is asked to render. For example, 200% will tell SPICE to render at half of the width of your screen/console and then will scale up by 200%.

**On VNC** it magnifies what arrives, because RFB gives a client no way to ask
for a different resolution. Useful for reading a small guest on a large screen. If the magnified picture is
larger than the tab, the console scrolls.

**On a serial console** the setting is greyed out. Text has its own font size
under Ctrl+plus and Ctrl+minus.

Two related switches sit in the same menu and in **Preferences > Console**:

- **Auto-resize Guest** asks the guest to match its resolution to the window.
  SPICE only, and it needs `spice-vdagent` running in the guest.
- **Scale to Fit** shrinks or stretches the picture to the tab instead of
  scrolling it.

## Splitting the window

The **Split** button cycles through one pane, side by side, one above the
other, and back, whenever more than one tab is open. Drag tabs between panes to reorganize.

The tab the toolbar and menus act on is shown in bold. Click anywhere in a pane
to make it active.

![Split view](/img/screenshots/split_view.png)

## Multiple windows

**Pop Out** moves the active console into a separate window.

## Sending intercepted keys

Ctrl+Alt+Del is a toolbar button. Additional key combinations are available under **VM > Send Key**.

- Ctrl+Alt+Del and Ctrl+Alt+Backspace
- Ctrl+Alt+F1 through Ctrl+Alt+F12
- Alt+Tab, Alt+Shift+Tab, Alt+F4, Ctrl+Esc, PrintScreen

## View only

**View > View Only** allows you to view without sending keyboard or mouse to the guest. Useful for multiple access to the same VM over VNC (SPICE is still restricted to only one connection).

## Keyboard grabs

While the pointer is over a console and that console's window is
active, the console grabs the keyboard so keypresses reach the guest.

In full screen, Ctrl+Alt hands the keyboard and mouse back to your desktop.

## Other console actions

|                                  |                                                                                   |
| -------------------------------- | --------------------------------------------------------------------------------- |
| **VM > Save Console Screenshot** | Writes the current frame to a PNG.                                                |
| **View > Refresh Framebuffer**   | Asks for a full frame again, for a console that has gone stale or partly corrupt. |
| **View > Close Console**         | Ctrl+W. Closes the tab.                                                           |

Files dragged onto a SPICE console are sent to the guest. See
[File drag and drop](../sharing/file-drag-and-drop.md).

Video codec, image compression and guest desktop effects are described in
[Console performance](../configuration/performance.md).
