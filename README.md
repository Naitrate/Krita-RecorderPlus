# Recorder+ for Krita

A Krita docker that records your canvas into a **video file** instead of
thousands of JPEG/PNG frames like the built-in Recorder does. Frames are piped
raw into ffmpeg and hardware-encoded, so the disk only ever holds compressed
video (typically a few KB per captured frame instead of hundreds).

Capture behaviour mirrors Krita's built-in Recorder:
- a frame is captured after every brush stroke (pen/mouse/touch release on
  the canvas) and every Krita action (fill, filters, transforms, layer ops,
  undo/redo, closing a dialog), once Krita has finished processing it (same
  non-blocking image lock check as the built-in recorder); frames identical to
  the previous one (pan, zoom) are dropped;
- recording is on/off **per document**; with "record automatically" each
  document starts recording when it is opened or created;
- one recording per document, identified by the document's creation date
  (stored in the .kra), so renames and Save As keep adding to it;
- recordings of untitled documents, or of files that aren't .kra/.krz/.psd
  (PNG, JPG…), are temporary: they are deleted when the document closes or
  Krita quits unless it was saved as .kra/.psd by then (the "save changes?"
  prompt counts). Anything that ever belonged to a .kra/.psd is never deleted.
  Temporary recordings orphaned by a crash are kept 3 days for autosave
  recovery, then cleaned up;
- transparency is blended onto white, odd edges are truncated to even sizes;
- frames follow the canvas size: after a crop/resize a new segment starts at
  the new size, and export fits everything into the **final** size with black
  bars, exactly like the built-in export.

## Settings

Anything not changed in this docker defaults to the built-in Recorder's
settings from `kritarc`: resolution, record automatically,
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
- `info.json` — fps and resolution (fixed per recording), segments with their sizes
- `last.png` — final frame, used for the "show result at start" option
- `ffmpeg.log`

**Convert old Recorder frames…** encodes the built-in Recorder's frame
folders into this format. Folder names are the same document ids, so converted
footage is placed in front of that document's newer recording. Originals can
optionally be deleted once a conversion's frame count is verified.

**Export…** joins segments into one MP4/MKV. With no extras it's a lossless
stream copy (instant); "show result at start", "hold last frame", "limit
length", or sessions recorded with different codecs trigger a re-encode with the
current encoder.

## Languages

The UI follows Krita's language (Settings → Switch Application Language, else
the system language) and ships translations for all 74 languages Krita does.
Catalogs are `RecorderPlus/locale/<code>.json`, keyed by the English text;
`python3 tools/check_strings.py` regenerates `template.json` and reports
missing strings or placeholder mismatches per language. Many translations were
machine-written and would benefit from native-speaker review.

## Install

Symlink or copy `RecorderPlus/` and `RecorderPlus.desktop` into
`~/.local/share/krita/pykrita/`, enable *Recorder+* in
Settings → Configure Krita → Python Plugin Manager, restart, then
Settings → Dockers → Recorder+. A shortcut can be bound to
"Toggle Recorder+ Recording". Requires ffmpeg (on PATH or set in the docker).
