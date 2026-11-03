"""Joins a recording's segments into one video, optionally adding Krita-style
extras (final image shown at the start, held at the end, length limit)."""

import os

from . import encoders, settings
from .qt import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                 QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                 QProcess, QProgressBar, QPushButton, QSpinBox, QVBoxLayout)
from .recorder import RECORDER, load_info, segment_paths


def _concat_line(path):
    return "file '" + path.replace("'", "'\\''") + "'\n"


def _probe_duration(path):
    probe = settings.ffprobe_path()
    if not probe:
        return 0.0
    try:
        out = encoders.run_quiet([probe, "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", path], 60).stdout
        return float(out.decode().strip() or 0)
    except Exception:
        return 0.0


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def plan(folder, info, out_path, preview, hold, limit, speed, out_fps):
    """Return (list of ffmpeg argument lists, expected output seconds).

    speed   -- captures shown per second of video (Krita's "input FPS")
    out_fps -- frame rate of the exported file"""
    ff = settings.ffmpeg_path()
    segs = segment_paths(folder, info)
    list_file = os.path.join(folder, "concat.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        f.writelines(_concat_line(p) for p in segs)

    entries = [s for s in info["segments"] if os.path.isfile(os.path.join(folder, s["file"]))]
    codecs = {s.get("codec") for s in entries}
    sizes = [(s.get("width", info["width"]), s.get("height", info["height"])) for s in entries]
    # Like Krita's recorder export: the video takes the size of the final frame.
    w, h = sizes[-1]
    mixed = len(codecs) > 1 or len(set(sizes)) > 1
    rec_fps = info["fps"]
    duration = sum(_probe_duration(p) for p in segs)   # at recording fps
    factor = rec_fps / speed                            # pts multiplier
    played = duration * factor
    if limit > 0 and played > limit:
        factor *= limit / played
        played = limit
    retime = abs(factor - 1) > 1e-6 or out_fps != rec_fps
    mp4 = os.path.splitext(out_path)[1].lower() in (".mp4", ".mov", ".m4v")
    reencode = preview > 0 or hold > 0 or retime or mixed

    if not reencode:
        codec = next(iter(codecs), "")
        cmd = [ff, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy"]
        if mp4:
            cmd += (["-tag:v", "hvc1"] if codec == "hevc" else []) + ["-movflags", "+faststart"]
        return [cmd + [out_path]], played

    steps = []
    last_png = os.path.join(folder, "last.png")
    if preview > 0 and not os.path.isfile(last_png):
        # Recording crashed before last.png was written: pull it from the video.
        steps.append([ff, "-hide_banner", "-y", "-sseof", "-3", "-i", segs[-1],
                      "-update", "1", last_png])

    enc = RECORDER.chosen_encoder()
    if max(w, h) > encoders.max_dim(enc):
        # e.g. H.264 (4096 max) chosen but the canvas grew past it: use the
        # best working encoder that can take the final size.
        enc = next((e for e in RECORDER.detected if max(w, h) <= encoders.max_dim(e)), enc)
    if mixed:
        # Segments differ in size (canvas cropped/resized) or codec, which the
        # concat demuxer can't handle: decode each one, fit it into the final
        # size with black bars as Krita's export does, and join in-graph.
        fit = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
        inputs = [a for p in segs for a in ("-i", p)]
        joined = "".join(f"[{i}:v]{fit},setsar=1,format=yuv420p[s{i}];" for i in range(len(segs)))
        main = joined + "".join(f"[s{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0"
    else:
        inputs = ["-f", "concat", "-safe", "0", "-i", list_file]
        main = "[0:v]setsar=1"
    still = len(segs) if mixed else 1
    if retime:
        main += f",setpts=PTS*{factor:.8f},fps={out_fps}"
    if hold > 0:
        main += f",tpad=stop_mode=clone:stop_duration={hold}"
    main += ",format=yuv420p"
    tail = ",".join(encoders.filter_tail(enc))

    if preview > 0:
        inputs += ["-loop", "1", "-framerate", str(out_fps), "-t", str(preview), "-i", last_png]
        graph = (f"[{still}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                 f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setsar=1[p];"
                 f"{main}[m];[p][m]concat=n=2:v=1:a=0,{tail}[out]")
    else:
        graph = f"{main},{tail}[out]"

    cmd = [ff, "-hide_banner", "-y", *encoders.input_args(enc), *inputs,
           "-filter_complex", graph, "-map", "[out]",
           *encoders.codec_args(enc, settings.get("quality"), out_fps), *encoders.COLOR_TAGS]
    if mp4:
        cmd += (["-tag:v", "hvc1"] if encoders.codec_of(enc) == "hevc" else []) + ["-movflags", "+faststart"]
    steps.append(cmd + [out_path])
    return steps, played + preview + hold


class ExportDialog(QDialog):

    def __init__(self, folder, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.info = load_info(folder)
        self.setWindowTitle("Export Timelapse")
        self.setMinimumWidth(480)

        segs = segment_paths(folder, self.info)
        size = sum(os.path.getsize(p) for p in segs)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{self.info.get('name', '')}</b> — {len(segs)} session(s), "
                             f"{self.info['width']}×{self.info['height']} @ {self.info['fps']} fps, "
                             f"{human_size(size)}"))

        form = QFormLayout()
        row = QHBoxLayout()
        self.path = QLineEdit(self._default_output())
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path)
        row.addWidget(browse)
        form.addRow("Output file:", row)

        self.preview = QDoubleSpinBox(suffix=" s", maximum=60, singleStep=0.5)
        self.preview.setValue(settings.get("export_preview"))
        self.preview.setToolTip("Show the finished image for this long before the timelapse plays.")
        form.addRow("Show result at start:", self.preview)
        self.hold = QDoubleSpinBox(suffix=" s", maximum=60, singleStep=0.5)
        self.hold.setValue(settings.get("export_hold"))
        form.addRow("Hold last frame:", self.hold)
        self.speed = QSpinBox(minimum=1, maximum=10000)
        self.speed.setValue(settings.get("export_speed") or self.info["fps"])
        self.speed.setToolTip("How many captures play per second of video (Krita's \"Input FPS\"). "
                              f"Equal to the recording's {self.info['fps']} fps = real speed, no re-encode.")
        form.addRow("Captures per second:", self.speed)
        self.out_fps = QSpinBox(minimum=1, maximum=240)
        self.out_fps.setValue(settings.get("export_fps") or self.info["fps"])
        form.addRow("Output FPS:", self.out_fps)
        self.limit = QSpinBox(suffix=" s", maximum=36000, specialValueText="No limit")
        self.limit.setValue(settings.get("export_limit"))
        self.limit.setToolTip("Speed the timelapse up so it fits in this many seconds.")
        form.addRow("Limit length to:", self.limit)
        lay.addLayout(form)

        self.note = QLabel(f"With captures/s and output FPS at {self.info['fps']} and the extras at 0, sessions "
                           "are joined without re-encoding (instant, lossless).")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.export_btn = self.buttons.addButton("Export", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self._start)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self.proc = None
        self.steps = []
        self.expected = 0.0
        self._stderr = b""

    def _default_output(self):
        doc = self.info.get("document") or ""
        base = os.path.splitext(os.path.basename(doc))[0] if doc else self.info.get("name") or "timelapse"
        where = settings.get("export_dir")
        if not os.path.isdir(where):
            where = os.path.dirname(doc) if doc and os.path.isdir(os.path.dirname(doc)) else self.folder
        return os.path.join(where, f"{base}_timelapse.mp4")

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export timelapse", self.path.text(),
                                              "Video (*.mp4 *.mkv *.mov)")
        if path:
            self.path.setText(path)

    def _start(self):
        settings.put("export_preview", self.preview.value())
        settings.put("export_hold", self.hold.value())
        settings.put("export_limit", self.limit.value())
        settings.put("export_speed", self.speed.value())
        settings.put("export_fps", self.out_fps.value())
        if RECORDER.segment is not None and RECORDER.folder == self.folder:
            # Finalise the open session so it's included.
            RECORDER.close_segment()
        self.info = load_info(self.folder)
        if not segment_paths(self.folder, self.info):
            QMessageBox.warning(self, "Export Timelapse", "This recording has no video yet.")
            return
        out = self.path.text().strip()
        if not os.path.splitext(out)[1]:
            out += ".mp4"
        self.out = out
        self.steps, self.expected = plan(self.folder, self.info, out, self.preview.value(),
                                         self.hold.value(), self.limit.value(),
                                         self.speed.value(), self.out_fps.value())
        self.export_btn.setEnabled(False)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.show()
        self._next()

    def _next(self):
        if not self.steps:
            self.bar.setValue(1000)
            self.export_btn.setEnabled(True)
            self.note.setText(f"Saved {self.out} ({human_size(os.path.getsize(self.out))}).")
            return
        cmd = self.steps.pop(0)
        self._stderr = b""
        self.proc = QProcess(self)
        self.proc.readyReadStandardOutput.connect(self._progress)
        self.proc.readyReadStandardError.connect(self._err)
        self.proc.finished.connect(self._done)
        self.proc.start(cmd[0], cmd[1:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]])

    def _progress(self):
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.startswith("out_time_us=") and self.expected > 0:
                try:
                    secs = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                self.bar.setValue(min(999, int(secs / self.expected * 1000)))

    def _err(self):
        self._stderr = (self._stderr + bytes(self.proc.readAllStandardError()))[-4000:]

    def _done(self, code, _status=None):
        if code != 0:
            self.export_btn.setEnabled(True)
            self.bar.hide()
            QMessageBox.warning(self, "Export Timelapse", "ffmpeg failed:\n\n"
                                + self._stderr.decode("utf-8", "replace")[-1500:])
            return
        self._next()

    def reject(self):
        if self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(3000)
        super().reject()
