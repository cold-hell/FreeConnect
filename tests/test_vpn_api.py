"""Api-методы VPN-для-Discord (Фаза 4): импорт подписки, выбор страны, вкл/выкл.
Реальный sing-box не запускаем — подменяем движок фейком; проверяем контракт
с UI (список стран, persist в конфиг, обработка ошибок)."""
import json
import threading
import unittest

from freeconnect import app as fcapp
from freeconnect import vpn


def _profiles():
    """Мини-подписка: Финляндия (VLESS-Reality) + Германия (Hysteria2)."""
    return json.dumps([
        {"remarks": "🇫🇮 Финляндия", "outbounds": [
            {"tag": "93_finland_vless-grpc", "protocol": "vless",
             "settings": {"vnext": [{"address": "2.26.97.167", "port": 2087,
                                     "users": [{"id": "uuid-fi", "encryption": "none"}]}]},
             "streamSettings": {"network": "grpc", "security": "reality",
                                "grpcSettings": {"serviceName": "grpc"},
                                "realitySettings": {"publicKey": "PUBFI", "shortId": "s",
                                                    "serverName": "ads.x5.ru"}}}]},
        {"remarks": "Hysteria2 | 🇩🇪 Германия", "outbounds": [
            {"tag": "hy2in-88", "protocol": "hysteria",
             "settings": {"version": 2, "address": "84.38.186.105", "port": 8447},
             "streamSettings": {"network": "hysteria", "security": "tls",
                                "tlsSettings": {"serverName": "www.microsoft.com", "alpn": ["h3"]},
                                "hysteriaSettings": {"version": 2, "auth": "u:h:streisand"}}}]},
    ])


class _FakeSingBox:
    """Заглушка движка: помнит, что запускали/останавливали, без реального процесса."""
    def __init__(self, has_binary=True):
        self._has = has_binary
        self._running = False
        self.started_cfg = None
        self.start_calls = 0
        self.stop_calls = 0

    def available(self):
        return self._has

    def is_running(self):
        return self._running

    def start(self, config):
        if not self._has:
            raise fcapp.SingBoxError("sing-box не установлен (обнови приложение)")
        self.started_cfg = config
        self.start_calls += 1
        self._running = True

    def stop(self):
        self.stop_calls += 1
        self._running = False


def _api(has_binary=True, cfg=None):
    api = fcapp.Api.__new__(fcapp.Api)
    api.cfg = dict(cfg or {})
    api.singbox = _FakeSingBox(has_binary)
    api._vpn_servers = []
    api._events = []
    api._events_lock = threading.Lock()
    return api


class TestVpnApi(unittest.TestCase):
    def setUp(self):
        # Не трогаем реальный config.json на диске.
        self._orig_save = fcapp.config.save
        fcapp.config.save = lambda c: None

    def tearDown(self):
        fcapp.config.save = self._orig_save

    def test_import_json_populates_country_rows(self):
        api = _api()
        st = api.vpn_import(json_text=_profiles())
        self.assertTrue(st["ok"])
        self.assertTrue(st["imported"])
        names = {r["name"]: r["sub"] for r in st["servers"]}
        self.assertIn("Германия", names)
        self.assertIn("Финляндия", names)
        self.assertEqual(names["Германия"], "Hysteria2")
        # Подписка кэшируется для восстановления без повторного импорта.
        self.assertEqual(api.cfg["vpn_config"], _profiles())

    def test_import_garbage_reports_error(self):
        api = _api()
        st = api.vpn_import(json_text="!!! not a config !!!")
        self.assertFalse(st["ok"])
        self.assertIn("error", st)
        self.assertFalse(st["imported"])

    def test_import_empty_input(self):
        api = _api()
        st = api.vpn_import(url="", json_text="")
        self.assertFalse(st["ok"])

    def test_select_persists_country(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        st = api.vpn_select("finland")
        self.assertEqual(api.cfg["vpn_country"], "finland")
        self.assertEqual(st["selected"], "finland")
        # 'auto' сбрасывает страну в пустую (лучший по приоритету).
        st = api.vpn_select("auto")
        self.assertEqual(api.cfg["vpn_country"], "")
        self.assertEqual(st["selected"], "auto")

    def test_enable_starts_singbox_with_discord_route(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        st = api.vpn_set_enabled(True)
        self.assertTrue(st["ok"])
        self.assertTrue(st["enabled"])
        self.assertEqual(api.singbox.start_calls, 1)
        self.assertTrue(api.cfg["vpn_enabled"])
        # Конфиг реально маршрутизирует именно Discord в vpn.
        rule = api.singbox.started_cfg["route"]["rules"][0]
        self.assertIn("Discord.exe", rule["process_name"])
        self.assertEqual(rule["outbound"], "vpn")

    def test_enable_auto_prefers_hysteria2(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        api.vpn_set_enabled(True)   # без выбора страны -> авто
        ob = next(o for o in api.singbox.started_cfg["outbounds"] if o["tag"] == "vpn")
        self.assertEqual(ob["type"], "hysteria2")   # Германия Hysteria2 в приоритете

    def test_enable_selected_country_overrides_priority(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        api.vpn_select("finland")
        api.vpn_set_enabled(True)
        ob = next(o for o in api.singbox.started_cfg["outbounds"] if o["tag"] == "vpn")
        self.assertEqual(ob["type"], "vless")

    def test_enable_without_binary_errors_gracefully(self):
        api = _api(has_binary=False)
        api.vpn_import(json_text=_profiles())
        st = api.vpn_set_enabled(True)
        self.assertFalse(st["ok"])
        self.assertIn("error", st)
        self.assertFalse(api.cfg.get("vpn_enabled"))

    def test_enable_without_import_errors(self):
        api = _api()
        st = api.vpn_set_enabled(True)
        self.assertFalse(st["ok"])
        self.assertEqual(api.singbox.start_calls, 0)

    def test_disable_stops_and_persists(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        api.vpn_set_enabled(True)
        st = api.vpn_set_enabled(False)
        self.assertTrue(st["ok"])
        self.assertFalse(st["enabled"])
        self.assertEqual(api.singbox.stop_calls, 1)
        self.assertFalse(api.cfg["vpn_enabled"])

    def test_select_while_running_restarts(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        api.vpn_set_enabled(True)      # авто (Германия/Hysteria2)
        self.assertEqual(api.singbox.start_calls, 1)
        api.vpn_select("finland")      # активен -> перезапуск на новый сервер
        self.assertEqual(api.singbox.start_calls, 2)
        ob = next(o for o in api.singbox.started_cfg["outbounds"] if o["tag"] == "vpn")
        self.assertEqual(ob["type"], "vless")


if __name__ == "__main__":
    unittest.main()
