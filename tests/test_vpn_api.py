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

    def start(self, config, settle=None):
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
    api._vpn_probe = _FakeSingBox(True)     # лёгкий пробник (без TUN)
    # Параллельные пробы иначе создавали бы РЕАЛЬНЫЕ sing-box — в тестах все
    # экземпляры заворачиваем на общий фейк.
    api._vpn_probe_engine = lambda idx: api._vpn_probe
    api._vpn_cancel = threading.Event()
    api._vpn_connecting = False
    api._vpn_servers = []
    api._events = []
    api._events_lock = threading.Lock()
    return api


def _connect(api):
    """Синхронный прогон фонового подбора (в бою он крутится в отдельном потоке)."""
    api._vpn_cancel.clear()
    api._vpn_connect_worker()
    return api.vpn_get_state()


class TestVpnApi(unittest.TestCase):
    def setUp(self):
        # Не трогаем реальный config.json на диске.
        self._orig_save = fcapp.config.save
        fcapp.config.save = lambda c: None
        # Проверку живости серверов подменяем: без сети, все «живы» с равным пингом —
        # тогда порядок определяется приоритетом протокола/выбором, как и раньше.
        self._orig_probe = vpn.probe_server
        vpn.probe_server = lambda s, timeout=2.0: (True, 10.0)
        # Фейковый sing-box не поднимает реальный SOCKS, поэтому проверку туннеля
        # подменяем на «работает» (её собственное поведение тестируется отдельно).
        self._orig_tunnel = vpn.tunnel_works
        vpn.tunnel_works = lambda port, **kw: True

    def tearDown(self):
        fcapp.config.save = self._orig_save
        vpn.probe_server = self._orig_probe
        vpn.tunnel_works = self._orig_tunnel

    def test_import_json_populates_country_rows(self):
        api = _api()
        st = api.vpn_import(json_text=_profiles())
        self.assertTrue(st["ok"])
        self.assertTrue(st["imported"])
        # По строке на СТРАНУ, id = слаг страны; конкретный сервер — при подключении.
        rows = {r["id"]: r for r in st["servers"]}
        self.assertIn("germany", rows)
        self.assertIn("finland", rows)
        self.assertEqual(rows["germany"]["name"], "Германия")
        self.assertTrue(rows["germany"]["sub"].startswith("Hysteria2"))
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
        # 'auto' сбрасывает страну в пустую (любая, живая по приоритету).
        st = api.vpn_select("auto")
        self.assertEqual(api.cfg["vpn_country"], "")
        self.assertEqual(st["selected"], "auto")

    def test_enable_is_async_and_reports_connecting(self):
        """Включение НЕ должно блокировать вызов из UI (иначе окно висит и тумблер
        «не выключается»): сразу отвечаем «подключаюсь», итог придёт событием."""
        api = _api()
        api.vpn_import(json_text=_profiles())
        started = []
        orig = threading.Thread
        try:
            threading.Thread = lambda *a, **kw: type(
                "T", (), {"start": lambda _s: started.append(kw.get("name")),
                          "daemon": True})()
            st = api.vpn_set_enabled(True)
        finally:
            threading.Thread = orig
        self.assertTrue(st["ok"])
        self.assertTrue(st["connecting"])
        self.assertEqual(api.singbox.start_calls, 0)   # TUN ещё не трогали
        self.assertIn("vpn-connect", started)

    def test_connect_starts_singbox_with_discord_route(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        _connect(api)
        self.assertEqual(api.singbox.start_calls, 1)
        self.assertTrue(api.cfg["vpn_enabled"])
        rule = next(r for r in api.singbox.started_cfg["route"]["rules"]
                    if "process_name" in r)
        self.assertIn("Discord.exe", rule["process_name"])
        self.assertEqual(rule["outbound"], "vpn")

    def test_tun_config_has_no_probe_socks(self):
        """Боевой TUN-конфиг чистый: проверочный SOCKS живёт в отдельном пробнике."""
        api = _api()
        api.vpn_import(json_text=_profiles())
        _connect(api)
        self.assertTrue(all(i["type"] != "socks"
                            for i in api.singbox.started_cfg["inbounds"]))

    def test_probe_runs_without_tun(self):
        """Кандидатов проверяем БЕЗ TUN — иначе адаптер не успевает освободиться
        и рвётся системная сеть."""
        api = _api()
        api.vpn_import(json_text=_profiles())
        _connect(api)
        self.assertGreaterEqual(api._vpn_probe.start_calls, 1)
        self.assertTrue(all(i["type"] != "tun"
                            for i in api._vpn_probe.started_cfg["inbounds"]))
        socks = [i for i in api._vpn_probe.started_cfg["inbounds"] if i["type"] == "socks"]
        self.assertEqual(socks[0]["listen"], "127.0.0.1")

    def test_connect_auto_prefers_hysteria2(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        _connect(api)
        ob = next(o for o in api.singbox.started_cfg["outbounds"] if o["tag"] == "vpn")
        self.assertEqual(ob["type"], "hysteria2")

    def test_connect_selected_country_overrides_priority(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        api.vpn_select("finland")
        _connect(api)
        ob = next(o for o in api.singbox.started_cfg["outbounds"] if o["tag"] == "vpn")
        self.assertEqual(ob["type"], "vless")

    def test_connect_liveness_overrides_protocol_priority(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        vpn.probe_server = lambda s, timeout=2.0: (s.kind == "vless-reality", 10.0)
        _connect(api)
        ob = next(o for o in api.singbox.started_cfg["outbounds"] if o["tag"] == "vpn")
        self.assertEqual(ob["type"], "vless")

    def test_dead_tunnel_falls_through_to_next_server(self):
        """Если через сервер не идёт трафик — берём другой, TUN поднимаем только
        для проверенного (кандидаты проверяются параллельно, без TUN)."""
        seen = []
        api = _api()
        api.vpn_import(json_text=_profiles())
        # Мёртв Hysteria2-выход; рабочим оказывается VLESS.
        def fake_probe(cand, idx=0):
            seen.append(cand.uid())
            return cand.kind == "vless-reality"
        api._vpn_probe_server = fake_probe
        _connect(api)
        self.assertGreaterEqual(len(seen), 2)             # проверили несколько
        self.assertEqual(api.singbox.start_calls, 1)      # TUN поднят один раз
        self.assertTrue(api.singbox.is_running())
        ob = next(o for o in api.singbox.started_cfg["outbounds"] if o["tag"] == "vpn")
        self.assertEqual(ob["type"], "vless")             # выбран тот, что реально работал

    def test_all_tunnels_dead_leaves_no_tun(self):
        """Ни один сервер не пропустил трафик -> TUN не поднят вовсе."""
        api = _api()
        api.vpn_import(json_text=_profiles())
        vpn.tunnel_works = lambda port, **kw: False
        _connect(api)
        self.assertEqual(api.singbox.start_calls, 0)
        self.assertFalse(api.singbox.is_running())
        self.assertFalse(api.cfg.get("vpn_enabled"))

    def test_cancel_during_probe_stops_connect(self):
        """Выключили тумблер, пока шёл подбор -> подключение прерывается."""
        api = _api()
        api.vpn_import(json_text=_profiles())
        vpn.tunnel_works = lambda port, **kw: (api._vpn_cancel.set(), False)[1]
        _connect(api)
        self.assertEqual(api.singbox.start_calls, 0)     # туннель не поднимали

    def test_enable_without_binary_errors_gracefully(self):
        api = _api(has_binary=False)
        api.vpn_import(json_text=_profiles())
        _connect(api)                       # TUN поднять нечем -> тихо и без падения
        self.assertFalse(api.singbox.is_running())
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

    def test_import_url_picks_ua_with_most_servers(self):
        """sub-сервис режет чужой UA (445) и полный конфиг отдаёт только «своему»
        (напр. Streisand). Импорт по ссылке перебирает UA и берёт лучший ответ."""
        import io
        import urllib.request
        good = _profiles()   # JSON с 2 странами
        calls = []

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): self.close()

        def fake_urlopen(req, timeout=0):
            ua = req.headers.get("User-agent") or req.get_header("User-agent")
            calls.append(ua)
            if ua == "Streisand":
                return _Resp(good.encode("utf-8"))
            if ua == "v2rayNG/1.9.5":
                return _Resp(b"garbage-not-a-config")   # 200, но не парсится
            raise urllib.error.HTTPError(req.full_url, 445, "blocked", {}, None)

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            api = _api()
            st = api.vpn_import(url="https://sub.example/sub/x")
        finally:
            urllib.request.urlopen = orig
        self.assertTrue(st["ok"])
        self.assertTrue(st["imported"])
        self.assertIn("Streisand", calls)              # рабочий UA перепробован
        self.assertGreaterEqual(len(st["servers"]), 2)

    def test_import_url_all_uas_fail(self):
        import urllib.request
        def fake_urlopen(req, timeout=0):
            raise urllib.error.URLError("no network")
        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            api = _api()
            st = api.vpn_import(url="https://sub.example/sub/x")
        finally:
            urllib.request.urlopen = orig
        self.assertFalse(st["ok"])
        self.assertIn("error", st)

    def test_select_while_running_reconnects(self):
        api = _api()
        api.vpn_import(json_text=_profiles())
        _connect(api)                       # авто (Германия/Hysteria2)
        self.assertEqual(api.singbox.start_calls, 1)
        orig = threading.Thread             # реальный поток в тесте не нужен
        try:
            threading.Thread = lambda *a, **kw: type("T", (), {"start": lambda _s: None})()
            st = api.vpn_select("finland")  # активен -> инициируем переподключение
        finally:
            threading.Thread = orig
        self.assertTrue(st.get("connecting"))
        api._vpn_connecting = False
        _connect(api)                       # прогоняем подбор синхронно
        self.assertEqual(api.singbox.start_calls, 2)
        ob = next(o for o in api.singbox.started_cfg["outbounds"] if o["tag"] == "vpn")
        self.assertEqual(ob["type"], "vless")


if __name__ == "__main__":
    unittest.main()
