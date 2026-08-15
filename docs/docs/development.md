---
id: development
title: Developer documentation
sidebar_label: Developer documentation
---

# Developer documentation

Building Proxima, running it from source, the test suite, how packages are
made, and the design decisions that are not guessable from the code.

## Layout

```
proxima/
  api/        Proxmox REST and websocket client, models, certificate pinning,
              and the metadata block stored in guest notes
  console/    SPICE, VNC (an RFB client of our own), the serial terminal
              (a VT emulator of our own), USB redirection, key tables
  theme/      Stylesheet, fonts, light and dark handling, native chrome
  ui/         Main window, sidebar, tabs, dialogs, toolbar, status bar
  config.py   Settings, importable before gi
  logs.py     Per run log files, importable before gi
  bundle.py   Fixups a packaged build needs before gi is imported
packaging/    PyInstaller spec, installer script, icons, plugin list
tools/        Icon build, smoke test, UsbDk fetch
tests/        The suite, and the fake Proxmox it runs against
```

`config.py` and `logs.py` are deliberately dependency free and importable
before `gi`. Some of the values they hold, the Pango backend in particular,
only take effect if they reach the environment before GTK loads.

## Running from source

```
python3 proxima.py                 normal start
python3 proxima.py --diagnose      report the GTK and SPICE stack
python3 proxima.py --fontconfig    force the FreeType font backend
python3 proxima.py --debug         log everything, including GLib's own
python3 proxima.py --logs          print the log directory and exit
```

PyGObject, pycairo and spice-gtk come from the system, never from pip. A pip
installed copy cannot find the GObject typelibs.

| | Linux | Windows (MSYS2 UCRT64) | macOS (Homebrew) |
| --- | --- | --- | --- |
| GTK stack | `apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-spiceclientgtk-3.0` | `pacman -S mingw-w64-ucrt-x86_64-{python-gobject,gtk3,spice-gtk}` | `brew install pygobject3 gtk+3 spice-gtk adwaita-icon-theme librsvg` |
| pytest | `apt install python3-pytest python3-pytest-xdist` | `pacman -S mingw-w64-ucrt-x86_64-python-pytest{,-xdist}` | `pip install --break-system-packages pytest pytest-xdist` into the same interpreter |

On Windows this must run under the MSYS2 UCRT64 Python
(`C:/msys64/ucrt64/bin/python.exe`), not a python.org install. PyGObject and
spice-gtk come from pacman and will not load anywhere else.

On macOS, Homebrew's `pygobject3` is built against a specific Homebrew Python.
Find it with `brew list pygobject3 | grep site-packages` and run Proxima with
that interpreter. `pyproject.toml` declares no runtime dependencies, so no
`pip install` is needed at all. `adwaita-icon-theme` and `librsvg` do not
arrive for free the way apt's dependency chain brings them. Without the theme,
toolbar and menu buttons come up blank, and without librsvg's gdk-pixbuf loader
nothing can read the SVGs the symbolic icons are drawn from.

If a console opens to `GLib-GIRepository-WARNING: Failed to load shared library
'libgobject-2.0.0.dylib'` from `gst-plugin-scanner`, put Homebrew's lib
directory on the fallback search path before launching:

```sh
export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib"
```

A plain `dlopen()` of a bare filename, which is how GIRepository loads the
library a typelib names, does not otherwise see it, and recent macOS no longer
searches `/usr/local/lib` by default.

A source checkout never checks for updates. The version there comes from
`pyproject.toml`, which is routinely behind the tree it describes.

## Tests

The suite builds the real UI against a fake Proxmox and pumps the GTK main
loop. That catches what only shows up once the widgets are realised: wrong
signal signatures, bad CSS, missing icons, TreeStore column mismatches. No
server is needed. The fakes and the main loop pump live in `tests/conftest.py`.

```
python3 -m pytest                  everything, about 30s, eight at a time
python3 -m pytest -k console       one area
python3 -m pytest -n0              serially, for a readable failure
python3 tools/smoke_test.py        the old entry point, same thing
```

Windows are module scoped. The tests in a file run in order against one window,
each putting back whatever it changed. That is why the run is split by file
(`--dist loadfile` in `pyproject.toml`) rather than by test: a file has to stay
on one worker, and several windows opening and closing at once is the suite
working as intended.

Wall clock is bounded by the slowest single file, so the way to make the suite
faster is to shorten its longest file, not to add workers. Find it with
`python3 -m pytest -n0 --durations=0`. Prefer `pump_until(...)` over `pump(n)`
when adding a test. A fixed sleep is both slower than it needs to be on a good
machine and too short on a busy one.

## Formatting and linting

```
uv run --only-group dev ruff check .          must pass
uv run --only-group dev ruff format --diff .  advisory
```

`ruff check` is enforced in CI. The tree is `ruff format` clean as well, but
the format job is `continue-on-error`, so it reports the diff without failing
the build. Drop that line from `ci.yml` to make it binding.

## Icons

Button and menu icons are not shipped with Proxima. They are freedesktop icon
names, `media-playback-start-symbolic` and the like, looked up in whatever icon
theme is live, which is Adwaita. That is why a packaged build carries
`share/icons/Adwaita`. Without it the buttons come up blank.

```
gtk3-icon-browser        MSYS2: mingw-w64-ucrt-x86_64-gtk3
                         Debian: apt install gtk-3-examples
```

Prefer the `-symbolic` variants. They recolour to match the theme, which is
what the toolbar and status bar rely on.

The application icon is `packaging/proxima.png`. After changing it, run
`python3 tools/make_icon.py` to rebuild `packaging/proxima.ico`, which is what
Windows uses for the executable, the installer and the taskbar.

## CI

| Workflow | |
| --- | --- |
| `ci.yml` | Format (advisory), lint, and the test suite on Linux under Xvfb with openbox, since the fullscreen tests wait on real window state changes. |
| `build.yml` | Standalone builds for all three platforms, uploaded as artifacts. Not run on push. Start it from **Actions > Build > Run workflow**, or put a `build-please` label on a pull request. |
| `release.yml` | Everything under Releasing. |

## Releasing

A release is cut from a tag, so every artifact traces back to one commit. The
version lives in `pyproject.toml` and nowhere else, and the app reads it back
at run time.

```
uv version 0.2.0                     or: uv version --bump minor
git commit -am "release: v0.2.0"
git tag -a v0.2.0 -m "Proxima 0.2.0"
git push origin main --follow-tags
```

The workflow refuses to publish a tag whose version does not match
`pyproject.toml`. A version with a suffix such as `0.2.0-rc1` is published as a
pre-release. `workflow_dispatch` rebuilds an existing tag and re-uploads the
files, for when a build fails for reasons unrelated to the code.

Pushing the tag builds all three platforms and publishes the installer, zip,
Linux tarball, AppImage, deb, rpm, the macOS disk image and `SHA256SUMS`.

### Trying a package without releasing one

Run the release workflow by hand from **Actions > Release > Run workflow** and
leave the tag box empty. Everything is built and packaged from the branch you
picked and uploaded as a `release-<version>` workflow artifact. Nothing is
published and no tag is touched.

The version on those files comes from `pyproject.toml`, which on a branch is
whatever the last release left behind. They are for testing, not for handing
out.

## What is in a package

Everything. No package asks the user to install GTK, spice-gtk, GStreamer or
Python. All three platforms are built by PyInstaller from one spec,
`packaging/proxima.spec`, and always `--onedir`, never `--onefile`, which would
unpack 200 MB to a temporary directory on every start.

PyInstaller earns its place three times over.

- It ships runtime hooks for the whole GNOME stack (`pyi_rth_gi`,
  `_gdkpixbuf`, `_gio`, `_glib`, `_gstreamer`, `_gtk`) which set
  `GI_TYPELIB_PATH`, `GDK_PIXBUF_MODULE_FILE`, `GIO_MODULE_DIR`,
  `XDG_DATA_DIRS` and the `GST_PLUGIN_*` variables before any of our code runs.
  Those paths otherwise have to be worked out by hand, because GTK loads most
  of itself at run time from directories compiled into it on the build machine.
- Its binary analysis follows the shared libraries the plugins pull in, not
  only what the program links against, which is what puts the codecs behind the
  GStreamer plugins into the bundle.
- On macOS it rewrites Mach-O load commands. A Homebrew dylib records its
  dependencies as absolute paths back into the Cellar, unlike an ELF SONAME
  which is only a name, so copying one next to the executable achieves nothing
  until every load command in every copied library is rewritten. That is what
  makes a Homebrew GTK relocatable at all.

The macOS `.dmg` is made by the macOS build job with `hdiutil`, which exists
only on a Mac, and is handed to the release job already built. It is an LZFSE
compressed HFS+ image holding `Proxima.app` and an `/Applications` symlink,
with no window styling. The release job only renames it, so the version comes
from `pyproject.toml` like every other artifact.

Linux and Windows were built with Nuitka until they were not. Measured on one
Windows machine, the same commit built both ways (Nuitka 4.1.3, Python 3.14,
best of five, in bundle):

| | Nuitka | PyInstaller |
| --- | --- | --- |
| `Terminal.feed`, 1500 screens of escape sequences | 145 ms | **112 ms** |
| `des.vnc_response` x2000 | 440 ms | **367 ms** |
| `Guest.from_api` x4000 | 91 ms | **14 ms** |
| `--logs`, process start to exit | **7 ms** | 132 ms |
| `--diagnose`, the whole stack up | **314 ms** | 685 ms |
| build, cold cache | 122 s | **30 s** |
| bundle | 200 MB | **193 MB** |

Compiled code is slower here, not faster. CPython 3.14's specialising
interpreter beats Nuitka's C on all three hot paths, and dataclass
construction, which is most of what parsing an API response is, by six times.
What compiling bought was startup, about 350 ms, paid once per launch against a
bundle that takes a second and a half to open a window either way.

Two things PyInstaller does not know about are supplied in
`packaging/pyinstaller/hooks/`: spice-gtk, which it has no hook for and which
`proxima/console/spicelib.py` imports through `importlib` so nothing in the
module graph names it either. A gi namespace is not a file on disk, so it needs
a `pre_safe_import_module` hook as well as an ordinary one. Without that first
half the hidden import is reported as not found and the typelib is silently
left out, which produces a bundle whose SPICE console cannot create a single
object.

Two more it gets wrong, both handled by `proxima/bundle.py` before anything
imports `gi`.

- **fontconfig.** PyInstaller looks for GTK's sysconfdir beside the GLib DLL,
  where a GTK built the usual way keeps it. MSYS2 keeps it one level up in
  `ucrt64/etc`, so the Windows bundle comes up with no fontconfig configuration
  at all. The spec carries `etc/fonts` by hand and `bundle.py` points
  `FONTCONFIG_FILE` at it. Only Windows needs this.
- **The GStreamer registry.** `pyi_rth_gstreamer` puts it inside the bundle,
  which is fine in a downloads folder and wrong once the thing is installed.
  Nothing writes to `Program Files` or `/opt`, so every start would rescan every
  plugin. `bundle.py` moves it to the config directory.

`packaging/gst-plugins.txt` is the list of GStreamer plugins a build carries.
Shipping every plugin costs about 200 MB, nearly all of it encoders a client
never uses.

The Windows installer can also carry the UsbDk driver. `tools/fetch_usbdk.py`
downloads the latest x64 MSI at package time and the release workflow passes it
to `makensis` as `-DUSBDK=...`. Without that define the installer compiles as
before and simply does not offer the driver.

## Building one locally

macOS, with the interpreter Homebrew built `pygobject3` against:

```sh
python3 -m pip install --break-system-packages pyinstaller
python3 -m PyInstaller packaging/proxima.spec --noconfirm \
    --distpath build/dist --workpath build/pyinstaller
./build/dist/Proxima.app/Contents/MacOS/proxima --diagnose
```

Windows, from an MSYS2 UCRT64 shell, with PyInstaller from pacman
(`mingw-w64-ucrt-x86_64-pyinstaller`) rather than pip, so it runs under the
same interpreter `python-gobject` was built for:

```sh
python -m PyInstaller packaging/proxima.spec --noconfirm \
    --distpath build/dist --workpath build/pyinstaller
./build/dist/proxima/proxima.exe --diagnose
```

Linux is the same command again, with `python3` from apt and PyInstaller from
pip.

A build that is interrupted can leave a lock in `build/pyinstaller/` that fails
the next one with `PermissionError: [WinError 5]` on
`_pyi_gschema_compilation`. Delete the work directory and build again.
`--noconfirm` does not cover it.

Check `--diagnose` output before trusting a local bundle. `[MISSING]` on any
required line, or `SPICE session usable: False`, is the same failure CI would
have caught.

H.264 is the codec worth checking on macOS. Homebrew ships no `openh264` plugin
at all, only `gst-libav` (left out for its licence and its size) and the x264
encoder, so the bundle's only H.264 decoder is `vtdec` from `applemedia`. That
is why **software decoding only** demotes `vtdec_hw` and leaves `vtdec` alone.
Demoting both would leave the setting with no H.264 at all rather than with a
slower path.

# Design notes

Things that are not guessable from the code, learned the hard way.

## Nothing reparents a live console

A `SpiceDisplay` moved between toplevels has its `GdkWindow` destroyed and
rebuilt underneath a running connection. So full screen hides the window's
chrome rather than moving the console into a new window, and the extra monitors
get display widgets built for the occasion and thrown away afterwards. The
tab's own head keeps the widget it has had since it connected.

`DisplayHolder` exists for a related reason: `SpiceDisplay` reports the guest
resolution as its natural size, which packed into a box makes a feedback loop
that ends with the guest out of video memory.

## Multi monitor SPICE

**A head is asked for, not waited for.** A guest does not offer a second
display until a client says it wants one, so counting the display channels that
have turned up answers "one" for a guest that would happily give you four.
Proxima says what virt-viewer's Displays menu says, that display N is enabled,
and the guest's driver makes the head.

**It is asked for twice.** Going full screen resizes the first head, and the
config spice-gtk sends for that resize is built from the displays it knows
about, which does not include a head asked for and not yet created, so on some
guests the first request is dropped. That was "it works if I go full screen
twice". Asking only after the resize is not the answer either, because other
guests make the head on the first ask and lose it when the ask is delayed. So
every window opens at once and any head the guest has not produced by the time
the resize lands is asked for again: off, on, and a new widget, three attempts,
then the window says what it knows. The retry rebuilds the widget rather than
re-enabling the head, because a head's size is reported by its widget and only
when the widget is allocated.

**Where the heads go is the guest's business.** It arranges the displays it has
been given, and it is good at it. A client that sends positions as well argues
with it continuously: every position provokes a resize and every resize changes
the numbers the next position would be computed from. On a guest with two QXL
devices it is worse than useless, because the agent matches monitor-config
entries to devices in the guest's own enumeration order, which need not match
the display channels. Proxima sends no positions and no sizes. Each head's
widget reports its own size through `resize-guest`.

**A head's number is its display id.** The head list stays in channel order and
is never rearranged to put the tab's own head first, because the index into it
is the number spice-gtk and the guest's agent both use. The tab attaches to
whichever display channel turns up first, and on a two device guest that can be
channel 1. `set_head_enabled` refuses to touch the head the tab is showing,
whatever number it turns out to be.

Heads are handed back on the way out. A desktop left with a monitor it cannot
see can save that layout and come back to it after a reboot with its session on
a screen that is not there.

## Keyboard grabs

spice-gtk grabs the keyboard while the display has focus, and on Windows that
grab is a low level hook that keeps taking keys after another window is
focused. So the grab is tied to two things together: the pointer being over the
console, and one of this console's windows being active. Without the second,
hovering the console of a background window swallows what you are typing into
your browser.

The same focus behaviour is why the active pane follows clicks rather than
focus. The console takes focus when the pointer merely crosses it, so a window
that followed focus changed which guest the toolbar acted on as the pointer
passed over a pane on the way to a button.

## Two polling speeds, not a timer per action

The inventory poll picks between an idle cadence and a faster one while
something this window asked for has not been reported yet. That replaced a
fixed 2s interval with a second once a second timer laid over it after every
action, which meant an idle window talking to the server thirty times a minute
and an action answered by both timers at once.

## Terminals and framebuffers are ours

`proxima/console/vt.py` is the terminal emulator and `proxima/console/rfb.py`
the VNC client, for the same reason `proxima/ui/graphs.py` draws with cairo
rather than pulling in a charting library: nothing new to install, on either
platform. VTE is the obvious answer on Linux and does not exist on Windows,
since MSYS2 ships no `vte3` for any mingw target.

## SPICE connection parameters

The dict from `POST /nodes/<node>/qemu/<vmid>/spiceproxy` has quirks worth
restating, because none of them are guessable.

- `host` is not a hostname. Proxmox puts an opaque proxy ticket there and the
  real network target is `proxy`. spice-glib passes host through to the proxy
  verbatim.
- `host-subject` means certificate validation should check the subject, not the
  hostname. The hostname is that ticket string and will never match.
- The ticket in `password` is short lived, so connect promptly.

## Metadata in guest notes

Proxmox has no per guest storage for third party settings, so folders and the
server side per guest settings live in a delimited block inside the guest's
description. `proxima/api/notes.py` owns the format. Three rules keep it from
destroying somebody's notes: the block is found wherever it happens to be,
writing replaces it in place, and a malformed block is treated as absent rather
than reinterpreted.

Values equal to the default are dropped on save, so a guest nobody has
configured never grows a block at all.

## The header bar is removable on purpose

The in application titlebar is a trial feature behind `use_header_bar`, and
every change made for it is listed in the repository so it can be taken out
cleanly. It reads its setting once at startup because GTK decides between
client side and server side decorations when a window is created and does not
revisit it.
