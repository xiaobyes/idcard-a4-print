# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 单文件打包配置（相对路径版，可直接用于 GitHub 源码仓库）
# 用法：pyinstaller --noconfirm build.spec
import os
from PyInstaller.utils.hooks import collect_all

HERE = globals().get('SPECPATH') or (
    os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd())
HERE = os.path.abspath(HERE)
ICON = os.path.join(HERE, 'assets', 'app.ico')

datas = [(ICON, '.')]
binaries = []
hiddenimports = []
for pkg in ('customtkinter', 'tkinterdnd2'):
    ret = collect_all(pkg)
    datas += ret[0]
    binaries += ret[1]
    hiddenimports += ret[2]

a = Analysis(
    [os.path.join(HERE, 'idcard2a4.py')],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'pandas', 'scipy', 'PyQt5', 'PyQt6', 'IPython',
              'notebook', 'torch', 'tensorflow', 'pytest'],
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
    name='身份证A4打印工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON],
)
