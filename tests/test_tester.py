"""STUN-пакет (мелкий и большой) и гейтинг голоса в ServiceResult (tester)."""
import struct
import unittest

from freeconnect import tester
from freeconnect.tester import ServiceResult, SiteResult, _stun_packet, check_voice


def _svc(service, n_ok, n_total, voice=None):
    sites = [SiteResult(host=f"h{i}", ok=(i < n_ok)) for i in range(n_total)]
    return ServiceResult(service=service, sites=sites, voice_ok=voice)


class TestStunPacket(unittest.TestCase):
    def test_small_packet_header(self):
        txid, pkt = _stun_packet(0)
        self.assertEqual(len(pkt), 20)
        mtype, mlen, cookie = struct.unpack("!HHI", pkt[:8])
        self.assertEqual(mtype, 0x0001)          # Binding Request
        self.assertEqual(mlen, 0)                # без атрибутов
        self.assertEqual(cookie, 0x2112A442)     # magic cookie
        self.assertEqual(pkt[8:20], txid)

    def test_big_packet_padding(self):
        _, pkt = _stun_packet(1000)
        # header(20) + attr-header(4) + value(1000) = 1024, длина кратна 4
        self.assertEqual(len(pkt), 1024)
        self.assertEqual((len(pkt) - 20) % 4, 0)
        _, mlen, _ = struct.unpack("!HHI", pkt[:8])
        self.assertEqual(mlen, len(pkt) - 20)    # поле длины = размер атрибутов


class TestVoiceGating(unittest.TestCase):
    def test_sites_ok_needs_majority(self):
        self.assertTrue(_svc("youtube", 2, 3).sites_ok)   # 2/3 >= ceil(3/2)=2
        self.assertFalse(_svc("youtube", 1, 3).sites_ok)  # 1/3 < 2

    def test_youtube_no_voice_ok_equals_sites(self):
        s = _svc("youtube", 3, 3, voice=None)
        self.assertTrue(s.ok)

    def test_discord_dead_voice_fails_even_if_sites_ok(self):
        s = _svc("discord", 3, 3, voice=False)
        self.assertTrue(s.sites_ok)
        self.assertFalse(s.ok)   # ключевой инвариант: сайт открыт, но голос мёртв -> не ок

    def test_discord_live_voice_ok(self):
        self.assertTrue(_svc("discord", 3, 3, voice=True).ok)


class TestVoiceRetry(unittest.TestCase):
    """Разовая сетевая осечка не должна помечать голос как мёртвый (иначе прячем
    рабочую стратегию, как было с ALT9)."""

    def setUp(self):
        self._orig = tester.stun_burst

    def tearDown(self):
        tester.stun_burst = self._orig

    def test_retry_recovers_from_transient_miss(self):
        calls = {"n": 0}

        def fake_burst(server, count, timeout, pad=0):
            # первый прогон мелких проб — полная потеря; ретрай — все ответили.
            if pad == 0:
                calls["n"] += 1
                if calls["n"] == 1:
                    return count, []                       # осечка
                return count, [30.0] * count               # норм
            return count, [31.0]                           # большой пакет прошёл

        tester.stun_burst = fake_burst
        res = check_voice(attempts=5, timeout=0.01, retries=1)
        self.assertTrue(res.voice_ok)          # ретрай спас голос
        self.assertEqual(res.voice_loss, 0.0)

    def test_no_retry_left_reports_dead(self):
        def fake_burst(server, count, timeout, pad=0):
            return count, []                               # всегда молчит

        tester.stun_burst = fake_burst
        res = check_voice(attempts=5, timeout=0.01, retries=1)
        self.assertFalse(res.voice_ok)         # реально мёртвый UDP — честный минус


if __name__ == "__main__":
    unittest.main()
