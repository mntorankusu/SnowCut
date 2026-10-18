"""SnowCut entry. Run: python main.py [project.sncproj | old project.json]"""
import sys
import os
if getattr(sys, "frozen", False):
    HERE = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if len(sys.argv) == 4 and sys.argv[1] == "--scsi-broker":
    # the elevated USB helper (pc-driver/scsipt.py): this same exe, started with a UAC prompt
    import scsipt
    sys.exit(scsipt.broker_main(sys.argv[2], sys.argv[3]))
from PySide6 import QtCore, QtGui, QtWidgets
from ui.main_window import MainWindow, APP_NAME, APP_VERSION, apply_theme

APP_ICON = os.path.join(HERE, "icon.ico")


def main():
    if sys.platform == "win32":
        # own taskbar identity, so Windows shows the app icon rather than python.exe's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SnowCut")
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QtGui.QIcon(APP_ICON))
    apply_theme(app)
    project = None
    for a in sys.argv[1:]:
        if a.lower().endswith((".sncproj", ".json")) and os.path.exists(a):
            project = os.path.abspath(a)    # an installer file association passes the path
    w = MainWindow()
    w.show()
    if project:
        w.open_project(project)     # same path as File > Open: recent list, error message
    # machine reachable only through Windows' USB storage driver -> offer the elevated helper,
    # then ask the machine whether it is running the SnowCut firmware (original -> USB-drive install)
    QtCore.QTimer.singleShot(600, w.startup_machine_check)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
