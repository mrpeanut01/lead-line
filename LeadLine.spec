# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for LeadLine.app — version is read from leadline/__init__.py
import re
from pathlib import Path

version = re.search(r'__version__ = "(.+)"',
                    Path("leadline/__init__.py").read_text()).group(1)

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
    [],
    exclude_binaries=True,
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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LeadLine',
)
app = BUNDLE(
    coll,
    name='LeadLine.app',
    icon=None,
    bundle_identifier='io.github.mrpeanut01.leadline',
    version=version,
    info_plist={
        'CFBundleShortVersionString': version,
        'CFBundleVersion': version,
        'NSHighResolutionCapable': True,
    },
)
