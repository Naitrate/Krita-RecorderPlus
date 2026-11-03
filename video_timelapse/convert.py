"""Converts folders of frames made by Krita's built-in Recorder into this
plugin's video recordings.

The built-in recorder names each folder after the document's creation date,
the same id this plugin uses, so a converted recording lands in the same
folder as (and is placed before) anything recorded for that document since.

Frames are decoded with QImage on a worker thread and fed through the same
Segment pipeline as live recording (so sizes changing mid-recording, e.g. after
a crop, become separate segments). The final merge into info.json happens on
the GUI thread so it can't race a live recording of the same document.
"""

import os
import re
import shutil
import threading
import time

from . import encoders, settings
from .export import human_size
from .qt import (QCheckBox, QColor, QDialog, QDialogButtonBox, QFileDialog,
                 QHBoxLayout, QImage, QLabel, QLineEdit, QListWidget,
                 QListWidgetItem, QMessageBox, QObject, QPainter,
                 QProgressBar, QPushButton, Qt, QVBoxLayout, image_bytes,
                 pyqtSignal)
from .recorder import RECORDER, Segment, load_info, save_info

FRAME_RE = re.compile(r"^(\d{7})\.(jpe?g|png)$", re.IGNORECASE)
STAGING = "import_staging"
IN_USE_SECONDS = 120


def builtin_snapshot_dir():
    from krita import Krita
    return Krita.instance().readSetting("", "recorder/snapshotdirectory", "")


def scan(root):
    """Find built-in recorder folders under root."""
    found = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return found
    now = time.time()
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        frames = []
        size = 0
        newest = 0
        with os.scandir(path) as it:
            for entry in it:
                m = FRAME_RE.match(entry.name)
                if m and entry.is_file():
                    st = entry.stat()
                    frames.append((int(m.group(1)), entry.name))
                    size += st.st_size
                    newest = max(newest, st.st_mtime)
        if len(frames) < 2:
            continue
        frames.sort()
        found.append({"key": name, "path": path, "frames": [f for _, f in frames],
                      "bytes": size, "in_use": now - newest < IN_USE_SECONDS})
    return found


def describe_key(key):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})", key)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}" if m else key


def already_imported(dest_root, job):
    info = load_info(os.path.join(dest_root, job["key"]))
    return any(i.get("source") == job["path"] for i in (info or {}).get("imported", []))


class Converter(QObject):
    progress = pyqtSignal(int, int, str)      # frames done, frames total, message
    job_done = pyqtSignal(dict)               # result, merged on GUI thread
    finished = pyqtSignal(str)

    def __init__(self, jobs, dest_root):
        super().__init__()
        self.jobs = jobs
        self.dest_root = dest_root
        self.cancelled = False
        self.encoder = RECORDER.chosen_encoder()
        self.quality = settings.get("quality")

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        total = sum(len(j["frames"]) for j in self.jobs)
        done = 0
        converted = 0
        for job in self.jobs:
            if self.cancelled:
                break
            try:
                result = self._convert(job, done, total)
            except Exception as e:  # keep going with the other folders
                result = {"job": job, "error": str(e)}
            done += len(job["frames"])
            if result is not None:
                if not result.get("error"):
                    converted += 1
                self.job_done.emit(result)
        self.finished.emit("Cancelled." if self.cancelled else
                           f"Converted {converted} of {len(self.jobs)} recording(s).")

    def _convert(self, job, done, total):
        folder = os.path.join(self.dest_root, job["key"])
        staging = os.path.join(folder, STAGING)
        if os.path.isdir(staging):
            shutil.rmtree(staging)          # leftovers of an interrupted run
        os.makedirs(staging)
        existing = load_info(folder)
        fps = existing["fps"] if existing else settings.get("fps")
        tmp = {"fps": fps, "segments": []}
        cap = encoders.max_dim(self.encoder)

        seg = None
        last = None
        written = 0
        skipped = 0
        name = describe_key(job["key"])
        for i, fname in enumerate(job["frames"]):
            if self.cancelled:
                break
            img = QImage(os.path.join(job["path"], fname))
            if img.isNull():
                skipped += 1
                continue
            frame = self._prepare(img, cap)
            if seg is None or seg.size != (frame.width(), frame.height()):
                if seg is not None:
                    seg.close()
                tmp["width"], tmp["height"] = frame.width(), frame.height()
                seg = Segment(staging, tmp, self.encoder, self.quality)
            if not seg.push(image_bytes(frame), block=True):
                raise RuntimeError(seg.error or "ffmpeg exited; see ffmpeg.log")
            last = frame
            written += 1
            if i % 25 == 0:
                self.progress.emit(done + i, total, f"{name}: frame {i + 1} of {len(job['frames'])}")
        if seg is not None:
            seg.close()
        if self.cancelled:
            shutil.rmtree(staging, ignore_errors=True)
            return None
        frames_in_video = sum(s["frames"] for s in tmp["segments"])
        if last is not None:
            last.save(os.path.join(staging, "last.png"))
        return {"job": job, "folder": folder, "staging": staging, "tmp": tmp,
                "written": written, "skipped": skipped,
                "verified": frames_in_video == written and written > 0}

    @staticmethod
    def _prepare(img, cap):
        w, h = img.width() & ~1, img.height() & ~1
        if max(w, h) > cap:
            s = cap / max(w, h)
            w, h = int(w * s) & ~1, int(h * s) & ~1
        frame = QImage(w, h, QImage.Format.Format_RGB32)
        frame.fill(QColor(255, 255, 255))   # built-in frames are already opaque
        p = QPainter(frame)
        if (img.width() & ~1, img.height() & ~1) == (w, h):
            p.drawImage(0, 0, img)          # truncates an odd edge, like Krita
        else:
            p.drawImage(0, 0, img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        p.end()
        return frame


def merge(result):
    """GUI thread: move staged segments in front of the document's recording."""
    folder, staging, tmp = result["folder"], result["staging"], result["tmp"]
    if RECORDER.folder == folder:
        RECORDER.close_segment()            # so its info.json isn't overwritten
    info = load_info(folder)
    for s in tmp["segments"]:
        os.replace(os.path.join(staging, s["file"]), os.path.join(folder, s["file"]))
    if info is None:
        last = tmp["segments"][-1]
        info = {"name": f"Recorder import {describe_key(result['job']['key'])}",
                "fps": tmp["fps"], "width": last["width"], "height": last["height"],
                "segments": []}
        os.replace(os.path.join(staging, "last.png"), os.path.join(folder, "last.png"))
    info["segments"] = tmp["segments"] + info["segments"]
    info.setdefault("imported", []).append({"source": result["job"]["path"],
                                            "frames": result["written"]})
    save_info(folder, info)
    shutil.rmtree(staging, ignore_errors=True)


def delete_frames(job):
    """Remove only the frame images the converter read, then the folder if
    nothing else is left in it."""
    for fname in job["frames"]:
        try:
            os.remove(os.path.join(job["path"], fname))
        except OSError:
            pass
    try:
        os.rmdir(job["path"])
    except OSError:
        pass


class ConvertDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recorder+ — Convert Krita Recorder Frames")
        self.setMinimumSize(560, 460)
        self.converter = None
        self.freed = 0

        lay = QVBoxLayout(self)
        intro = QLabel("Encodes folders of frames made by Krita's built-in Recorder into video "
                       "recordings for Recorder+. Each one is matched to its document, so "
                       "it can be exported together with anything recorded since.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        row = QHBoxLayout()
        self.source = QLineEdit(builtin_snapshot_dir())
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        scan_btn = QPushButton("Scan")
        scan_btn.clicked.connect(self._scan)
        row.addWidget(QLabel("Frames folder:"))
        row.addWidget(self.source, 1)
        row.addWidget(browse)
        row.addWidget(scan_btn)
        lay.addLayout(row)

        self.list = QListWidget()
        lay.addWidget(self.list, 1)

        self.delete = QCheckBox("Delete the original frames after each successful conversion")
        self.delete.setToolTip("Only after the video's frame count matches the number of frames read.")
        lay.addWidget(self.delete)

        self.label = QLabel()
        self.label.setWordWrap(True)
        lay.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.go = self.buttons.addButton("Convert", QDialogButtonBox.ButtonRole.ActionRole)
        self.go.clicked.connect(self._start)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._scan()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Krita Recorder folder", self.source.text())
        if d:
            self.source.setText(d)
            self._scan()

    def _scan(self):
        self.list.clear()
        dest = settings.get("directory")
        jobs = scan(self.source.text())
        for job in jobs:
            done = already_imported(dest, job)
            text = (f"{describe_key(job['key'])}  —  {len(job['frames'])} frames, "
                    f"{human_size(job['bytes'])}")
            if done:
                text += "  (already converted)"
            elif job["in_use"]:
                text += "  (still being recorded? turn off the built-in Recorder first)"
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if done or job["in_use"] else Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, job)
            self.list.addItem(item)
        total = sum(j["bytes"] for j in jobs)
        self.label.setText(f"Found {len(jobs)} recording(s) using {human_size(total)}." if jobs
                           else "No Krita Recorder frame folders found here.")

    def _checked_jobs(self):
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def _start(self):
        jobs = self._checked_jobs()
        if not jobs:
            return
        if self.delete.isChecked():
            size = human_size(sum(j["bytes"] for j in jobs))
            if QMessageBox.question(self, "Delete originals",
                                    f"After each recording converts and its frame count checks out, "
                                    f"its original frame images ({size} in total) will be permanently "
                                    f"deleted. Continue?") != QMessageBox.StandardButton.Yes:
                return
        if not settings.ffmpeg_path():
            QMessageBox.warning(self, "Convert", "ffmpeg was not found.")
            return
        self.go.setEnabled(False)
        self.list.setEnabled(False)
        self.delete.setEnabled(False)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.show()
        self.freed = 0
        self._delete = self.delete.isChecked()
        self.converter = Converter(jobs, settings.get("directory"))
        self.converter.progress.connect(self._progress)
        self.converter.job_done.connect(self._job_done)
        self.converter.finished.connect(self._finished)
        self.converter.start()

    def _progress(self, done, total, msg):
        self.bar.setValue(int(done / max(1, total) * 1000))
        self.label.setText(f"{msg}  ({encoders.BY_ID.get(self.converter.encoder, ('', ''))[1]})")

    def _job_done(self, result):
        job = result["job"]
        if result.get("error"):
            QMessageBox.warning(self, "Convert", f"{describe_key(job['key'])} failed:\n{result['error']}")
            return
        merge(result)
        if self._delete and result["verified"] and not result["skipped"]:
            self.freed += job["bytes"]
            threading.Thread(target=delete_frames, args=(job,), daemon=True).start()

    def _finished(self, summary):
        self.bar.setValue(1000)
        if self.freed:
            summary += f" Freed {human_size(self.freed)}."
        self.label.setText(summary)
        self.go.setEnabled(True)
        self.list.setEnabled(True)
        self.delete.setEnabled(True)
        self.converter = None
        self._scan()

    def reject(self):
        if self.converter is not None:
            if QMessageBox.question(self, "Convert", "Stop converting? The recording in progress "
                                    "is discarded; finished ones are kept.") != QMessageBox.StandardButton.Yes:
                return
            self.converter.cancelled = True
        super().reject()
