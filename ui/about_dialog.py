"""Help > About: the SnowCut logo, version and a link to the project page."""
import os
import sys
from PySide6 import QtWidgets, QtGui, QtCore

BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGO_PATH = os.path.join(BUNDLE_DIR, "logo.png")
PROJECT_URL = "https://github.com/mntorankusu/SnowCut"
APP_VERSION = "1.0.0"


class AboutDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About SnowCut")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 16)
        lay.setSpacing(16)

        self.logo = QtWidgets.QLabel()
        self.logo.setAlignment(QtCore.Qt.AlignCenter)
        pix = QtGui.QPixmap(LOGO_PATH)
        if pix.isNull():
            self.logo.setText("SnowCut")
        else:
            self.logo.setPixmap(pix)
        lay.addWidget(self.logo)

        self.version = QtWidgets.QLabel("Version %s" % APP_VERSION)
        self.version.setAlignment(QtCore.Qt.AlignCenter)
        lay.addWidget(self.version)

        self.link = QtWidgets.QLabel('<a href="%s">%s</a>' % (PROJECT_URL, PROJECT_URL))
        self.link.setAlignment(QtCore.Qt.AlignCenter)
        self.link.setTextFormat(QtCore.Qt.RichText)
        self.link.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        self.link.setOpenExternalLinks(True)
        lay.addWidget(self.link)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.setFixedSize(self.sizeHint())
