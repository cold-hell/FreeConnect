# -*- coding: utf-8 -*-
"""Тесты двухступенчатого автоподбора.

Почему этот файл вообще появился: «подбор готовых идёт медленно» — потому что при
фильтрации провайдера проба возвращается ровно по лимиту (замерено 5.05 с на цель),
и каждый набор стоил полный таймаут. Проверяем, что:
  * сито идёт коротким лимитом и БЕЗ голоса;
  * точная оценка достаётся только выжившим;
  * если сито не открыло ничего, второй проход не запускается (по умолчанию);
  * полный проход сохранил прежнее поведение (голос, длинный лимит).
"""
from __future__ import annotations

import unittest
from unittest import mock

from freeconnect import autosearch, tester
from freeconnect.strategies import Strategy
from freeconnect.tester import ServiceResult, SiteResult


def _sites(ok: bool, advisory: bool = False) -> list[SiteResult]:
    return [SiteResult(host="discord.com", ok=ok, status="OK" if ok else "TIMEOUT",
                       latency_ms=100.0 if ok else -1.0),
            SiteResult(host="redirector.googlevideo.com", ok=False, status="TIMEOUT",
                       advisory=True) if advisory else
            SiteResult(host="cdn.discordapp.com", ok=ok, status="OK" if ok else "TIMEOUT")]


class FakeEngine:
    """Движок-заглушка: считает запуски, чтобы видеть, сколько раз поднимали winws."""

    def __init__(self) -> None:
        self.starts = 0

    def start(self, strategy, settle: float = 4.0) -> None:
        self.starts += 1

    def stop(self) -> None:
        pass


def _strategies(n: int) -> list[Strategy]:
    return [Strategy(id=f"id{i}", name=f"S{i}", source_bat=f"s{i}.bat",
                     args=["--filter-tcp=443", "--dpi-desync=fake"])
            for i in range(n)]


class TestScreenPhase(unittest.TestCase):
    def test_screen_uses_short_timeout_and_no_voice(self):
        seen: list[dict] = []

        def fake_test_all(services=None, **kw):
            seen.append(kw)
            return [ServiceResult(service="discord", sites=_sites(ok=False), voice_ok=False)]

        eng = FakeEngine()
        with mock.patch.object(tester, "test_all", side_effect=fake_test_all):
            autosearch.evaluate_strategy(eng, _strategies(1)[0], ["discord"],
                                        screen=True, screen_timeout=0.9)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["timeout"], 0.9, "сито должно идти коротким лимитом")
        self.assertFalse(seen[0]["check_voice_udp"], "на сите голос не измеряем")

    def test_full_uses_full_timeout_and_voice(self):
        seen: list[dict] = []

        def fake_test_all(services=None, **kw):
            seen.append(kw)
            return [ServiceResult(service="discord", sites=_sites(ok=True), voice_ok=True,
                                  voice_conf="high")]

        eng = FakeEngine()
        with mock.patch.object(tester, "test_all", side_effect=fake_test_all):
            sc = autosearch.evaluate_strategy(eng, _strategies(1)[0], ["discord"],
                                             timeout=5.0, screen=False)
        self.assertEqual(seen[0]["timeout"], 5.0)
        self.assertTrue(seen[0]["check_voice_udp"], "на точной оценке голос обязателен")
        self.assertTrue(sc.working)


class TestTwoPhaseSearch(unittest.TestCase):
    def test_recheck_only_survivors(self):
        """Второй проход — только по тем, у кого сито открыло решающие цели."""
        calls: list[tuple[str, bool]] = []

        def fake_eval(engine, strategy, services, timeout=5.0, settle=4.0,
                      probe_freeze=True, screen=False, screen_timeout=0.9,
                      check_voice_udp=True):
            calls.append((strategy.name, screen))
            ok = strategy.name in ("S0", "S1")
            sites = _sites(ok=ok, advisory=True)
            disc = ServiceResult(service="discord", sites=sites, voice_ok=ok,
                                 voice_conf="high" if ok else "")
            sc = autosearch.StrategyScore(strategy=strategy, services=[disc], score=0.0)
            autosearch._compute_score(sc, 1)
            return sc

        strats = _strategies(5)
        with mock.patch.object(autosearch, "evaluate_strategy", side_effect=fake_eval):
            res = autosearch.search(engine=FakeEngine(), strategies=strats)

        screened = [n for n, screen in calls if screen]
        rechecked = [n for n, screen in calls if not screen]
        self.assertEqual(screened, [s.name for s in strats], "сито должно пройти все наборы")
        self.assertEqual(sorted(rechecked), ["S0", "S1"], "полная оценка — только выжившим")
        self.assertEqual(len(res), len(strats), "в итоговом списке должны быть все наборы")
        self.assertTrue(res[0].working)

    def test_no_recheck_when_nothing_alive(self):
        """Сито не открыло ничего — не тратим время на полный проход."""
        calls: list[bool] = []

        def fake_eval(engine, strategy, services, screen=False, **kw):
            calls.append(screen)
            disc = ServiceResult(service="discord", sites=_sites(ok=False), voice_ok=False)
            sc = autosearch.StrategyScore(strategy=strategy, services=[disc], score=0.0)
            autosearch._compute_score(sc, 1)
            return sc

        strats = _strategies(4)
        with mock.patch.object(autosearch, "evaluate_strategy", side_effect=fake_eval):
            res = autosearch.search(engine=FakeEngine(), strategies=strats)
        self.assertTrue(all(calls), "при полностью мёртвом сите второй проход не нужен")
        self.assertEqual(len(res), len(strats))

    def test_recheck_can_be_forced_on_dead_screen(self):
        """Параметр full_recheck_no_alive возвращает старый «дожимающий» проход."""
        calls: list[bool] = []

        def fake_eval(engine, strategy, services, screen=False, **kw):
            calls.append(screen)
            disc = ServiceResult(service="discord", sites=_sites(ok=False), voice_ok=False)
            sc = autosearch.StrategyScore(strategy=strategy, services=[disc], score=0.0)
            autosearch._compute_score(sc, 1)
            return sc

        strats = _strategies(4)
        with mock.patch.object(autosearch, "evaluate_strategy", side_effect=fake_eval):
            autosearch.search(engine=FakeEngine(), strategies=strats,
                              full_recheck_no_alive=2)
        self.assertEqual(calls.count(False), 2, "ожидаем ровно два дожимающих прохода")

    def test_cancel_stops_before_recheck(self):
        import threading

        calls: list[bool] = []

        def fake_eval(engine, strategy, services, screen=False, **kw):
            calls.append(screen)
            disc = ServiceResult(service="discord", sites=_sites(ok=True), voice_ok=True)
            sc = autosearch.StrategyScore(strategy=strategy, services=[disc], score=0.0)
            autosearch._compute_score(sc, 1)
            return sc

        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(autosearch, "evaluate_strategy", side_effect=fake_eval):
            autosearch.search(engine=FakeEngine(), strategies=_strategies(3), cancel=cancel)
        self.assertEqual(calls, [], "после отмены не должно быть ни одного запуска")


if __name__ == "__main__":
    unittest.main()
