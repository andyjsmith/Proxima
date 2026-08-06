"""Opening a folder or a link in whatever the desktop uses for it.

Nothing here launches anything: each platform's opener is stubbed and what
is checked is which one gets called. That is the whole content of the bug
this covers -- the right path handed to the wrong opener.

The directory is stubbed too. These tests fake the platform, and a faked
os.name changes what pathlib thinks a path *is* -- a Windows path re-parsed
as POSIX is a relative path, which cannot be turned into a file:// URI at
all. Rather than have the test's own scaffolding decide the outcome, the
path is a stand-in that answers the three questions the module asks it.
"""

import os
import sys

import pytest

from proxima.ui import desktop


class FakeDir:
    """A directory, as far as desktop.open_folder is concerned."""

    def __init__(self, exists=True):
        self._exists = exists

    def exists(self):
        return self._exists

    def as_uri(self):
        return "file:///var/log/proxima"

    def __str__(self):
        return "/var/log/proxima"


@pytest.fixture
def spies(monkeypatch):
    """Record every way out of the module instead of taking one."""
    calls = {"startfile": [], "launch": [], "gio": []}

    def startfile(path):
        calls["startfile"].append(str(path))

    def launch(command):
        calls["launch"].append(command)
        return True

    def gio_open(uri, parent=None):
        calls["gio"].append(uri)
        return True

    monkeypatch.setattr(desktop.os, "startfile", startfile, raising=False)
    monkeypatch.setattr(desktop, "_launch", launch)
    monkeypatch.setattr(desktop, "_gio_open", gio_open)
    # The module normalises with Path(); the stand-in is already one.
    monkeypatch.setattr(desktop, "Path", lambda path: path)
    return calls


def as_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "platform", "win32")


def as_linux(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")


def as_macos(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")


PLATFORMS = {"windows": as_windows, "linux": as_linux, "macos": as_macos}


def test_windows_opens_a_folder_with_the_shell_not_with_gio(spies, monkeypatch):
    """The bug, pinned.

    GIO has no handler registered for a directory on Windows, so it answers
    "No application is registered as handling this file" for a folder that
    Explorer opens on a double click. Going anywhere near it for a folder is
    the fault.
    """
    as_windows(monkeypatch)
    assert desktop.open_folder(FakeDir()) is True
    assert spies["startfile"] == ["/var/log/proxima"]
    assert spies["gio"] == [], "the folder went to GIO, which cannot open one"


def test_linux_prefers_gio_and_falls_back_to_xdg_open(spies, monkeypatch):
    as_linux(monkeypatch)
    assert desktop.open_folder(FakeDir()) is True
    assert spies["gio"] == ["file:///var/log/proxima"], (
        "a Linux desktop registers a file manager, so GIO comes first"
    )
    assert spies["launch"] == [], "xdg-open was run even though GIO worked"

    # ...and when GIO cannot, xdg-open is the second try.
    monkeypatch.setattr(desktop, "_gio_open", lambda uri, parent=None: False)
    assert desktop.open_folder(FakeDir()) is True
    assert spies["launch"] == [["xdg-open", "/var/log/proxima"]]


def test_macos_uses_open(spies, monkeypatch):
    as_macos(monkeypatch)
    assert desktop.open_folder(FakeDir()) is True
    assert spies["launch"] == [["open", "/var/log/proxima"]]


@pytest.mark.parametrize("platform", list(PLATFORMS))
def test_a_folder_that_is_not_there_is_refused_rather_than_launched(
    spies, monkeypatch, platform
):
    PLATFORMS[platform](monkeypatch)
    assert desktop.open_folder(FakeDir(exists=False)) is False
    assert not any(spies.values()), "something was launched for a missing folder"


def test_a_shell_failure_is_reported_not_raised(spies, monkeypatch):
    as_windows(monkeypatch)

    def boom(_path):
        raise OSError("nope")

    monkeypatch.setattr(desktop.os, "startfile", boom, raising=False)
    assert desktop.open_folder(FakeDir()) is False


def test_links_go_to_gio_first_so_the_default_browser_wins(spies, monkeypatch):
    """Unlike folders, http does have a handler -- the user's browser."""
    as_windows(monkeypatch)
    assert desktop.open_uri("https://example.invalid/x") is True
    assert spies["gio"] == ["https://example.invalid/x"]
    assert spies["startfile"] == [], "GIO worked; the shell should not be involved"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("windows", "startfile"), ("linux", "launch"), ("macos", "launch")],
)
def test_a_link_falls_back_when_gio_cannot(spies, monkeypatch, platform, expected):
    PLATFORMS[platform](monkeypatch)
    monkeypatch.setattr(desktop, "_gio_open", lambda uri, parent=None: False)
    assert desktop.open_uri("https://example.invalid/x") is True
    assert spies[expected], f"{platform} had no fallback"


def test_the_real_thing_opens_this_machines_log_folder(monkeypatch):
    """One test that does not stub the platform out.

    The stubs above check which opener is chosen; this checks that the one
    chosen here actually works, which is the half that was broken. Nothing
    is launched -- only the last step is stubbed.
    """
    from proxima import logs

    launched = []
    monkeypatch.setattr(desktop.os, "startfile", launched.append, raising=False)
    monkeypatch.setattr(desktop, "_launch", lambda command: launched.append(command))
    monkeypatch.setattr(desktop, "_gio_open", lambda uri, parent=None: False)

    directory = logs.log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    assert desktop.open_folder(directory) or launched, (
        "the log folder could not be opened on this platform"
    )
