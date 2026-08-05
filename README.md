# Proxima

A VMware Workstation style client for Proxmox VE, built on GTK 3, SPICE and
VNC.

```
python3 proxima.py                 normal start
python3 proxima.py --diagnose      report the theme/SPICE stack
python3 proxima.py --fontconfig    force the FreeType font backend
```

On Windows this must run under the MSYS2 UCRT64 Python
(`C:/msys64/ucrt64/bin/python.exe`), not a python.org install: PyGObject and
spice-gtk come from pacman and will not load anywhere else.

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
| Windows needs the UsbDk driver | [UsbDk](https://www.spice-space.org/download.html) (spice-space downloads, or bundled with virt-viewer). Devices are listed without it and the list looks perfectly healthy -- Windows just will not hand one over when it is claimed. |
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
