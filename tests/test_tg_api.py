"""Api-методы соединения Telegram: состояние, вкл/выкл, автонастройка, настройки,
проверка ядра. Реальный прокси не поднимаем — подменяем адаптер фейком."""
import threading
import unittest
from unittest import mock

from freeconnect import app as fcapp
from freeconnect import tgproxy, tgws_update


class _FakeTg:
    """Заглушка адаптера: помнит вызовы, отдаёт статус в формате настоящего."""

    def __init__(self, available=True, move_to=None, fail_start=None):
        self._available = available
        self._running = False
        self._port = None
        self._move_to = move_to
        self._fail = fail_start
        self.starts: list[dict] = []
        self.stops = 0
        self.secret = ""

    def available(self):
        return self._available

    def is_running(self):
        return self._running

    def port(self):
        return self._port

    def link(self):
        if not self._running:
            return ""
        return tgproxy.deeplink(self._port, tgproxy.LOCAL_HOST, self.secret)

    def status(self):
        return {"available": self._available, "running": self._running,
                "host": tgproxy.LOCAL_HOST, "port": self._port,
                "secret": self.secret, "link": self.link(), "fake_tls": "",
                "version": "1.11.2", "error": ""}

    def start(self, **kwargs):
        if self._fail:
            raise tgproxy.TgProxyError(self._fail)
        self.starts.append(kwargs)
        self.secret = kwargs.get("secret", "")
        self._port = self._move_to or kwargs.get("port")
        self._running = True
        return self._port

    def stop(self):
        self.stops += 1
        self._running = False
        self._port = None


def _api(cfg=None, tg=None):
    api = fcapp.Api.__new__(fcapp.Api)
    api.cfg = dict(cfg or {})
    api.tgproxy = tg or _FakeTg()
    api._events = []
    api._events_lock = threading.Lock()
    return api


class TestTgApi(unittest.TestCase):
    def setUp(self):
        self.saved: list[dict] = []
        self._orig_save = fcapp.config.save
        fcapp.config.save = lambda cfg: self.saved.append(dict(cfg))
        self.addCleanup(lambda: setattr(fcapp.config, "save", self._orig_save))

    # -- состояние ---------------------------------------------------------

    def test_state_when_off(self):
        api = _api()
        state = api.tg_get_state()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["port"], tgproxy.DEFAULT_PORT)
        self.assertTrue(state["link"].startswith("tg://proxy?"))
        self.assertIn("secret=dd", state["link"])
        self.assertEqual(state["version"], "1.11.2")

    def test_secret_is_generated_once_and_persisted(self):
        api = _api()
        api.tg_get_state()
        secret = api.cfg["tg_secret"]
        self.assertEqual(len(secret), 32)
        int(secret, 16)                       # секрет — валидный hex
        api.tg_get_state()                    # второй раз не перегенерируем
        self.assertEqual(api.cfg["tg_secret"], secret)

    def test_existing_secret_is_reused(self):
        api = _api(cfg={"tg_secret": "a" * 32})
        api.tg_get_state()
        self.assertEqual(api.cfg["tg_secret"], "a" * 32)

    # -- включение / выключение -------------------------------------------

    def test_enable_passes_settings_from_config(self):
        api = _api(cfg={"tg_port": 1500, "tg_secret": "b" * 32,
                        "tg_dc_ips": {"2": "1.1.1.1"}, "tg_cfproxy": False,
                        "tg_h2": False, "tg_cf_domains": ["relay.example"],
                        "tg_faketls": "mask.example", "tg_pool_size": 2})
        result = api.tg_set_enabled(True)
        self.assertTrue(result["ok"])
        kwargs = api.tgproxy.starts[0]
        self.assertEqual(kwargs["port"], 1500)
        self.assertEqual(kwargs["dc_ips"], {"2": "1.1.1.1"})
        self.assertFalse(kwargs["cfproxy"])
        self.assertFalse(kwargs["h2"])
        self.assertEqual(kwargs["cf_domains"], ["relay.example"])
        self.assertEqual(kwargs["fake_tls_domain"], "mask.example")
        self.assertEqual(kwargs["pool_size"], 2)
        self.assertTrue(api.cfg["tg_enabled"])
        self.assertEqual(api.cfg["tg_core_version"], "1.11.2")

    def test_enable_remembers_actual_port_when_preferred_was_busy(self):
        api = _api(cfg={"tg_port": 1443}, tg=_FakeTg(move_to=50555))
        result = api.tg_set_enabled(True)
        self.assertTrue(result["ok"])
        self.assertEqual(api.cfg["tg_port"], 50555)
        self.assertIn("50555", result["message"])
        self.assertIn("занят", result["message"])

    def test_enable_reports_core_error(self):
        api = _api(tg=_FakeTg(fail_start="порт занят"))
        result = api.tg_set_enabled(True)
        self.assertFalse(result["ok"])
        self.assertIn("порт занят", result["error"])
        self.assertFalse(api.cfg.get("tg_enabled"))

    def test_disable_stops_proxy(self):
        api = _api(cfg={"tg_secret": "c" * 32})
        api.tg_set_enabled(True)
        result = api.tg_set_enabled(False)
        self.assertTrue(result["ok"])
        self.assertEqual(api.tgproxy.stops, 1)
        self.assertFalse(api.cfg["tg_enabled"])

    # -- автонастройка -----------------------------------------------------

    def test_autoconfigure_starts_and_opens_link(self):
        api = _api(cfg={"tg_secret": "d" * 32})
        with mock.patch.object(fcapp.os, "startfile") as startfile:
            result = api.tg_autoconfigure()
        self.assertTrue(result["ok"])
        self.assertEqual(len(api.tgproxy.starts), 1)          # прокси подняли
        opened = startfile.call_args[0][0]
        self.assertTrue(opened.startswith("tg://proxy?"))
        self.assertIn("secret=dd", opened)

    def test_autoconfigure_reports_failure_with_link(self):
        api = _api(cfg={"tg_secret": "e" * 32})
        with mock.patch.object(fcapp.os, "startfile",
                               side_effect=OSError("нет обработчика")):
            result = api.tg_autoconfigure()
        self.assertFalse(result["ok"])
        self.assertIn("tg://proxy?", result["error"])

    # -- настройки ---------------------------------------------------------

    def test_options_apply_and_restart_when_running(self):
        api = _api(cfg={"tg_secret": "f" * 32, "tg_port": 1443,
                        "tg_cfproxy": True, "tg_h2": True})
        api.tg_set_enabled(True)
        result = api.tg_set_options(port=1500, cfproxy=False, h2=False,
                                    cf_domains=["Relay.Example"],
                                    faketls="Mask.Example")
        self.assertTrue(result["ok"])
        self.assertEqual(api.cfg["tg_port"], 1500)
        self.assertFalse(api.cfg["tg_cfproxy"])
        self.assertEqual(api.cfg["tg_cf_domains"], ["relay.example"])
        self.assertEqual(api.cfg["tg_faketls"], "mask.example")
        self.assertEqual(api.tgproxy.stops, 1)                # перезапуск был
        self.assertEqual(len(api.tgproxy.starts), 2)

    def test_options_without_changes_do_not_restart(self):
        api = _api(cfg={"tg_secret": "1" * 32, "tg_port": 1443})
        api.tg_set_enabled(True)
        result = api.tg_set_options(port=1443)
        self.assertTrue(result["ok"])
        self.assertEqual(api.tgproxy.stops, 0)

    def test_options_ignore_invalid_port(self):
        api = _api(cfg={"tg_port": 1443})
        api.tg_set_options(port="не число")
        self.assertEqual(api.cfg["tg_port"], 1443)
        api.tg_set_options(port=0)
        self.assertEqual(api.cfg["tg_port"], 1443)

    # -- диагностика и ядро ------------------------------------------------

    def test_diagnose_passes_rows_through(self):
        api = _api(cfg={"tg_secret": "2" * 32})
        payload = {"ok": True,
                   "rows": [{"step": "Ядро обхода", "ok": True, "detail": "1.11.2"}],
                   "verdict": "Соединение Telegram работает", "hint": ""}
        with mock.patch.object(fcapp.tgproxy, "diagnose", return_value=payload) as diag:
            result = api.tg_diagnose()
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], payload["verdict"])
        self.assertIs(diag.call_args.kwargs["running"], False)

    def test_diagnose_survives_exception(self):
        api = _api()
        with mock.patch.object(fcapp.tgproxy, "diagnose",
                               side_effect=RuntimeError("бум")):
            result = api.tg_diagnose()
        self.assertFalse(result["ok"])
        self.assertIn("бум", result["verdict"])

    def test_check_core_update_reports_newer_version(self):
        api = _api()
        with mock.patch.object(tgws_update, "check",
                               return_value={"local": "1.11.2", "latest": "1.12.0",
                                             "tag": "v1.12.0", "url": "",
                                             "available": True, "error": ""}):
            result = api.tg_check_core_update()
        self.assertTrue(result["ok"])
        self.assertIn("1.12.0", result["message"])
        self.assertEqual(api.cfg["tg_core_latest"], "1.12.0")

    def test_check_core_update_reports_failure(self):
        api = _api()
        with mock.patch.object(tgws_update, "check",
                               return_value={"local": "1.11.2", "latest": "", "tag": "",
                                             "url": "", "available": False,
                                             "error": "URLError: нет сети"}):
            result = api.tg_check_core_update()
        self.assertTrue(result["ok"])
        self.assertIn("нет сети", result["message"])


if __name__ == "__main__":
    unittest.main()
