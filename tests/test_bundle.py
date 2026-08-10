"""The runtime fix-ups a packaged build still needs.

None of this runs from a source checkout, so the tests drive it directly
against a fake bundle rather than waiting for a build. PyInstaller's own
runtime hooks do nearly all of the work; what is left here is the two things
they do not cover, so those are what is tested.
"""

import os
import sys

import pytest

from proxima import bundle


@pytest.fixture
def fake_bundle(tmp_path, monkeypatch):
    """A bundle laid out the way PyInstaller lays one out."""
    root = tmp_path / "_internal"
    fonts = root / "etc" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "fonts.conf").write_text("<fontconfig/>", encoding="utf-8")
    (root / "share" / "icons").mkdir(parents=True)
    monkeypatch.setenv("PROXIMA_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("FONTCONFIG_PATH", "FONTCONFIG_FILE", "GST_REGISTRY"):
        monkeypatch.delenv(name, raising=False)
    return root


def test_a_source_checkout_is_left_completely_alone(monkeypatch):
    monkeypatch.setattr(bundle, "is_bundled", lambda: False)
    before = dict(os.environ)
    assert bundle.apply() == {}
    assert dict(os.environ) == before


def test_fontconfig_is_pointed_at_the_bundles_own_configuration(fake_bundle):
    applied = bundle.apply(fake_bundle)

    fonts = fake_bundle / "etc" / "fonts"
    assert applied["FONTCONFIG_PATH"] == str(fonts)
    assert applied["FONTCONFIG_FILE"] == str(fonts / "fonts.conf")


def test_a_bundle_without_fontconfig_invents_nothing(fake_bundle):
    (fake_bundle / "etc" / "fonts" / "fonts.conf").unlink()

    applied = bundle.apply(fake_bundle)

    assert "FONTCONFIG_FILE" not in applied


def test_an_explicit_setting_in_the_environment_wins(fake_bundle, monkeypatch):
    monkeypatch.setenv("FONTCONFIG_FILE", "/somewhere/else/fonts.conf")

    applied = bundle.apply(fake_bundle)

    assert "FONTCONFIG_FILE" not in applied
    assert os.environ["FONTCONFIG_FILE"] == "/somewhere/else/fonts.conf"


def test_the_gstreamer_registry_is_moved_out_of_the_bundle(fake_bundle, monkeypatch):
    """PyInstaller puts it inside the bundle, which may be in Program Files.

    Left there, an installed copy cannot write it and rescans every plugin on
    every start.
    """
    monkeypatch.setenv("GST_REGISTRY", str(fake_bundle / "registry.bin"))

    applied = bundle.apply(fake_bundle)

    assert applied["GST_REGISTRY"] == os.environ["GST_REGISTRY"]
    assert applied["GST_REGISTRY"].startswith(str(fake_bundle.parent / "config"))
    # And somewhere that exists, or GStreamer simply fails to write it.
    assert os.path.isdir(os.path.dirname(applied["GST_REGISTRY"]))


def test_a_registry_pointed_outside_the_bundle_is_left_alone(fake_bundle, monkeypatch):
    """A deliberate override, or a platform whose hook did not set one."""
    monkeypatch.setenv("GST_REGISTRY", str(fake_bundle.parent / "mine.bin"))

    applied = bundle.apply(fake_bundle)

    assert "GST_REGISTRY" not in applied
    assert os.environ["GST_REGISTRY"] == str(fake_bundle.parent / "mine.bin")


def test_the_report_names_what_is_missing(tmp_path):
    root = tmp_path / "_internal"
    (root / "share" / "icons").mkdir(parents=True)
    text = "\n".join(bundle.report(root))
    assert "[MISSING] pixbuf loaders" in text
    assert "[ok] icon themes" in text
    # Supplied by every desktop on Linux, so it must not read as a broken
    # build there -- and it is the whole of font rendering on Windows.
    expected = (
        "[MISSING] fontconfig"
        if sys.platform == "win32"
        else "[not bundled] fontconfig"
    )
    assert expected in text


# -- the version the bundle reports --------------------------------------


def test_the_version_is_read_out_of_pyproject():
    """A checkout reports the version pyproject carries, not the fallback.

    The bundle carries pyproject.toml for exactly this read, so a build whose
    version came back "0.0.0+unknown" would be a release that cannot say what
    it is.
    """
    import pathlib

    import proxima

    text = (pathlib.Path(proxima.__file__).parent.parent / "pyproject.toml").read_text()
    assert proxima.__version__ == proxima._version_in(text)
    assert proxima.__version__ != "0.0.0+unknown"


def test_only_the_project_table_supplies_the_version():
    """No TOML parser is involved, so the table has to be tracked by hand."""
    from proxima import _version_in

    assert (
        _version_in(
            '[tool.poetry]\nversion = "9.9.9"\n'
            '[project]\nname = "proxima"\nversion = "1.2.3"\n'
            '[tool.other]\nversion = "0.0.1"\n'
        )
        == "1.2.3"
    )
    # A file with no [project] table at all, which must not raise.
    assert _version_in('[tool.ruff]\nversion = "9.9.9"\n') is None
    assert _version_in("") is None
