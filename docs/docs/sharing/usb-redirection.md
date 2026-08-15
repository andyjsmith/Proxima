---
title: USB redirection
sidebar_position: 2
---

# USB redirection

A USB device plugged into this computer can be handed to a VM over SPICE. This
computer loses the device while it is redirected and gets it back when you
release it or close the console.

Three ways pass a device through:

- **VM > USB Devices**, which lists what is plugged in here.
- The USB indicator in the [status bar](../using/status-bar.md).
- The prompt Proxima raises when you plug something in while a console is in
  front of you.

VNC has no channel to carry a device, so redirection is offered on SPICE only.

## Requirements

### A SPICE USB port on the VM

Proxmox adds none. In the web interface, **Hardware > Add > USB Device > Spice
Port**, which writes `usb0: spice` into the VM config. One line carries one
device at a time. Add a second line for a second simultaneous device.

From the command line:

```bash
qm set 101 --usb0 spice
qm set 101 --usb1 spice
```

### The UsbDk driver on Windows

The installer offers it on its components page, or install it from
[daynix/UsbDk](https://github.com/daynix/UsbDk/releases/latest).

Without the driver the device list still looks healthy, and Windows then
refuses to hand a device over when it is claimed.

Uninstalling Proxima leaves the driver in place, because virt-viewer and other
SPICE clients use the same one.

### Device access on Linux

Proxima claims the device itself rather than through a system service, so your
user has to be able to open the device node under `/dev/bus/usb`. Distributions
shipping `spice-client-glib-usb-acl-helper` handle this through PolicyKit.
Without it, add a udev rule or a group membership granting access to the device.

## Hotplug prompt

Plug a device in while a console is in front and Proxima offers to hand it to
that guest. Turn it off with **Preferences > Console > Ask when a USB device is plugged
in**.
