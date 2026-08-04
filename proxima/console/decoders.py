"""GStreamer decoder ranking.

Hardware-accelerated decoders arrive with gst-plugins-bad and outrank the
software ones. If a D3D/NVDEC/Vulkan decode session wedges it can break GPU
decoding for other processes until the machine reboots, so there is a setting
to demote them. It has to be applied before Gst.init(), which spice-gtk calls
lazily on the first streamed frame.
"""

import os

HARDWARE_DECODERS = [
    "d3d11h264dec",
    "d3d12h264dec",
    "nvh264dec",
    "vulkanh264dec",
    "d3d11h265dec",
    "d3d12h265dec",
    "nvh265dec",
    "vulkanh265dec",
    "d3d11vp9dec",
    "d3d12vp9dec",
    "nvvp9dec",
    "vulkanvp9dec",
    "nvvp8dec",
    "nvjpegdec",
]

GST_ELEMENT_PACKAGES = {
    "vp8dec": "gst-plugins-good",
    "vp9dec": "gst-plugins-good",
    "jpegdec": "gst-plugins-good",
    "autoaudiosink": "gst-plugins-good",
    "autoaudiosrc": "gst-plugins-good",
    "avdec_h264": "gst-libav",
}


def demote_hardware_decoders():
    existing = os.environ.get("GST_PLUGIN_FEATURE_RANK", "")
    ranks = ",".join(f"{name}:NONE" for name in HARDWARE_DECODERS)
    os.environ["GST_PLUGIN_FEATURE_RANK"] = f"{existing},{ranks}" if existing else ranks
    print("[spice] hardware decoders demoted; software decoding only")


def gstreamer_report():
    """Which video decoders and audio sinks spice-gtk can actually use.

    Missing decoders make the client advertise no codec support, so the
    server falls back to MJPEG (artifacts plus buffering). A missing audio
    sink makes spice-gtk fail to build its playback pipeline, which can also
    stall the main loop.
    """
    lines = []
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
    except Exception as exc:
        return [
            f"GStreamer unavailable: {exc}",
            (
                "  -> pacman -S mingw-w64-ucrt-x86_64-gstreamer "
                "mingw-w64-ucrt-x86_64-gst-plugins-good "
                "mingw-w64-ucrt-x86_64-gst-libav"
            ),
        ]

    # Several elements can decode the same thing, and which one is present
    # depends on how the build was put together -- a packaged build carries
    # openh264 rather than ffmpeg, for size and licensing. Any of them
    # answering means the codec works, so the first one found is reported.
    wanted = [
        ("VP8", ["vp8dec"]),
        ("VP9", ["vp9dec"]),
        ("H.264", ["avdec_h264", "openh264dec", "d3d11h264dec", "nvh264dec"]),
        ("MJPEG", ["jpegdec"]),
        ("audio out", ["autoaudiosink"]),
        ("audio in", ["autoaudiosrc"]),
    ]

    packages = set()
    for label, elements in wanted:
        found = next(
            (e for e in elements if Gst.ElementFactory.find(e) is not None), None
        )
        lines.append(
            f"  {label:<9} {found or elements[0]:<14} "
            f"{'available' if found else 'MISSING'}"
        )
        if found is None:
            packages.update(
                GST_ELEMENT_PACKAGES[e] for e in elements if e in GST_ELEMENT_PACKAGES
            )

    for package in sorted(packages):
        lines.append(f"  -> pacman -S mingw-w64-ucrt-x86_64-{package}")
    return lines
