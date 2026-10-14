"""Api-методы обхода Telegram: вкл/выкл прокси, персист в конфиг, автонастройка
через tg://socks. Реальный прокси не поднимаем — подменяем движок фейком."""
import threading
import unittest

from freeconnect import app as fcapp
from freeconnect import tgproxy


class _FakeTg:
    """Заглушка движка Telegram-прокси: помнит вызовы, без реального сокета."""
    def __init__(self, avail=True):
        self._avail = avail
        self._running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.started_port = None

    def available(self):
        return self._avail

    def is_running(self):
        return self._running

    def start(self, port=tgproxy.DEFAULT_PORT, lan=False):
        if not self._avail:
            raise fcapp.TgProxyError("нет зависимостей")
        self.started_port = port
        self.start_calls += 1
        self._running = True

    def stop(self):
        self.stop_calls += 1
        self._running = False

    def set_endpoints(self, endpoints):
        self.endpoints = endpoints


def _api(avail=True, cfg=None):
    api = fcapp.Api.__new__(fcapp.Api)
    api.cfg = dict(cfg or {})
    api.tgproxy = _FakeTg(avail)
    api._events = []
    api._events_lock = threading.Lock()
    return api


class TestTgApi(unittest.TestCase):
    def setUp(self):
        self._orig_save = fcapp.config.save
        fcapp.config.save = lambda c: None

    def tearDown(self):
        fcapp.config.save = self._orig_save

    def _diagnose_with(self, rows):
        """Подменяет сетевую диагностику готовыми строками и возвращает вердикт."""
        orig = fcapp.tgproxy.diagnose
        fcapp.tgproxy.diagnose = lambda **_kw: {
            "dc": 2, "host": "kws2.web.telegram.org", "dns": ["149.154.167.99"],
            "rows": rows, "ok": any(r.get("ok") for r in rows)}
        try:
            return _api().tg_diagnose()
        finally:
            fcapp.tgproxy.diagnose = orig

    def test_diagnose_reports_working_endpoint(self):
        st = self._diagnose_with([
            {"ip": "149.154.167.220", "tcp": "ok 45мс", "tls": "ok", "ws": "ok (101)", "ok": True},
            {"ip": "149.154.167.99", "tcp": "нет ответа (блокировка)", "ok": False},
        ])
        self.assertTrue(st["ok"])
        self.assertIn("работает", st["verdict"])
        self.assertIn("149.154.167.220", st["verdict"])

    def test_diagnose_reports_total_ip_block(self):
        # Все адреса молчат на TCP — это ровно картина блокировки по IP.
        st = self._diagnose_with([
            {"ip": "149.154.167.220", "tcp": "нет ответа (блокировка)", "ok": False},
            {"ip": "149.154.167.99", "tcp": "нет ответа (блокировка)", "ok": False},
        ])
        self.assertFalse(st["ok"])
        self.assertIn("блокирует", st["verdict"])
        self.assertTrue(st["hint"])          # подсказка, что делать дальше

    def test_diagnose_reports_partial_failure(self):
        # Доходим до сервера, но канал не встаёт — не спутать с блокировкой.
        st = self._diagnose_with([
            {"ip": "149.154.167.220", "tcp": "ok 45мс", "tls": "ok",
             "ws": "сбой (TimeoutError)", "ok": False},
        ])
        self.assertFalse(st["ok"])
        self.assertIn("доходим", st["verdict"].lower())

    def test_diagnose_survives_engine_error(self):
        orig = fcapp.tgproxy.diagnose

        def boom(**_kw):
            raise OSError("сеть недоступна")

        fcapp.tgproxy.diagnose = boom
        try:
            st = _api().tg_diagnose()
        finally:
            fcapp.tgproxy.diagnose = orig
        self.assertFalse(st["ok"])
        self.assertEqual(st["rows"], [])

    def _run_discover(self, found):
        """Прогоняет фонового воркера поиска синхронно, подменив сам перебор."""
        saved = {}
        orig_disc, orig_save = fcapp.tgproxy.discover, fcapp.config.save
        fcapp.tgproxy.discover = lambda **_kw: list(found)
        fcapp.config.save = lambda c: saved.update(c)
        api = _api()
        api._tg_discovering = True
        try:
            api._tg_discover_worker()
        finally:
            fcapp.tgproxy.discover, fcapp.config.save = orig_disc, orig_save
        events = [e["fn"] for e in api._events]
        done = [e["args"][0] for e in api._events if e["fn"] == "onTgDiscoverDone"]
        return api, saved, events, done[0] if done else None

    def test_discover_saves_found_endpoint_for_dc2_and_dc4(self):
        api, saved, _events, done = self._run_discover(["149.154.167.221"])
        # Один узел обслуживает ДЦ2 и ДЦ4 — адрес прописываем обоим.
        self.assertEqual(api.cfg["tg_endpoints"]["2"], ["149.154.167.221"])
        self.assertEqual(api.cfg["tg_endpoints"]["4"], ["149.154.167.221"])
        self.assertEqual(saved.get("tg_endpoints"), api.cfg["tg_endpoints"])
        self.assertEqual(api.tgproxy.endpoints, api.cfg["tg_endpoints"])
        self.assertTrue(done["ok"])
        self.assertFalse(api._tg_discovering)      # флаг снят -> можно искать снова

    def test_discover_reports_failure_without_touching_config(self):
        api, saved, _events, done = self._run_discover([])
        self.assertNotIn("tg_endpoints", api.cfg)   # ничего не портим
        self.assertEqual(saved, {})
        self.assertFalse(done["ok"])
        self.assertFalse(api._tg_discovering)

    def test_discover_refuses_second_run_while_busy(self):
        api = _api()
        api._tg_discovering = True
        st = api.tg_discover()
        self.assertFalse(st["ok"])
        self.assertIn("идёт", st["error"])

    def test_state_defaults(self):
        st = _api().tg_get_state()
        self.assertTrue(st["available"])
        self.assertFalse(st["enabled"])
        self.assertEqual(st["port"], tgproxy.DEFAULT_PORT)
        self.assertIn("127.0.0.1", st["deeplink"])
        self.assertIn(f"port={tgproxy.DEFAULT_PORT}", st["deeplink"])

    def test_enable_starts_and_persists(self):
        api = _api()
        st = api.tg_set_enabled(True)
        self.assertTrue(st["ok"])
        self.assertTrue(st["enabled"])
        self.assertEqual(api.tgproxy.start_calls, 1)
        self.assertEqual(api.tgproxy.started_port, tgproxy.DEFAULT_PORT)
        self.assertTrue(api.cfg["tg_enabled"])

    def test_enable_uses_custom_port(self):
        api = _api(cfg={"tg_port": 1090})
        st = api.tg_set_enabled(True)
        self.assertEqual(api.tgproxy.started_port, 1090)
        self.assertIn("port=1090", st["deeplink"])

    def test_disable_stops_and_persists(self):
        api = _api()
        api.tg_set_enabled(True)
        st = api.tg_set_enabled(False)
        self.assertTrue(st["ok"])
        self.assertFalse(st["enabled"])
        self.assertEqual(api.tgproxy.stop_calls, 1)
        self.assertFalse(api.cfg["tg_enabled"])

    def test_enable_without_deps_errors(self):
        api = _api(avail=False)
        st = api.tg_set_enabled(True)
        self.assertFalse(st["ok"])
        self.assertIn("error", st)
        self.assertFalse(api.cfg.get("tg_enabled"))

    def test_autoconfigure_opens_deeplink_and_starts(self):
        api = _api()
        opened = []
        orig = fcapp.os.startfile
        fcapp.os.startfile = lambda link: opened.append(link)
        try:
            st = api.tg_autoconfigure()
        finally:
            fcapp.os.startfile = orig
        self.assertTrue(st["ok"])
        self.assertTrue(api.tgproxy.is_running())        # автонастройка сама поднимает прокси
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("tg://socks"))

    def test_autoconfigure_reports_manual_fallback_on_failure(self):
        api = _api()
        orig = fcapp.os.startfile

        def boom(_link):
            raise OSError("no handler")

        fcapp.os.startfile = boom
        try:
            st = api.tg_autoconfigure()
        finally:
            fcapp.os.startfile = orig
        self.assertFalse(st["ok"])
        self.assertIn("SOCKS5", st["error"])             # подсказка на ручную настройку


if __name__ == "__main__":
    unittest.main()
