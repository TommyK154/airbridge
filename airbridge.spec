# PyInstaller spec for the AirBridge tray app (onedir, windowed).
# Build: uv run python build_assets/make_ico.py && uv run pyinstaller airbridge.spec
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["tray.py"],
    pathex=[],
    binaries=[],
    datas=[("web/index.html", "web")],
    # pystray picks its backend dynamically; pillow_heif is imported lazily.
    hiddenimports=["pystray._win32", "pillow_heif"],
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
