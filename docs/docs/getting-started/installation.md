---
title: Installation
sidebar_position: 1
---

# Installation

Builds are published in [GitHub releases](https://github.com/andyjsmith/proxima/releases/latest).
This is the recommended way to run Proxima. All dependencies are bundled into the installer/image.

## Windows

| Type      | File                                         | Description                                                             |
| --------- | -------------------------------------------- | ----------------------------------------------------------------------- |
| Installer | `proxima-<version>-windows-x86_64-setup.exe` | Recommended. Installable as user or admin.                              |
| Portable  | `proxima-<version>-windows-x86_64.zip`       | The same program in a folder. Unpack it anywhere and run `proxima.exe`. |

The installer also offers **UsbDk**, which is required for USB redirection on
Windows. See [USB redirection](../sharing/usb-redirection.md) for more information.

## Linux

| Type                 | File                                    | Description                                                                      |
| -------------------- | --------------------------------------- | -------------------------------------------------------------------------------- |
| Universal (AppImage) | `Proxima-<version>-x86_64.AppImage`     | Any supported distribution. `chmod +x` it and run it.                            |
| Debian package       | `proxima_<version>_amd64.deb`           | Debian, Ubuntu, etc. `sudo apt install ./proxima_<version>_amd64.deb`            |
| RPM package          | `proxima-<version>-1.x86_64.rpm`        | Fedora, RHEL, openSUSE, etc. `sudo dnf install ./proxima-<version>-1.x86_64.rpm` |
| Portable archive     | `proxima-<version>-linux-x86_64.tar.gz` | Unpack anywhere and run `./proxima`                                              |

USB redirection on Linux needs read and write access to the device node. See
[USB redirection](../sharing/usb-redirection.md) for more information.

## macOS

| Type | File                                | Description                                                           |
| ---- | ----------------------------------- | --------------------------------------------------------------------- |
| DMG  | `proxima-<version>-macos-arm64.dmg` | Apple silicon only. Open and drag **Proxima** into your Applications. |

:::warning[First open]

The app is signed but not notarized, so the first open is refused. **System
Settings > Privacy & Security** then offers an **Open Anyway** button for it.

:::

:::info[Intel Macs]

While Intel x86 builds aren't published for macOS, Proxima does run on Intel Macs. You will need to follow the developer documentation or clone the repo, install all dependencies, and run the Python application directly.

:::

## Verifying downloads

`SHA256SUMS` is published with every release.

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

```powershell
Get-FileHash proxima-0.2.0-windows-x86_64-setup.exe
```

## Command line options

```shell
proxima                  # normal start
proxima --diagnose       # check the console and video support, then exit
proxima --logs           # print the log directory and exit
proxima --debug          # log everything to the console as well as the log file
proxima --fontconfig     # force the FreeType font backend
```

`--diagnose` is the first thing to run when a console will not open. It reports
what the installed copy can do, which video decoders it has, and whether a
SPICE session can be created at all. See
[Troubleshooting](../troubleshooting.md).

## Upgrading

By default, Proxima checks for new releases on launch. Download the new installer and install the latest update.

You can turn off update checking in **Preferences > Behaviour**, or run it on demand from
**Help > Check for Updates**.
