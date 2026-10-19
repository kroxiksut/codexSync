# -*- mode: python ; coding: utf-8 -*-
#
# The Linux build (D-037, CS-421): one folder holding both programs --
# `codexsync`, the console without Qt, and `codexsync-gui`, the window that is
# also the CLI when given a command -- over one shared `_internal` of libraries.
# The .deb, .rpm, AppImage and tar.gz are this folder, packed four ways
# (scripts/linux_packages.py); nothing is built twice.
#
# A folder rather than one file per program: a one-file build unpacks itself
# into /tmp on every start, and the scheduler starts it every half hour. In a
# folder the programs start in place, and a package can put that folder under
# /opt with the commands linked onto PATH.
#
#   python -m PyInstaller --clean --noconfirm codexsync-linux.spec
#   -> dist/codexsync/{codexsync, codexsync-gui, _internal/}

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


project_root = Path(SPECPATH).resolve()
package = project_root / "src" / "codexsync"

# `copy_metadata` carries the `dist-info` that `version.py` reads; without it
# the build reports `0.0.0+unknown` and stamps that into snapshot manifests.
datas = copy_metadata("codexsync")
datas += [(str(package / "config.example.toml"), "codexsync")]
datas += [(str(path), "codexsync/gui/locale") for path in sorted((package / "gui" / "locale").glob("*.json"))]
# The menu entry and icon: `codexsync shortcut install` writes them from a
# tar.gz or an AppImage, which install no menu entry of their own (CS-399).
datas += [(str(path), "codexsync/desktop") for path in sorted((package / "desktop").iterdir()) if path.is_file()]
datas += [
    (str(path), "codexsync/gui/resources")
    for path in sorted((package / "gui" / "resources").iterdir())
    if path.is_file()
]

# The console program never carries Qt, even when the build machine has the
# optional extra installed -- the same build-time guarantee as codexsync.spec.
console = Analysis(
    [str(project_root / "scripts" / "pyinstaller_entrypoint.py")],
    pathex=[str(project_root / "src")],
    datas=datas,
    hiddenimports=[],
    excludes=["PySide6", "shiboken6", "PySide6.QtCore", "PySide6.QtWidgets", "PySide6.QtGui"],
    noarchive=False,
    optimize=0,
)
# The window loads its screens by module, and the CLI it falls back to imports
# its commands lazily: the whole package travels.
window = Analysis(
    [str(project_root / "scripts" / "pyinstaller_gui_entrypoint.py")],
    pathex=[str(project_root / "src")],
    datas=datas,
    hiddenimports=collect_submodules("codexsync"),
    # The window uses QtCore, QtGui and QtWidgets and nothing else.
    excludes=["PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf"],
    noarchive=False,
    optimize=0,
)

# What the Qt hooks bring along and the window never loads: QML and Quick, PDF,
# the on-screen keyboard, the embedded-device platforms (EGLFS, linuxfb, VNC,
# Vulkan display) and network status. Each would also need system libraries a
# desktop does not always have, and the release check (`ldd` on a clean
# system, scripts/linux_package_check.sh) would rightly refuse them. The
# platforms that remain are xcb, wayland, offscreen and minimal.
UNUSED_QT = (
    "libQt6Quick", "libQt6Qml", "libQt6Pdf", "libQt6VirtualKeyboard", "libQt6EglFS", "libQt6EglFsKms",
    "libqtvirtualkeyboardplugin", "imageformats/libqpdf", "egldeviceintegrations/",
    "platforms/libqeglfs", "platforms/libqminimalegl", "platforms/libqlinuxfb", "platforms/libqvnc",
    "platforms/libqvkkhrdisplay", "networkinformation/", "tls/", "PySide6/QtNetwork", "libQt6Network",
)


def _used(entry) -> bool:
    destination = entry[0].replace("\\", "/")
    return not any(part in destination for part in UNUSED_QT)


window.binaries = [entry for entry in window.binaries if _used(entry)]
window.datas = [entry for entry in window.datas if _used(entry)]

console_exe = EXE(
    PYZ(console.pure),
    console.scripts,
    [],
    exclude_binaries=True,
    name="codexsync",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
window_exe = EXE(
    PYZ(window.pure),
    window.scripts,
    [],
    exclude_binaries=True,
    name="codexsync-gui",
    debug=False,
    strip=False,
    # UPX is known to corrupt Qt libraries.
    upx=False,
    console=True,
)

COLLECT(
    console_exe,
    window_exe,
    console.binaries,
    console.datas,
    window.binaries,
    window.datas,
    strip=False,
    upx=False,
    name="codexsync",
)
