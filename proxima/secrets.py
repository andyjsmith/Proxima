"""Storage for saved connection passwords.

A saved connection has to reconnect at startup without prompting, which means
the password has to be recoverable -- there is no way around that. What can
be avoided is leaving it readable in a plain JSON file.

On Windows, DPAPI (CryptProtectData) encrypts against the logged-in user
account, so the blob is useless to another user or on another machine, and it
needs no key management or third-party dependency. Everywhere else there is
no equivalent without pulling in a keyring library, so the value is merely
obfuscated and `is_secure()` says so -- the UI warns rather than implying a
protection that is not there.
"""

import base64
import ctypes
import ctypes.wintypes
import logging
import os

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]

    @classmethod
    def from_bytes(cls, data):
        buffer = ctypes.create_string_buffer(data, len(data))
        return cls(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    def value(self):
        return ctypes.string_at(self.pbData, self.cbData)


def is_secure():
    """Whether stored secrets are actually encrypted on this platform."""
    return IS_WINDOWS


def _dpapi(func_name, data, description=None):
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    source = _Blob.from_bytes(data)
    result = _Blob()
    args = [ctypes.byref(source)]
    if func_name == "CryptProtectData":
        args += [description, None, None, None, 0, ctypes.byref(result)]
    else:
        args += [None, None, None, None, 0, ctypes.byref(result)]

    if not getattr(crypt32, func_name)(*args):
        raise OSError(f"{func_name} failed")
    try:
        return result.value()
    finally:
        kernel32.LocalFree(result.pbData)


def encode(password):
    """Turn a password into a string safe to put in the settings file."""
    if not password:
        return ""
    raw = password.encode("utf-8")
    if IS_WINDOWS:
        try:
            return "dpapi:" + base64.b64encode(
                _dpapi("CryptProtectData", raw, "proxima")
            ).decode("ascii")
        except Exception as exc:
            log.warning("DPAPI unavailable (%s); storing obfuscated", exc)
    return "plain:" + base64.b64encode(raw).decode("ascii")


def decode(stored):
    """Recover a password stored by encode(). Returns '' if it cannot."""
    if not stored:
        return ""
    scheme, _, payload = stored.partition(":")
    try:
        raw = base64.b64decode(payload)
    except Exception:
        return ""
    if scheme == "dpapi":
        if not IS_WINDOWS:
            return ""
        try:
            return _dpapi("CryptUnprotectData", raw).decode("utf-8")
        except Exception:
            return ""
    if scheme == "plain":
        try:
            return raw.decode("utf-8")
        except Exception:
            return ""
    return ""
