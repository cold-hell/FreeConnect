"""DoH: сборка DNS-запроса, валидация ответа, фолбэк резолвера (без сети/без смены DNS)."""
import struct
import unittest
from unittest import mock

from freeconnect import doh


def _resp(rcode: int, ancount: int) -> bytes:
    tid = b"\xab\xcd"
    flags = struct.pack("!H", 0x8000 | (rcode & 0x0F))  # QR=1 + RCODE
    return tid + flags + struct.pack("!HHHH", 1, ancount, 0, 0)


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._d = data
    def read(self) -> bytes:
        return self._d
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class TestDNSWire(unittest.TestCase):
    def test_build_query_structure(self):
        pkt = doh._build_query("example.com", qtype=1)
        self.assertEqual(pkt[2:4], b"\x01\x00")               # RD=1, стандартный запрос
        self.assertEqual(struct.unpack("!H", pkt[4:6])[0], 1)  # QDCOUNT
        self.assertEqual(struct.unpack("!H", pkt[6:8])[0], 0)  # ANCOUNT
        self.assertIn(b"\x07example\x03com\x00", pkt)          # имя в формате меток
        qtype, qclass = struct.unpack("!HH", pkt[-4:])
        self.assertEqual((qtype, qclass), (1, 1))              # A, IN

    def test_response_ok_true(self):
        self.assertTrue(doh._response_ok(_resp(0, 1)))         # NOERROR + есть ответ

    def test_response_ok_false(self):
        self.assertFalse(doh._response_ok(_resp(3, 0)))        # NXDOMAIN
        self.assertFalse(doh._response_ok(_resp(0, 0)))        # нет ответов
        self.assertFalse(doh._response_ok(b""))                # пусто
        self.assertFalse(doh._response_ok(b"\x00" * 8))        # короче заголовка


class TestDoHForward(unittest.TestCase):
    def test_valid_answer_wins_when_one_resolver_broken(self):
        # Резолверы опрашиваются параллельно; сломанный (cert/таймаут) не должен мешать
        # рабочему ответить. Иначе прокси затыкается на сломанном и молчит.
        good = _resp(0, 1)
        def fake_urlopen(req, timeout=None):
            if "1.1.1.1" in req.full_url:
                raise OSError("этот резолвер сломан")
            return _FakeResp(good)
        with mock.patch.object(doh.urllib.request, "urlopen", fake_urlopen):
            out = doh._doh_query(b"Q", timeout=1.0)
        self.assertEqual(out, good)

    def test_invalid_response_rejected(self):
        # Мусор вместо валидного DNS-ответа не принимаем (иначе вернём подмену).
        def fake_urlopen(req, timeout=None):
            return _FakeResp(b"not-a-dns-answer")
        with mock.patch.object(doh.urllib.request, "urlopen", fake_urlopen):
            self.assertIsNone(doh._doh_query(b"Q", timeout=1.0))

    def test_all_resolvers_fail_returns_none(self):
        def fake_urlopen(req, timeout=None):
            raise OSError("все недоступны")
        with mock.patch.object(doh.urllib.request, "urlopen", fake_urlopen):
            self.assertIsNone(doh._doh_query(b"Q", timeout=1.0))


class TestResolverOrder(unittest.TestCase):
    def test_cloudflare_before_google(self):
        # В РФ IP Google душат: если 8.8.8.8 впереди, каждый запрос висит на его
        # таймауте, DoH тормозит и система откатывается на подменяемый plaintext.
        urls = doh._DOH_URLS
        cf = next(i for i, u in enumerate(urls) if "1.1.1.1" in u)
        goog = next(i for i, u in enumerate(urls) if "8.8.8.8" in u)
        self.assertLess(cf, goog, "Cloudflare должен опрашиваться раньше Google")


class TestPsEncoding(unittest.TestCase):
    def test_ps_decodes_non_utf8_without_crashing(self):
        # Имена адаптеров на русской Windows кириллические и приходят не в UTF-8.
        # _ps не должен падать на декоде (иначе адаптер «теряется» и DoH не включается).
        class _P:
            returncode = 0
            stdout = "Беспроводная сеть".encode("cp866")  # OEM-байты, не UTF-8
        with mock.patch.object(doh.subprocess, "run", lambda *a, **k: _P()):
            rc, out = doh._ps("dummy")
        self.assertEqual(rc, 0)
        self.assertIsInstance(out, str)   # не упало, вернулась строка


if __name__ == "__main__":
    unittest.main()
