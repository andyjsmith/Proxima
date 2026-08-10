# Developing Proxima

Everything about building, testing and releasing. For what the program does
and how to install a built one, see [README.md](README.md).

## Running from source

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

PyGObject, pycairo and spice-gtk come from the system, never from pip -- a
pip-installed copy cannot find the GObject typelibs.

| | Linux | Windows (MSYS2 UCRT64) | macOS (Homebrew) |
| --- | --- | --- | --- |
| GTK stack | `apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-spiceclientgtk-3.0` | `pacman -S mingw-w64-ucrt-x86_64-{python-gobject,gtk3,spice-gtk}` | `brew install pygobject3 gtk+3 spice-gtk adwaita-icon-theme librsvg` |
| pytest | `apt install python3-pytest python3-pytest-xdist` | `pacman -S mingw-w64-ucrt-x86_64-python-pytest{,-xdist}` | `pip install --break-system-packages pytest pytest-xdist` into the same interpreter (see below) |

`spice-gtk` on Homebrew pulls in a full `gstreamer` -- current Homebrew
bundles every `gst-plugins-*` set (including `gst-libav`) into that one
formula, so nothing else needs installing for codecs. `adwaita-icon-theme`
and `librsvg` do *not* come along for free on macOS the way they do via
apt's dependency chain -- without the theme, toolbar and menu buttons come
up blank, and without librsvg's gdk-pixbuf loader nothing can read the SVGs
the symbolic ones are drawn from.

Homebrew's `pygobject3` is built against a specific Homebrew Python (find it
with `brew list pygobject3 | grep site-packages`, e.g.
`/usr/local/Cellar/pygobject3/.../lib/python3.14/site-packages`) -- run
Proxima with *that* interpreter, not a pip venv or a python.org install, for
the same reason as the other two platforms: a separately installed PyGObject
would not find the GObject typelibs. `python3 proxima.py` in a source
checkout works from there directly, no `pip install` needed at all --
`pyproject.toml` declares no runtime dependencies.

If a console opens to warnings like `GLib-GIRepository-WARNING: Failed to
load shared library 'libgobject-2.0.0.dylib'` from `gst-plugin-scanner`, put
Homebrew's lib directory on the fallback library search path before
launching: `export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib"`. A
plain `dlopen()` of a bare filename -- which is how GIRepository loads the
library a `.typelib` names -- does not otherwise see it, and recent macOS no
longer searches `/usr/local/lib` by default the way older releases did.

A source checkout never checks for updates by itself: the version there
comes from `pyproject.toml`, which is routinely behind the tree it
describes.

## Tests

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

## Formatting and linting

```
uv run --only-group dev ruff check .          must pass
uv run --only-group dev ruff format --diff .  advisory, see below
```

`ruff check` is enforced in CI. The tree is `ruff format` clean as well, but
the format job is still `continue-on-error`: it reports the diff without
failing the build. Drop that line from `ci.yml` to make it binding.

## Icons

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

The application icon is `packaging/proxima.png`. After changing it, run
`python3 tools/make_icon.py` to rebuild `packaging/proxima.ico`, which is
what Windows uses for the executable, the installer and the taskbar. The PNG
is set as the default window icon at startup, so it applies in a source
checkout too -- no build needed to see it.

## CI

* `.github/workflows/ci.yml` -- format (advisory), lint, and the test suite on
  Linux under Xvfb with openbox, since the fullscreen tests wait on real
  window-state changes.
* `.github/workflows/build.yml` -- standalone builds for Linux, Windows and
  macOS, uploaded as artifacts. The Windows job builds inside MSYS2 UCRT64
  for the same reason the app runs there; the macOS job builds arm64 only,
  since Homebrew has no cross-compiled bottles and the whole GTK stack there
  comes from bottles -- an Intel bundle has to be built on an Intel Mac.
  It takes the better part of an hour,
  so it does not run on a push: start one from **Actions -> Build -> Run
  workflow**, or put a `build-please` label on a pull request to build that
  branch. The release workflow calls it regardless.
* `.github/workflows/release.yml` -- everything under [Releasing](#releasing).

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

Pushing the tag builds all three platforms and publishes:

| Artifact | |
| --- | --- |
| `proxima-<v>-windows-x86_64-setup.exe` | NSIS installer, per-user or all-users |
| `proxima-<v>-windows-x86_64.zip` | the same bundle, unpacked wherever you like |
| `proxima-<v>-linux-x86_64.tar.gz` | tarball, not a zip: zip has nowhere to keep the executable bit |
| `Proxima-<v>-x86_64.AppImage` | |
| `proxima_<v>_amd64.deb` | installs to `/opt/proxima` |
| `proxima-<v>-1.x86_64.rpm` | the same |
| `proxima-<v>-macos-arm64.tar.gz` | `Proxima.app`, arm64, ad-hoc signed only. A tarball for a stronger version of the same reason: zip keeps neither the symlinks nor the executable bits a `.app` is made of. The packaging job runs on Linux, which is also why it is not a `.dmg` -- `hdiutil` only exists on a Mac. |
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

## What is in a package

Everything. None of them ask the user to install GTK, spice-gtk, GStreamer or
Python -- and they are built `--standalone`, never `--onefile`, which would
unpack 60 MB to a temporary directory on every start.

Linux and Windows are built with Nuitka; macOS with PyInstaller, from
`packaging/proxima.spec`. That split is not a preference. A Homebrew
`.dylib` records its own dependencies as *absolute paths back into the
Cellar*, unlike an ELF SONAME, which is only a name -- so copying one next
to the executable achieves nothing on its own, and every load command in
every copied library has to be rewritten before the bundle will run
anywhere but the machine that built it. PyInstaller does that rewriting
itself, and ships runtime hooks for the whole GNOME stack (`pyi_rth_gi`,
`_gdkpixbuf`, `_gio`, `_glib`, `_gstreamer`) which set the same paths
`proxima/bundle.py` sets by hand for the other two. So on macOS
`bundle.py` deliberately stands aside; `pyinstaller_root()` is what it
checks.

Two things PyInstaller does not know about are supplied in
`packaging/pyinstaller/hooks/`: spice-gtk, which it has no hook for and
which `proxima/console/spicelib.py` imports through `importlib` so nothing
in the module graph names it either. A gi namespace is not a file on disk,
so it needs a `pre_safe_import_module` hook as well as an ordinary one --
without that first half the hidden import is reported "not found" and the
typelib is silently left out, which produces a bundle whose SPICE console
cannot create a single object.

Getting there on Linux and Windows takes two steps beyond a plain Nuitka
build, because Nuitka follows what the program *links against*, and GTK
loads most of itself by hand later:

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

## Building one locally

macOS, with the interpreter Homebrew built `pygobject3` against (see
[Running from source](#running-from-source) for how to find it):

```sh
python3 -m pip install --break-system-packages pyinstaller
python3 -m PyInstaller packaging/proxima.spec --noconfirm \
    --distpath build/dist --workpath build/pyinstaller
./build/dist/Proxima.app/Contents/MacOS/proxima --diagnose
```

The result is signed with nothing but the ad-hoc signature PyInstaller
applies, which is enough to *run* -- an arm64 Mach-O will not launch
unsigned at all -- but not enough for Gatekeeper, so a downloaded copy needs
clearing once in **System Settings -> Privacy & Security**. Notarizing
instead would take a paid Developer ID.

H.264 is the one codec worth checking in `--diagnose` output there. Homebrew
ships no `openh264` plugin at all, only `gst-libav` (ffmpeg, left out for
its licence and its size) and the x264 *encoder*, so the bundle's only H.264
decoder is `vtdec` -- VideoToolbox, out of the `applemedia` plugin. It is
listed under hardware decoding in `packaging/gst-plugins.txt` for that
reason, and only its hardware-only sibling `vtdec_hw` is demoted by
**Preferences -> software decoding only**; demoting `vtdec` as well would
leave that setting with no H.264 at all rather than with a slower path.

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

---

# Design notes

Things that are not guessable from the code, and were learned the hard way.

## Nothing reparents a live console

A `SpiceDisplay` moved between toplevels has its `GdkWindow` destroyed and
rebuilt underneath a running connection. So full screen hides the window's
chrome rather than moving the console into a new window, and the extra
monitors get display widgets built for the occasion and thrown away
afterwards -- the tab's own head keeps the widget it has had since it
connected. `DisplayHolder` exists for a related reason: `SpiceDisplay`
reports the guest resolution as its natural size, which packed into a box
makes a feedback loop that ends with the guest out of video memory.

## Multi-monitor SPICE

A head is *asked for*, not waited for. A guest does not offer a second
display until a client says it wants one, so counting the display channels
that have turned up answers "one" for a guest that would happily give you
four. Proxima says what virt-viewer's Displays menu says -- display N is
enabled -- and the guest's driver makes the head.

**It is asked for twice.** Going full screen resizes the first head, and the
config spice-gtk sends for that resize is built from the displays it knows
about -- which does not include a head asked for and not yet created, so on
some guests the first request is dropped. That was "it works if I go full
screen twice". Asking *only* after the resize is not the answer either:
other guests make the head on the first ask and lose it when the ask is
delayed. So every window opens at once and any head the guest has not
produced by the time the resize lands is asked for again -- off, on, and a
new widget, three attempts, then the window says what it knows instead. The
retry rebuilds the widget rather than just re-enabling the head, because a
head's size is reported by its widget and only when the widget is allocated:
a head that was dropped and merely re-enabled has no size, and the guest is
entitled to ignore it.

**Where the heads go is the guest's business.** It arranges the displays it
has been given, and it is good at it. A client that sends positions as well
argues with it continuously: every position provokes a resize and every
resize changes the numbers the next position would be computed from. On a
guest with two QXL devices it is worse than useless, because the agent
matches monitor-config entries to devices in the guest's own enumeration
order, which need not match the display channels -- so a position meant for
one monitor lands on the other and the two sizes swap back and forth
forever. Proxima sends no positions and no sizes. Each head's widget reports
its own size through `resize-guest`, which is exactly one monitor.

**A head's number is its display id.** The head list stays in channel order
and is never rearranged to put the tab's own head first, because the index
into it is the number spice-gtk and the guest's agent both use. The tab
attaches to whichever display channel turns up first, and on a two-device
guest that can be channel 1 -- reordering around that renumbers every head,
and then switching off "the second head" on the way out switches off the one
on screen. `set_head_enabled` refuses to touch the head the tab is showing,
whatever number it turns out to be.

Heads are handed back on the way out, and that matters more than it sounds:
a desktop left with a monitor it cannot see can save that layout and come
back to it after a reboot with its session on a screen that is not there.

## Keyboard grabs

spice-gtk grabs the keyboard while the display has focus, and on Windows
that grab is a low-level hook that keeps taking keys after another window is
focused. So the grab is tied to two things together: the pointer being over
the console, *and* one of this console's windows being active. Without the
second, hovering the console of a background window swallows what you are
typing into your browser.

The same focus behaviour is why the active pane follows clicks rather than
focus: the console takes the focus when the pointer merely crosses it, so a
window that followed the focus changed which guest the toolbar acted on as
the pointer passed over a pane on the way to a button.

## Two polling speeds, not a timer per action

The inventory poll picks between an idle cadence and a faster one while
something this window asked for has not been reported yet. That replaced a
fixed 2s interval with a second once-a-second timer laid over it after every
action -- which meant an idle window talking to the server thirty times a
minute, and an action answered by both timers at once.

## Terminals and framebuffers are ours

`proxima/console/vt.py` is the terminal emulator and `proxima/console/rfb.py`
the VNC client, for the same reason `proxima/ui/graphs.py` draws with cairo
rather than pulling in a charting library: nothing new to install, on either
platform. VTE is the obvious answer on Linux and does not exist on Windows,
since MSYS2 ships no `vte3` for any mingw target.

## SPICE connection parameters

The dict from `POST /nodes/<node>/qemu/<vmid>/spiceproxy` has quirks worth
restating because none of them are guessable:

* `host` is **not** a hostname. Proxmox puts an opaque proxy ticket there and
  the real network target is `proxy`; spice-glib passes host through to the
  proxy verbatim.
* `host-subject` means certificate validation should check the subject, not
  the hostname -- the hostname is that ticket string and will never match.
* the ticket in `password` is short lived, so connect promptly.
