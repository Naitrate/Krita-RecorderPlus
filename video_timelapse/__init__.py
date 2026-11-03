from krita import DockWidgetFactory, DockWidgetFactoryBase, Extension, Krita

from .docker import TimelapseDocker, detect_encoders
from .qt import QApplication
from .recorder import RECORDER

TOGGLE_ID = "video_timelapse_toggle"


class VideoTimelapseExtension(Extension):

    def __init__(self, parent):
        super().__init__(parent)
        self._actions = []

    def setup(self):
        notifier = Krita.instance().notifier()
        notifier.setActive(True)
        notifier.applicationClosing.connect(RECORDER.shutdown)
        QApplication.instance().aboutToQuit.connect(RECORDER.shutdown)
        RECORDER.changed.connect(self._sync_actions)
        detect_encoders()
        RECORDER.start()

    def createActions(self, window):
        action = window.createAction(TOGGLE_ID, "Toggle Recorder+ Recording", "tools")
        action.setCheckable(True)
        action.setChecked(RECORDER.enabled)
        action.toggled.connect(RECORDER.set_enabled)
        self._actions.append(action)

    def _sync_actions(self):
        for action in self._actions:
            if action.isChecked() != RECORDER.enabled:
                action.blockSignals(True)
                action.setChecked(RECORDER.enabled)
                action.blockSignals(False)


Krita.instance().addExtension(VideoTimelapseExtension(Krita.instance()))
Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("video_timelapse", DockWidgetFactoryBase.DockPosition.DockRight, TimelapseDocker))
