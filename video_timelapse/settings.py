import os
import shutil

from krita import Krita

GROUP = "VideoTimelapse"

DEFAULTS = {
    "encoder": "",            # empty = best detected
    "quality": "medium",
    "fps": 30,
    "resolution": 0,          # 0 = original, 1 = half, 2 = quarter
    "directory": os.path.join(os.path.expanduser("~"), "Videos", "Krita Recordings"),
    "auto_record": False,
    "ffmpeg": "",
    "vaapi_device": "/dev/dri/renderD128",
    "detected": "",           # cached "ffmpeg-path|enc1,enc2,..."
    "export_preview": 2.0,
    "export_hold": 3.0,
    "export_limit": 0,
    "export_speed": 0,        # 0 = recording fps
    "export_fps": 0,          # 0 = recording fps
    "export_dir": "",
}

_UNSET = "\x00unset"
SUBFOLDER = "Video Timelapse"


def _builtin(key):
    """Krita's own Recorder docker settings (top level of kritarc), used as
    defaults for anything not yet set here. Returns None if unavailable."""
    def rd(name):
        v = Krita.instance().readSetting("", name, _UNSET)
        return None if v == _UNSET else v

    def flag(name):
        return (rd(name) or "").lower() == "true"

    if key == "directory":
        d = rd("recorder/snapshotdirectory")
        return os.path.join(d, SUBFOLDER) if d else None
    if key == "export_dir":
        return rd("recorder_export/videodirectory")
    if key == "export_preview":
        return rd("recorder_export/firstframesec") if flag("recorder_export/resultpreview") else "0"
    if key == "export_hold":
        return rd("recorder_export/lastframesec") if flag("recorder_export/extendresult") else "0"
    name = {"resolution": "recorder/resolution",
            "auto_record": "recorder/recordautomatically",
            "fps": "recorder_export/fps",
            "export_fps": "recorder_export/fps",
            "export_speed": "recorder_export/inputfps"}.get(key)
    return rd(name) if name else None


def get(key):
    default = DEFAULTS[key]
    raw = Krita.instance().readSetting(GROUP, key, _UNSET)
    if raw == _UNSET:
        raw = _builtin(key)
        if raw is None:
            raw = str(default)
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
