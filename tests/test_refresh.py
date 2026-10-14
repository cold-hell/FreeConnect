"""Пере-тест сохранённых стратегий (кнопка «↻ Обновить»): заново меряет и сортирует,
не теряя список при «Стоп» и возвращая ранее активную стратегию."""
import threading
import unittest

from freeconnect import app as fcapp
from freeconnect import autosearch
from freeconnect.autosearch import StrategyScore
from freeconnect.strategies import Strategy
from freeconnect.tester import ServiceResult, SiteResult


def _score(name, disc_ok, yt_ok, lat):
    st = Strategy(id=name, name=name, source_bat="x", args=["--a"])
    disc = ServiceResult(service="discord",
                         sites=[SiteResult(host=f"d{i}", ok=disc_ok, latency_ms=lat) for i in range(3)])
    yt = ServiceResult(service="youtube",
                       sites=[SiteResult(host=f"y{i}", ok=yt_ok, latency_ms=lat) for i in range(3)])
    return StrategyScore(strategy=st, services=[disc, yt])


def _fake_api(working, active):
    api = fcapp.Api.__new__(fcapp.Api)
    api._cancel = threading.Event()
    api._searching = True
    api.strategy_name = active
    api.working = working
    api.enabled = True

    class _Eng:
        def __init__(self):
            self.started = []
            self.stopped = 0
        def start(self, s, *a, **k):
            self.started.append(getattr(s, "name", s))
        def stop(self, *a, **k):
            self.stopped += 1
    api.engine = _Eng()
    api._find_strategy = lambda n: Strategy(id=n, name=n, source_bat="x", args=["--a"])
    api._save = lambda: None
    api._stop_monitors = lambda: None
    api._start_monitors = lambda: None
    api._state = lambda: {"working": api.working}
    api._pushes = []
    api._push = lambda ev, *a: api._pushes.append((ev, a))
    return api


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self._orig = autosearch.evaluate_strategy

    def tearDown(self):
        autosearch.evaluate_strategy = self._orig

    def test_resorts_by_availability_then_latency(self):
        working = [
            {"name": "S1 YouTube", "discord": 0, "youtube": 3, "latency": 100, "custom": True},
            {"name": "S2 All", "discord": 3, "youtube": 3, "latency": 300, "custom": True},
            {"name": "S3 All", "discord": 3, "youtube": 3, "latency": 150, "custom": True},
        ]
        scores = {
            "S1 YouTube": _score("S1 YouTube", False, True, 100),
            "S2 All": _score("S2 All", True, True, 300),
            "S3 All": _score("S3 All", True, True, 150),
        }
        autosearch.evaluate_strategy = lambda eng, strat, svcs, **k: scores[strat.name]
        api = _fake_api(working, active="S3 All")
        api._run_refresh()
        order = [w["name"] for w in api.working]
        # оба сервиса раньше yt-only; среди All меньший пинг раньше
        self.assertEqual(order, ["S3 All", "S2 All", "S1 YouTube"])
        # активную стратегию вернули и обход остался включённым
        self.assertEqual(api.strategy_name, "S3 All")
        self.assertTrue(api.enabled)
        self.assertEqual(api.engine.started[-1], "S3 All")
        self.assertTrue(any(ev == "onSearchDone" for ev, _ in api._pushes))

    def test_stop_keeps_untested_list(self):
        working = [{"name": n, "discord": 3, "youtube": 3, "latency": 150, "custom": True}
                   for n in ("S1 All", "S2 All", "S3 All")]
        autosearch.evaluate_strategy = lambda *a, **k: _score("x", True, True, 100)
        api = _fake_api(working, active="S1 All")
        api._cancel.set()          # «Стоп» ещё до первого прогона
        api._run_refresh()
        # ни одна не пере-тестирована, но список не потерян
        self.assertEqual({w["name"] for w in api.working}, {"S1 All", "S2 All", "S3 All"})


if __name__ == "__main__":
    unittest.main()
