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
    def test_fallback_to_second_resolver(self):
        calls = []
        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise OSError("первый резолвер задушен")
            return _FakeResp(b"RESPONSE")
        with mock.patch.object(doh.urllib.request, "urlopen", fake_urlopen):
            out = doh._doh_query(b"QUERY", timeout=1.0)
        self.assertEqual(out, b"RESPONSE")
        self.assertEqual(len(calls), 2)                        # упал первый -> пошли на второй

    def test_all_resolvers_fail_returns_none(self):
        def fake_urlopen(req, timeout=None):
            raise OSError("оба недоступны")
        with mock.patch.object(doh.urllib.request, "urlopen", fake_urlopen):
            self.assertIsNone(doh._doh_query(b"Q", timeout=1.0))


if __name__ == "__main__":
    unittest.main()
