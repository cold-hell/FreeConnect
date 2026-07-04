# -*- mode: python ; coding: utf-8 -*-
"""Сборка FreeConnect в единый .exe с админ-манифестом.

Бандлим:
  - ui/                     фронтенд (index.html/app.js/style.css)
  - C:\\FreeConnect\\runtime  winws.exe + WinDivert + lists + strategies.json
Первый запуск разворачивает runtime в C:\\FreeConnect\\runtime (см. app._provision_runtime).
"""
from PyInstaller.utils.hooks import collect_submodules

datas = [
    ('ui', 'ui'),
    (r'C:\FreeConnect\runtime', 'runtime'),
]

hiddenimports = []
hiddenimports += collect_submodules('webview')   # backend winforms/edgechromium
hiddenimports += ['pystray._win32', 'PIL._tkinter_finder', 'clr']

a = Analysis(
    ['freeconnect_main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FreeConnect',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # оконное приложение (логи пишутся в C:\FreeConnect\logs)
    disable_windowed_traceback=False,
    uac_admin=True,         # winws/WinDivert требуют администратора
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='ui/icon.ico',
)
