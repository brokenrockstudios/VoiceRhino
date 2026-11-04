# PyInstaller spec for the portable Windows build. Run through packaging/build.py, which makes the
# icon and version file first and zips the result. Folder build (not one-file): nothing unpacks to
# %TEMP% at start, which is what antivirus heuristics dislike most. No UPX for the same reason.
# Models are never bundled; the app downloads them when they're picked.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
WORK = Path(os.environ["VOICERHINO_BUILD_DIR"])  # icon + version file from build.py
CONSOLE = os.environ.get("VOICERHINO_CONSOLE") == "1"  # debug builds: print to a terminal

# The whole standard library (minus GUI/test bits), so the GPU add-on's packages, unpacked next to
# the models later, find every stdlib module they import. Costs a few MB.
STDLIB_SKIP = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip", "venv",
               "pydoc_data", "distutils", "msilib", "curses", "readline", "this", "antigravity",
               "__phello__", "_tkinter", "_curses", "_curses_panel", "_testcapi", "_testinternalcapi"}
stdlib = []
for name in sorted(sys.stdlib_module_names):
    if name in STDLIB_SKIP or name.startswith(("_test", "_xx")):
        continue
    stdlib += collect_submodules(name, filter=lambda n: ".test" not in n and "idle_test" not in n)

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    hiddenimports=collect_submodules("voicerhino") + stdlib,
    datas=[(str(WORK / "gpu_addon.json"), ".")],
    excludes=[
        "tkinter", "_tkinter", "pytest", "_pytest", "pygments", "IPython", "matplotlib",
        "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL",
        "PySide6.QtSvg", "PySide6.QtPdf", "PySide6.QtDBus",
        "faster_whisper",  # GPU add-on, downloaded in the app when a GPU model is picked
    ],
    noarchive=False,
)

# Qt pieces the three widget modules don't need. Keep the platform, style, image-format and
# icon-engine plugins; drop translations of Qt's own dialogs, software OpenGL and QML.
DROP = ("opengl32sw.dll", "Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6Network", "Qt6OpenGL", "Qt6Svg",
        "Qt6VirtualKeyboard", "Qt6DBus", "translations", "qml", "qmlls", "Designer",
        "plugins/tls", "plugins/networkinformation", "plugins/generic", "plugins/platforminputcontexts",
        "plugins/imageformats/qpdf", "plugins/imageformats/qsvg", "plugins/iconengines/qsvg")


def keep(entry) -> bool:
    name = entry[0].replace("\\", "/")
    return not any(part in name for part in DROP)


a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoiceRhino",
    icon=str(WORK / "VoiceRhino.ico"),
    version=str(WORK / "version_info.txt"),
    console=CONSOLE,
    upx=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="VoiceRhino", upx=False)
