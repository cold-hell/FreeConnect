"""Метки/подсчёт рабочих сервисов у StrategyScore (autosearch).
Определяет имя своей стратегии (All / Discord / YouTube) и решение о сохранении."""
import unittest

from freeconnect.autosearch import StrategyScore, _compute_score
from freeconnect.strategies import Strategy
from freeconnect.tester import ServiceResult, SiteResult


def _svc(service, ok_sites, voice=None):
    sites = [SiteResult(host=f"h{i}", ok=True) for i in range(3)] if ok_sites else \
            [SiteResult(host=f"h{i}", ok=False) for i in range(3)]
    return ServiceResult(service=service, sites=sites, voice_ok=voice)


def _score(services):
    st = Strategy(id="x", name="x", source_bat="x", args=[])
    return StrategyScore(strategy=st, services=services)


class TestWorkingServices(unittest.TestCase):
    def test_only_passing_listed(self):
        sc = _score([_svc("discord", True, voice=True), _svc("youtube", False)])
        self.assertEqual(sc.working_services, ["discord"])
        self.assertEqual(sc.services_ok, 1)


class TestResultLabel(unittest.TestCase):
    def test_all_when_every_service_ok(self):
        sc = _score([_svc("discord", True, voice=True), _svc("youtube", True)])
        self.assertEqual(sc.result_label(), "All")

    def test_discord_only(self):
        sc = _score([_svc("discord", True, voice=True), _svc("youtube", False)])
        self.assertEqual(sc.result_label(), "Discord")

    def test_youtube_only_when_voice_dead(self):
        # Discord-сайт открыт, но голос мёртв -> Discord не считается -> только YouTube.
        sc = _score([_svc("discord", True, voice=False), _svc("youtube", True)])
        self.assertEqual(sc.result_label(), "YouTube")

    def test_empty_when_nothing_works(self):
        sc = _score([_svc("discord", False, voice=False), _svc("youtube", False)])
        self.assertEqual(sc.result_label(), "")


def _all_svc(loss, jitter, rtt):
    disc = ServiceResult(
        service="discord",
        sites=[SiteResult(host=f"d{i}", ok=True) for i in range(3)],
        voice_ok=True, voice_rtt=rtt, voice_loss=loss, voice_jitter=jitter)
    yt = ServiceResult(service="youtube",
                       sites=[SiteResult(host=f"y{i}", ok=True) for i in range(3)])
    return _score([disc, yt])


class TestVoiceRanking(unittest.TestCase):
    def test_cleaner_voice_scores_higher(self):
        clean = _all_svc(loss=0.0, jitter=10, rtt=30)   # быстрый стабильный голос
        lossy = _all_svc(loss=0.3, jitter=200, rtt=60)  # потери/джиттер → долгий коннект
        _compute_score(clean, 2)
        _compute_score(lossy, 2)
        self.assertEqual(clean.services_ok, 2)
        self.assertEqual(lossy.services_ok, 2)
        self.assertGreater(clean.score, lossy.score)   # чистый голос — выше

    def test_all_still_beats_discord_only_despite_voice_penalty(self):
        # Even a lossy All must outrank a Discord-only (services_ok решает первым).
        lossy_all = _all_svc(loss=0.4, jitter=300, rtt=90)
        disc_only = _score([_svc("discord", True, voice=True), _svc("youtube", False)])
        _compute_score(lossy_all, 2)
        _compute_score(disc_only, 2)
        self.assertGreater(lossy_all.score, disc_only.score)


if __name__ == "__main__":
    unittest.main()
