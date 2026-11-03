"""Encoder table, ffmpeg argument building and hardware probing."""

import glob
import os
import subprocess
import sys

from . import settings

# (ffmpeg encoder, label, family, codec, max dimension)
# Within a family, earlier entries are preferred. Cross-family order is decided
# by fallback_order() from the GPUs actually present.
ENCODERS = [
    ("hevc_nvenc", "H.265 · NVIDIA NVENC", "nvenc", "hevc", 8192),
    ("h264_nvenc", "H.264 · NVIDIA NVENC", "nvenc", "h264", 4096),
    ("av1_nvenc", "AV1 · NVIDIA NVENC", "nvenc_av1", "av1", 8192),
    ("hevc_vaapi", "H.265 · VA-API (AMD/Intel)", "vaapi", "hevc", 8192),
    ("h264_vaapi", "H.264 · VA-API (AMD/Intel)", "vaapi", "h264", 4096),
    ("hevc_qsv", "H.265 · Intel Quick Sync", "qsv", "hevc", 8192),
    ("h264_qsv", "H.264 · Intel Quick Sync", "qsv", "h264", 4096),
    ("hevc_amf", "H.265 · AMD AMF", "amf", "hevc", 8192),
    ("h264_amf", "H.264 · AMD AMF", "amf", "h264", 4096),
    ("hevc_videotoolbox", "H.265 · Apple VideoToolbox", "vt", "hevc", 8192),
    ("h264_videotoolbox", "H.264 · Apple VideoToolbox", "vt", "h264", 4096),
    ("libx264", "H.264 · CPU (x264)", "x264", "h264", 8192),
    ("libx265", "H.265 · CPU (x265)", "x265", "hevc", 8192),
    ("libsvtav1", "AV1 · CPU (SVT-AV1)", "svtav1", "av1", 8192),
]
BY_ID = {e[0]: e for e in ENCODERS}

QUALITIES = [("high", "High"), ("medium", "Balanced"), ("low", "Small file")]
_QI = {"high": 0, "medium": 1, "low": 2}

COLOR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709",
              "-color_trc", "bt709", "-color_range", "tv"]

# RGB -> limited-range BT.709 YUV, done once in swscale so every encoder gets
# identical, correctly tagged colours.
_CONVERT = "scale=out_color_matrix=bt709:out_range=tv"

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# Vendor-native encoder families, in the order vendors are tried when a
# machine has more than one GPU (discrete before Intel's iGPU).
VENDOR_FAMILIES = [("nvidia", ("nvenc", "nvenc_av1")), ("amd", ("amf",)),
                   ("intel", ("qsv",)), ("apple", ("vt",))]
SOFTWARE_FAMILIES = ("x264", "x265", "svtav1")
_PCI_VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x1022": "amd", "0x8086": "intel"}


def detect_gpu_vendors():
    """Best-effort list of GPU vendors present ("nvidia", "amd", "intel", "apple")."""
    found = set()
    if sys.platform == "darwin":
        found.add("apple")
    elif os.name == "nt":
        try:
            out = run_quiet(["powershell", "-NoProfile", "-Command",
                             "(Get-CimInstance Win32_VideoController).Name"], 15).stdout
            text = out.decode("utf-8", "replace").lower()
            for needle, vendor in (("nvidia", "nvidia"), ("amd", "amd"), ("radeon", "amd"),
                                   ("intel", "intel")):
                if needle in text:
                    found.add(vendor)
        except Exception:
            pass
    else:
        for vendor_file in glob.glob("/sys/class/drm/card*/device/vendor"):
            try:
                with open(vendor_file) as f:
                    vendor = _PCI_VENDORS.get(f.read().strip().lower())
            except OSError:
                continue
            if vendor:
                found.add(vendor)
    return [v for v, _ in VENDOR_FAMILIES if v in found]


def fallback_order(vendors):
    """Encoder ids in try-order: the detected vendors' native encoders
    (NVENC / AMF / Quick Sync / VideoToolbox), then VA-API, then software."""
    families = []
    for vendor, fams in VENDOR_FAMILIES:
        # If detection found nothing, still try every native family; the
        # test encode weeds out the ones without hardware behind them.
        if not vendors or vendor in vendors:
            families.extend(fams)
    families.append("vaapi")
    families.extend(SOFTWARE_FAMILIES)
    return [e[0] for fam in families for e in ENCODERS if e[2] == fam]


def codec_of(encoder):
    return BY_ID[encoder][3] if encoder in BY_ID else ""


def max_dim(encoder):
    return BY_ID[encoder][4] if encoder in BY_ID else 4096


def input_args(encoder):
    """Arguments that must precede the first -i."""
    if BY_ID[encoder][2] == "vaapi":
        return ["-vaapi_device", settings.get("vaapi_device")]
    return []


def filter_tail(encoder):
    """Filters (after colour conversion) that get frames into the encoder."""
    family = BY_ID[encoder][2]
    if family == "vaapi":
        return ["format=nv12", "hwupload"]
    if family in ("qsv", "amf"):
        return ["format=nv12"]
    return ["format=yuv420p"]


def codec_args(encoder, quality, fps):
    i = _QI.get(quality, 1)
    family = BY_ID[encoder][2]
    if family == "nvenc":
        q = [20, 26, 32][i]
        args = ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0"]
    elif family == "nvenc_av1":
        q = [26, 34, 42][i]
        args = ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0"]
    elif family == "vaapi":
        q = [20, 26, 32][i]
        args = ["-rc_mode", "CQP", "-qp", str(q)]
    elif family == "qsv":
        q = [20, 26, 32][i]
        args = ["-global_quality", str(q), "-preset", "slower"]
    elif family == "amf":
        q = [20, 26, 32][i]
        args = ["-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q), "-quality", "quality"]
    elif family == "vt":
        args = ["-q:v", str([75, 60, 45][i])]
    elif family == "x264":
        args = ["-preset", "medium", "-crf", str([18, 23, 28][i])]
    elif family == "x265":
        args = ["-preset", "medium", "-crf", str([20, 26, 31][i]), "-x265-params", "log-level=error"]
    else:  # svtav1
        args = ["-preset", "8", "-crf", str([26, 34, 42][i])]
    return ["-c:v", encoder, *args, "-g", str(max(1, int(fps)) * 10)]


def video_filter(encoder, prefix=None):
    parts = list(prefix or []) + [_CONVERT] + filter_tail(encoder)
    return ",".join(parts)


def run_quiet(cmd, timeout):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout,
                          creationflags=NO_WINDOW)


def probe(ffmpeg):
    """Return the encoder ids that actually work on this machine, best first.

    ffmpeg lists e.g. NVENC/AMF even without the hardware (or, for AMF on
    Linux, without AMD's proprietary runtime), so every candidate is
    test-encoded with the exact arguments a recording would use."""
    try:
        listed = run_quiet([ffmpeg, "-hide_banner", "-encoders"], 15).stdout.decode("utf-8", "replace")
    except Exception:
        return []
    working = []
    for enc in fallback_order(detect_gpu_vendors()):
        if f" {enc} " not in listed:
            continue
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", *input_args(enc),
               "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=30",
               "-frames:v", "5", "-vf", video_filter(enc),
               *codec_args(enc, "medium", 30), "-f", "null", "-"]
        try:
            if run_quiet(cmd, 20).returncode == 0:
                working.append(enc)
        except Exception:
            pass
    return working
