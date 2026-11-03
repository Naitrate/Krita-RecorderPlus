import os
import shutil

from krita import Krita

GROUP = "VideoTimelapse"

DEFAULTS = {
    "encoder": "",            # empty = best detected
    "quality": "medium",
    "interval": 1.0,          # seconds between captures
    "fps": 30,
    "resolution": 0,          # 0 = original, 1 = half, 2 = quarter
    "background": "#ffffff",
    "directory": os.path.join(os.path.expanduser("~"), "Videos", "Krita Timelapses"),
    "auto_record": False,
    "ffmpeg": "",
    "vaapi_device": "/dev/dri/renderD128",
    "detected": "",           # cached "ffmpeg-path|enc1,enc2,..."
    "export_preview": 2.0,
    "export_hold": 3.0,
    "export_limit": 0,
}


def get(key):
    default = DEFAULTS[key]
    raw = Krita.instance().readSetting(GROUP, key, str(default))
    if isinstance(default, bool):
        return raw.lower() == "true"
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


def put(key, value):
    Krita.instance().writeSetting(GROUP, key, str(value))


def ffmpeg_path():
    return get("ffmpeg") or shutil.which("ffmpeg") or ""


def ffprobe_path():
    ff = ffmpeg_path()
    if ff:
        candidate = os.path.join(os.path.dirname(ff), "ffprobe" + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("ffprobe") or ""
