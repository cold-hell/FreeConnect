"""Движок обхода Telegram (Фаза 1): определение дата-центра, SOCKS5-рукопожатие
и сквозной релей SOCKS5→WebSocket. Реальный Telegram/сеть не трогаем — веб-сокет
подменяем эхо-заглушкой, клиента поднимаем обычным сокетом."""
import asyncio
import os
import socket
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from freeconnect import tgproxy


def _make_init(dc: int) -> bytes:
    """Собирает валидный obfuscated2 init-пакет, кодирующий заданный ДЦ, — как это
    делает клиент Telegram: первые 56 байт открытые (в т.ч. ключ/IV), хвост [56:64]
    зашифрован AES-256-CTR(ключ=body[8:40], iv=body[40:56])."""
    body = bytearray(os.urandom(64))
    body[0] = 0x01                       # не 0xef (требование транспорта)
    key, iv = bytes(body[8:40]), bytes(body[40:56])
    body[56:60] = b"\xee\xee\xee\xee"    # метка транспорта
    body[60:62] = int(dc).to_bytes(2, "little", signed=True)
    body[62:64] = b"\x00\x00"
    enc = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor().update(bytes(body))
    return bytes(body[0:56]) + enc[56:64]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeWS:
    """Заглушка веб-сокета: пишет отправленное в .sent и эхом возвращает обратно
    (проверяет оба направления релея)."""
    instances: list["_FakeWS"] = []

    def __init__(self, uri: str):
        self.uri = uri
        self.sent: list[bytes] = []
        self.closed = False
        self._q: asyncio.Queue = asyncio.Queue()
        _FakeWS.instances.append(self)

    async def send(self, data):
        self.sent.append(bytes(data))
        await self._q.put(bytes(data))     # эхо назад -> WebSocket→TCP

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.closed = True
        await self._q.put(None)


async def _fake_connect(uri, **_kw):
    return _FakeWS(uri)


class TestEndpoints(unittest.TestCase):
    """Выбор адреса-кандидата: главный путь обхода — идти на подобранный живой IP,
    а не на тот, что отдаёт DNS (он заблокирован по TCP)."""

    def test_known_endpoints_for_working_dcs(self):
        # ДЦ2/ДЦ4 обслуживает один живой узел (полевой замер, РФ).
        self.assertEqual(tgproxy.known_endpoints(2), ["149.154.167.220"])
        self.assertEqual(tgproxy.known_endpoints(4), ["149.154.167.220"])

    def test_known_endpoints_absent_for_other_dcs(self):
        for dc in (1, 3, 5):
            self.assertEqual(tgproxy.known_endpoints(dc), [])

    def test_overrides_replace_table(self):
        # config.json может переопределить таблицу — ключ приходит строкой.
        self.assertEqual(tgproxy.known_endpoints(2, {"2": ["9.9.9.9"]}), ["9.9.9.9"])
        self.assertEqual(tgproxy.known_endpoints(2, {2: ["8.8.8.8"]}), ["8.8.8.8"])

    def test_tls_context_alpn_is_http11_only(self):
        # h2 в ALPN сломал бы WebSocket-апгрейд (сервер ответит фреймами HTTP/2).
        ctx = tgproxy._tls_context()
        self.assertTrue(ctx.check_hostname)          # сертификат проверяем
        self.assertEqual(ctx.verify_mode, __import__("ssl").CERT_REQUIRED)


class TestDcDetection(unittest.TestCase):
    def test_dc_from_init_roundtrip(self):
        for dc in (1, 2, 3, 4, 5):
            self.assertEqual(tgproxy.dc_from_init(_make_init(dc)), dc)

    def test_dc_from_init_media_dc_negative(self):
        # media-ДЦ кодируется отрицательным id -> abs даёт номер ДЦ.
        self.assertEqual(tgproxy.dc_from_init(_make_init(-2)), 2)

    def test_dc_from_init_rejects_garbage(self):
        self.assertIsNone(tgproxy.dc_from_init(b"\x00" * 64))   # id вне 1..5
        self.assertIsNone(tgproxy.dc_from_init(b"short"))

    def test_dc_from_ip(self):
        self.assertEqual(tgproxy.dc_from_ip("149.154.175.50"), 1)
        self.assertEqual(tgproxy.dc_from_ip("149.154.167.51"), 2)
        self.assertEqual(tgproxy.dc_from_ip("91.108.56.130"), 5)
        self.assertIsNone(tgproxy.dc_from_ip("8.8.8.8"))
        self.assertIsNone(tgproxy.dc_from_ip(None))
        self.assertIsNone(tgproxy.dc_from_ip("not-an-ip"))

    def test_deeplink(self):
        self.assertEqual(tgproxy.deeplink(1080),
                         "tg://socks?server=127.0.0.1&port=1080")
        self.assertIn("port=9999", tgproxy.deeplink(9999))


class TestDiscover(unittest.TestCase):
    """Автопоиск живого адреса. Сеть не трогаем: подменяем скан и проверку узла.
    Главное свойство — принимаем адрес ТОЛЬКО если он подтверждён сертификатом."""

    SUBNET = "192.0.2.0/29"   # 6 адресов (документационная подсеть)

    def setUp(self):
        self._orig_alive = tgproxy._tcp_alive
        self._orig_probe = tgproxy._probe_ip

    def tearDown(self):
        tgproxy._tcp_alive = self._orig_alive
        tgproxy._probe_ip = self._orig_probe

    def _discover(self, alive, verified, limit=3, progress=None):
        async def fake_alive(ip, _timeout, _sem):
            return ip if ip in alive else None

        async def fake_probe(_dc, ip, _timeout):
            return {"ip": ip, "ok": ip in verified}

        tgproxy._tcp_alive = fake_alive
        tgproxy._probe_ip = fake_probe
        return tgproxy.discover(dc=2, subnets=[self.SUBNET], limit=limit,
                                progress=progress)

    def test_accepts_only_certificate_verified(self):
        # Отвечают трое, но настоящий узел Telegram — один.
        found = self._discover(alive={"192.0.2.1", "192.0.2.2", "192.0.2.3"},
                               verified={"192.0.2.2"})
        self.assertEqual(found, ["192.0.2.2"])

    def test_nothing_alive(self):
        self.assertEqual(self._discover(alive=set(), verified=set()), [])

    def test_alive_but_not_telegram(self):
        # Адрес отвечает, но сертификат не подтверждает kws -> не берём.
        self.assertEqual(
            self._discover(alive={"192.0.2.1"}, verified=set()), [])

    def test_stops_at_limit(self):
        every = {f"192.0.2.{i}" for i in range(1, 7)}
        found = self._discover(alive=every, verified=every, limit=2)
        self.assertEqual(len(found), 2)

    def test_reports_progress(self):
        seen = []
        self._discover(alive={"192.0.2.1"}, verified={"192.0.2.1"},
                       progress=seen.append)
        self.assertTrue(seen)
        self.assertIn(seen[-1]["stage"], ("scan", "verify"))


class TestLifecycle(unittest.TestCase):
    def test_available(self):
        self.assertTrue(tgproxy.TgProxy().available())   # зависимости стоят

    def test_start_reports_port_and_stops(self):
        p = tgproxy.TgProxy()
        port = _free_port()
        p.start(port=port)
        try:
            self.assertTrue(p.is_running())
            self.assertEqual(p.port(), port)
        finally:
            p.stop()
        self.assertFalse(p.is_running())

    def test_start_on_busy_port_raises(self):
        port = _free_port()
        first = tgproxy.TgProxy()
        first.start(port=port)
        second = tgproxy.TgProxy()
        try:
            with self.assertRaises(tgproxy.TgProxyError):
                second.start(port=port)
        finally:
            first.stop()
            second.stop()


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        _FakeWS.instances.clear()
        self.dialed: list[tuple[int, str]] = []
        self.fail_ips: set[str] = set()
        dialed, fail_ips = self.dialed, self.fail_ips

        # Сеть не трогаем: подменяем и список кандидатов, и сам набор соединения.
        async def fake_candidates(_self, dc):
            return ["10.0.0.1", "10.0.0.2"]

        async def fake_dial(_self, dc, ip):
            dialed.append((dc, ip))
            if ip in fail_ips:
                raise OSError("узел недоступен")
            return _FakeWS(tgproxy.WS_URL_TMPL.format(dc=dc))

        self._orig_c = tgproxy.TgProxy._candidates
        self._orig_d = tgproxy.TgProxy._dial
        tgproxy.TgProxy._candidates = fake_candidates
        tgproxy.TgProxy._dial = fake_dial

    def tearDown(self):
        tgproxy.TgProxy._candidates = self._orig_c
        tgproxy.TgProxy._dial = self._orig_d

    def _socks_connect(self, port, target_ip=(149, 154, 175, 5)):
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.settimeout(5)
        c.sendall(b"\x05\x01\x00")                       # greeting: no-auth
        self.assertEqual(c.recv(2), b"\x05\x00")
        c.sendall(b"\x05\x01\x00\x01" + bytes(target_ip) + (443).to_bytes(2, "big"))
        rep = c.recv(10)                                 # connect success
        self.assertEqual(rep[:2], b"\x05\x00")
        return c

    def test_full_relay_prefers_init_dc(self):
        p = tgproxy.TgProxy()
        port = _free_port()
        p.start(port=port)
        try:
            # IP говорит ДЦ1 (149.154.175.x), init кодирует ДЦ3 — init приоритетнее.
            c = self._socks_connect(port, target_ip=(149, 154, 175, 5))
            init = _make_init(3)
            c.sendall(init)
            c.sendall(b"hello-mtproto")

            want = init + b"hello-mtproto"                # эхо: init + полезная нагрузка
            got = b""
            while len(got) < len(want):
                chunk = c.recv(4096)
                if not chunk:
                    break
                got += chunk
            c.close()
            self.assertEqual(got, want)

            self.assertTrue(_FakeWS.instances)
            ws = _FakeWS.instances[-1]
            self.assertEqual(ws.uri, "wss://kws3.web.telegram.org/apiws")
            self.assertEqual(ws.sent[0], init)           # init ушёл первым фреймом
        finally:
            p.stop()

    def test_relay_falls_back_to_ip_dc(self):
        p = tgproxy.TgProxy()
        port = _free_port()
        p.start(port=port)
        try:
            c = self._socks_connect(port, target_ip=(91, 108, 56, 130))   # ДЦ5 по IP
            init = _make_init(99)                         # id вне 1..5 -> init не разобран
            c.sendall(init)
            # эхо init-а подтверждает, что веб-сокет открылся и релей заработал.
            got = b""
            while len(got) < len(init):
                chunk = c.recv(4096)
                if not chunk:
                    break
                got += chunk
            c.close()
            self.assertEqual(got, init)
            self.assertEqual(_FakeWS.instances[-1].uri,
                             "wss://kws5.web.telegram.org/apiws")   # выбран ДЦ по IP
        finally:
            p.stop()

    def _one_connection(self, port, dc=2):
        """Прогоняет одно соединение до эха init-а (веб-сокет открыт, релей пошёл)."""
        c = self._socks_connect(port, target_ip=(149, 154, 167, 51))
        init = _make_init(dc)
        c.sendall(init)
        got = b""
        while len(got) < len(init):
            chunk = c.recv(4096)
            if not chunk:
                break
            got += chunk
        c.close()
        return init, got

    def test_dials_first_candidate_ip(self):
        p = tgproxy.TgProxy()
        port = _free_port()
        p.start(port=port)
        try:
            init, got = self._one_connection(port, dc=2)
            self.assertEqual(got, init)
            # Идём на первый подобранный адрес, а не в DNS.
            self.assertEqual(self.dialed, [(2, "10.0.0.1")])
        finally:
            p.stop()

    def test_failover_to_next_ip(self):
        self.fail_ips.add("10.0.0.1")           # первый адрес «умер»
        p = tgproxy.TgProxy()
        port = _free_port()
        p.start(port=port)
        try:
            init, got = self._one_connection(port, dc=2)
            self.assertEqual(got, init)         # соединение всё равно состоялось
            # Два ретрая на мёртвом адресе, затем переход на запасной.
            self.assertEqual(self.dialed,
                             [(2, "10.0.0.1"), (2, "10.0.0.1"), (2, "10.0.0.2")])
        finally:
            p.stop()


class TestCfFallback(unittest.TestCase):
    """Резервный канал через Cloudflare: когда прямые адреса недоступны, идём на
    kws{dc}.{cf_domain} (host, ip=None -> DNS)."""

    def test_falls_back_to_cf_when_direct_dead(self):
        p = tgproxy.TgProxy(cf_domains=["relay.example"])
        calls = []

        async def fake_candidates(dc):
            return ["10.0.0.1"]

        async def fake_dial(dc, ip, host=None):
            calls.append((dc, ip, host))
            if ip is not None:            # прямой адрес — мёртв
                raise OSError("dead")
            return _FakeWS(f"wss://{host}/apiws")   # CF-домен — успех

        p._candidates = fake_candidates
        p._dial = fake_dial
        ws = asyncio.run(p._ws_connect(2))
        self.assertEqual(ws.uri, "wss://kws2.relay.example/apiws")
        self.assertIn((2, None, "kws2.relay.example"), calls)   # ушли в CF
        self.assertTrue(any(ip == "10.0.0.1" for _, ip, _ in calls))  # но сперва пробовали прямой

    def test_direct_success_skips_cf(self):
        p = tgproxy.TgProxy(cf_domains=["relay.example"])
        calls = []

        async def fake_candidates(dc):
            return ["10.0.0.1"]

        async def fake_dial(dc, ip, host=None):
            calls.append((dc, ip, host))
            return _FakeWS("wss://direct/apiws")

        p._candidates = fake_candidates
        p._dial = fake_dial
        asyncio.run(p._ws_connect(2))
        self.assertTrue(all(host is None for _, _, host in calls))  # CF не трогали
        self.assertTrue(all(ip is not None for _, ip, _ in calls))

    def test_no_direct_no_cf_raises(self):
        p = tgproxy.TgProxy(cf_domains=[])

        async def fake_candidates(dc):
            return []

        p._candidates = fake_candidates
        with self.assertRaises(tgproxy.TgProxyError):
            asyncio.run(p._ws_connect(2))

    def test_set_cf_domains_updates(self):
        p = tgproxy.TgProxy()
        self.assertEqual(p._cf_domains, [])
        p.set_cf_domains(["a.com", "b.com"])
        self.assertEqual(p._cf_domains, ["a.com", "b.com"])


if __name__ == "__main__":
    unittest.main()
