---
title: Audio and microphone
sidebar_position: 3
---

# Audio and microphone

Audio requres a SPICE console and an audio device on the VM.

## Adding an audio device

Proxmox adds none by default. In the web interface, **Hardware > Add > Audio Device**, with backend SPICE. 

From the command line:

```bash
qm set 101 --audio0 device=ich9-intel-hda,driver=spice
```

The device choices are `ich9-intel-hda`, `intel-hda` and `AC97`. Use
`ich9-intel-hda` unless the guest is old enough to need another. It carries an
input as well as an output, which the microphone needs.

The driver has to be `spice`.

Adding or changing `audio0` needs the VM stopped and started again.

## Playback

The Audio indicator in the [status bar](../using/status-bar.md) reports the
state and toggles it. The setting is per guest and stored locally.

**Preferences > Console > Enable audio** turns sound off for every console at
once.

Switching playback needs the console to reconnect. Audio is set up when the SPICE
session is created, so the change is applied by rebuilding.

## Microphone

Microphone passthrough is off by default.

Turn it on from the Microphone indicator, or from **Settings > Proxmox
Manager > Microphone** under **This computer only**.

## Audio faults

A broken audio pipeline on this machine can stall the window rather than just
playing nothing, which is one reason the global switch exists. Turn **Enable
audio** off to rule it out. `proxima --diagnose` reports the audio and video
support the installed copy has. See [Troubleshooting](../troubleshooting.md).
