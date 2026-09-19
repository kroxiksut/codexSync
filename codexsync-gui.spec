# -*- mode: python ; coding: utf-8 -*-
#
# Windowed CodexSync.exe: the Qt window, and -- when given a command -- the
# same CLI as codexsync.exe. The scheduled task runs this file directly
# (system_scheduler.job_command() returns sys.executable when frozen), which is
# why it is built without a console: a task that starts every minute must not
# flash a window. codexsync.spec stays the console CLI without Qt.
#
# Build it into its own output directory when both exes are built:
#   pyinstaller --clean --noconfirm --distpath dist/gui --workpath build/gui codexsync-gui.spec
# Windows file names are case-insensitive, so dist/CodexSync.exe and the CLI's
# dist/codexsync.exe are one file and the second build would replace the first.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).resolve()
package = project_root / "src" / "codexsync"
entrypoint = project_root / "scripts" / "pyinstaller_gui_entrypoint.py"
icon = package / "gui" / "resources" / "codexsync.ico"

datas = [(str(package / "config.example.toml"), "codexsync")]
datas += [(str(path), "codexsync/gui/locale") for path in sorted((package / "gui" / "locale").glob("*.json"))]
datas += [
    (str(path), "codexsync/gui/resources")
    for path in sorted((package / "gui" / "resources").iterdir())
    if path.is_file()
]


a = Analysis(
    [str(entrypoint)],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    # The entry point imports the CLI and the GUI lazily, and the window loads
    # its screens by module; bundling the whole package keeps a command that
    # no import statement happens to name from failing only in the frozen exe.
    hiddenimports=collect_submodules("codexsync"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CodexSync",
    icon=str(icon),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is known to corrupt Qt plugin DLLs; the size saving is not worth a
    # window that fails to start on some machines.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
