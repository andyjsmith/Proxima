# Proxima

A VMware Workstation style client for Proxmox VE, built on GTK 3, SPICE,
VNC and a serial console for containers.

```
python3 proxima.py                 normal start
python3 proxima.py --diagnose      report the GTK/SPICE stack
python3 proxima.py --fontconfig    force the FreeType font backend
python3 proxima.py --debug         log everything, including GLib's own
python3 proxima.py --logs          print the log directory and exit
```

On Windows this must run under the MSYS2 UCRT64 Python
(`C:/msys64/ucrt64/bin/python.exe`), not a python.org install: PyGObject and
spice-gtk come from pacman and will not load anywhere else.

## The inventory tree

Three shapes, cycled by the button beside the search box or chosen in
Preferences -> Behaviour; both write the same setting.

| | |
| --- | --- |
| Node | Server, node, guest -- how Proxmox itself organises things. |
| Folder | Your own folders, stored in each guest's notes, so they span the datacenter and follow a guest between nodes. |
| Tag | The tags Proxmox keeps. |

Tag view repeats guests, deliberately: tags are not a hierarchy, so a guest
tagged `prod` and `web` appears under both. Grouping by the *combination*
instead would make a separate group for every set of tags anyone had ever
used, which stops being useful the moment it is populated. Tags differing
only in case are one group, spelled the way the first guest to use it spells
it, and guests with no tags collect under **Untagged** at the end. The node
rows stay at the top in folder and tag view, since what is in the cluster is
worth knowing whatever the guests below are sorted by.

The search box already matches tags, so filtering to one is a matter of
typing it.

## Certificates

Proxmox signs its own certificate, so there is no public authority to vouch
for it. Rather than turn checking off -- which is the usual answer, and
means talking to anything that answers on port 8006 while holding root
credentials -- Proxima does what ssh does:

1. The first connection to a server shows you its certificate, including the
   SHA-256 fingerprint. **Datacenter -> Certificates** in the Proxmox web
   interface prints the same value, as does
   `openssl x509 -fingerprint -sha256`, so it can be compared by eye.
2. Accept it once and the fingerprint is pinned in the settings under
   `trusted_certs`. Nothing asks again.
3. If that server ever presents a different certificate, the connection
   stops before any credential is sent and says so. That is a renewal, or it
   is somebody in the middle, and nothing at this end can tell which -- so
   the safe answer is the default one.

There is no "skip the check" setting, deliberately: it would be set once,
forgotten, and left on the one connection that carries root. A certificate
that does chain to a public CA is verified normally and is not pinned, so
renewing it changes nothing. A pinned certificate is not checked for
hostname, because the fingerprint *is* the identity at that point -- and a
Proxmox certificate frequently does not match the address you reach it on.

The console's own connection is held to the same pin, so a VNC session
cannot end up less checked than the API call that opened it. To be asked
again about a server, delete its entry from `trusted_certs` in
`settings.json`.

## Which console you get

| Guest | Default | The other option |
| --- | --- | --- |
| VM with a SPICE display | SPICE | VNC |
| VM without one | VNC | - |
| Container | Serial | VNC |

**VM -> Reopen Console with...** switches for as long as the tab is open;
the guest's own **Protocol** setting, in VM Settings -> Proxmox Manager,
makes it stick for everyone.

The serial console is Proxmox's `termproxy` -- the same thing the web UI
opens with xterm.js, and what the `pct console` command attaches to. It is
a real character terminal rather than a picture of one, which is what a
container's VNC console actually is:

* the text can be selected with the mouse, **Ctrl+Shift+C** copies and
  **Ctrl+Shift+V** pastes (middle click pastes the primary selection)
* the terminal is as wide as the tab, not a fixed 80x24, and the container
  is told when that changes
* there is scrollback -- the wheel, the scrollbar, or Shift+Page Up
* **Ctrl+plus** and **Ctrl+minus** change the font size, per container
* it costs a few hundred bytes a screen rather than a framebuffer

VNC stays one menu entry away, because a container whose serial console is
wedged is exactly when a second opinion is worth having. The emulator is
ours (`proxima/console/vt.py`): VTE is the obvious answer on Linux and does
not exist on Windows, since MSYS2 ships no `vte3` for any mingw target.

## Sending keys the host swallows

Ctrl+Alt+Del is a toolbar button; its arrow, and **VM -> Send Key**, carry
the rest -- Ctrl+Alt+Backspace, the twelve virtual terminals, Alt+Tab,
Alt+F4, Ctrl+Esc and PrintScreen. These exist because pressing them does
not work: the window manager on *this* computer takes Alt+Tab, a Linux
desktop takes Ctrl+Alt+F2, and something is usually listening for
PrintScreen, so none of them ever reach the guest. SPICE and VNC both carry
them -- keysyms go over SPICE and RFB alike -- so the menu works on a VNC
console too.

On a serial console the menu only offers what a character terminal can
express, and the entries that cannot are refused by name rather than
quietly doing something else: Ctrl+Alt+F2 switches virtual terminals on a
machine with a console driver, and there is no console driver behind a pty.
Ctrl+Alt+Del is greyed out there for the same reason.

## Logs

Every run writes one, and **Help -> Open Log Folder** is the short way to
find it. The last five runs are kept, and a single run is capped at 10 MB
with one previous chunk retained -- so a window left open for a fortnight
keeps its recent past without growing without limit. A run's chunks age out
together, so a long one never pushes four short ones out of the directory.

| | |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Proxima\logs` |
| Linux | `$XDG_STATE_HOME/proxima/logs`, or `~/.local/state/proxima/logs` |
| macOS | `~/Library/Logs/Proxima` |
| Anywhere | `PROXIMA_LOG_DIR` overrides all of the above |

The file always gets full detail; `--debug` additionally puts it on the
console and stops filtering GLib's own chatter (`G_MESSAGES_DEBUG` picks
individual domains, as it would anywhere else). This matters more than it
looks: a packaged Windows build is a GUI-subsystem executable with **no
stdout and no stderr at all**, so on Windows the log file is the only
account of a run there will ever be. GTK, GStreamer and spice-gtk log
through GLib rather than through Python, and those messages are routed into
the same file -- a SPICE fault usually explains itself on a `GSpice` line.

## Updates

A packaged build asks GitHub for the latest release a few seconds after the
window opens, and says so only when there is a newer one. It never downloads
or installs anything; the dialog shows the release notes and a link. Turn it
off in Preferences -> Behaviour, or check on demand from Help -> Check for
Updates. A source checkout never checks by itself -- the version there comes
from `pyproject.toml`, which is routinely behind the tree it describes.

## USB redirection

A USB device plugged into this computer can be handed to a VM over SPICE,
from **VM -> USB Devices**, from the icon in the status bar, or from the
question Proxima asks when something is plugged in while a console is in
front of you. VNC has no channel to carry a device over, so it is offered
only on SPICE.

Three things have to be in place, and the status bar says which one is
missing:

| | |
| --- | --- |
| The VM needs a SPICE USB port | Proxmox adds none. Hardware -> Add -> USB Device -> Spice Port, which writes `usb0: spice`. One line, one device at a time; add more for more. |
| Windows needs the UsbDk driver | The installer offers to install it (a tickbox on the components page), or get it from [daynix/UsbDk](https://github.com/daynix/UsbDk/releases/latest). Devices are listed without it and the list looks perfectly healthy -- Windows just will not hand one over when it is claimed. |
| Linux needs access to the device | spice-gtk talks to libusb directly, so the user has to be able to open `/dev/bus/usb`. The distribution's `spice-client-glib-usb-acl-helper` handles this where it is installed. |

A redirected device is taken away from this computer for as long as it is
redirected; closing the console gives it back. The prompt on plug-in can be
turned off in Preferences -> Console.

## Development

PyGObject, pycairo and spice-gtk come from the system, never from pip -- a
pip-installed copy cannot find the GObject typelibs.

| | Linux | Windows (MSYS2 UCRT64) |
| --- | --- | --- |
| GTK stack | `apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-spiceclientgtk-3.0` | `pacman -S mingw-w64-ucrt-x86_64-{python-gobject,gtk3,spice-gtk}` |
| pytest | `apt install python3-pytest python3-pytest-xdist` | `pacman -S mingw-w64-ucrt-x86_64-python-pytest{,-xdist}` |

### Tests

The suite in `tests/` builds the real UI against a fake Proxmox and pumps the
GTK main loop, which catches what only shows up once the widgets are realised
-- wrong signal signatures, bad CSS, missing icons, TreeStore column
mismatches -- without needing a server. The fakes and the main-loop pump live
in `tests/conftest.py`.

```
python3 -m pytest                  everything (~30s, eight at a time)
python3 -m pytest -k console       one area
python3 -m pytest -n0              serially, for a readable failure
python3 tools/smoke_test.py        same thing, the old entry point
```

Windows are module-scoped: the tests in a file run in order against one
window, each putting back whatever it changed. That is why the run is split
by file (`--dist loadfile` in `pyproject.toml`) rather than by test -- a file
has to stay on one worker, and several windows opening and closing at once is
the suite working as intended, not a fault.

The wall clock is bounded by the slowest single file, so the way to make the
suite faster is to make its longest file shorter, not to add workers. Which
file that is comes out of `python3 -m pytest -n0 --durations=0`. Prefer
`pump_until(...)` over `pump(n)` when adding a test: a fixed sleep is both
slower than it needs to be on a good machine and too short on a busy one.

### Formatting and linting

```
uv run --only-group dev ruff check .          must pass
uv run --only-group dev ruff format --diff .  advisory, see below
```

`ruff check` is enforced in CI. The tree is `ruff format` clean as well, but
the format job is still `continue-on-error`: it reports the diff without
failing the build. Drop that line from `ci.yml` to make it binding.

### Icons

The icons on buttons and menus are not shipped with Proxima. They are
freedesktop icon names -- `media-playback-start-symbolic`,
`view-fullscreen-symbolic` -- looked up in whatever icon theme is live, which
is **Adwaita**, the one GTK itself comes with. That is why a packaged build
carries `share/icons/Adwaita`: without it the buttons come up blank.

To see what is available, GTK ships a browser:

```
gtk3-icon-browser        MSYS2: it is in mingw-w64-ucrt-x86_64-gtk3
                         Debian: apt install gtk-3-examples
```

Anything it lists can be passed to `set_icon_name()`. The names also follow
the freedesktop icon naming specification, so most of them are portable to
other themes. Prefer the `-symbolic` variants: they recolour to match the
theme, which is what the toolbar and the status bar rely on.

### CI

* `.github/workflows/ci.yml` -- format (advisory), lint, and the test suite on
  Linux under Xvfb with openbox, since the fullscreen tests wait on real
  window-state changes.
* `.github/workflows/build.yml` -- standalone Nuitka builds for Linux and
  Windows, uploaded as artifacts. The Windows job builds inside MSYS2 UCRT64
  for the same reason the app runs there. It takes the better part of an hour,
  so it does not run on a push: start one from **Actions -> Build -> Run
  workflow**, or put a `build-please` label on a pull request to build that
  branch. The release workflow calls it regardless.
* `.github/workflows/release.yml` -- everything below.

## Releasing

A release is cut from a tag, so every artifact traces back to one commit.
The version lives in `pyproject.toml` and nowhere else -- the app reads it
back at run time -- so `uv version` is all it takes:

```
uv version 0.2.0                     or: uv version --bump minor
git commit -am "release: v0.2.0"
git tag -a v0.2.0 -m "Proxima 0.2.0"
git push origin main --follow-tags   this is what starts the release
```

The workflow refuses to publish a tag whose version does not match
`pyproject.toml`. A version with a suffix (`0.2.0-rc1`) is published as a
pre-release.

Pushing the tag builds both platforms and publishes:

| Artifact | |
| --- | --- |
| `proxima-<v>-windows-x86_64-setup.exe` | NSIS installer, per-user or all-users |
| `proxima-<v>-windows-x86_64.zip` | the same bundle, unpacked wherever you like |
| `proxima-<v>-linux-x86_64.tar.gz` | tarball, not a zip: zip has nowhere to keep the executable bit |
| `Proxima-<v>-x86_64.AppImage` | |
| `proxima_<v>_amd64.deb` | installs to `/opt/proxima` |
| `proxima-<v>-1.x86_64.rpm` | the same |
| `SHA256SUMS` | |

`workflow_dispatch` on the release workflow rebuilds an existing tag and
re-uploads the files, for when a build fails for reasons that have nothing to
do with the code.

### Trying a package without releasing one

Run the release workflow by hand from **Actions -> Release -> Run workflow**
and leave the tag box **empty**. Everything is built and packaged from the
branch you picked -- installer, AppImage, deb, rpm, zip, checksums -- and the
lot is uploaded as a `release-<version>` workflow artifact. Nothing is
published and no tag is touched, so this is the way to look at an actual
AppImage or installer before deciding to cut a release.

The version on those files comes from `pyproject.toml`, which on a branch is
whatever the last release left behind. They are for testing, not for handing
out.

### What is in a package

Everything. None of them ask the user to install GTK, spice-gtk, GStreamer or
Python -- and they are built `--standalone`, never `--onefile`, which would
unpack 60 MB to a temporary directory on every start.

Getting there takes two steps beyond a plain Nuitka build, because Nuitka
follows what the program *links against*, and GTK loads most of itself by hand
later:

* the build passes `--include-raw-dir` for the pixbuf loaders, the GStreamer
  plugins and the GIO modules. Not `--include-data-dir`, which silently drops
  shared libraries and leaves plugin directories that do nothing;
* `tools/bundle_deps.py` then asks every bundled plugin what it needs, copies
  the codec libraries in beside the executable, and on Linux gives the plugins
  an RPATH back to the top of the bundle.

At run time `proxima/bundle.py` points GTK at all of it -- the loader cache is
rewritten, since the paths in it belong to the machine the build was made on
-- before anything imports `gi`. It does nothing in a source checkout.
`proxima --diagnose` prints what a bundle carries and what it is missing.

`packaging/` holds the installer script, the desktop entry, the icon and
`gst-plugins.txt` -- the list of GStreamer plugins a build carries. Shipping
every plugin costs about 200 MB, nearly all of it encoders a client never
uses.

The Windows installer can also carry the UsbDk driver, which USB redirection
needs. `tools/fetch_usbdk.py` downloads the latest x64 MSI at package time
and the release workflow passes it to `makensis` as `-DUSBDK=...`; without
that define the installer compiles exactly as before and simply does not
offer the driver. It is one tickbox on the components page, unticked
automatically when the machine already has UsbDk, and the uninstaller
deliberately leaves the driver alone -- virt-viewer and friends use the same
one.

The installer runs as the invoking user and asks for administrator rights
only when they are actually needed: choosing "all users" relaunches it
elevated at that moment, and the uninstaller does the same for an all-users
installation. A per-user install never sees a UAC prompt from Proxima
itself. (The driver's own MSI raises one when it is installed, whichever
mode is in use -- a kernel driver is per-machine either way.)

The application icon is `packaging/proxima.png`. After changing it, run
`python3 tools/make_icon.py` to rebuild `packaging/proxima.ico`, which is
what Windows uses for the executable, the installer and the taskbar. The PNG
is set as the default window icon at startup, so it applies in a source
checkout too -- no build needed to see it.

### Building one locally

Windows, from the MSYS2 UCRT64 tree but *not* from an MSYS2 shell -- Nuitka's
`gi` plugin trips over its own path handling when `MSYSTEM` is set:

```powershell
$env:PATH = "C:\msys64\ucrt64\bin;" + $env:PATH
python -m nuitka --standalone --zig --include-package=proxima proxima.py
```

`--zig` is not optional: Nuitka cannot use MinGW with Python 3.13 or newer,
and it refuses any gcc it did not download itself. Zig has to be on PATH.

If a build starts segfaulting or dies with `init_fs_encoding: failed to get
the Python codec of the filesystem encoding`, the Zig cache is poisoned --
a build that is interrupted or fails part-way can leave it that way, and
every build afterwards inherits it. Nuitka's own `--disable-cache=all` does
not cover it:

```powershell
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Nuitka\Nuitka\Cache\zig"
```

CI never hits this, since every run starts on a clean machine.
