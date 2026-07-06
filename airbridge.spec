# PyInstaller spec for the AirBridge tray app (onedir, windowed).
# Build: uv run python build_assets/make_ico.py && uv run pyinstaller airbridge.spec
# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ["tray.py"],
    pathex=[],
    binaries=[],
    # imageio_ffmpeg ships the ffmpeg exe as package data; collect it so
    # video thumbnails work in the frozen app.
    datas=[("web/index.html", "web")] + collect_data_files("imageio_ffmpeg"),
    # pystray picks its backend dynamically; pillow_heif and imageio_ffmpeg
    # are imported lazily.
    hiddenimports=["pystray._win32", "pillow_heif", "imageio_ffmpeg"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AirBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="build/airbridge.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AirBridge",
)
