"""
Пути FreeConnect.

Ключевая идея: winws.exe и списки (bin/lists) должны лежать в пути БЕЗ кириллицы,
иначе на машинах с кириллическим именем пользователя обход ломается. Поэтому
рантайм по умолчанию — C:\\FreeConnect (переопределяется переменной окружения
FREECONNECT_HOME).

Конфиг и логи тоже держим там же — так у друзей всё в одном месте.
"""
from __future__ import annotations

import os
from pathlib import Path


def _default_home() -> Path:
    env = os.environ.get("FREECONNECT_HOME")
    if env:
        return Path(env)
    # Диск системного каталога (обычно C:), чтобы путь гарантированно без кириллицы.
    system_drive = os.environ.get("SystemDrive", "C:")
    return Path(f"{system_drive}\\FreeConnect")


APP_HOME: Path = _default_home()
RUNTIME_DIR: Path = APP_HOME / "runtime"
BIN_DIR: Path = RUNTIME_DIR / "bin"
LISTS_DIR: Path = RUNTIME_DIR / "lists"
STRATEGIES_JSON: Path = RUNTIME_DIR / "strategies.json"

CONFIG_PATH: Path = APP_HOME / "config.json"
LOG_DIR: Path = APP_HOME / "logs"

WINWS_EXE: Path = BIN_DIR / "winws.exe"


def has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def ensure_dirs() -> None:
    for d in (APP_HOME, RUNTIME_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def runtime_ready() -> bool:
    """Готов ли рантайм к запуску (есть winws и списки)."""
    return WINWS_EXE.is_file() and LISTS_DIR.is_dir() and STRATEGIES_JSON.is_file()
