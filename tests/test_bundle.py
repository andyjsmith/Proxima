"""The runtime fix-ups a packaged build needs.

None of this runs from a source checkout, so the tests drive it directly
against a fake bundle rather than waiting for a build.
"""

import os

import pytest

from proxima import bundle

# The two shapes that turn up in the wild: Debian writes absolute paths into
# the loader cache, MSYS2 writes them relative to its prefix. Neither
# survives being copied into a bundle.
DEBIAN_CACHE = """\
# GdkPixbuf Image Loader Modules file
# Automatically generated file, do not edit
#
# LoaderDir = /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/2.10.0/loaders
#
"/usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/2.10.0/loaders/libpixbufloader-png.so"
"png" 5 "gdk-pixbuf" "PNG" "LGPL"
"image/png" ""
"png" ""
"""

MSYS2_CACHE = """\
# GdkPixbuf Image Loader Modules file
#
"lib\\\\gdk-pixbuf-2.0\\\\2.10.0\\\\loaders\\\\libpixbufloader-png.dll"
"png" 5 "gdk-pixbuf" "PNG" "LGPL"
"image/png" ""
"""


@pytest.fixture
def loaders(tmp_path):
    directory = tmp_path / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders"
    directory.mkdir(parents=True)
    (directory / "libpixbufloader-png.so").write_bytes(b"")
    (directory / "libpixbufloader-png.dll").write_bytes(b"")
    return directory


@pytest.mark.parametrize("cache", [DEBIAN_CACHE, MSYS2_CACHE], ids=["debian", "msys2"])
def test_the_loader_path_is_repointed_at_the_bundle(loaders, cache):
    rewritten = bundle.rewrite_loader_cache(cache, loaders)
    module_line = next(
        line
        for line in rewritten.splitlines()
        if "libpixbufloader-png" in line and line.startswith('"')
    )
    assert str(loaders).replace("\\", "\\\\") in module_line


def test_everything_that_is_not_a_module_path_is_left_alone(loaders):
    rewritten = bundle.rewrite_loader_cache(DEBIAN_CACHE, loaders)
    assert '"png" 5 "gdk-pixbuf" "PNG" "LGPL"' in rewritten
    assert '"image/png" ""' in rewritten
    assert rewritten.startswith("# GdkPixbuf Image Loader Modules file")


def test_a_loader_missing_from_the_bundle_is_not_invented(loaders):
    cache = DEBIAN_CACHE.replace("libpixbufloader-png.so", "libpixbufloader-tiff.so")
    rewritten = bundle.rewrite_loader_cache(cache, loaders)
    # Pointing at a file that is not there would turn a missing format into a
    # loader that fails at run time.
    assert str(loaders) not in rewritten


def test_a_source_checkout_is_left_completely_alone(monkeypatch):
    monkeypatch.setattr(bundle, "is_bundled", lambda: False)
    before = dict(os.environ)
    assert bundle.apply() == {}
    assert dict(os.environ) == before


def build_fake_bundle(root):
    (root / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders").mkdir(parents=True)
    (root / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders.cache").write_text(
        DEBIAN_CACHE, encoding="utf-8"
    )
    (root / "lib" / "gstreamer-1.0").mkdir(parents=True)
    (root / "lib" / "gio" / "modules").mkdir(parents=True)
    schemas = root / "share" / "glib-2.0" / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "gschemas.compiled").write_bytes(b"")
    (root / "share" / "icons").mkdir(parents=True)
    return root


def test_a_bundle_points_gtk_at_its_own_data(tmp_path, monkeypatch):
    root = build_fake_bundle(tmp_path / "dist")
    monkeypatch.setenv("PROXIMA_CONFIG_DIR", str(tmp_path / "config"))
    for name in (
        "GDK_PIXBUF_MODULE_FILE",
        "GDK_PIXBUF_MODULEDIR",
        "GST_PLUGIN_SYSTEM_PATH",
        "GSETTINGS_SCHEMA_DIR",
        "GIO_MODULE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_DATA_DIRS", "/usr/share")

    applied = bundle.apply(root)

    assert applied["GDK_PIXBUF_MODULEDIR"].startswith(str(root))
    assert applied["GST_PLUGIN_SYSTEM_PATH"] == str(root / "lib" / "gstreamer-1.0")
    assert applied["GSETTINGS_SCHEMA_DIR"].startswith(str(root))
    assert applied["GIO_MODULE_DIR"].startswith(str(root))
    # The rewritten cache is written somewhere writable, not into the bundle,
    # which may sit in Program Files.
    assert applied["GDK_PIXBUF_MODULE_FILE"].startswith(str(tmp_path / "config"))
    # The desktop's own icons and mime types are kept, just not trusted to be
    # the only ones.
    assert applied["XDG_DATA_DIRS"].split(os.pathsep) == [
        str(root / "share"),
        "/usr/share",
    ]


def test_an_explicit_setting_in_the_environment_wins(tmp_path, monkeypatch):
    root = build_fake_bundle(tmp_path / "dist")
    monkeypatch.setenv("PROXIMA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GST_PLUGIN_SYSTEM_PATH", "/somewhere/else")

    applied = bundle.apply(root)

    assert "GST_PLUGIN_SYSTEM_PATH" not in applied
    assert os.environ["GST_PLUGIN_SYSTEM_PATH"] == "/somewhere/else"


def test_the_report_names_what_is_missing(tmp_path):
    root = tmp_path / "dist"
    (root / "share" / "icons").mkdir(parents=True)
    text = "\n".join(bundle.report(root))
    assert "[MISSING] pixbuf loaders" in text
    assert "[ok] icon themes" in text
    # Absent on Linux by design, so it must not read as a broken build.
    assert "[not bundled] fontconfig" in text
