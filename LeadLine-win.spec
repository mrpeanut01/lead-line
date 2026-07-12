# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for LeadLine.exe — single-file windowed Windows build.
# Must be built on Windows (PyInstaller does not cross-compile); CI does this
# in .github/workflows/build-windows.yml. Requires the Edge WebView2 runtime
# at run time (preinstalled on Windows 11 and kept current on Windows 10).

a = Analysis(
    ['run_leadline.py'],
    pathex=[],
    binaries=[],
    datas=[('leadline/ui', 'leadline/ui')],
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
    name='LeadLine',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
