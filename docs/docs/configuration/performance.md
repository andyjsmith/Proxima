---
title: Console performance
sidebar_position: 4
---

# Console performance

## Display scaling

On SPICE this is the one with the largest effect, because it changes how many pixels the guest renders in the first place. On a HiDPI display, setting **View > Display Scaling** to 200% can dramatically improve performance. See [Consoles](../using/consoles.md#display-scaling) for further details.

## Video codec

**View > Video Codec** asks the SPICE server to prefer one codec for the video streams it detects.

|                |                                                                                    |
| -------------- | ---------------------------------------------------------------------------------- |
| server default | Whatever the host chose.                                                           |
| MJPEG          | Every frame a JPEG. Cheap to decode, expensive on bandwidth.                       |
| VP8            | Widely supported, moderate cost.                                                   |
| H.264          | Usually the best quality per bit, and the most likely to be hardware decoded here. |
| VP9            | Better compression than VP8 at more CPU cost.                                      |

It is a preference, not an instruction. A QEMU built without a given encoder ignores the request. The choice is remembered per guest.

## Image compression

**View > Image Compression** covers the still parts of the screen, which on a desktop is most of it.

|                |                                                                                                    |
| -------------- | -------------------------------------------------------------------------------------------------- |
| server default |                                                                                                    |
| off (lossless) | No compression. Only sensible on a LAN.                                                            |
| auto GLZ       | Global LZ with history across frames. A good default for a desktop.                                |
| QUIC           | Better on photographic content.                                                                    |
| LZ4            | Fastest to compress and decompress, larger output. Worth trying when the *host* is the bottleneck. |

## Bandwidth

**View > Bandwidth** can asks the guest to:

- Disable Wallpaper
- Disable Font Smoothing
- Disable Animation

Depending on the guest, these may or may not actually do anything.

## Software decoding only

**Preferences > Console > Software video decoding only** demotes this
machine's hardware video decoders so the software ones are chosen instead.
Restart required, because the decoder ranking is read once at startup.

Reach for it when video is corrupt, tears, or crashes the console.

On macOS the VideoToolbox decoder is deliberately left alone, because it is the
only H.264 decoder there. Demoting it would leave the setting with no H.264 at
all rather than with a slower path.

## Measuring the effect

Hover the protocol label in the [status bar](../using/status-bar.md). Its
tooltip carries throughput, frame rate and the guest's resolution, sampled every
second. Change one thing at a time and watch the number.
