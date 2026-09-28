# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

ROOT = Path(SPECPATH)


a = Analysis(
    [str(ROOT / 'app_260928.py')],
    pathex=[],
    binaries=[(str(ROOT / 'runtime' / 'ffmpeg.exe'), 'runtime'), (str(ROOT / 'runtime' / 'ffprobe.exe'), 'runtime')],
    datas=[(str(ROOT / 'index_260928.html'), '.')],
    hiddenimports=[],
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
    name='PixelKeeper_260928',
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
