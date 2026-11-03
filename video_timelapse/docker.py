import os
import shutil
import threading

from krita import DockWidget, Krita

from . import encoders, settings
from .export import ExportDialog, human_size
from .qt import (QCheckBox, QColor, QColorDialog, QComboBox, QDesktopServices,
                 QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                 QHBoxLayout, QLabel, QLineEdit, QMessageBox, QObject,
                 QPushButton, QSpinBox, QTimer, QToolButton, QUrl,
                 QVBoxLayout, QWidget, pyqtSignal)
from .recorder import RECORDER, load_info, segment_paths


class _Detector(QObject):
    done = pyqtSignal(list)

    def run(self, ffmpeg):
        threading.Thread(target=lambda: self.done.emit(encoders.probe(ffmpeg)), daemon=True).start()


DETECTOR = _Detector()
_detecting = False


def detect_encoders(force=False):
    """Fill RECORDER.detected, using the cached result unless forced."""
    global _detecting
    ff = settings.ffmpeg_path()
    cached = settings.get("detected")
    if not force and cached.startswith(ff + "|") and ff:
        RECORDER.detected = [e for e in cached.split("|", 1)[1].split(",") if e in encoders.BY_ID]
        RECORDER.changed.emit()
        return
    if _detecting or not ff:
        return
    _detecting = True
    RECORDER.status = "Detecting encoders…"
    RECORDER.changed.emit()
    DETECTOR.run(ff)


def _on_detected(found):
    global _detecting
    _detecting = False
    RECORDER.detected = found
    settings.put("detected", settings.ffmpeg_path() + "|" + ",".join(found))
    if not RECORDER.enabled:
        RECORDER.status = "Idle" if found else "No working encoder found (see ffmpeg path)"
    RECORDER.changed.emit()


DETECTOR.done.connect(_on_detected)


class TimelapseDocker(DockWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Timelapse")
        root = QWidget()
        lay = QVBoxLayout(root)

        top = QHBoxLayout()
        self.rec = QPushButton("● Record")
        self.rec.setCheckable(True)
        self.rec.toggled.connect(RECORDER.set_enabled)
        top.addWidget(self.rec)
        self.export_btn = QPushButton("Export…")
        self.export_btn.clicked.connect(self._export)
        top.addWidget(self.export_btn)
        lay.addLayout(top)

        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        lay.addWidget(self.stats)

        tools = QHBoxLayout()
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open_folder)
        tools.addWidget(open_btn)
        self.discard_btn = QPushButton("Discard…")
        self.discard_btn.setToolTip("Delete this document's recording")
        self.discard_btn.clicked.connect(self._discard)
        tools.addWidget(self.discard_btn)
        lay.addLayout(tools)

        box = QGroupBox("Settings")
        form = QFormLayout(box)

        enc_row = QHBoxLayout()
        self.encoder = QComboBox()
        self.encoder.currentIndexChanged.connect(self._encoder_changed)
        enc_row.addWidget(self.encoder, 1)
        redetect = QToolButton(text="⟳")
        redetect.setToolTip("Re-detect working encoders")
        redetect.clicked.connect(lambda: detect_encoders(force=True))
        enc_row.addWidget(redetect)
        form.addRow("Encoder:", enc_row)

        self.quality = QComboBox()
        for key, label in encoders.QUALITIES:
            self.quality.addItem(label, key)
        self.quality.setCurrentIndex(max(0, self.quality.findData(settings.get("quality"))))
        self.quality.currentIndexChanged.connect(lambda: settings.put("quality", self.quality.currentData()))
        form.addRow("Quality:", self.quality)

        self.interval = QDoubleSpinBox(suffix=" s", minimum=0.1, maximum=60, singleStep=0.1, decimals=1)
        self.interval.setValue(settings.get("interval"))
        self.interval.valueChanged.connect(self._interval_changed)
        form.addRow("Capture every:", self.interval)

        self.fps = QSpinBox(minimum=1, maximum=120)
        self.fps.setValue(settings.get("fps"))
        self.fps.valueChanged.connect(lambda v: settings.put("fps", v))
        self.fps.setToolTip("Video frame rate. Fixed per recording once it has started.")
        form.addRow("Video FPS:", self.fps)

        self.resolution = QComboBox()
        self.resolution.addItems(["Original", "Half", "Quarter"])
        self.resolution.setCurrentIndex(settings.get("resolution"))
        self.resolution.currentIndexChanged.connect(lambda i: settings.put("resolution", i))
        self.resolution.setToolTip("Fixed per recording once it has started.")
        form.addRow("Resolution:", self.resolution)

        self.bg = QPushButton()
        self.bg.clicked.connect(self._pick_bg)
        self._paint_bg()
        form.addRow("Background:", self.bg)

        dir_row = QHBoxLayout()
        self.directory = QLineEdit(settings.get("directory"))
        self.directory.editingFinished.connect(lambda: settings.put("directory", self.directory.text()))
        dir_btn = QToolButton(text="…")
        dir_btn.clicked.connect(self._pick_dir)
        dir_row.addWidget(self.directory)
        dir_row.addWidget(dir_btn)
        form.addRow("Folder:", dir_row)

        ff_row = QHBoxLayout()
        self.ffmpeg = QLineEdit(settings.get("ffmpeg"))
        self.ffmpeg.setPlaceholderText(settings.ffmpeg_path() or "not found on PATH")
        self.ffmpeg.editingFinished.connect(self._ffmpeg_changed)
        ff_btn = QToolButton(text="…")
        ff_btn.clicked.connect(self._pick_ffmpeg)
        ff_row.addWidget(self.ffmpeg)
        ff_row.addWidget(ff_btn)
        form.addRow("ffmpeg:", ff_row)

        self.auto = QCheckBox("Start recording when Krita starts")
        self.auto.setChecked(settings.get("auto_record"))
        self.auto.toggled.connect(lambda v: settings.put("auto_record", v))
        form.addRow(self.auto)

        lay.addWidget(box)
        lay.addStretch(1)
        self.setWidget(root)

        RECORDER.changed.connect(self._refresh)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(2000)
        self._refresh()

    def canvasChanged(self, canvas):
        self._refresh()

    # ---- helpers -------------------------------------------------------

    def _current_folder(self):
        if RECORDER.folder and RECORDER.segment is not None:
            return RECORDER.folder
        doc = Krita.instance().activeDocument()
        return RECORDER.folder_for(doc) if doc is not None else None

    def _refresh(self):
        if self.rec.isChecked() != RECORDER.enabled:
            self.rec.blockSignals(True)
            self.rec.setChecked(RECORDER.enabled)
            self.rec.blockSignals(False)
        self.rec.setText("■ Stop" if RECORDER.enabled else "● Record")

        # encoder list
        want = [""] + RECORDER.detected
        have = [self.encoder.itemData(i) for i in range(self.encoder.count())]
        if want != have:
            self.encoder.blockSignals(True)
            self.encoder.clear()
            best = encoders.BY_ID[RECORDER.detected[0]][1] if RECORDER.detected else "none found"
            self.encoder.addItem(f"Auto ({best})", "")
            for enc in RECORDER.detected:
                self.encoder.addItem(encoders.BY_ID[enc][1], enc)
            self.encoder.setCurrentIndex(max(0, self.encoder.findData(settings.get("encoder"))))
            self.encoder.blockSignals(False)

        seg = RECORDER.segment
        status = RECORDER.status
        if seg is not None:
            status = f"<b style='color:#e04040'>● REC</b> {status} — {encoders.BY_ID[seg.encoder][1]}"
            if seg.dropped:
                status += f" ({seg.dropped} dropped)"
        self.status.setText(status)

        folder = self._current_folder()
        info = load_info(folder) if folder else None
        if info and info.get("segments"):
            paths = segment_paths(folder, info)
            size = sum(os.path.getsize(p) for p in paths)
            frames = sum(s.get("frames", 0) for s in info["segments"]) + (seg.frames if seg else 0)
            secs = frames / max(1, info["fps"])
            self.stats.setText(f"{info.get('name', '')}: {frames} frames ({secs:.0f} s of video), "
                               f"{human_size(size)}, {info['width']}×{info['height']} @ {info['fps']} fps")
        else:
            self.stats.setText("No recording for this document yet." if folder else "No document open.")
        has = bool(info and info.get("segments"))
        self.export_btn.setEnabled(has)
        self.discard_btn.setEnabled(has)

    def _encoder_changed(self):
        settings.put("encoder", self.encoder.currentData() or "")

    def _interval_changed(self, v):
        settings.put("interval", v)
        RECORDER.restart_timer()

    def _paint_bg(self):
        c = settings.get("background")
        self.bg.setText(c)
        self.bg.setStyleSheet(f"background:{c}; color:{'#000' if QColor(c).lightness() > 128 else '#fff'}")

    def _pick_bg(self):
        c = QColorDialog.getColor(QColor(settings.get("background")), self, "Background behind transparency")
        if c.isValid():
            settings.put("background", c.name())
            self._paint_bg()

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Timelapse folder", self.directory.text())
        if d:
            self.directory.setText(d)
            settings.put("directory", d)

    def _pick_ffmpeg(self):
        f, _ = QFileDialog.getOpenFileName(self, "ffmpeg executable", self.ffmpeg.text())
        if f:
            self.ffmpeg.setText(f)
            self._ffmpeg_changed()

    def _ffmpeg_changed(self):
        settings.put("ffmpeg", self.ffmpeg.text().strip())
        detect_encoders()

    def _open_folder(self):
        folder = self._current_folder() or settings.get("directory")
        if not os.path.isdir(folder):
            folder = settings.get("directory")
            os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _export(self):
        folder = self._current_folder()
        if folder and load_info(folder):
            ExportDialog(folder, self).exec()
            self._refresh()

    def _discard(self):
        folder = self._current_folder()
        info = load_info(folder) if folder else None
        if not info:
            return
        answer = QMessageBox.question(self, "Discard recording",
                                      f"Permanently delete the timelapse recording for "
                                      f"“{info.get('name', '')}”?\n\n{folder}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if RECORDER.folder == folder:
            RECORDER.close_segment()
            RECORDER.forget(RECORDER.key)
        shutil.rmtree(folder, ignore_errors=True)
        self._refresh()
