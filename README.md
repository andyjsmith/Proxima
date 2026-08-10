# Proxima

A VMware Workstation style desktop client for Proxmox VE. Your whole
datacenter in a tree down one side, guests in tabs, and a real console in
each tab -- SPICE for VMs that have it, VNC for the rest, and a proper
terminal for containers.

It talks to the Proxmox API directly. There is nothing to install on the
server, and nothing runs there that would not run for the web interface.

* **Consoles in tabs**, split across up to four panes, or full screen across
  every monitor you have
* **Power, snapshots and settings** without leaving the console you are
  looking at
* **USB devices** handed to a VM over SPICE, offered as you plug them in
* **Node pages** with the meters and graphs the web interface shows, and a
  root shell one click away
* **Folders and tags** for organising guests across the whole datacenter
* **Certificate pinning**, so a self-signed Proxmox is verified rather than
  ignored

---

## Download

Builds are on the [releases
page](https://github.com/andyjsmith/proxima/releases/latest). Everything is
self-contained: GTK, SPICE, GStreamer and Python are inside the package, so
there is nothing else to install.

Windows and Linux are 64-bit x86 (`x86_64`/`amd64`) and macOS is Apple
silicon (`arm64`); there is no 32-bit build, and no Intel Mac build --
Homebrew, which the macOS package is built from, publishes no
cross-compiled bottles, so an Intel bundle has to be built on an Intel Mac.
The Linux packages are built on Ubuntu 22.04, so they need its glibc (2.35)
or newer -- which covers every current distribution.

### Windows

| File | |
| --- | --- |
| `proxima-<version>-windows-x86_64-setup.exe` | Installer. **Start here.** |
| `proxima-<version>-windows-x86_64.zip` | The same program in a folder. Unpack it anywhere and run `proxima.exe`. |

The installer offers **Just me** or **All users**. Just me is the default
and never shows a UAC prompt; choosing all users asks for administrator
rights at that moment and not before.

It can also install **UsbDk**, the driver USB redirection needs on Windows
-- a tickbox on the components page, ticked off automatically if you already
have it. See [USB redirection](#usb-redirection) below. Uninstalling Proxima
leaves the driver alone, since virt-viewer and friends use the same one.

### Linux

| File | |
| --- | --- |
| `Proxima-<version>-x86_64.AppImage` | Any distribution, nothing installed. `chmod +x` it and run it. |
| `proxima_<version>_amd64.deb` | Debian, Ubuntu, Mint. `sudo apt install ./proxima_<version>_amd64.deb` |
| `proxima-<version>-1.x86_64.rpm` | Fedora, RHEL, openSUSE. `sudo dnf install ./proxima-<version>-1.x86_64.rpm` |
| `proxima-<version>-linux-x86_64.tar.gz` | Unpack anywhere and run `./proxima`. A tarball rather than a zip, because zip has nowhere to keep the executable bit. |

The deb and rpm install to `/opt/proxima`, put `proxima` on your `PATH` and
add a desktop entry. The AppImage and the tarball leave nothing behind and
need no root.

### macOS

| File | |
| --- | --- |
| `proxima-<version>-macos-arm64.tar.gz` | Unpack it and drag `Proxima.app` to Applications. A tarball rather than a zip, because zip keeps neither the symlinks nor the executable bits an app bundle is made of. |

Apple silicon only, and not notarized: the app carries an ad-hoc signature,
which is enough to run but not enough for Gatekeeper. The first open is
refused, and **System Settings -> Privacy & Security** then offers an **Open
Anyway** button for it. That is once per download.

### Checking what you downloaded

`SHA256SUMS` is published with every release.

```bash
sha256sum -c SHA256SUMS --ignore-missing        # Linux
```

```powershell
Get-FileHash proxima-0.2.0-windows-x86_64-setup.exe   # Windows, compare by eye
```

---

## Connecting

**File -> Connect**, then the host, the username with its realm
(`root@pam`), and the password. Port 8006 is assumed, so `pve.example.com`
is enough; `10.0.0.5:8006` and a pasted `https://host:8006/...` are both
understood too. Saved connections reopen on the next start.

Proxmox signs its own certificate, so there is no public authority to vouch
for it. Rather than turn checking off -- the usual answer, and one that
means talking to anything that answers on port 8006 while holding root
credentials -- Proxima does what ssh does:

1. The first connection shows you the certificate, including its SHA-256
   fingerprint. **Datacenter -> Certificates** in the Proxmox web interface
   prints the same value, as does `openssl x509 -fingerprint -sha256`, so
   you can compare it by eye.
2. Accept it once and the fingerprint is pinned. Nothing asks again.
3. If that server ever presents a *different* certificate, the connection
   stops before any credential is sent and says so. That is a renewal, or it
   is somebody in the middle, and nothing at this end can tell which -- so
   the safe answer is the default one.

There is deliberately no "skip the check" setting: it would be set once,
forgotten, and left on the one connection that carries root. A certificate
that does chain to a public CA is verified normally and is not pinned, so
renewing it changes nothing. The console's own connection is held to the
same pin, so a VNC session cannot end up less checked than the API call that
opened it.

To be asked again about a server, delete its entry from `trusted_certs` in
your settings file (see [Where things are kept](#where-things-are-kept)).

---

## The inventory tree

Three shapes, cycled by the button beside the search box or chosen in
**Preferences -> Behaviour**; both write the same setting.

| | |
| --- | --- |
| Node | Server, node, guest -- how Proxmox itself organises things. |
| Folder | Your own folders, stored in each guest's notes, so they span the datacenter and follow a guest between nodes. |
| Tag | The tags Proxmox keeps. |

Tag view repeats guests, deliberately: tags are not a hierarchy, so a guest
tagged `prod` and `web` appears under both. Tags differing only in case are
one group, spelled the way the first guest to use it spells it, and guests
with no tags collect under **Untagged** at the end. Node rows stay at the
top in folder and tag view, since what is in the cluster is worth knowing
whatever the guests below are sorted by.

The search box matches tags too, so filtering to one is a matter of typing
it.

Guests can be dragged between folders. If you would rather they could not,
turn dragging off in **Preferences -> Behaviour**.

---

## Consoles

| Guest | Default | The other option |
| --- | --- | --- |
| VM with a SPICE display | SPICE | VNC |
| VM without one | VNC | - |
| Container | Serial | VNC |

**VM -> Reopen Console with...** switches for as long as the tab is open;
the guest's own **Protocol** setting, in VM Settings -> Proxmox Manager,
makes it stick for everyone.

The serial console is Proxmox's `termproxy` -- the same thing the web UI
opens with xterm.js, and what `pct console` attaches to. It is a real
character terminal rather than a picture of one, which is what a container's
VNC console actually is:

* the text can be selected with the mouse; **Ctrl+Shift+C** copies and
  **Ctrl+Shift+V** pastes (middle click pastes the primary selection)
* the terminal is as wide as the tab, not a fixed 80x24, and the container
  is told when that changes
* there is scrollback -- the wheel, the scrollbar, or Shift+Page Up
* **Ctrl+plus** and **Ctrl+minus** change the font size, per container
* it costs a few hundred bytes a screen rather than a framebuffer

VNC stays one menu entry away, because a container whose serial console is
wedged is exactly when a second opinion is worth having.

### Display scaling

**View -> Display Scaling**, from 100% to 200%, draws the guest larger. It
is remembered per guest, and **Preferences -> Console** sets the default for
guests that have no answer of their own.

It is worth reaching for on a 4K or Retina screen, where it does more than
make things readable. A SPICE console asks the guest to match the window in
*device* pixels, so a half-screen window on a Retina display quietly asks
the guest for four times as many pixels as it looks like it needs -- all of
which the guest renders, the host encodes and the network carries. 200%
asks for a quarter as many and draws each one twice the size, which is the
difference between a console that keeps up and one that does not.

VNC works differently, because RFB gives a client no way to ask the guest
for a different resolution: there the setting magnifies what arrives. It
costs nothing and saves nothing -- useful for reading a small guest on a
large screen, and no faster. 200% doubles every pixel exactly and stays
sharp; the steps in between are interpolated and will look slightly soft. If
the magnified picture is larger than the tab, the console scrolls.

A serial console is text and has its own **Ctrl+plus**/**Ctrl+minus** font
size, so this setting is greyed out there.

### Splitting the window

The **Split** button cycles: one pane, side by side, one above the other,
and back. It works whenever more than one tab is open. Tabs can be dragged
between panes, which is also how you get three panes or a 2x2 grid.

With the window split, the tab the toolbar and menus are acting on is shown
in **bold**. Click anywhere in a pane to point them at that one.

### Sending keys the host swallows

Ctrl+Alt+Del is a toolbar button; its arrow, and **VM -> Send Key**, carry
the rest -- Ctrl+Alt+Backspace, the twelve virtual terminals, Alt+Tab,
Alt+F4, Ctrl+Esc and PrintScreen. These exist because pressing them does not
work: the window manager on *this* computer takes Alt+Tab, a Linux desktop
takes Ctrl+Alt+F2, and something is usually listening for PrintScreen, so
none of them ever reach the guest. Both SPICE and VNC carry them.

On a serial console the menu offers only what a character terminal can
express, and refuses the rest by name rather than quietly doing something
else.

---

## Full screen and multiple monitors

**Ctrl+Alt+Enter** puts the console in front full screen, with a floating
bar at the top edge for the way back out. Ctrl+Alt on its own hands the
keyboard and mouse back to your desktop.

**View -> Use All Monitors** spreads the guest across every monitor you
have: the tab's window takes the monitor it is already on, and each further
display gets a window on the next monitor along, ordered by where your
monitors actually sit. Leaving full screen closes them and gives the extra
displays back to the guest.

It is **off by default**, because it asks the guest to create displays it
did not have, which changes the guest's own desktop layout. Once you turn it
on it stays on.

The entry is greyed out when it would do nothing, and its tooltip says which
reason applies:

| Why it is greyed out | What to do |
| --- | --- |
| Only one monitor here | Nothing to spread onto. |
| The console is not connected | Wait for it, or start the guest. |
| A VNC console | VNC carries one framebuffer with the guest's displays side by side inside it. Reopen the console with SPICE. |
| The display adapter has one head | In Proxmox, Hardware -> Display. See below. |

**Pick SPICE (`qxl`) for multiple monitors.** QXL carries up to four
displays on its own, so plain `qxl` is already enough -- `qxl2`/`qxl3`/`qxl4`
add QXL *devices*, which is a different thing and not needed for a second
monitor. **VirtIO-GPU is one display only**: QEMU's `virtio-gpu` defaults to
a single output and Proxmox exposes no setting to raise it.

The guest has to co-operate, since it is the guest that creates the display:
the SPICE guest agent must be running (`spice-vdagent` on Linux, the SPICE
guest tools on Windows) and its driver has to support a second monitor. If a
display never arrives, that window says so rather than sitting there blank.

---

## USB redirection

A USB device plugged into this computer can be handed to a VM over SPICE,
from **VM -> USB Devices**, from the icon in the status bar, or from the
question Proxima asks when you plug something in while a console is in front
of you. VNC has no channel to carry a device, so it is offered only on
SPICE.

Three things have to be in place, and the status bar says which one is
missing:

| | |
| --- | --- |
| The VM needs a SPICE USB port | Proxmox adds none. Hardware -> Add -> USB Device -> Spice Port, which writes `usb0: spice`. One line, one device at a time; add more for more. |
| Windows needs the UsbDk driver | The installer offers it, or get it from [daynix/UsbDk](https://github.com/daynix/UsbDk/releases/latest). Devices are listed without it and the list looks perfectly healthy -- Windows just will not hand one over when it is claimed. |
| Linux needs access to the device | spice-gtk talks to libusb directly, so you have to be able to open `/dev/bus/usb`. Your distribution's `spice-client-glib-usb-acl-helper` handles this where it is installed. |

A redirected device is taken away from this computer for as long as it is
redirected; closing the console gives it back. The prompt on plug-in can be
turned off in **Preferences -> Console**.

---

## Nodes

A node opens like a guest does -- double-click it in the tree, or right-click
for **Open Node** -- and gets a tab with two sides.

The summary is what Proxmox's own node Summary shows: **meters** for CPU
usage, IO delay, load average, RAM, swap and the root filesystem; **graphs**
of CPU (with IO delay over it), memory used against installed, and network
traffic in and out, over the hour, day, week, month or year; and status,
uptime, guest count, processor, kernel and pve-manager versions. Hovering a
graph reads off the sample under the pointer.

Load is drawn against the processor count, so a full bar means a machine
with no idle capacity left rather than an arbitrary ceiling. The graphs come
from the node's round-robin history, which records a sample a minute, so the
page re-reads it once a minute and no faster.

**Open Shell** is the node's own terminal -- `termproxy`, the same thing the
web interface's Shell button opens. It is never opened for you: a node page
restored on the next start brings the figures, not a root shell.

A node the cluster cannot reach says so in the tree and on its page, and its
Shell is greyed out rather than offered and then refused.

---

## Polling

The inventory is polled at two speeds and the window picks between them.

| | | |
| --- | --- | --- |
| At rest | 6s | Nothing outstanding; watching in case somebody else changes something. |
| While waiting | 2s | A change this window asked for has not been reported yet. |

"Waiting" is a state, not a countdown: a power action, a rename, a console
being restored at startup or a server still connecting all hold the faster
cadence for exactly as long as they take. **Keep waiting for** (15s) only
covers actions that leave nothing else to watch -- a guest merely carrying a
lock does not count, since a backup can hold one for an hour.

All three are in **Preferences -> Polling**, along with the task pane's own
interval. The task pane polls only while it is open.

---

## Updates

A packaged build asks GitHub for the latest release a few seconds after the
window opens, and says so only when there is a newer one. It never downloads
or installs anything: the dialog shows the release notes and a link. Turn it
off in **Preferences -> Behaviour**, or check on demand from **Help -> Check
for Updates**.

---

## Appearance

Proxima ships its own compact styling, and follows your desktop's light/dark
setting by default. **Preferences -> Appearance** has the colour choice, the
interface font and how text is rendered.

If you would rather it simply looked like the rest of your desktop, tick
**Use the system theme**. That drops the whole of the above: no stylesheet
of ours, the theme and accent colour your desktop chose, the fonts it
configured, and windows that dim when they lose focus like every other
window does. It applies immediately, and turning it off puts everything
back -- the settings it covers are remembered, only ignored while it is on,
which is why they grey out rather than disappear.

The trade is space. The compact styling exists because it is what fits a
large datacenter down one side of the window; with the desktop's theme in
charge the tree is roomier and fewer guests fit on screen. Everything else
works exactly the same.

One part cannot be changed without a restart: which backend Pango uses to
rasterise text is read once, as the program starts.

---

## Where things are kept

Settings, saved connections and pinned certificates:

| | |
| --- | --- |
| Windows | `%APPDATA%\Proxima\settings.json` |
| Linux | `$XDG_CONFIG_HOME/proxima/settings.json`, or `~/.config/proxima/settings.json` |
| Anywhere | `PROXIMA_CONFIG_DIR` overrides both |

Logs. Every run writes one, and **Help -> Open Log Folder** is the short way
to find it. The last five runs are kept, and a single run is capped at 10 MB
with one previous chunk retained -- so a window left open for a fortnight
keeps its recent past without growing without limit.

| | |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Proxima\logs` |
| Linux | `$XDG_STATE_HOME/proxima/logs`, or `~/.local/state/proxima/logs` |
| macOS | `~/Library/Logs/Proxima` |
| Anywhere | `PROXIMA_LOG_DIR` overrides all of the above |

A packaged Windows build is a GUI executable with **no stdout and no stderr
at all**, so on Windows the log file is the only account of a run there will
ever be. GTK, GStreamer and spice-gtk log through GLib rather than through
Python, and those messages land in the same file -- a SPICE fault usually
explains itself on a `GSpice` line.

---

## Command line

```
proxima                  normal start
proxima --diagnose       report the GTK/SPICE stack and exit
proxima --logs           print the log directory and exit
proxima --debug          log everything, including GLib's own, to the console
proxima --fontconfig     force the FreeType font backend
```

`--diagnose` is the first thing to run when a console will not open: it
prints what the build carries, which GStreamer decoders are present, and
whether a SPICE session can actually be created.

---

## Contributing

Building it, running it from source, the test suite and how releases are cut
are in **[DEVELOPMENT.md](DEVELOPMENT.md)**.

## Licence

Proxima is MIT licensed; see [LICENSE](LICENSE). A package bundles GTK,
spice-gtk and GStreamer, which carry their own licences -- those are listed
in `packaging/NOTICE.md` and shipped in `licenses/` inside every build.
