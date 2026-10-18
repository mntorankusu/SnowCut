# -*- mode: python ; coding: utf-8 -*-
import os
import sys

# Directory where this spec file lives (pc-app)
APP_DIR = os.path.abspath(SPECPATH)
# Everything the app needs lives in pc-app (pc-driver/, tools/, patches/, replacement/ are copies)
DRIVER_DIR = os.path.join(APP_DIR, "pc-driver")

# Determine packaging mode: default is onedir (faster startup, clean folder distribution),
# but SNOWCUT_ONEFILE=1 can be passed via environment to build a single standalone EXE.
is_onefile = os.environ.get("SNOWCUT_ONEFILE", "0").lower() in ("1", "true", "yes")

# Base data files: icon and logo only
datas = [
    (os.path.join(APP_DIR, "icon.ico"), "."),
    (os.path.join(APP_DIR, "logo.png"), "."),
    # SnowCut firmware patch (only SnowCut's own bytes, applied to the user's stock .upf)
    (os.path.join(APP_DIR, "patches", "cm110_v1819w.sncpatch.json"), "patches"),
]
# Replacement boot logo / splash PNGs applied by the patcher
import glob
datas += [(p, "replacement") for p in sorted(glob.glob(os.path.join(APP_DIR, "replacement", "*.png")))]

# Audit & collect ONLY the SVG icons actually referenced in the application (from ui/icons.py)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
try:
    from ui import icons as icons_module
    used_icon_files = set()
    for name in icons_module.ICONS:
        p = icons_module.icon_path(name)
        if p and os.path.exists(p):
            rel = os.path.relpath(p, APP_DIR)
            dest_dir = os.path.dirname(rel)
            used_icon_files.add((os.path.abspath(p), dest_dir))
    datas.extend(sorted(used_icon_files))
except Exception as e:
    print(f"Warning: could not resolve used icons: {e}")

# Search paths for module resolution:
pathex = [
    APP_DIR,
    DRIVER_DIR,
]

# Explicit hidden imports to ensure dynamically imported and optional packages are included:
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtSvg",
    "PIL",
    "PIL.Image",
    "PIL.ImageOps",
    "numpy",
    "usb",
    "usb.core",
    "usb.util",
    "usb.backend",
    "usb.backend.libusb1",
    "pypdfium2",
    # Hardware driver modules:
    "memdump",
    "scsipt",
    "busy",
    "buttons",
    "cm110_fs",
    "cm110_eeprom",
    "cm110_settings",
    "cm110_usb",
    "updater",
    "winfsp_mount",
    "tools.settings_handler",
    "tools.apply_patch",
    "tools.upf",
    "tools.lzss",
    "tools.splash_handler",
]

# Exclude unnecessary modules and packages to keep the bundle lean:
excludes = [
    # Heavy / unused Qt modules
    "PySide6.QtNetwork",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    # Unused standard library & 3rd party modules
    "tkinter",
    "matplotlib",
    "scipy",
    "pytest",
    "unittest",
    "sqlite3",
    "ssl",
    "_ssl",
    "asyncio",
    "_asyncio",
    "multiprocessing",
    "_multiprocessing",
    "concurrent",
    "xmlrpc",
    "email",
    "http",
    "urllib",
    "pdb",
    "doctest",
]

a = Analysis(
    [os.path.join(APP_DIR, "main.py")],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# Audit & filter binaries: exclude massive unused DLLs
excluded_binary_patterns = (
    "opengl32sw.dll",      # 24MB software OpenGL fallback (not used, Windows native D3D/raster is used)
    "Qt6Network.dll",      # Unused network layer
    "QtNetwork.pyd",
    "libcrypto",           # Unused OpenSSL crypto
    "libssl",              # Unused OpenSSL SSL
    "qopensslbackend",     # TLS plugin
    "qschannelbackend",
    "qcertonlybackend",
    "qnetworkinformation", # Network info plugin
    "qtuiotouchplugin",    # Touch plugin
    "_avif.cp",            # Unused AVIF image plugin in PIL (8MB)
)

def keep_binary(entry):
    name = os.path.basename(entry[0]).lower()
    for pat in excluded_binary_patterns:
        if pat.lower() in name:
            return False
    return True

a.binaries = [b for b in a.binaries if keep_binary(b)]

# Audit & filter datas: exclude translations and dist-info metadata
def keep_data(entry):
    dest = entry[0].replace("\\", "/").lower()
    if "pyside6/translations" in dest:
        return False
    if ".dist-info" in dest:
        return False
    return True

a.datas = [d for d in a.datas if keep_data(d)]

pyz = PYZ(a.pure)

if is_onefile:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="SnowCut",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=os.path.join(APP_DIR, "icon.ico"),
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="SnowCut",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=os.path.join(APP_DIR, "icon.ico"),
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="SnowCut",
    )
