"""Логика восстановления и видимости стратегий (app-хелперы).

Покрывает три фикса «A»:
  - recovery переключается ТОЛЬКО на стратегию с живым Discord (не на YouTube-only);
  - автоподбор не прячет Discord-рабочую стратегию (кейс ALT9) из-за минуса голоса;
  - _score_to_item выставляет discord_ok/discord_sites_ok/youtube_ok корректно.
"""
import unittest

from freeconnect import app as fcapp
from freeconnect.app import (_extract_exe_from_zip, _is_discord_capable, _is_offerable,
                             _pick_switch_candidate, _score_to_item)
from freeconnect.autosearch import StrategyScore
from freeconnect.strategies import Strategy
from freeconnect.tester import ServiceResult, SiteResult


def _svc(service, ok_sites, voice=None):
    sites = [SiteResult(host=f"{service}{i}", ok=ok_sites) for i in range(3)]
    return ServiceResult(service=service, sites=sites, voice_ok=voice)


def _item(name, services):
    st = Strategy(id=name, name=name, source_bat="x", args=[])
    sc = StrategyScore(strategy=st, services=services)
    # проставляем working так же, как это делает autosearch._compute_score
    sc.working = sc.services_ok > 0
    return _score_to_item(sc)


class TestScoreToItemFlags(unittest.TestCase):
    def test_discord_dead_voice_sites_ok(self):
        # Discord открыт по сайтам, но голос мёртв, YouTube не открыт (кейс ALT9 у нас).
        it = _item("ALT9", [_svc("discord", True, voice=False), _svc("youtube", False)])
        self.assertFalse(it["discord_ok"])          # голос мёртв -> сервис не ок
        self.assertTrue(it["discord_sites_ok"])      # но сайты Discord открыты
        self.assertFalse(it["youtube_ok"])
        self.assertFalse(it["working"])              # строго — не рабочая

    def test_all_live(self):
        it = _item("ALT", [_svc("discord", True, voice=True), _svc("youtube", True)])
        self.assertTrue(it["discord_ok"])
        self.assertTrue(it["youtube_ok"])
        self.assertTrue(it["working"])


class TestOfferable(unittest.TestCase):
    def test_alt9_is_offerable_even_without_voice(self):
        it = _item("ALT9", [_svc("discord", True, voice=False), _svc("youtube", False)])
        self.assertTrue(_is_offerable(it))   # НЕ прячем — юзер сможет выбрать руками

    def test_dead_strategy_not_offerable(self):
        it = _item("DEAD", [_svc("discord", False, voice=False), _svc("youtube", False)])
        self.assertFalse(_is_offerable(it))


class TestDiscordCapable(unittest.TestCase):
    def test_confirmed_voice(self):
        self.assertTrue(_is_discord_capable({"discord_ok": True}))

    def test_guided_site_candidate(self):
        # гайд-кандидат: голос ещё не подтверждён, но Discord открыт по сайтам
        self.assertTrue(_is_discord_capable({"discord_ok": False, "discord_sites_ok": True}))

    def test_builtin_alt9_via_discord_count(self):
        # старая запись из конфига без *_ok, но с discord=3 (ALT9) — тоже способна
        self.assertTrue(_is_discord_capable({"discord": 3}))

    def test_youtube_only_not_capable(self):
        self.assertFalse(_is_discord_capable({"discord": 0, "youtube": 3}))


class TestSwitchCandidate(unittest.TestCase):
    def test_cycles_to_next_after_current(self):
        working = [
            {"name": "A", "discord_ok": True},
            {"name": "B", "discord_sites_ok": True},
            {"name": "C", "discord": 3},
        ]
        self.assertEqual(_pick_switch_candidate(working, "A"), "B")
        self.assertEqual(_pick_switch_candidate(working, "B"), "C")
        self.assertEqual(_pick_switch_candidate(working, "C"), "A")   # по кругу

    def test_skips_non_discord(self):
        working = [
            {"name": "CUR", "discord_ok": True},
            {"name": "YT", "discord": 0, "youtube": 3},   # ютуб-онли — пропустить
            {"name": "GOOD", "discord_sites_ok": True},
        ]
        self.assertEqual(_pick_switch_candidate(working, "CUR"), "GOOD")

    def test_none_when_no_alternatives(self):
        working = [{"name": "CUR", "discord_ok": True}]
        self.assertIsNone(_pick_switch_candidate(working, "CUR"))

    def test_current_missing_picks_first(self):
        working = [{"name": "A", "discord_ok": True}, {"name": "B", "discord_ok": True}]
        self.assertEqual(_pick_switch_candidate(working, None), "A")


class TestManualVoiceSwitch(unittest.TestCase):
    """Ручная кнопка «Голос лагает — сменить стратегию»: переключает на следующую
    Discord-способную стратегию и включает обход, не проходя весь подбор заново."""

    def _api(self, working, current):
        api = fcapp.Api.__new__(fcapp.Api)
        api.working = working
        api.strategy_name = current
        api.enabled = False
        api.tray = None
        api._state = lambda: {"working": working, "strategy": api.strategy_name}
        api._find_strategy = lambda n: Strategy(id=n, name=n, source_bat="x", args=[])
        api._start_monitors = lambda: None
        api._save = lambda: None
        api._pushes = []
        api._push = lambda ev, *a: api._pushes.append((ev, a))

        class _Eng:
            started = []
            def start(s, strat, *a, **k): _Eng.started.append(strat.name)
        api.engine = _Eng()
        return api

    def test_switches_to_next(self):
        working = [{"name": "A", "discord_ok": True}, {"name": "B", "discord_sites_ok": True}]
        api = self._api(working, "A")
        r = api.manual_voice_switch()
        self.assertTrue(r["switched"])
        self.assertEqual(r["name"], "B")
        self.assertEqual(api.strategy_name, "B")
        self.assertTrue(api.enabled)
        self.assertEqual(api.engine.started, ["B"])

    def test_no_candidates(self):
        working = [{"name": "A", "discord_ok": True}]
        api = self._api(working, "A")
        r = api.manual_voice_switch()
        self.assertFalse(r["switched"])
        self.assertEqual(r["reason"], "no_candidates")


class TestCyclicRecovery(unittest.TestCase):
    """Авто-восстановление должно обходить ВЕСЬ пул Discord-способных стратегий по кругу
    (включая встроенные ALT9/FAKE...), а не биться между двумя подтверждёнными; перебрав
    всё без успеха — сообщить (onRecoveryExhausted) и взять паузу."""

    def _api(self, working, current):
        api = fcapp.Api.__new__(fcapp.Api)
        api.working = working
        api.strategy_name = current
        api._find_strategy = lambda n: Strategy(id=n, name=n, source_bat="x", args=[])
        api._save = lambda: None
        api._state = lambda: {"working": api.working}
        api._pushes = []
        api._push = lambda ev, *a: api._pushes.append(ev)
        api._recover_tried = set()
        api._recovery_paused_until = 0.0
        api.recovery_exhaust_pause = 120.0

        class _Eng:
            def start(s, strat, *a, **k): pass
        api.engine = _Eng()
        return api

    def test_cycles_full_pool_then_exhausts(self):
        working = [
            {"name": "S1", "discord_ok": True},       # подтверждённая (генерённая)
            {"name": "S2", "discord_ok": True},       # подтверждённая (генерённая)
            {"name": "ALT9", "discord": 3},           # встроенная — раньше НЕ пробовалась
            {"name": "FAKE", "discord": 3},           # встроенная — раньше НЕ пробовалась
        ]
        api = self._api(working, "S1")
        visited = []
        for _ in range(3):
            api._switch_to_next_working()
            visited.append(api.strategy_name)
        # обошли ВЕСЬ пул (в т.ч. встроенные), а не бился S1<->S2
        self.assertEqual(visited, ["S2", "ALT9", "FAKE"])
        self.assertIn("onRecoveryExhausted", api._pushes)
        self.assertGreater(api._recovery_paused_until, 0.0)   # взята пауза

    def test_reset_recovery_state_clears_pause_and_tried(self):
        api = self._api([{"name": "A", "discord_ok": True}], "A")
        api._recover_tried = {"A", "B"}
        api._recover_count = 5
        api._reset_recovery_state(grace=0.0)
        self.assertEqual(api._recover_tried, set())
        self.assertEqual(api._recover_count, 0)
        self.assertEqual(api._recovery_paused_until, 0.0)


class TestExtractExeFromZip(unittest.TestCase):
    """Установщик из codeload-архива зеркала (файлы завёрнуты в подпапку)."""

    def _zip(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("millerloves-freeconnect-mirror-dist-abc/README.md", "x")
            z.writestr("millerloves-freeconnect-mirror-dist-abc/FreeConnect-Setup.exe",
                       b"MZ-fake-installer")
        return buf.getvalue()

    def test_extracts_by_name(self):
        import os
        import tempfile
        dest = os.path.join(tempfile.mkdtemp(), "out.exe")
        _extract_exe_from_zip(self._zip(), "FreeConnect-Setup.exe", dest)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"MZ-fake-installer")

    def test_missing_exe_raises(self):
        import os
        import tempfile
        dest = os.path.join(tempfile.mkdtemp(), "out.exe")
        with self.assertRaises(RuntimeError):
            _extract_exe_from_zip(self._zip(), "NoSuch.exe", dest)


if __name__ == "__main__":
    unittest.main()
