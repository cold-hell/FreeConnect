"""
Автоподбор стратегии.

Перебирает стратегии: поднимает каждую через движок, прогоняет тесты доступности,
считает оценку, гасит и переходит к следующей. В конце ранжирует рабочие.

Оценка (по убыванию важности):
  1. сколько сервисов полностью открылись (discord + youtube);
  2. сколько всего целей открылось;
  3. меньшая средняя задержка.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from . import tester
from .engine import Engine, EngineError
from .strategies import Strategy, load_strategies
from .tester import ServiceResult


@dataclass
class StrategyScore:
    strategy: Strategy
    services: list[ServiceResult] = field(default_factory=list)
    score: float = 0.0
    working: bool = False
    error: str = ""

    @property
    def services_ok(self) -> int:
        return sum(1 for s in self.services if s.ok)

    @property
    def sites_ok(self) -> int:
        return sum(sum(1 for x in s.sites if x.ok) for s in self.services)

    @property
    def avg_latency_ms(self) -> float:
        lats = [s.avg_latency_ms for s in self.services if s.avg_latency_ms >= 0]
        return sum(lats) / len(lats) if lats else -1.0

    @property
    def working_services(self) -> list[str]:
        """Имена сервисов, полностью прошедших проверку (для Discord — с живым голосом)."""
        return [s.service for s in self.services if s.ok]

    def result_label(self) -> str:
        """Метка для имени своей стратегии: All / Discord / YouTube / Discord+YouTube."""
        display = {"discord": "Discord", "youtube": "YouTube"}
        ok = self.working_services
        if ok and len(ok) == len(self.services):
            return "All"
        return "+".join(display.get(s, s.capitalize()) for s in ok)

    def summary(self) -> str:
        parts = []
        for s in self.services:
            good = sum(1 for x in s.sites if x.ok)
            tag = f"{s.service}:{good}/{len(s.sites)}"
            if s.voice_ok is not None:
                tag += " голос" + ("✓" if s.voice_ok else "✗")
            parts.append(tag)
        lat = f"{self.avg_latency_ms:.0f}ms" if self.avg_latency_ms >= 0 else "-"
        return f"{', '.join(parts)} | лат {lat}"


# Колбэки: (index, total, strategy) -> None  и  (StrategyScore) -> None
ProgressCb = Callable[[int, int, Strategy], None]
ResultCb = Callable[[StrategyScore], None]


def _compute_score(sc: StrategyScore, n_services: int) -> None:
    # «Рабочая» = прошёл ХОТЯ БЫ ОДИН целевой сервис полностью (для Discord —
    # обязательно с живым голосом/медиа). Такую стратегию есть смысл сохранить;
    # универсальные (все сервисы) стоят выше за счёт services_ok в оценке.
    sc.working = sc.services_ok > 0
    lat = sc.avg_latency_ms
    lat_penalty = (lat / 10.0) if lat >= 0 else 100.0
    # Штраф за качество голоса: среди равных (напр. двух All) выше встанет та, что
    # коннектится к войсу быстрее/стабильнее (меньше потерь/джиттера/RTT). Всегда
    # меньше разрыва между All и Discord-only (services_ok*1000), порядок не ломает.
    voice_penalty = sum(s.voice_score() for s in sc.services)
    sc.score = sc.services_ok * 1000 + sc.sites_ok * 100 - lat_penalty - voice_penalty


def evaluate_strategy(
    engine: Engine,
    strategy: Strategy,
    services: list[str],
    timeout: float = 5.0,
    settle: float = 4.0,
    probe_freeze: bool = True,
) -> StrategyScore:
    """Поднимает одну стратегию и оценивает её."""
    sc = StrategyScore(strategy=strategy)
    try:
        engine.start(strategy, settle=settle)
    except EngineError as e:
        sc.error = str(e)
        return sc
    try:
        sc.services = tester.test_all(services, timeout=timeout, probe_freeze=probe_freeze)
    finally:
        engine.stop()
    _compute_score(sc, len(services))
    return sc


def search(
    services: list[str] | None = None,
    strategies: list[Strategy] | None = None,
    timeout: float = 5.0,
    settle: float = 4.0,
    probe_freeze: bool = True,
    stop_on_first: bool = False,
    stop_after_all: int = 0,
    engine: Engine | None = None,
    on_progress: ProgressCb | None = None,
    on_result: ResultCb | None = None,
    cancel: threading.Event | None = None,
) -> list[StrategyScore]:
    """Перебирает стратегии и возвращает список StrategyScore, отсортированный по оценке.

    stop_on_first — остановиться на первой полностью рабочей (быстрый режим).
    stop_after_all — 0 (по умолчанию) = перебрать ВСЕ стратегии и показать полный
      список на выбор. >0 = остановиться, набрав столько All (быстрый, но урезанный
      список — по фидбеку не нужен: юзер хочет ускорять саму проверку, а не резать
      перебор). Оставлено параметром на случай «быстрого» режима.
    """
    svcs = services or list(tester.DEFAULT_TARGETS.keys())
    strats = strategies if strategies is not None else load_strategies()
    eng = engine or Engine()
    results: list[StrategyScore] = []
    total = len(strats)
    all_ok_count = 0

    try:
        for i, strat in enumerate(strats):
            if cancel is not None and cancel.is_set():
                break
            if on_progress:
                on_progress(i, total, strat)
            sc = evaluate_strategy(
                eng, strat, svcs, timeout=timeout, settle=settle, probe_freeze=probe_freeze
            )
            results.append(sc)
            if on_result:
                on_result(sc)
            if stop_on_first and sc.working:
                break
            if sc.services_ok == len(svcs) and len(svcs) > 0:
                all_ok_count += 1
                if stop_after_all and all_ok_count >= stop_after_all:
                    break
    finally:
        eng.stop()

    results.sort(key=lambda s: s.score, reverse=True)
    return results
