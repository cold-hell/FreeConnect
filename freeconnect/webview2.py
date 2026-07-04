"""
Страховка WebView2 Runtime.

Приложение рисует интерфейс через Microsoft Edge WebView2. На Windows 11 и свежих
Windows 10 рантайм уже есть, но не гарантированно у всех. Без него pywebview не
может создать окно. Поэтому ДО создания окна проверяем наличие рантайма и, если
его нет, тихо ставим (Evergreen Bootstrapper): сначала из бандла, потом скачиваем.
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

from . import paths

# Официальный GUID Evergreen WebView2 Runtime в ключах EdgeUpdate.
_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
# Официальная ссылка на bootstrapper (сам подтягивает актуальную версию).
_BOOTSTRAP_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def is_installed() -> bool:
    """Установлен ли WebView2 Runtime (по ключам реестра EdgeUpdate)."""
    if sys.platform != "win32":
        return True
    import winreg
    locations = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + _GUID),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + _GUID),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\\" + _GUID),
    ]
    for hive, sub in locations:
        try:
            with winreg.OpenKey(hive, sub) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


def _bundled_bootstrap() -> Path | None:
    """Bootstrapper, вложенный в сборку (для оффлайн-установки), если есть."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    p = base / "runtime" / "MicrosoftEdgeWebview2Setup.exe"
    return p if p.is_file() else None


def _run_installer(exe: Path, log) -> bool:
    try:
        log(f"WebView2: запускаю установщик {exe.name} …")
        subprocess.run([str(exe), "/silent", "/install"],
                       creationflags=CREATE_NO_WINDOW, timeout=300)
    except Exception as e:  # noqa: BLE001
        log(f"WebView2: установщик упал: {e}")
        return False
    return is_installed()


def ensure(log=lambda *_: None) -> bool:
    """Гарантирует наличие WebView2. Возвращает True, если рантайм есть/поставлен."""
    if sys.platform != "win32" or is_installed():
        return True
    log("WebView2 Runtime не найден — ставлю…")
    # 1) из бандла (оффлайн)
    b = _bundled_bootstrap()
    if b and _run_installer(b, log):
        return True
    # 2) скачиваем bootstrapper и ставим
    try:
        paths.ensure_dirs()
        dst = paths.APP_HOME / "MicrosoftEdgeWebview2Setup.exe"
        log("WebView2: скачиваю установщик…")
        with urllib.request.urlopen(_BOOTSTRAP_URL, timeout=60) as r:
            dst.write_bytes(r.read())
        if _run_installer(dst, log):
            return True
    except Exception as e:  # noqa: BLE001
        log(f"WebView2: скачать не удалось: {e}")
    return is_installed()


def show_missing_message() -> None:
    """Нативное окно (GUI ещё нет), если рантайм так и не удалось поставить."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "Для работы FreeConnect нужен компонент Microsoft Edge WebView2 Runtime.\n\n"
            "Автоматическая установка не удалась (нет интернета?). Установите вручную:\n"
            "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
            "После установки запустите FreeConnect снова.",
            "FreeConnect — нужен WebView2", 0x10,  # MB_ICONERROR
        )
    except Exception:
        pass
