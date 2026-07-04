"""Сохранение состояния и настроек FreeConnect в config.json."""
from __future__ import annotations

import json
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "strategy": None,       # id выбранной стратегии
    "working": [],          # последний список рабочих стратегий (для быстрого выбора)
    "autostart": False,     # автозапуск при старте Windows
    "monitor": True,        # мониторинг пинга голосового + авто-восстановление
    "auto_enable": True,    # включать обход сразу при запуске приложения
    "onboarded": False,     # обучение уже пройдено (иначе localStorage стирается WebView2)
    "game_filter": False,   # покрывать игровой трафик (порты 1024-65535) — для игр
    "strategies_updated_at": 0,  # unixtime последнего автообновления стратегий из upstream
}


def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        if paths.CONFIG_PATH.is_file():
            data = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: data.get(k, v) for k, v in DEFAULTS.items()})
    except Exception:
        pass
    return cfg


def save(cfg: dict[str, Any]) -> None:
    try:
        paths.ensure_dirs()
        paths.CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
