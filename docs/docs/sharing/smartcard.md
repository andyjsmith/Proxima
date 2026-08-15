---
title: Smartcard redirection
sidebar_position: 4
---

# Smartcard redirection

Smartcard redirection is disabled by default. SPICE carries a card reader from this machine into the guest as a CCID device,
so a card in your reader is a card in the VM.

There are three requirements:

## 1. A local reader

Proxima lists the readers it can see and names them in the tooltip. On
Linux and macOS that means `pcscd` is running and can see the reader. On
Windows the smartcard service is present by default.

With no reader, the indicator reads "no smartcard reader on this machine".

## 2. A CCID device on the VM

Proxmox adds none by default and has no field for it in the web interface, so
it goes in the VM's `args` line.

```bash
qm set 101 --args "-device usb-ccid,id=ccid0 -chardev spicevmc,id=ccid,name=smartcard -device ccid-card-passthru,chardev=ccid"
```

`ccid-card-passthru` is the part that matters. It passes the client's reader
through rather than emulating a card from certificates on the host.

The usual `args` caveats apply. It replaces the whole line, so a VM that
already has `args` needs both sets of arguments in one string, and it needs the
VM stopped and started rather than rebooted from inside. Check it with
`qm showcmd 101`.

The guest also needs a driver stack that talks to a CCID reader, which on
Linux means `pcscd` and `ccid`, and on Windows is built in.

## 3. The switch

Turn it on from the Smartcard indicator in the status bar, or from
**Settings > Proxmox Manager > Smartcard** under **This computer only**. It is
stored per guest on this machine, because the reader is attached to this
machine.

Smartcard support is set up when the session is built, so switching it
reconnects the console.

