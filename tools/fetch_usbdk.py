#!/usr/bin/env python3
"""Download the UsbDk installer for the Windows package to carry.

USB redirection needs a kernel driver on Windows, and telling somebody who
has just installed a VM client to go and install a second thing before the
feature works is a poor way to ship it. So the installer offers to put it
on, which means the MSI has to be in the installer, which means fetching it
at package time:

    python3 tools/fetch_usbdk.py build/usbdk/UsbDk_x64.msi

Prints the path it wrote. Exits non-zero if it could not get a plausible
MSI, so a release either carries a working driver or fails loudly rather
than shipping a checkbox that installs nothing.

Deliberately stdlib-only: it runs on the packaging runner, which has no
business growing a requirements file for one download.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "daynix/UsbDk"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"

# Every Windows build Proxima ships is 64-bit.
WANTED_SUFFIX = "_x64.msi"

# An MSI is an OLE2 compound document, and this is what one starts with. The
# check is not about trust -- it is about noticing that what arrived was an
# error page or half a file before it is baked into an installer.
MSI_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MIN_BYTES = 1_000_000


def _get(url, accept="application/octet-stream"):
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": "proxima-packaging"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def latest_asset():
    """(name, url, tag) for the newest x64 MSI."""
    payload = json.loads(_get(LATEST, "application/vnd.github+json").decode("utf-8"))
    tag = payload.get("tag_name") or "?"
    for asset in payload.get("assets") or []:
        name = asset.get("name") or ""
        if name.endswith(WANTED_SUFFIX):
            return name, asset["browser_download_url"], tag
    raise SystemExit(f"no *{WANTED_SUFFIX} asset in {REPO} {tag}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="where to write the .msi")
    args = parser.parse_args()

    try:
        name, url, tag = latest_asset()
        print(f"UsbDk {tag}: {name}", file=sys.stderr)
        payload = _get(url)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        raise SystemExit(f"could not fetch UsbDk: {exc}") from None

    if len(payload) < MIN_BYTES or not payload.startswith(MSI_MAGIC):
        raise SystemExit(
            f"what came back from {url} is not an MSI "
            f"({len(payload)} bytes, starts {payload[:8]!r})"
        )

    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
