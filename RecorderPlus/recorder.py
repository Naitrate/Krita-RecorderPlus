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
import shutil
import subprocess
import threading
import time

from krita import Krita

from . import encoders, settings
from .i18n import tr
from .qt import (QApplication, QColor, QEvent, QImage, QMainWindow, QObject,
                 QPainter, Qt, QTimer, QWidget, image_bytes, pyqtSignal)

MAX_QUEUE = 6
# Only documents in these formats keep their creation-date id across sessions
# (and are files the user clearly means to keep). Recordings of anything else
# (untitled canvases, opened PNG/JPG...) are temporary until saved as one.
PERSISTENT_EXTENSIONS = (".kra", ".krz", ".psd")
# Temporary recordings left behind by a crash are kept this long, in case
# Krita's autosave recovery brings the document back.
ORPHAN_DAYS = 3
CHECKPOINT_SECONDS = 10


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


def is_persistent_file(path):
    return os.path.splitext(path or "")[1].lower() in PERSISTENT_EXTENSIONS


def clean_orphans(root):
    """Delete temporary recordings abandoned by a crash long enough ago."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    cutoff = time.time() - ORPHAN_DAYS * 86400
    for name in names:
        folder = os.path.join(root, name)
        info = load_info(folder)
        if info and info.get("temporary"):
            try:
                if os.path.getmtime(os.path.join(folder, "info.json")) < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass


def segment_paths(folder, info):
    return [os.path.join(folder, s["file"]) for s in (info or {}).get("segments", [])
            if os.path.isfile(os.path.join(folder, s["file"]))]


class Segment:
    """One ffmpeg process fed from a background writer thread."""

    def __init__(self, folder, info, encoder, quality, live=True):
        self.folder = folder
        self.info = info
        self.encoder = encoder
        stamp = time.strftime("seg_%Y%m%d_%H%M%S")
        self.file, n = stamp + ".mkv", 1
        while os.path.exists(os.path.join(folder, self.file)):
            n += 1
            self.file = f"{stamp}_{n}.mkv"
        self.path = os.path.join(folder, self.file)
        self.frames = 0
        self.dropped = 0
        self.error = ""
        w, h, fps = info["width"], info["height"], info["fps"]
        self.size = (w, h)
        cmd = [settings.ffmpeg_path(), "-hide_banner", "-loglevel", "warning", "-y",
               *encoders.input_args(encoder),
               "-f", "rawvideo", "-pix_fmt", "bgr0", "-video_size", f"{w}x{h}",
               "-framerate", str(fps), "-i", "pipe:0",
               "-vf", encoders.video_filter(encoder),
               *encoders.codec_args(encoder, quality, fps, live=live),
               *encoders.COLOR_TAGS, *(encoders.LIVE_MUX if live else []), self.path]
        self._log = open(os.path.join(folder, "ffmpeg.log"), "ab")
        self._log.write(("\n# " + " ".join(cmd) + "\n").encode())
        self._log.flush()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=self._log, creationflags=encoders.NO_WINDOW)
        self._last_checkpoint = time.time()
        self._queue = queue.Queue(MAX_QUEUE)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        info["segments"].append({"file": self.file, "encoder": encoder,
                                 "codec": encoders.codec_of(encoder), "frames": 0,
                                 "width": w, "height": h})
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
                self.error = tr("ffmpeg stopped accepting frames ({error}); see ffmpeg.log").format(error=e)
                break
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def checkpoint(self, last_digest=""):
        """Record progress in info.json so a crash leaves accurate metadata."""
        for s in self.info["segments"]:
            if s["file"] == self.file:
                s["frames"] = self.frames
        self.info["last_digest"] = last_digest
        save_info(self.folder, self.info)
        self._last_checkpoint = time.time()

    @property
    def alive(self):
        return not self.error and self.proc.poll() is None

    @property
    def accepting_frames(self):
        return not self._queue.full()

    def push(self, data, block=False):
        if not self.alive:
            return False
        if block:
            # Offline conversion: wait for the encoder instead of dropping.
            while self.alive:
                try:
                    self._queue.put(data, timeout=0.5)
                    return True
                except queue.Full:
                    pass
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


# Canvas widget classes (OpenGL and software canvas).
CANVAS_CLASSES = {"KisOpenGLCanvas2", "KisQPainterCanvas"}
STROKE_DELAY_MS = 150
ACTION_DELAY_MS = 150
SETTLE_MS = 1200
LOCK_RETRY_MS = 100
MIN_CAPTURE_SECONDS = 0.25
# A press whose release never reached the canvas (e.g. a popup grabbed it)
# must not block captures forever.
STROKE_STUCK_SECONDS = 30
_STROKE_START = {QEvent.Type.TabletPress, QEvent.Type.MouseButtonPress,
                 QEvent.Type.TouchBegin}
_STROKE_END = {QEvent.Type.TabletRelease, QEvent.Type.MouseButtonRelease,
               QEvent.Type.TouchEnd, QEvent.Type.KeyRelease}


class _CanvasWatcher(QObject):
    """Event filter on canvas widgets: a pen/mouse/touch release ends a
    stroke (key release covers e.g. text and shortcut-driven tools)."""

    def __init__(self, recorder):
        super().__init__(recorder)
        self._recorder = recorder

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in _STROKE_START:
            self._recorder._stroke_active = True
            self._recorder._stroke_started = time.monotonic()
        elif kind in _STROKE_END:
            if kind != QEvent.Type.KeyRelease:
                self._recorder._stroke_active = False
            self._recorder.request_capture(STROKE_DELAY_MS)
        elif kind == QEvent.Type.TouchCancel:
            # Not FocusOut: focus can move while the pen is still down.
            self._recorder._stroke_active = False
        return False


class Recorder(QObject):
    changed = pyqtSignal()
    _processed = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.status = tr("Idle")
        self.segment = None
        self.key = None
        self.folder = None
        self.last_image = None
        self.detected = []          # working encoders, filled by detection
        # Like Krita's recorder, recording is switched on per document (keyed
        # the same way as the recording folder) rather than globally.
        self._enabled_keys = set()
        self._seen_keys = set()      # documents already offered auto-record
        self._digests = {}           # key -> digest of last written frame
        self._failed = set()         # encoders that died this session
        self._temporary = {}         # key -> folder of temporary recordings
        self._stroke_active = False
        self._stroke_started = 0.0
        self._processing = False
        self._generation = 0         # reject worker results after stop/switch
        self._last_capture = 0.0
        self._processed.connect(self._frame_ready)
        # Bookkeeping poll (no capturing) and the coalescing capture timer.
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._watch)
        self._capture_timer = QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._capture_timer.timeout.connect(self._tick)
        self._canvas_filter = _CanvasWatcher(self)
        self._hooked_actions = False

    # ---- control -------------------------------------------------------

    def start(self):
        """Begin watching documents. Capturing only happens for documents
        with recording switched on."""
        self._watch_timer.start(1000)
        QApplication.instance().focusWindowChanged.connect(self._focus_changed)
        # Saving (including from the "save changes?" prompt on close) can turn
        # a temporary recording into a kept one; check right away.
        Krita.instance().notifier().imageSaved.connect(lambda *_: self._update_temporary())
        clean_orphans(settings.get("directory"))

    def request_capture(self, delay_ms):
        """Capture after delay_ms; multiple requests coalesce into one."""
        if not self._capture_timer.isActive() or self._capture_timer.remainingTime() > delay_ms:
            self._capture_timer.start(delay_ms)

    # ---- capture triggers (event mode) -----------------------------------

    def _hook_canvases(self):
        """Watch every canvas widget for the end of strokes."""
        for win in Krita.instance().windows():
            qwin = win.qwindow()
            if qwin is None:
                continue
            for w in qwin.findChildren(QWidget):
                cls = w.metaObject().className()
                if cls in CANVAS_CLASSES and not w.property("vt_watched"):
                    w.setProperty("vt_watched", True)
                    w.installEventFilter(self._canvas_filter)
        if not self._hooked_actions:
            # Every Krita action (fill, filters, transform, layer ops,
            # undo/redo...) may change the image. View-only actions (zoom,
            # pan) produce an identical frame, which is then dropped.
            actions = Krita.instance().actions()
            for action in actions:
                action.triggered.connect(self._action_triggered)
            self._hooked_actions = bool(actions)

    def _action_triggered(self, *_):
        self.request_capture(ACTION_DELAY_MS)
        # Some actions finish asynchronously (fills, filters): look again.
        QTimer.singleShot(SETTLE_MS, lambda: self.request_capture(0))

    def _focus_changed(self, window):
        # Returning to the main window after a dialog (filter, layer
        # properties...) may have changed the image.
        if window is not None:
            self.request_capture(ACTION_DELAY_MS)

    @property
    def enabled(self):
        """Whether the active document is being recorded."""
        doc = Krita.instance().activeDocument()
        return doc is not None and document_key(doc) in self._enabled_keys

    def set_enabled(self, on):
        doc = Krita.instance().activeDocument()
        if doc is None:
            return
        key = document_key(doc)
        self._seen_keys.add(key)
        if on == (key in self._enabled_keys):
            return
        if on and not settings.ffmpeg_path():
            self.status = tr("ffmpeg not found — set its path in the docker")
            self.changed.emit()
            return
        if on:
            self._enabled_keys.add(key)
            self.status = tr("Recording")
        else:
            self._enabled_keys.discard(key)
            if self.key == key:
                self.close_segment()
            self.status = tr("Not recording this document")
        self.changed.emit()

    def chosen_encoder(self):
        usable = [e for e in self.detected if e not in self._failed]
        enc = settings.get("encoder")
        if enc in usable:
            return enc
        return usable[0] if usable else "libx264"

    def close_segment(self):
        self._generation += 1
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
        if self.key is not None:
            self._enabled_keys.discard(self.key)
        self.close_segment()
        self.status = message
        self.changed.emit()

    def shutdown(self):
        self._watch_timer.stop()
        self._capture_timer.stop()
        self.close_segment()
        # Runs on aboutToQuit, i.e. after Krita's save prompts: whatever is
        # still temporary was never saved as .kra/.psd.
        self._update_temporary(closing=True)

    # ---- temporary recordings ------------------------------------------

    def is_temporary(self, folder):
        info = self.segment.info if self.segment and self.folder == folder else load_info(folder)
        return bool(info and info.get("temporary"))

    def _update_temporary(self, closing=False):
        """Promote temporary recordings whose document is now saved as
        .kra/.psd; discard those whose document is gone."""
        open_docs = {}
        if not closing:
            for doc in Krita.instance().documents():
                open_docs[document_key(doc)] = doc
        for key, folder in list(self._temporary.items()):
            doc = open_docs.get(key)
            if doc is not None and is_persistent_file(doc.fileName()):
                self._set_temporary(key, folder, False)
                del self._temporary[key]
            elif doc is None:
                if self.key == key:
                    self.close_segment()
                    self.key = None
                del self._temporary[key]
                self._digests.pop(key, None)
                if self.is_temporary(folder):   # never delete a kept recording
                    shutil.rmtree(folder, ignore_errors=True)
                self.changed.emit()

    def _set_temporary(self, key, folder, value):
        info = self.segment.info if self.segment and self.key == key else load_info(folder)
        if info is None:
            return
        info["temporary"] = value
        save_info(folder, info)

    # ---- capture -------------------------------------------------------

    def folder_for(self, doc):
        return os.path.join(settings.get("directory"), document_key(doc))

    def _target_size(self, w, h, resolution=None):
        if resolution is None:
            resolution = settings.get("resolution")
        div = 1 << max(0, min(2, resolution))
        w, h = max(2, w // div), max(2, h // div)
        cap = encoders.max_dim(self.chosen_encoder())
        if max(w, h) > cap:
            s = cap / max(w, h)
            w, h = int(w * s), int(h * s)
        return max(2, w & ~1), max(2, h & ~1)

    def _watch(self, hook_canvases=True):
        """Cheap bookkeeping, run every second: follow the active document,
        auto-enable newly opened ones, hook new canvases. Returns the active
        document if it is being recorded."""
        app = Krita.instance()
        doc = app.activeDocument()
        if hook_canvases:
            self._hook_canvases()
        if doc is None:
            self.close_segment()
            return None

        key = document_key(doc)
        if key not in self._seen_keys:
            # First time this document is active this session (i.e. it was just
            # opened or created): start recording it if "record automatically".
            self._seen_keys.add(key)
            if settings.get("auto_record") and settings.ffmpeg_path():
                self._enabled_keys.add(key)
                self.request_capture(500)   # initial frame
        if key != self.key:
            self.close_segment()
            self.key = key
            self.folder = self.folder_for(doc)
            self.last_image = None
            info = load_info(self.folder)
            if info and key not in self._digests:
                self._digests[key] = info.get("last_digest", "")
            if info and info.get("temporary"):
                self._temporary[key] = self.folder   # e.g. recovered after a crash
            self.status = tr("Recording") if key in self._enabled_keys else tr("Not recording this document")
            self.changed.emit()

        self._update_temporary()
        if key not in self._enabled_keys:
            self.close_segment()
            return None
        return doc

    def _tick(self):
        doc = self._watch(hook_canvases=False)
        if doc is None:
            return
        # Like Krita's recorder: only capture while the main window is focused,
        # so modal dialogs (filters, etc.) aren't disturbed.
        if not isinstance(QApplication.activeWindow(), QMainWindow):
            return
        # A timer from the previous release/action may fire during a new
        # stroke. Never take a document lock while the user is drawing.
        if self._stroke_active and time.monotonic() - self._stroke_started > STROKE_STUCK_SECONDS:
            self._stroke_active = False
        if self._stroke_active or self._processing:
            self.request_capture(LOCK_RETRY_MS)
            return
        if self.segment is not None and self.segment.alive and not self.segment.accepting_frames:
            # Backpressure before projection, not after the expensive work.
            self.request_capture(LOCK_RETRY_MS)
            return
        remaining = MIN_CAPTURE_SECONDS - (time.monotonic() - self._last_capture)
        if remaining > 0:
            self.request_capture(max(1, int(remaining * 1000) + 1))
            return
        self._capture(doc)

    def _capture(self, doc):
        key = self.key
        w, h = doc.width(), doc.height()
        if w * h * 4 >= 2 ** 31:
            self.status = tr("Canvas too large to record")
            self.changed.emit()
            return

        info = self.segment.info if self.segment else load_info(self.folder)
        # Like Krita's recorder, frames follow the canvas size, so a crop or
        # resize changes the frame size. Each size gets its own segment;
        # export fits them all to the final size.
        resolution = info.get("resolution", settings.get("resolution")) if info else settings.get("resolution")
        tw, th = self._target_size(w, h, resolution)
        if self.segment is not None and self.segment.size != (tw, th):
            self.close_segment()
            info = load_info(self.folder)

        div = 1 << max(0, min(2, resolution))
        cw, ch = tw * div, th * div
        # Only accessing Krita's projection needs the barrier lock. QImage
        # processing, encoder startup, file I/O and UI updates must not hold it.
        if not doc.tryBarrierLock():
            self.request_capture(LOCK_RETRY_MS)
            return
        try:
            if cw <= w and ch <= h:
                # Preserve the existing odd-edge truncation before downscaling.
                src = doc.projection(0, 0, cw, ch)
            else:
                src = doc.projection(0, 0, w, h)
        finally:
            doc.unlock()
        self._last_capture = time.monotonic()
        context = {"key": key, "generation": self._generation, "info": info,
                   "resolution": resolution, "width": tw, "height": th,
                   "name": doc.name(), "document": doc.fileName()}
        self._processing = True
        threading.Thread(target=self._process_frame, args=(src, context), daemon=True).start()

    def _process_frame(self, src, context):
        """Work only on the captured QImage, never Krita objects or widgets."""
        try:
            tw, th = context["width"], context["height"]
            if src.width() != tw or src.height() != th:
                src = src.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            frame = QImage(tw, th, QImage.Format.Format_RGB32)
            # Match Krita's recorder: composite transparency onto white.
            frame.fill(QColor(255, 255, 255))
            p = QPainter(frame)
            try:
                p.drawImage(0, 0, src)
            finally:
                p.end()
            data = image_bytes(frame)
            digest = hashlib.blake2b(data, digest_size=16).hexdigest()
            self._processed.emit((context, frame, data, digest, None))
        except Exception as e:
            self._processed.emit((context, None, None, None, str(e)))

    def _frame_ready(self, result):
        """Queued back onto the UI thread; discard stale document results."""
        self._processing = False
        context, frame, data, digest, error = result
        key = context["key"]
        doc = Krita.instance().activeDocument()
        if (context["generation"] != self._generation or key != self.key
                or key not in self._enabled_keys or doc is None
                or document_key(doc) != key):
            return
        if error is not None:
            self._fail(tr("Could not process recording frame: {error}").format(error=error))
            return
        if digest == self._digests.get(key):
            return  # canvas unchanged
        info = context["info"]
        resolution = context["resolution"]
        tw, th = context["width"], context["height"]

        if self.segment is None or not self.segment.alive:
            if self.segment is not None:
                dead = self.segment
                self.close_segment()
                if dead.frames == 0 and len(self._failed) < len(self.detected):
                    # Passed the probe but died on real frames (e.g. size
                    # limits): fall through to the next encoder in line.
                    self._failed.add(dead.encoder)
                    self.status = tr("{failed} failed, falling back to {encoder}").format(
                        failed=dead.encoder, encoder=self.chosen_encoder())
                else:
                    self._fail(dead.error or tr("ffmpeg exited; see ffmpeg.log in the recording folder"))
                    return
            os.makedirs(self.folder, exist_ok=True)
            if info is None:
                info = {"name": context["name"] or os.path.basename(context["document"]) or "Untitled",
                        "fps": settings.get("fps"), "resolution": resolution,
                        "segments": [],
                        "temporary": not is_persistent_file(context["document"])}
            if info.get("temporary"):
                self._temporary[key] = self.folder
            info["width"], info["height"] = tw, th   # latest size
            info["name"] = context["name"] or info.get("name", "")
            info["document"] = context["document"]
            try:
                self.segment = Segment(self.folder, info, self.chosen_encoder(), settings.get("quality"))
            except OSError as e:
                self._fail(tr("Could not start ffmpeg: {error}").format(error=e))
                return

        if self.segment.push(data):
            self._digests[key] = digest
            self.last_image = frame
            self.status = tr("Recording")
            if time.time() - self.segment._last_checkpoint > CHECKPOINT_SECONDS:
                self.segment.checkpoint(digest)
        else:
            self.status = tr("Encoder falling behind — frame dropped")
        # The docker already refreshes on its own timer. Avoid doing disk
        # reads and rebuilding UI state after every stroke.


RECORDER = Recorder()
