# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('Sensors.xlsx', '.'), ('sensor_overrides.json', '.')]
binaries = []
hiddenimports = ['scipy.signal', 'scipy.io', 'scipy.fft', 'scipy.signal._signaltools', 'openpyxl', 'pandas', 'alert2', 'alert2_app', 'alert2_decoder']
tmp_ret = collect_all('sounddevice')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['field_application.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here was ever filtered, so PyInstaller swept up whatever else
    # lives in site-packages: torch alone was 302 MB of a 799 MB build, plus
    # transformers, sklearn, pyarrow and llvmlite. This app uses numpy, scipy,
    # pandas (Excel only), openpyxl, sounddevice and tkinter - nothing else.
    # Anything added here must be re-tested by launching the built exe, since
    # a wrongly excluded module fails at startup, not at build time.
    excludes=[
        'torch', 'torchvision', 'torchaudio', 'transformers', 'tokenizers',
        'safetensors', 'huggingface_hub', 'accelerate',
        'sklearn', 'scikit-learn', 'pyarrow', 'llvmlite', 'numba',
        'tensorflow', 'jax', 'sympy', 'cv2',
        'matplotlib', 'PIL', 'IPython', 'jupyter', 'notebook', 'nbformat',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SDR ALERT Decoder',
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
    name='SDR ALERT Decoder',
)
