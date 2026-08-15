# Proxima

[Documentation](https://andyjsmith.github.io/Proxima/docs/) | [Download](https://github.com/andyjsmith/proxima/releases/latest)

A VMware Workstation style desktop client for Proxmox VE. Your whole
datacenter in a tree down one side, guests in tabs, and a real console in
each tab -- SPICE for VMs that have it, VNC for the rest, and a proper
terminal for containers.

![Main window](/docs/static/img/screenshots/main_window.png)

Features:

* **Consoles in tabs**, or full screen across multiple monitors
* **Power, snapshots, and settings**, for quick changes without needing the web interface
* **USB devices** handed to a VM over SPICE, offered as you plug them in
* **Node pages** with the meters and graphs the web interface shows, and a
  root shell one click away
* **Folders and tags** for organizing guests across the whole datacenter

---

## Download

Download from the [releases
page](https://github.com/andyjsmith/proxima/releases/latest).

### Windows

| Type      | File                                         | Description                                                             |
| --------- | -------------------------------------------- | ----------------------------------------------------------------------- |
| Installer | `proxima-<version>-windows-x86_64-setup.exe` | Recommended. Installable as user or admin.                              |
| Portable  | `proxima-<version>-windows-x86_64.zip`       | The same program in a folder. Unpack it anywhere and run `proxima.exe`. |

The installer also offers **UsbDk**, which is required for USB redirection on
Windows.

### Linux

| Type                 | File                                    | Description                                                                      |
| -------------------- | --------------------------------------- | -------------------------------------------------------------------------------- |
| Universal (AppImage) | `Proxima-<version>-x86_64.AppImage`     | Any supported distribution. `chmod +x` it and run it.                            |
| Debian package       | `proxima_<version>_amd64.deb`           | Debian, Ubuntu, etc. `sudo apt install ./proxima_<version>_amd64.deb`            |
| RPM package          | `proxima-<version>-1.x86_64.rpm`        | Fedora, RHEL, openSUSE, etc. `sudo dnf install ./proxima-<version>-1.x86_64.rpm` |
| Portable archive     | `proxima-<version>-linux-x86_64.tar.gz` | Unpack anywhere and run `./proxima`                                              |

USB redirection on Linux needs read and write access to the device node.

### macOS

| Type | File                                | Description                                                           |
| ---- | ----------------------------------- | --------------------------------------------------------------------- |
| DMG  | `proxima-<version>-macos-arm64.dmg` | Apple silicon only. Open and drag **Proxima** into your Applications. |

### Checking what you downloaded

`SHA256SUMS` is published with every release.

```bash
sha256sum -c SHA256SUMS --ignore-missing        # Linux
```

```powershell
Get-FileHash proxima-0.2.0-windows-x86_64-setup.exe   # Windows, compare by eye
```

## Documentation

[View the documentation](https://andyjsmith.github.io/Proxima/docs/)

## Contributing

Building it, running it from source, the test suite, and how releases are cut
are in **[Developer docs](https://andyjsmith.github.io/Proxima/docs/development/)**.

## License

Proxima is MIT licensed; see [LICENSE](LICENSE). A package bundles GTK,
spice-gtk and GStreamer, which carry their own licenses -- those are listed
in `packaging/NOTICE.md` and shipped in `licenses/` inside every build.
