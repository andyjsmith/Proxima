# Third-party software in this package

Proxima is distributed as a self-contained bundle so that it needs nothing
installed alongside it. That means this package carries copies of other
people's software, and their terms travel with it. The full license text of
every component is in the `licenses/` directory beside the program.

The main components, and the terms they are under:

| Component | License |
| --- | --- |
| GTK 3, GLib, GdkPixbuf, Pango, ATK | LGPL-2.1-or-later |
| spice-gtk | LGPL-2.1-or-later |
| GStreamer core and plugins | LGPL-2.1-or-later |
| PyGObject | LGPL-2.1-or-later |
| cairo | LGPL-2.1-or-later or MPL-1.1 |
| Python | PSF-2.0 |
| openh264 | BSD-2-Clause |
| libvpx | BSD-3-Clause |
| libjpeg-turbo | BSD-like |
| HarfBuzz | MIT |
| FreeType | FTL (or GPL-2.0-or-later, at your option) |
| fontconfig, libpng, zlib | permissive, see `licenses/` |

None of these libraries have been modified. They are the stock builds from
Debian (Linux packages) and MSYS2 UCRT64 (Windows packages), and the source
for any of them can be obtained from those projects, or from us on request
for as long as this version is distributed.

The LGPL libraries are linked dynamically and shipped as ordinary shared
libraries in this bundle, so they can be replaced with your own build: drop a
compatible `.so`/`.dll` in over ours and the program will use it.

Proxima's own source code is at the repository this was built from; the
license for that is in the repository root.
