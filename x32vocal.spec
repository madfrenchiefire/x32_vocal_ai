# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the single-file X32 Vocal AI .exe.

Build on Windows (PyInstaller targets the OS it runs on -- a Windows .exe
must be built on Windows):

    pip install -r requirements.txt pyinstaller
    python -m app.tools.license_gen init        # once, embeds your public key
    pyinstaller x32vocal.spec

Output: dist/X32VocalAI.exe (one file). See docs/PACKAGING.md for the ASIO
DLL step and antivirus notes.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# The Flask template (index.html) must be bundled -- render_template reads it
# from app/web/templates at runtime.
datas = [("app/web/templates", "app/web/templates")]

# sounddevice ships the PortAudio DLL in _sounddevice_data; pull in its data
# files and dynamic libs so audio works in the frozen app. (Replace that DLL
# with an ASIO-enabled build before shipping -- see docs/PACKAGING.md.)
datas += collect_data_files("sounddevice")
binaries = collect_dynamic_libs("sounddevice")

hiddenimports = [
    # flask-socketio in threading async mode loads this driver dynamically.
    "engineio.async_drivers.threading",
    # mido loads its backend by name at runtime.
    "mido.backends.rtmidi",
    "rtmidi",
]

# Keep the binary lean: the ML path (onnxruntime) isn't built yet, and torch
# is training-only. Excluding them avoids bundling hundreds of MB that the
# runtime never imports.
excludes = ["onnxruntime", "torch", "matplotlib", "pytest"]


a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="X32VocalAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=True keeps a window that shows startup/license/errors. Flip to
    # False for a windowless launch (logs still go to logs/*.jsonl).
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="assets/icon.ico",   # add your own icon if you have one
)
