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
hiddenimports += ['freeconnect.voicewatch']   # импортируется лениво из app.py

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

# onedir (папка exe + DLL рядом), НЕ onefile. Onefile при каждом запуске распаковывал
# python314.dll во временную %TEMP%\_MEIxxxx и грузил оттуда — в контексте «сразу после
# установки / из установщика» распаковка стабильно падала «Failed to load Python DLL».
# onedir грузит DLL прямо из своей папки {app}\_internal — никакой распаковки, ошибки нет.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir: бинарники/данные собирает COLLECT, не EXE
    name='FreeConnect',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # оконное приложение (логи пишутся в C:\FreeConnect\logs)
    disable_windowed_traceback=False,
    uac_admin=True,         # winws/WinDivert требуют администратора
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='ui/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='FreeConnect',     # -> dist\FreeConnect\ (FreeConnect.exe + _internal\)
)
