# office_radio_overlay.spec
# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

# --- Собираем всё нужное из pjsua2 (DLL + скрытые импорты) ---
pjsua2_datas, pjsua2_binaries, pjsua2_hidden = collect_all('pjsua2')

# --- sounddevice: добавляем папку _sounddevice_data с portaudio DLL ---
# PyInstaller иногда её подхватывает, но надёжнее указать явно.
import sounddevice
sd_dir = os.path.dirname(sounddevice.__file__)
sd_data_dir = os.path.join(os.path.dirname(sd_dir), '_sounddevice_data')

datas = [
    ('templates', 'templates'),
    ('static', 'static'),
    # pjsua2
    *pjsua2_datas,
]

# Если _sounddevice_data существует — добавляем
if os.path.isdir(sd_data_dir):
    datas.append((sd_data_dir, '_sounddevice_data'))

binaries = [
    *pjsua2_binaries,
]

hiddenimports = [
    # Flask-шаблоны и Jinja компилируются динамически
    'jinja2.ext',
    # pjsua2
    *pjsua2_hidden,
    # scipy/numpy часто требуют явных подсказок
    'scipy.signal',
    'scipy._lib.array_api_compat.numpy.fft',
    'numpy',
    'pydub',
    'psutil',
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
        # Уменьшаем размер — выкидываем неиспользуемые тяжёлые пакеты
        'tkinter', 'matplotlib', 'PyQt5', 'PySide2', 'IPython',
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
    icon='app.ico',
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