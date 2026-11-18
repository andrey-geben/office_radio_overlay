# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

# --- Собираем нативные зависимости pjsua2 и sounddevice ---
pj_datas, pj_binaries, pj_hidden = collect_all('pjsua2')
sd_datas, sd_binaries, sd_hidden = collect_all('sounddevice')

# --- Проверка иконки ---
icon_path = os.path.join(SPECPATH, 'app.ico')
if not os.path.isfile(icon_path):
    icon_path = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pj_binaries + sd_binaries,
    datas=pj_datas + sd_datas + [
        ('templates', 'templates'),
        ('static', 'static'),
        # CA-бандл для HTTPS-потоков: PyInstaller 6 кладёт его в _internal,
        # audio_capture._find_ca_bundle() ищет и там, и рядом с .exe.
        ('cacert.pem', '.'),
        # ВНИМАНИЕ: assets НЕ включаем — создаётся при первом запуске
    ],
    hiddenimports=pj_hidden + sd_hidden + [
        'engine',
        'sip_client',
        'sip_worker',
        'audio_capture',
        'audio_output',
        'preflight',
        'auth',
        'announcement_store',
        'settings_store',
        'users_store',
        'config',
        'tone_detector',
        'dtmf_detector',
        'mixer',
        'make_chime',
        'logging_setup',
        'sounddevice',
        'numpy',
        'scipy',
        'scipy._lib.array_api_compat.numpy.fft',
        'scipy.signal.windows',
        'scipy.special',
        'soxr',
        'pydub',
        'psutil',
        'pjsua2',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OfficeRadioOverlay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='OfficeRadioOverlay',
)