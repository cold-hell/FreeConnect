"""
Автозапуск FreeConnect через Планировщик задач Windows.

Используем schtasks с /RL HIGHEST — приложению нужны права администратора
(winws+WinDivert), а задача в планировщике поднимает их при входе в систему
без запроса UAC. Автозапуск включается только у собранного .exe.
"""
from __future__ import annotations

import subprocess
import sys

TASK_NAME = "FreeConnect"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _target_exe() -> str | None:
    """Путь до исполняемого файла для автозапуска (только для собранного .exe)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return None  # в режиме разработки автозапуск не прописываем


def _run(cmd: list[str]) -> int:
    try:
        p = subprocess.run(
            cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15
        )
        return p.returncode
    except Exception:
        return 1


def set_enabled(enabled: bool) -> bool:
    if sys.platform != "win32":
        return False
    if enabled:
        exe = _target_exe()
        if not exe:
            return False
        return _run([
            "schtasks", "/Create", "/TN", TASK_NAME,
            "/TR", f'"{exe}" --tray', "/SC", "ONLOGON", "/RL", "HIGHEST", "/F",
        ]) == 0
    return _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]) == 0


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    return _run(["schtasks", "/Query", "/TN", TASK_NAME]) == 0
