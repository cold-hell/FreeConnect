"""Гайд-подтверждение голоса (ground truth от живого Discord) — машина состояний.

Проверяем backend-поток `_run_deep_guided`: сбор шорт-листа, ожидание вердикта
человека по кандидату, фиксация первого подтверждённого, честный «не подтверждён».
"""
import threading
import time
import unittest

from freeconnect import app as fcapp
from freeconnect import custom, deepsearch
from freeconnect.autosearch import StrategyScore
from freeconnect.strategies import Strategy
from freeconnect.tester import ServiceResult, SiteResult


def _sc(name):
    st = Strategy(id=name, name=name, source_bat="x", args=["--a"])
    disc = ServiceResult(service="discord",
                         sites=[SiteResult(host=f"d{i}", ok=True) for i in range(3)])
    yt = ServiceResult(service="youtube",
                       sites=[SiteResult(host=f"y{i}", ok=True) for i in range(3)])
    return StrategyScore(strategy=st, services=[disc, yt])


def _fake_api():
    api = fcapp.Api.__new__(fcapp.Api)          # без тяжёлого __init__
    api._cancel = threading.Event()
    api._vc_event = threading.Event()
    api._vc_verdict = None
    api._searching = True
    api.strategy_name = None
    api.working = []
    api.enabled = False

    class _Eng:
        started = []
        stopped = 0
        def start(self, cand, *a, **k):
            _Eng.started.append(getattr(cand, "name", cand))
        def stop(self, *a, **k):
            _Eng.stopped += 1
    api.engine = _Eng()
    # изолируем от реального custom_strategies.json на диске: гайд поднимает сам
    # sc.strategy кандидата, а не найденную по имени запись
    api._find_strategy = lambda n: None
    api._save = lambda: None
    api._start_monitors = lambda: None
    api._start_doh_async = lambda: None
    api._state = lambda: {"working": api.working}
    api._pushes = []
    api._push = lambda ev, *a: api._pushes.append((ev, a))
    return api


def _wait_probe(api, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if any(ev == "onVoiceConfirmProbe" for ev, _ in api._pushes):
            return True
        time.sleep(0.01)
    return False


class TestGuidedVoiceConfirm(unittest.TestCase):
    def setUp(self):
        self._collect = deepsearch.collect_site_candidates
        self._add = custom.add_custom
        # Пул сохраняется для ВСЕХ кандидатов (нужен ручной кнопке): счётчик даёт
        # уникальные имена, чтобы не писать в реальный custom_strategies.json.
        self._n = 0

        def _fake_add(args, base_name=None, label=None):
            self._n += 1
            nm = f"FreeConnect #{self._n}" + (f" {label}" if label else "")
            return Strategy(id=f"custom_{self._n}", name=nm, source_bat="x", args=args)
        custom.add_custom = _fake_add

    def tearDown(self):
        deepsearch.collect_site_candidates = self._collect
        custom.add_custom = self._add

    def test_confirm_locks_current_candidate(self):
        deepsearch.collect_site_candidates = lambda *a, **k: [_sc("c1"), _sc("c2")]
        api = _fake_api()
        base = Strategy(id="ALT", name="ALT", source_bat="x", args=[])
        t = threading.Thread(target=api._run_deep_guided, args=(base,))
        t.start()
        self.assertTrue(_wait_probe(api))
        api.voice_confirm_result(True)             # человек: «голос подключился»
        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual(api.strategy_name, "FreeConnect #1 All")   # первый кандидат
        self.assertTrue(api.enabled)
        # подтверждённая помечена флагами для recovery/ручного переключения
        conf = next(w for w in api.working if w["name"] == api.strategy_name)
        self.assertTrue(conf["discord_ok"] and conf.get("voice_confirmed"))
        done = [a[0] for ev, a in api._pushes if ev == "onVoiceConfirmDone"]
        self.assertTrue(done and done[0]["confirmed"])

    def test_saves_whole_pool_as_alternatives(self):
        # Весь шорт-лист сохраняется в working — иначе ручной кнопке нечего переключать.
        deepsearch.collect_site_candidates = lambda *a, **k: [_sc("c1"), _sc("c2"), _sc("c3")]
        api = _fake_api()
        base = Strategy(id="ALT", name="ALT", source_bat="x", args=[])
        t = threading.Thread(target=api._run_deep_guided, args=(base,))
        t.start()
        self.assertTrue(_wait_probe(api))
        api.voice_confirm_result(True)
        t.join(timeout=3)
        # 3 кандидата -> 3 своих стратегии в пуле, у всех открыт Discord по сайтам
        self.assertEqual(len(api.working), 3)
        self.assertTrue(all(w["discord_sites_ok"] for w in api.working))

    def test_next_then_confirm_second(self):
        deepsearch.collect_site_candidates = lambda *a, **k: [_sc("c1"), _sc("c2")]
        api = _fake_api()
        base = Strategy(id="ALT", name="ALT", source_bat="x", args=[])
        t = threading.Thread(target=api._run_deep_guided, args=(base,))
        t.start()
        self.assertTrue(_wait_probe(api))
        api.voice_confirm_result(False)            # первый — «дальше»
        time.sleep(0.05)
        api.voice_confirm_result(True)             # второй — подтвердил
        t.join(timeout=3)
        probes = [a[0] for ev, a in api._pushes if ev == "onVoiceConfirmProbe"]
        self.assertEqual(len(probes), 2)           # дошли до второго кандидата
        # Первый старт — бутстрап-обход (чтобы Discord открылся ещё до просьбы зайти
        # в канал), затем перебор кандидатов c1, c2.
        self.assertEqual(api.engine.started[0], "ALT")           # бутстрап на базе
        self.assertEqual(api.engine.started[-2:], ["c1", "c2"])  # затем кандидаты
        boot = [ev for ev, a in api._pushes if ev == "onGuidedBootstrap"]
        self.assertEqual(len(boot), 1)             # UI получил сигнал «обход включён»

    def test_no_confirm_reports_unconfirmed(self):
        deepsearch.collect_site_candidates = lambda *a, **k: [_sc("only")]
        api = _fake_api()
        base = Strategy(id="ALT", name="ALT", source_bat="x", args=[])
        t = threading.Thread(target=api._run_deep_guided, args=(base,))
        t.start()
        self.assertTrue(_wait_probe(api))
        api.voice_confirm_result(False)            # «дальше» на единственном
        t.join(timeout=3)
        done = [a[0] for ev, a in api._pushes if ev == "onVoiceConfirmDone"]
        self.assertTrue(done and not done[0]["confirmed"])
        self.assertGreaterEqual(api.engine.stopped, 1)   # движок погашен

    def test_empty_shortlist(self):
        deepsearch.collect_site_candidates = lambda *a, **k: []
        api = _fake_api()
        base = Strategy(id="ALT", name="ALT", source_bat="x", args=[])
        api._run_deep_guided(base)
        done = [a[0] for ev, a in api._pushes if ev == "onVoiceConfirmDone"]
        self.assertTrue(done and done[0].get("empty"))

    def test_cancel_during_wait(self):
        deepsearch.collect_site_candidates = lambda *a, **k: [_sc("c1")]
        api = _fake_api()
        base = Strategy(id="ALT", name="ALT", source_bat="x", args=[])
        t = threading.Thread(target=api._run_deep_guided, args=(base,))
        t.start()
        self.assertTrue(_wait_probe(api))
        api._cancel.set()
        api._vc_event.set()                        # как cancel_search
        t.join(timeout=3)
        self.assertFalse(t.is_alive())


def _sc_lat(name, disc_sites, yt_sites, lat):
    st = Strategy(id=name, name=name, source_bat="x", args=["--a"])
    disc = ServiceResult(service="discord",
                         sites=[SiteResult(host=f"d{i}", ok=disc_sites, latency_ms=lat)
                                for i in range(3)])
    yt = ServiceResult(service="youtube",
                       sites=[SiteResult(host=f"y{i}", ok=yt_sites, latency_ms=lat)
                              for i in range(3)])
    return StrategyScore(strategy=st, services=[disc, yt])


class TestShortlistSort(unittest.TestCase):
    """Регресс: закрепляли #8 c 1038мс, хотя рядом лежал 172мс. Шорт-лист теперь
    сортируется: сперва «All» (Discord+YouTube по сайтам), затем по задержке."""

    def test_all_first_then_latency(self):
        items = [
            _sc_lat("slow_all", True, True, 1038),
            _sc_lat("fast_all", True, True, 172),
            _sc_lat("disc_only", True, False, 120),   # быстрее, но не All -> в хвост
        ]
        items.sort(key=deepsearch._shortlist_sort_key)
        self.assertEqual([i.strategy.name for i in items],
                         ["fast_all", "slow_all", "disc_only"])

    def test_all_sites_ok_predicate(self):
        self.assertTrue(deepsearch._all_sites_ok(_sc_lat("a", True, True, 100)))
        self.assertFalse(deepsearch._all_sites_ok(_sc_lat("b", True, False, 100)))


class TestVoiceConfirmSetting(unittest.TestCase):
    """Регресс: флаг voice_confirm должен читаться get_settings и сохраняться
    set_setting — иначе кнопка «Глубокий поиск» не увидит его и гайд не запустится
    (ровно тот баг, что фича «ничего не делала»)."""

    def test_roundtrip(self):
        from freeconnect import config
        api = fcapp.Api.__new__(fcapp.Api)
        api.cfg = {"voice_confirm": False, "monitor": True, "auto_enable": True,
                   "game_filter": False, "doh": False}
        api.enabled = False
        orig = config.save
        config.save = lambda cfg: None
        try:
            self.assertIn("voice_confirm", api.get_settings())
            self.assertFalse(api.get_settings()["voice_confirm"])
            api.set_setting("voice_confirm", True)
            self.assertTrue(api.cfg["voice_confirm"])                 # сохранилось
            self.assertTrue(api.get_settings()["voice_confirm"])      # и читается
        finally:
            config.save = orig


class TestMonitorGating(unittest.TestCase):
    """При voice_confirm ON ненадёжный STUN-монитор голоса НЕ поднимается (иначе он
    ложно уронил бы подтверждённую человеком стратегию); watchdog по сайтам — да."""

    def _api(self, voice_confirm):
        api = fcapp.Api.__new__(fcapp.Api)
        api.cfg = {"monitor": True, "voice_confirm": voice_confirm}

        class _M:
            def __init__(s): s.started = False
            def start(s): s.started = True
        api.monitor = _M()
        api.watchdog = _M()
        return api

    def test_voice_confirm_skips_stun_monitor(self):
        api = self._api(True)
        api._start_monitors()
        self.assertFalse(api.monitor.started)   # STUN-монитор НЕ поднят
        self.assertTrue(api.watchdog.started)    # watchdog поднят

    def test_off_starts_both(self):
        api = self._api(False)
        api._start_monitors()
        self.assertTrue(api.monitor.started)
        self.assertTrue(api.watchdog.started)


if __name__ == "__main__":
    unittest.main()
