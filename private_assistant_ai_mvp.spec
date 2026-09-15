# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all
voice_datas, voice_binaries, voice_imports = [], [], []
for package in ("faster_whisper", "ctranslate2", "sounddevice", "_sounddevice_data", "sherpa_onnx", "pypinyin", "opencc", "sentencepiece", "webrtcvad"):
    data, binaries, imports = collect_all(package)
    voice_datas += data
    voice_binaries += binaries
    voice_imports += imports
block_cipher = None

a = Analysis(
    ["run_silent.pyw"],
    pathex=[],
    binaries=voice_binaries,
    datas=voice_datas,
    hiddenimports=voice_imports + [
        "win32gui",
        "win32process",
        "psutil",
        "winotify",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name="PrivateAssistantAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PrivateAssistantAI",
)
