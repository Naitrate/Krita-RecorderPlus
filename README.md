# Video Timelapse for Krita

A Krita docker that records your canvas into a **video file** instead of
thousands of JPEG/PNG frames like the built-in Recorder does. Frames are piped
raw into ffmpeg and hardware-encoded, so the disk only ever holds compressed
video (typically a few KB per captured frame instead of hundreds).

Capture behaviour mirrors Krita's built-in Recorder: a capture every N seconds,
only when the canvas actually changed, skipped while a dialog has focus, and one
recording per document (keyed by creation date, so renames/Save As keep going).

## Settings

Anything not changed in this docker defaults to the built-in Recorder's
settings from `kritarc`: capture interval, resolution, record automatically,
export FPS, input FPS ("Captures per second" here), result preview / extend
result seconds, and the video directory. Recordings go to
`<Recorder snapshot directory>/Video Timelapse/`.

## Encoder selection

Every candidate is test-encoded at startup (ffmpeg lists encoders even when the
hardware/driver is missing), then the first working one in this order is used:

1. The detected GPU vendor's own encoder: **NVENC** (NVIDIA), **AMF** (AMD),
   **Quick Sync** (Intel), VideoToolbox (macOS)
2. **VA-API**
3. Software: x264 → x265 → SVT-AV1

An encoder that passes the probe but dies on real frames is skipped and the
next one takes over. You can also pick one manually in the docker.

## Files

`<folder>/<document id>/`
- `seg_*.mkv` — one segment per record session (MKV survives crashes)
- `info.json` — size/fps (fixed per recording), segment list
- `last.png` — final frame, used for the "show result at start" option
- `ffmpeg.log`

**Export…** joins segments into one MP4/MKV. With no extras it's a lossless
stream copy (instant); "show result at start", "hold last frame", "limit
length", or sessions recorded with different codecs trigger a re-encode with the
current encoder.

## Install

Symlink or copy `video_timelapse/` and `video_timelapse.desktop` into
`~/.local/share/krita/pykrita/`, enable *Video Timelapse* in
Settings → Configure Krita → Python Plugin Manager, restart, then
Settings → Dockers → Video Timelapse. A shortcut can be bound to
"Toggle Video Timelapse Recording". Requires ffmpeg (on PATH or set in the docker).
