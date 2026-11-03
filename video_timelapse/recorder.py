"""Captures the canvas on a timer and pipes frames into a running ffmpeg.

Modelled on Krita's built-in Recorder docker (plugins/dockers/recorder):
  * capture every N seconds, but only when the canvas actually changed;
  * skip captures while a dialog has focus (filters etc. misbehave otherwise);
  * one recording folder per document, keyed by the document's creation date.
Instead of writing a JPEG/PNG per capture, frames are streamed as raw BGRx
into ffmpeg's stdin, so disk only ever holds the compressed video.

Each record session becomes its own .mkv segment (MKV stays playable if Krita
crashes); Export concatenates segments into one file.
"""

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time

from krita import Krita

from . import encoders, settings
from .qt import (QApplication, QColor, QImage, QMainWindow, QObject,
                 QPainter, Qt, QTimer, image_bytes, pyqtSignal)

MAX_QUEUE = 6


def document_key(doc):
    """Stable per-document id, same scheme as Krita's recorder: the digits of
    the document's creation date (survives renames / Save As)."""
    m = re.search(r"<creation-date>([^<]*)</creation-date>", doc.documentInfo() or "")
    digits = re.sub(r"[^0-9]", "", m.group(1)) if m else ""
    if digits:
        return digits
    return "file_" + hashlib.md5((doc.fileName() or doc.name() or "untitled").encode()).hexdigest()[:12]


def load_info(folder):
    try:
        with open(os.path.join(folder, "info.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_info(folder, info):
    tmp = os.path.join(folder, "info.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    os.replace(tmp, os.path.join(folder, "info.json"))


def segment_paths(folder, info):
    return [os.path.join(folder, s["file"]) for s in (info or {}).get("segments", [])
            if os.path.isfile(os.path.join(folder, s["file"]))]


class Segment:
    """One ffmpeg process fed from a background writer thread."""

    def __init__(self, folder, info, encoder, quality):
        self.folder = folder
        self.info = info
        self.encoder = encoder
        self.file = time.strftime("seg_%Y%m%d_%H%M%S.mkv")
        self.path = os.path.join(folder, self.file)
        self.frames = 0
        self.dropped = 0
        self.error = ""
        w, h, fps = info["width"], info["height"], info["fps"]
        cmd = [settings.ffmpeg_path(), "-hide_banner", "-loglevel", "warning", "-y",
               *encoders.input_args(encoder),
               "-f", "rawvideo", "-pix_fmt", "bgr0", "-video_size", f"{w}x{h}",
               "-framerate", str(fps), "-i", "pipe:0",
               "-vf", encoders.video_filter(encoder),
               *encoders.codec_args(encoder, quality, fps),
               *encoders.COLOR_TAGS, self.path]
        self._log = open(os.path.join(folder, "ffmpeg.log"), "ab")
        self._log.write(("\n# " + " ".join(cmd) + "\n").encode())
        self._log.flush()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=self._log, creationflags=encoders.NO_WINDOW)
        self._queue = queue.Queue(MAX_QUEUE)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        info["segments"].append({"file": self.file, "encoder": encoder,
                                 "codec": encoders.codec_of(encoder), "frames": 0})
        save_info(folder, info)

    def _pump(self):
        while True:
            data = self._queue.get()
            if data is None:
                break
            try:
                self.proc.stdin.write(data)
                self.frames += 1
            except (BrokenPipeError, OSError, ValueError) as e:
                self.error = f"ffmpeg stopped accepting frames ({e}); see ffmpeg.log"
                break
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    @property
    def alive(self):
        return not self.error and self.proc.poll() is None

    def push(self, data):
        if not self.alive:
            return False
        try:
            self._queue.put_nowait(data)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def close(self):
        # Blocks until the queue drains; never drop the tail of a session.
        while self._thread.is_alive():
            try:
                self._queue.put(None, timeout=0.5)
                break
            except queue.Full:
                pass
        self._thread.join(30)
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._log.close()
        for s in self.info["segments"]:
            if s["file"] == self.file:
                s["frames"] = self.frames
        if self.frames == 0:
            # Nothing was ever written: don't leave an empty/broken file.
            self.info["segments"] = [s for s in self.info["segments"] if s["file"] != self.file]
            try:
                os.remove(self.path)
            except OSError:
                pass
        save_info(self.folder, self.info)


class Recorder(QObject):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.enabled = False
        self.status = "Idle"
        self.segment = None
        self.key = None
        self.folder = None
        self.last_image = None
        self.detected = []          # working encoders, filled by detection
        self._digests = {}           # key -> digest of last written frame
        self._failed = set()         # encoders that died this session
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---- control -------------------------------------------------------

    def set_enabled(self, on):
        if on == self.enabled:
            return
        if on and not settings.ffmpeg_path():
            self.status = "ffmpeg not found — set its path in the docker"
            self.changed.emit()
            return
        self.enabled = on
        if on:
            self._timer.start(int(max(0.1, settings.get("interval")) * 1000))
            self.status = "Recording"
        else:
            self._timer.stop()
            self.close_segment()
            self.status = "Idle"
        self.changed.emit()

    def restart_timer(self):
        if self.enabled:
            self._timer.start(int(max(0.1, settings.get("interval")) * 1000))

    def chosen_encoder(self):
        usable = [e for e in self.detected if e not in self._failed]
        enc = settings.get("encoder")
        if enc in usable:
            return enc
        return usable[0] if usable else "libx264"

    def close_segment(self):
        seg, self.segment = self.segment, None
        if seg is None:
            return
        if self.last_image is not None and seg.frames:
            self.last_image.save(os.path.join(seg.folder, "last.png"))
        seg.info["last_digest"] = self._digests.get(self.key, "")
        seg.close()
        self.changed.emit()

    def forget(self, key):
        """Drop the remembered last frame (after a recording is discarded)."""
        self._digests.pop(key, None)
        self.last_image = None

    def _fail(self, message):
        self.set_enabled(False)
        self.status = message
        self.changed.emit()

    def shutdown(self):
        self._timer.stop()
        self.close_segment()

    # ---- capture -------------------------------------------------------

    def folder_for(self, doc):
        return os.path.join(settings.get("directory"), document_key(doc))

    def _target_size(self, w, h):
        div = 1 << max(0, min(2, settings.get("resolution")))
        w, h = max(2, w // div), max(2, h // div)
        cap = encoders.max_dim(self.chosen_encoder())
        if max(w, h) > cap:
            s = cap / max(w, h)
            w, h = int(w * s), int(h * s)
        return max(2, w & ~1), max(2, h & ~1)

    def _tick(self):
        app = Krita.instance()
        doc = app.activeDocument()
        if doc is None:
            self.close_segment()
            return
        # Like Krita's recorder: only capture while the main window is focused,
        # so modal dialogs (filters, etc.) aren't disturbed.
        if not isinstance(QApplication.activeWindow(), QMainWindow):
            return

        key = document_key(doc)
        if key != self.key:
            self.close_segment()
            self.key = key
            self.folder = self.folder_for(doc)
            self.last_image = None
            info = load_info(self.folder)
            if info and key not in self._digests:
                self._digests[key] = info.get("last_digest", "")

        w, h = doc.width(), doc.height()
        if w * h * 4 >= 2 ** 31:
            self.status = "Canvas too large to record"
            self.changed.emit()
            return

        info = self.segment.info if self.segment else load_info(self.folder)
        tw, th = (info["width"], info["height"]) if info else self._target_size(w, h)

        frame = QImage(tw, th, QImage.Format.Format_RGB32)
        frame.fill(QColor(settings.get("background")))
        src = doc.projection(0, 0, w, h)
        p = QPainter(frame)
        if (w, h) == (tw, th):
            p.drawImage(0, 0, src)
        else:
            # Downscale / letterbox (canvas may have been resized since the
            # recording started; output size is fixed per recording).
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            scaled = src.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
            p.drawImage((tw - scaled.width()) // 2, (th - scaled.height()) // 2, scaled)
        p.end()

        data = image_bytes(frame)
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        if digest == self._digests.get(key):
            return  # canvas unchanged

        if self.segment is None or not self.segment.alive:
            if self.segment is not None:
                dead = self.segment
                self.close_segment()
                if dead.frames == 0 and len(self._failed) < len(self.detected):
                    # Passed the probe but died on real frames (e.g. size
                    # limits): fall through to the next encoder in line.
                    self._failed.add(dead.encoder)
                    self.status = f"{dead.encoder} failed, falling back to {self.chosen_encoder()}"
                else:
                    self._fail(dead.error or "ffmpeg exited; see ffmpeg.log in the recording folder")
                    return
            os.makedirs(self.folder, exist_ok=True)
            if info is None:
                info = {"name": doc.name() or os.path.basename(doc.fileName()) or "Untitled",
                        "width": tw, "height": th, "fps": settings.get("fps"),
                        "segments": []}
            info["name"] = doc.name() or info.get("name", "")
            info["document"] = doc.fileName()
            try:
                self.segment = Segment(self.folder, info, self.chosen_encoder(), settings.get("quality"))
            except OSError as e:
                self._fail(f"Could not start ffmpeg: {e}")
                return

        if self.segment.push(data):
            self._digests[key] = digest
            self.last_image = frame
            self.status = "Recording"
        else:
            self.status = "Encoder falling behind — frame dropped"
        self.changed.emit()


RECORDER = Recorder()
