# -*- coding: utf-8 -*-
"""Тесты правок скорости/качества проб в tester.

Что проверяем:
  * цель-«советник» (redirector.googlevideo.com) не жжёт полный таймаут и не
    влияет на вердикт sites_ok — из-за неё один кандидат стоил 5 секунд;
  * голос не проверяется, когда сайты Discord не открылись (стратегия и так
    нерабочая) — это главная экономия времени в переборе;
  * быстрый режим проверки голоса даёт ответ «жив/мёртв» без полной многометрики.
"""
from __future__ import annotations

import unittest
from unittest import mock

from freeconnect import tester


def _site(host: str, ok: bool, status: str = "OK") -> tester.SiteResult:
    return tester.SiteResult(host=host, ok=ok, status=status, latency_ms=50.0 if ok else -1.0)


class TestAdvisoryTargets(unittest.TestCase):
    """Мёртвая цель не должна определять время прогона и вердикт."""

    def test_advisory_target_is_marked_and_short_timeout(self):
        seen: list[tuple[str, float]] = []

        def fake_check(host, path="/", timeout=5.0, probe_freeze=True):
            seen.append((host, timeout))
            return _site(host, ok=False, status="TIMEOUT")

        with mock.patch.object(tester, "check_site", side_effect=fake_check):
            res = tester.test_service("youtube", timeout=5.0, check_voice_udp=False)

        host, timeout = next((h, t) for h, t in seen if h == "redirector.googlevideo.com")
        self.assertLessEqual(timeout, tester.ADVISORY_TIMEOUT,
                             "у цели-советника должен быть короткий таймаут, а не полный")
        decisive = [t for h, t in seen if h != "redirector.googlevideo.com"]
        self.assertTrue(all(t == 5.0 for t in decisive),
                        "обычные цели проверяются полным таймаутом")
        advisory = [s for s in res.sites if s.advisory]
        self.assertEqual(len(advisory), 1)
        self.assertFalse(advisory[0].ok)

    def test_sites_ok_ignores_advisory(self):
        """Две решающие цели открыты, советник висит — сервис всё равно рабочий."""
        sites = [
            tester.SiteResult(host="www.youtube.com", ok=True, status="OK"),
            tester.SiteResult(host="i.ytimg.com", ok=True, status="OK"),
            tester.SiteResult(host="redirector.googlevideo.com", ok=False,
                              status="TIMEOUT", advisory=True),
        ]
        svc = tester.ServiceResult(service="youtube", sites=sites)
        self.assertTrue(svc.sites_ok)

    def test_decisive_only_threshold(self):
        """Советник не добивает порог: 1 из 1 решающей закрыт -> не ок."""
        sites = [
            tester.SiteResult(host="www.youtube.com", ok=False, status="RST"),
            tester.SiteResult(host="redirector.googlevideo.com", ok=True, status="OK",
                              advisory=True),
        ]
        svc = tester.ServiceResult(service="youtube", sites=sites)
        self.assertFalse(svc.sites_ok)

    def test_advisory_target_still_in_targets(self):
        """Цель не выкинули из набора — она по-прежнему проверяется (как советник)."""
        hosts = [h for h, _ in tester.DEFAULT_TARGETS["youtube"]]
        self.assertIn("redirector.googlevideo.com", hosts)


class TestVoiceGate(unittest.TestCase):
    """Голос не измеряем там, где TCP уже провалился."""

    def test_voice_not_called_when_sites_fail(self):
        with mock.patch.object(tester, "check_site",
                               side_effect=lambda host, path="/", **kw: _site(host, ok=False, status="TIMEOUT")), \
             mock.patch.object(tester, "check_voice") as voice:
            res = tester.test_service("discord", timeout=5.0)
        voice.assert_not_called()
        self.assertFalse(res.voice_ok)
        self.assertIn("не проверялся", res.voice_detail)
        self.assertFalse(res.ok)

    def test_voice_called_when_sites_pass(self):
        with mock.patch.object(tester, "check_site",
                               side_effect=lambda host, path="/", **kw: _site(host, ok=True)), \
             mock.patch.object(tester, "check_voice") as voice:
            voice.return_value = tester.ServiceResult(service="_voice", voice_ok=True,
                                                      voice_rtt=30.0, voice_conf="high",
                                                      voice_detail="ok")
            res = tester.test_service("discord", timeout=5.0)
        voice.assert_called_once()
        self.assertTrue(res.voice_ok)
        self.assertTrue(res.ok)

    def test_gate_can_be_disabled(self):
        with mock.patch.object(tester, "check_site",
                               side_effect=lambda host, path="/", **kw: _site(host, ok=False, status="RST")), \
             mock.patch.object(tester, "check_voice") as voice:
            voice.return_value = tester.ServiceResult(service="_voice", voice_ok=True,
                                                      voice_conf="high")
            tester.test_service("discord", timeout=5.0, skip_voice_if_sites_fail=False)
        voice.assert_called_once()

    def test_youtube_has_no_voice_probe(self):
        with mock.patch.object(tester, "check_site",
                               side_effect=lambda host, path="/", **kw: _site(host, ok=True)), \
             mock.patch.object(tester, "check_voice") as voice:
            res = tester.test_service("youtube", timeout=5.0)
        voice.assert_not_called()
        self.assertIsNone(res.voice_ok)


class TestQuickVoice(unittest.TestCase):
    """Быстрый режим: один короткий залп вместо многометрики."""

    def test_quick_voice_returns_on_first_reply(self):
        with mock.patch.object(tester, "stun_burst", return_value=(2, [25.0, 27.0])) as burst:
            res = tester.check_voice(quick=True)
        self.assertEqual(burst.call_count, 1, "быстрый режим — один залп")
        self.assertTrue(res.voice_ok)
        self.assertEqual(res.voice_rtt, 25.0)
        self.assertEqual(res.voice_conf, "low")
        self.assertIn("быстрая проба", res.voice_detail)

    def test_quick_voice_dead_udp(self):
        with mock.patch.object(tester, "stun_burst", return_value=(2, [])):
            res = tester.check_voice(quick=True)
        self.assertFalse(res.voice_ok)
        self.assertEqual(res.voice_rtt, -1.0)

    def test_full_voice_still_measures_metrics(self):
        """Полный режим не сломан: считает потери, джиттер и большой пакет."""
        calls = {"n": 0}

        def fake_burst(server, count, timeout, pad=0):
            calls["n"] += 1
            if pad:
                return count, [30.0]
            return count, [20.0, 40.0, 22.0, 21.0, 23.0]

        with mock.patch.object(tester, "stun_burst", side_effect=fake_burst):
            res = tester.check_voice()
        self.assertTrue(res.voice_ok)
        self.assertEqual(res.voice_loss, 0.0)
        self.assertGreater(res.voice_jitter, 0.0)
        self.assertIn("big=ok", res.voice_detail)


if __name__ == "__main__":
    unittest.main()
