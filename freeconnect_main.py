"""Точка входа для PyInstaller.

app.py использует относительные импорты (from . import ...), поэтому не может быть
собран напрямую как __main__. Этот лаунчер импортирует пакет и вызывает run().
"""
import traceback

from freeconnect.app import run, _log

if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # noqa: BLE001
        _log("FATAL: " + repr(e) + "\n" + traceback.format_exc())
        raise
