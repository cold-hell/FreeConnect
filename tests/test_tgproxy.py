"""Адаптер ядра обхода Telegram (freeconnect/tgproxy.py).

Реальное ядро в тестах не поднимаем: оно ходит в сеть. Для проверки жизненного
цикла подменяем его фейком, который просто слушает порт и ждёт сигнала остановки, —
так проверяется именно наш код: раскладка настроек, порт, ссылка, старт/стоп.
"""
import asyncio
import socket
import threading
import types
import unittest

from freeconnect import tgproxy

SECRET = "0123456789abcdef0123456789abcdef"


class _FakeCore:
    """Мини-ядро: открывает порт и держит его до stop_event (как настоящее _run)."""

    def __init__(self, config) -> None:
        self._config = config
        self.finished = threading.Event()

    async def _run(self, stop_event):
        cfg = self._config.proxy_config

        async def _drop(reader, writer):
            writer.close()

        server = await asyncio.start_server(_drop, cfg.host, cfg.port)
        try:
            await stop_event.wait()
        finally:
            server.close()
            await server.wait_closed()
            self.finished.set()


class TestLinks(unittest.TestCase):
    def test_plain_secret(self):
        link = tgproxy.deeplink(1443, "127.0.0.1", SECRET)
        self.assertEqual(link,
                         f"tg://proxy?server=127.0.0.1&port=1443&secret=dd{SECRET}")

    def test_without_secret(self):
        self.assertEqual(tgproxy.deeplink(1443, "127.0.0.1"),
                         "tg://proxy?server=127.0.0.1&port=1443")

    def test_fake_tls_uses_ee_prefix_and_domain_hex(self):
        link = tgproxy.deeplink(1443, "127.0.0.1", SECRET, "example.com")
        self.assertEqual(link,
                         "tg://proxy?server=127.0.0.1&port=1443"
                         f"&secret=ee{SECRET}{'example.com'.encode().hex()}")

    def test_defaults_applied_for_empty_port_and_host(self):
        link = tgproxy.deeplink(0, "", SECRET)
        self.assertIn(f"port={tgproxy.DEFAULT_PORT}", link)
        self.assertIn(f"server={tgproxy.LOCAL_HOST}", link)


class TestDcIps(unittest.TestCase):
    def test_empty_gives_defaults(self):
        self.assertEqual(tgproxy.normalize_dc_ips(None), tgproxy.DEFAULT_DC_IPS)
        self.assertEqual(tgproxy.normalize_dc_ips({}), tgproxy.DEFAULT_DC_IPS)

    def test_string_and_int_keys(self):
        self.assertEqual(tgproxy.normalize_dc_ips({"2": "1.2.3.4", 4: "5.6.7.8"}),
                         {2: "1.2.3.4", 4: "5.6.7.8"})

    def test_garbage_is_dropped(self):
        self.assertEqual(tgproxy.normalize_dc_ips({"2": "1.2.3.4", "x": "9.9.9.9",
                                                   "4": "не-ip", "5": ""}),
                         {2: "1.2.3.4"})

    def test_all_garbage_falls_back_to_defaults(self):
        self.assertEqual(tgproxy.normalize_dc_ips({"2": "nope"}), tgproxy.DEFAULT_DC_IPS)


class TestPorts(unittest.TestCase):
    def test_preferred_returned_when_free(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        self.assertEqual(tgproxy.pick_port(free), free)

    def test_busy_preferred_is_avoided(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            self.assertFalse(tgproxy.port_is_free(taken))
            chosen = tgproxy.pick_port(taken)
        self.assertNotEqual(chosen, taken)
        self.assertTrue(tgproxy.port_is_free(chosen))


class TestVendoredCore(unittest.TestCase):
    def test_core_is_present_and_versioned(self):
        self.assertTrue(tgproxy.available())
        self.assertRegex(tgproxy.core_version(), r"^\d+\.\d+")


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.fake_config = types.SimpleNamespace()
        self.fake_config.proxy_config = types.SimpleNamespace()
        self.fake_core = _FakeCore(self.fake_config)
        self._orig_core = tgproxy._core
        tgproxy._core = lambda: (self.fake_config, self.fake_core)
        self.proxy = tgproxy.TgProxy()
        self.addCleanup(self._restore)

    def _restore(self):
        self.proxy.stop()
        tgproxy._core = self._orig_core

    def _free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_start_returns_port_and_builds_link(self):
        port = self.proxy.start(port=self._free_port(), secret=SECRET)
        self.assertTrue(self.proxy.is_running())
        self.assertEqual(self.proxy.port(), port)
        self.assertIn(f"port={port}", self.proxy.link())
        self.assertIn(f"secret=dd{SECRET}", self.proxy.link())

    def test_start_maps_settings_into_core_config(self):
        self.proxy.start(port=self._free_port(), secret=SECRET,
                         dc_ips={"2": "1.1.1.1"}, cfproxy=False, h2=False,
                         cf_domains=["Relay.Example", " ", "b.example"], pool_size=7)
        cfg = self.fake_config.proxy_config
        self.assertEqual(cfg.dc_redirects, {2: "1.1.1.1"})
        self.assertFalse(cfg.fallback_cfproxy)
        self.assertFalse(cfg.cfproxy_h2_media)
        self.assertEqual(cfg.cfproxy_user_domains, ["relay.example", "b.example"])
        self.assertEqual(cfg.pool_size, 7)

    def test_second_start_is_noop(self):
        port = self.proxy.start(port=self._free_port(), secret=SECRET)
        self.assertEqual(self.proxy.start(port=self._free_port(), secret=SECRET), port)

    def test_stop_finishes_core_and_resets_state(self):
        self.proxy.start(port=self._free_port(), secret=SECRET)
        self.proxy.stop()
        self.assertFalse(self.proxy.is_running())
        self.assertIsNone(self.proxy.port())
        self.assertEqual(self.proxy.link(), "")
        self.assertTrue(self.fake_core.finished.wait(5))

    def test_busy_port_falls_back_to_free_one(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            port = self.proxy.start(port=taken, secret=SECRET)
        self.assertNotEqual(port, taken)
        self.assertTrue(self.proxy.is_running())

    def test_status_shape(self):
        status = self.proxy.status()
        for key in ("available", "running", "version", "host", "port", "link"):
            self.assertIn(key, status)
        self.assertFalse(status["running"])

    def test_missing_core_raises(self):
        def _boom():
            raise RuntimeError("нет ядра")

        tgproxy._core = _boom
        with self.assertRaises(tgproxy.TgProxyError):
            self.proxy.start(port=self._free_port(), secret=SECRET)


class TestDiagnose(unittest.TestCase):
    def test_diagnose_has_steps_and_verdict(self):
        """Проверяем форму ответа, а не сеть: ступени и вердикт на месте."""
        result = tgproxy.diagnose(running=False, port=None, dc_ips={"2": "127.0.0.1"})
        steps = [row["step"] for row in result["rows"]]
        self.assertIn("Ядро обхода", steps)
        self.assertIn("Локальный прокси", steps)
        self.assertTrue(result["verdict"])
        self.assertFalse(result["ok"])          # прокси выключен -> не всё хорошо

    def test_off_proxy_is_reported_as_off(self):
        result = tgproxy.diagnose(running=False, port=1443)
        local = [r for r in result["rows"] if r["step"] == "Локальный прокси"][0]
        self.assertFalse(local["ok"])
        self.assertIn("выключен", local["detail"])


if __name__ == "__main__":
    unittest.main()
