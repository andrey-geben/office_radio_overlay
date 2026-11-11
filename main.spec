# office_radio_overlay.spec
# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all

# --- pjsua2: DLL + скрытые импорты ---
pjsua2_datas, pjsua2_binaries, pjsua2_hidden = collect_all('pjsua2')

# --- sounddevice: папка _sounddevice_data с portaudio DLL ---
import sounddevice
sd_dir = os.path.dirname(sounddevice.__file__)
sd_data_dir = os.path.join(os.path.dirname(sd_dir), '_sounddevice_data')

datas = [
    ('templates', 'templates'),
    ('static', 'static'),
    # assets/ (chime.wav, settings.json, users.json, announcements/) и ffmpeg.exe
    # НЕ бандлим — копируются вручную рядом с .exe после сборки.
    *pjsua2_datas,
]

if os.path.isdir(sd_data_dir):
    datas.append((sd_data_dir, '_sounddevice_data'))

binaries = [
    *pjsua2_binaries,
]

hiddenimports = [
    'jinja2.ext',
    'sip_worker',
    'preflight',
    *pjsua2_hidden,
    'scipy.signal',
    'scipy._lib.array_api_compat.numpy.fft',
    'numpy',
    'pydub',
    'psutil',
    'sounddevice',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'PyQt5', 'PySide2', 'IPython',
        'PySide6', 'pandas', 'notebook',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='office_radio_overlay',
    icon='app.ico' if os.path.isfile('app.ico') else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='office_radio_overlay',
)
