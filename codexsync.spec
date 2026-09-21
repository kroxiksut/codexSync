# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


project_root = Path(SPECPATH).resolve()
entrypoint = project_root / "scripts" / "pyinstaller_entrypoint.py"
template = project_root / "src" / "codexsync" / "config.example.toml"


a = Analysis(
    [str(entrypoint)],
    pathex=[str(project_root / "src")],
    binaries=[],
    # `copy_metadata` carries the `dist-info` that `version.py` reads; without
    # it the exe reports `0.0.0+unknown` and stamps that into snapshot
    # manifests. Hardcoding the version instead is forbidden by design.
    datas=copy_metadata("codexsync") + [(str(template), "codexsync")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The CLI exe must never grow a Qt payload just because the build machine
    # happens to have the optional extra installed. Nothing in the CLI import
    # graph reaches PySide6, and this makes that a build-time guarantee rather
    # than a property of whatever was in site-packages that day.
    excludes=["PySide6", "shiboken6", "PySide6.QtCore", "PySide6.QtWidgets", "PySide6.QtGui"],
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
    name="codexsync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
