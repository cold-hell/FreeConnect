"""Чистая логика пассивного детектора голоса (voicewatch): разбор пакетов, выбор пира,
вердикт окна. Захват через WinDivert тут НЕ трогаем (нужен драйвер + живой звонок —
проверяется на машине пользователя); проверяем только детерминированную логику."""
import struct
import unittest

from freeconnect import voicewatch as vw


def _ipv4_udp(src_ip, dst_ip, sport, dport, payload=b"\x00" * 20):
    def ip4(s):
        return bytes(int(x) for x in s.split("."))
    ihl_ver = 0x45
    total = 20 + 8 + len(payload)
    ip = struct.pack("!BBHHHBBH", ihl_ver, 0, total, 0, 0, 64, 17, 0) + ip4(src_ip) + ip4(dst_ip)
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0)
    return ip + udp + payload


class TestParse(unittest.TestCase):
    def test_parse_ipv4_and_ports(self):
        pkt = _ipv4_udp("192.168.1.5", "66.22.200.10", 51000, 50005)
        self.assertEqual(vw.parse_ipv4(pkt), ("192.168.1.5", "66.22.200.10", 17))
        self.assertEqual(vw.udp_ports(pkt), (51000, 50005))

    def test_is_voice_udp(self):
        self.assertTrue(vw.is_voice_udp(_ipv4_udp("192.168.1.5", "66.22.200.10", 51000, 50005)))
        # низкие порты (напр. DNS) — не голос
        self.assertFalse(vw.is_voice_udp(_ipv4_udp("192.168.1.5", "8.8.8.8", 40000, 53)))

    def test_remote_ip_direction(self):
        pkt = _ipv4_udp("192.168.1.5", "66.22.200.10", 51000, 50005)
        self.assertEqual(vw.remote_ip(pkt, outbound=True), "66.22.200.10")   # цель = сервер
        pkt2 = _ipv4_udp("66.22.200.10", "192.168.1.5", 50005, 51000)
        self.assertEqual(vw.remote_ip(pkt2, outbound=False), "66.22.200.10")  # источник = сервер

    def test_non_ipv4_rejected(self):
        self.assertIsNone(vw.parse_ipv4(b"\x60" + b"\x00" * 40))   # IPv6 версия
        self.assertIsNone(vw.parse_ipv4(b"\x45\x00"))               # слишком короткий


class TestVerdict(unittest.TestCase):
    def test_idle_when_little_outbound(self):
        self.assertEqual(vw.verdict(out_pkts=5, in_pkts=0, min_out=25, dead_in=3), "idle")

    def test_dead_when_sending_but_no_inbound(self):
        self.assertEqual(vw.verdict(out_pkts=120, in_pkts=1, min_out=25, dead_in=3), "dead")

    def test_ok_when_bidirectional(self):
        self.assertEqual(vw.verdict(out_pkts=120, in_pkts=110, min_out=25, dead_in=3), "ok")

    def test_pick_peer_by_traffic(self):
        peers = {"a": [10, 10], "b": [100, 90], "c": [1, 1]}
        self.assertEqual(vw.pick_peer(peers), "b")
        self.assertIsNone(vw.pick_peer({}))


class TestVoiceWatchLoop(unittest.TestCase):
    def test_dead_streak_triggers_on_dead(self):
        """Симулируем поток: много исходящих, ноль входящих — после need_dead_windows
        окон должен прийти on_dead(). Подменяем _recv и _open, время — фейковыми часами."""
        fired = []
        w = vw.VoiceWatch(on_dead=lambda r: fired.append(r), window=1.0,
                          min_out=5, dead_in=1, need_dead_windows=2)

        # фейковый захват: поток исходящих пакетов к одному пиру, без входящих
        pkt = _ipv4_udp("192.168.1.5", "66.22.200.10", 51000, 50005)
        seq = iter([(pkt, True)] * 1000)

        def fake_recv():
            try:
                return next(seq)
            except StopIteration:
                w._stop.set()
                return None
        w._recv = fake_recv
        w._open = lambda: None
        w._close = lambda: None

        # фейковые монотонные часы: каждый вызов +0.2с — окна закрываются регулярно
        clock = {"t": 0.0}
        orig = vw.time.monotonic
        vw.time.monotonic = lambda: clock.__setitem__("t", clock["t"] + 0.2) or clock["t"]
        try:
            w._run()
        finally:
            vw.time.monotonic = orig
        self.assertTrue(fired, "on_dead должен сработать при одностороннем потоке")


if __name__ == "__main__":
    unittest.main()
