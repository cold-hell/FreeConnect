"""
Хранилище собственных стратегий («FreeConnect #N»), сгенерированных глубоким поиском.

Стратегии сохраняются с плейсхолдерами путей ({BIN}/{LISTS}) — как встроенные,
поэтому Strategy.resolve_args() работает одинаково. Файл лежит в APP_HOME, чтобы
пережить обновление программы.
"""
from __future__ import annotations

import json
import re

from . import paths
from .strategies import Strategy

CUSTOM_PATH = paths.APP_HOME / "custom_strategies.json"


def load_custom() -> list[Strategy]:
    try:
        if CUSTOM_PATH.is_file():
            data = json.loads(CUSTOM_PATH.read_text(encoding="utf-8"))
            return [Strategy(**s) for s in data.get("strategies", [])]
    except Exception:
        pass
    return []


def _save_all(strategies: list[Strategy]) -> None:
    paths.ensure_dirs()
    CUSTOM_PATH.write_text(
        json.dumps(
            {"strategies": [
                {"id": s.id, "name": s.name, "source_bat": s.source_bat, "args": s.args}
                for s in strategies
            ]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


def _next_number(existing: list[Strategy]) -> int:
    nums = []
    for s in existing:
        m = re.search(r"FreeConnect\s*#(\d+)", s.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def add_custom(args: list[str], base_name: str = "", label: str = "") -> Strategy:
    """Сохраняет новую свою стратегию, присваивая имя «FreeConnect #N <label>».

    label — какие сервисы стратегия открыла: All / Discord / YouTube / Discord+YouTube.
    Для Discord метка ставится только если прошёл и голос (см. tester.ServiceResult.ok).
    """
    existing = load_custom()
    n = _next_number(existing)
    name = f"FreeConnect #{n}" + (f" {label}" if label else "")
    strat = Strategy(id=f"custom_{n}", name=name,
                     source_bat=f"deepsearch(base={base_name})", args=list(args))
    existing.append(strat)
    _save_all(existing)
    return strat


def delete_custom(strategy_id: str) -> None:
    existing = [s for s in load_custom() if s.id != strategy_id]
    _save_all(existing)
