"""The login dialog builds, and knows how to read a host field."""

import pytest

from proxima.ui.login_dialog import LoginDialog

from .conftest import make_config, pump


@pytest.fixture
def login():
    dialog = LoginDialog(None, make_config())
    pump(0.2)
    yield dialog
    dialog.destroy()


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("https://10.0.0.5:8006/#v1", ("10.0.0.5", 8006)),
        ("pve.local", ("pve.local", 8006)),
        ("[fd00::1]:8007", ("fd00::1", 8007)),
    ],
)
def test_split_host(login, typed, expected):
    assert login._split_host(typed) == expected
