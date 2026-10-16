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
    decisive_ok: int = 0     # сколько решающих целей открылось (для порядка и отчёта)
    decisive_total: int = 0  # сколько их всего было
    complete: bool = False   # ВСЕ решающие цели открылись (strict: «сервис реально открыт»)

    @property
    def services_ok(self) -> int:
        return sum(1 for s in self.services if s.ok)

    @property
    def services_complete(self) -> int:
        """Сколько сервисов открылись ПОЛНОСТЬЮ (все решающие цели)."""
        return sum(1 for s in self.services if s.complete)

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
    # «Рабочая» = прошёл ХОТЯ БЫ ОДИН целевой сервис (для Discord — обязательно с
    # живым голосом/медиа). Такую стратегию есть смысл СОХРАНИТЬ в список.
    sc.working = sc.services_ok > 0
    # «Полностью рабочая» = ВСЕ решающие цели каждого сервиса ответили. Это отдельный,
    # более строгий критерий: он решает, можно ли ВКЛЮЧАТЬ обход автоматически.
    # Разбор жалобы: автоподбор авто-включил набор с 2 из 3 сайтов Discord, а Discord
    # у человека не работал вовсе — половина целей ещё не означает рабочий сервис.
    sc.decisive_ok = sum(s.decisive_ok for s in sc.services)
    sc.decisive_total = sum(s.decisive_total for s in sc.services)
    sc.complete = any(s.complete for s in sc.services)
    lat = sc.avg_latency_ms
    lat_penalty = (lat / 10.0) if lat >= 0 else 100.0
    # Штраф за качество голоса: среди равных (напр. двух All) выше встанет та, что
    # коннектится к войсу быстрее/стабильнее (меньше потерь/джиттера/RTT). Всегда
    # меньше разрыва между All и Discord-only (services_ok*1000), порядок не ломает.
    voice_penalty = sum(s.voice_score() for s in sc.services)
    # Полностью открытые сервисы весят больше частично открытых: +50 «половин» не
    # должны перевешивать один сервис, открытый целиком.
    sc.score = (sc.services_ok * 1000 + sc.services_complete * 500
                + sc.sites_ok * 100 - lat_penalty - voice_penalty)


def evaluate_strategy(
    engine: Engine,
    strategy: Strategy,
    services: list[str],
    timeout: float = 5.0,
    settle: float = 4.0,
    probe_freeze: bool = True,
    screen: bool = False,
    screen_timeout: float = 1.2,
    check_voice_udp: bool = True,
) -> StrategyScore:
    """Поднимает одну стратегию и оценивает её.

    screen=True — быстрый проход для СИТА (первый этап перебора): пробы сайтов
    идут с коротким лимитом (`screen_timeout`), голос не измеряется вовсе. Смысл:
    при фильтрации провайдера проба возвращается ровно по лимиту (замерено 5.05 с),
    поэтому сито на 5 секундах означает «каждый кандидат стоит 5 секунд». Выживших
    потом перепроверяем полностью — см. search().
    """
    from . import tester
    sc = StrategyScore(strategy=strategy)
    if screen:
        # Сито: короткий лимит проб и без голоса (голос меряем только на выживших —
        # при задушенном UDP он сам стоит до ~6 с и решает только для финальной оценки).
        kwargs = {"timeout": screen_timeout, "probe_freeze": probe_freeze,
                  "check_voice_udp": False}
    else:
        kwargs = {"timeout": timeout, "probe_freeze": probe_freeze,
                  "check_voice_udp": check_voice_udp}
    try:
        engine.start(strategy, settle=settle)
    except EngineError as e:
        sc.error = str(e)
        return sc
    try:
        sc.services = tester.test_all(services, **kwargs)
    finally:
        engine.stop()
    _compute_score(sc, len(services))
    return sc


def _screen_sort_key(sc: StrategyScore):
    """Порядок для второго прохода: сперва те, у кого открылось больше целей."""
    return (-sc.sites_ok, -sc.services_ok)


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
    screen_timeout: float = 0.9,
    recheck_timeout: float = 2.5,
    full_recheck: int = 3,
    full_recheck_no_alive: int = 0,
    on_recheck: ResultCb | None = None,
) -> list[StrategyScore]:
    """Перебирает готовые стратегии в два прохода и возвращает список по оценке.

    Зачем два прохода. Длинный таймаут нужен только тем стратегиям, которые
    что-то открывают; для «мёртвых» он превращается в чистую задержку. Замерено на
    живой машине: при фильтрации каждая проба сайта возвращается ровно по лимиту
    (5.05 с), поэтому проход по 22 наборам занимал ~110 с почти целиком из ожиданий.
      этап A (сито): все наборы, короткий лимит, без голоса  -> единицы секунд;
      этап B (точная оценка): выжившие, полный лимит, голос, задержка.
    Итоговый список — по лучшей из двух оценок каждого набора.

    stop_on_first / stop_after_all сохранены для совместимости: ими по-прежнему
    можно прервать перебор раньше.
    """
    svcs = services or list(tester.DEFAULT_TARGETS.keys())
    strats = strategies if strategies is not None else load_strategies()
    eng = engine or Engine()
    total = len(strats)
    cancelled = False

    # ---------------------------------------------------------------- этап A: сито
    screened: list[StrategyScore] = []
    try:
        for i, strat in enumerate(strats):
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if on_progress:
                on_progress(i, total, strat)
            sc = evaluate_strategy(eng, strat, svcs, timeout=timeout, settle=settle,
                                   probe_freeze=probe_freeze, screen=True,
                                   screen_timeout=screen_timeout)
            screened.append(sc)
            if on_result:
                on_result(sc)
            if stop_on_first and sc.working:
                break
    finally:
        eng.stop()

    # ------------------------------------------------- этап B: точная оценка выживших
    final: dict[str, StrategyScore] = {sc.strategy.name: sc for sc in screened}
    if not cancelled and full_recheck > 0:
        def _decisive_ok(sc: StrategyScore) -> int:
            """Открылось ли ХОТЬ ЧТО-ТО решающее (советники не в счёт)."""
            return sum(1 for s in sc.services for x in s.sites if x.ok and not x.advisory)

        alive = [sc for sc in screened if not sc.error and _decisive_ok(sc) > 0]
        # Сито вообще ничего не открыло? Тогда точная оценка почти наверняка не
        # поможет: у «мертвяков» тот же TLS-таймаут, только в 5 раз длиннее. По
        # умолчанию второй проход в этом случае не запускаем — пользователю важнее
        # быстро получить ответ «ни один набор не подошёл», чем ждать ещё минуту.
        if not alive:
            alive = sorted([sc for sc in screened if not sc.error],
                           key=_screen_sort_key)[:full_recheck_no_alive]
        for sc in sorted(alive, key=_screen_sort_key)[:full_recheck]:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            try:
                full = evaluate_strategy(
                    eng, sc.strategy, svcs,
                    # Полный проход в 2.5 с, а не 5: это втрое больше сита и уже
                    # покрывает медленные, но рабочие соединения; пятисекундное
                    # ожидание на «мёртвой» цели только удлиняло финальную оценку.
                    timeout=min(timeout, recheck_timeout), settle=settle,
                    probe_freeze=probe_freeze,
                    check_voice_udp=any(s.service in tester.VOICE_SERVICES for s in sc.services),
                )
            except Exception:  # noqa: BLE001
                continue
            final[sc.strategy.name] = full
            if on_recheck:
                on_recheck(full)

    results = list(final.values())
    # Порядок: сперва те, где сервис открыт ЦЕЛИКОМ (иначе «2 из 3 сайтов Discord»
    # поднимались выше реально рабочих наборов), затем по оценке.
    results.sort(key=lambda s: (s.services_complete, s.score), reverse=True)
    return results
