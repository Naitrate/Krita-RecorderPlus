"""PyQt6 (Krita 6) / PyQt5 (Krita 5) compatibility shim."""

try:
    from PyQt6.QtCore import (QObject, QProcess, QTimer, Qt, QUrl,
                              pyqtSignal)
    from PyQt6.QtGui import QColor, QDesktopServices, QImage, QPainter
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QColorDialog,
                                 QComboBox, QDialog, QDialogButtonBox,
                                 QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                                 QMainWindow, QMessageBox, QProgressBar,
                                 QPushButton, QScrollArea, QSpinBox,
                                 QToolButton,
                                 QVBoxLayout, QWidget)
except ImportError:
    from PyQt5.QtCore import (QObject, QProcess, QTimer, Qt, QUrl,
                              pyqtSignal)
    from PyQt5.QtGui import QColor, QDesktopServices, QImage, QPainter
    from PyQt5.QtWidgets import (QApplication, QCheckBox, QColorDialog,
                                 QComboBox, QDialog, QDialogButtonBox,
                                 QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                                 QMainWindow, QMessageBox, QProgressBar,
                                 QPushButton, QScrollArea, QSpinBox,
                                 QToolButton,
                                 QVBoxLayout, QWidget)


def image_bytes(img):
    """Raw pixel bytes of a QImage (works on both sip.voidptr flavours)."""
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return bytes(ptr)
