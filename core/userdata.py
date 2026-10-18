"""Where the app keeps persistent user data: Documents/SnowCut.

    Documents/SnowCut/settings.ini    app settings (QSettings, ini format)
    Documents/SnowCut/Mats/*.json     custom mat profiles (core.mat)
    Documents/SnowCut/Scans/          scans taken while no saved project is open
    Documents/SnowCut/PDF Images/     images broken out of PDF objects (ui.image_ops)

SNOWCUT_USER_DIR overrides the folder (tests point it at a temp dir).
"""
import os

APP_DIR_NAME = "SnowCut"
ENV_OVERRIDE = "SNOWCUT_USER_DIR"


def documents_dir():
    """The user's Documents folder (follows OneDrive / folder redirection via Qt), else home."""
    try:
        from PySide6.QtCore import QStandardPaths
        d = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        if d and os.path.isdir(d):
            return d
    except Exception:
        pass
    d = os.path.join(os.path.expanduser("~"), "Documents")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def user_dir(*sub):
    """Documents/SnowCut (or a subfolder of it), created on first use."""
    root = os.environ.get(ENV_OVERRIDE) or os.path.join(documents_dir(), APP_DIR_NAME)
    d = os.path.join(root, *sub)
    os.makedirs(d, exist_ok=True)
    return d


def settings_path():
    return os.path.join(user_dir(), "settings.ini")


def settings():
    """The app's QSettings, stored as Documents/SnowCut/settings.ini."""
    from PySide6.QtCore import QSettings
    return QSettings(settings_path(), QSettings.IniFormat)
