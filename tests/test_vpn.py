"""Парсер подписки VPN-для-Discord: извлечение зарубежных серверов, приоритет
Hysteria2, пропуск цепочек (dialerProxy) и RU-relay. Фикстура структурно повторяет
реальный xray-конфиг Happ."""
import base64
import json
import socket
import unittest
from pathlib import Path

from freeconnect import vpn


def _finland_vless_profile():
    return {"remarks": "🇫🇮 Финляндия", "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
        {"tag": "93_finland_vless-grpc", "protocol": "vless",
         "settings": {"vnext": [{"address": "2.26.97.167", "port": 2087,
                                 "users": [{"id": "uuid-fi", "encryption": "none"}]}]},
         "streamSettings": {"network": "grpc", "security": "reality",
                            "grpcSettings": {"serviceName": "grpc"},
                            "realitySettings": {"publicKey": "PUBFI", "shortId": "sid",
                                                "serverName": "ads.x5.ru", "fingerprint": "firefox"}}},
        {"tag": "lo-out-1", "protocol": "loopback", "settings": {"inboundTag": "lo-in-1"}},
    ]}


def _germany_hy2_profile():
    """Прямой зарубежный выход (без цепочки). Цепочки проверяются в TestChains."""
    return {"remarks": "Hysteria2 | 🇩🇪 Германия", "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "hy2-88_germany", "protocol": "hysteria",
         "settings": {"version": 2, "address": "84.38.186.105", "port": 8447},
         "streamSettings": {"network": "hysteria", "security": "tls",
                            "tlsSettings": {"serverName": "www.microsoft.com",
                                            "pinnedPeerCertSha256": "SHA", "alpn": ["h3"]},
                            "hysteriaSettings": {"version": 2, "auth": "user:hash:streisand"}}},
    ]}


def _russia_relay_profile():
    return {"remarks": "🇷🇺 Россия", "outbounds": [
        {"tag": "181_russia_vless-tcp", "protocol": "vless",
         "settings": {"vnext": [{"address": "31.184.218.96", "port": 445,
                                 "users": [{"id": "ru", "flow": "xtls-rprx-vision"}]}]},
         "streamSettings": {"network": "tcp", "security": "reality",
                            "realitySettings": {"publicKey": "fvDOw6D_luD3gayjRMTnhxmMaziQme_DBg-bvR8Hk3k", "shortId": "c226283c4de5b1ad", "serverName": "ads.x5.ru"}}},
    ]}


class TestParseServers(unittest.TestCase):
    def setUp(self):
        self.cfg = [_finland_vless_profile(), _germany_hy2_profile(), _russia_relay_profile()]

    def test_extracts_foreign_and_prioritizes_hysteria2(self):
        servers = vpn.parse_servers(json.dumps(self.cfg))
        kinds = [(s.kind, s.country) for s in servers]
        # Германия Hysteria2 первой (приоритет), затем Финляндия VLESS
        self.assertEqual(kinds[0], ("hysteria2", "germany"))
        self.assertIn(("vless-reality", "finland"), kinds)
        # Самостоятельный RU-relay пропущен (нам нужен зарубежный выход)
        self.assertFalse(any(c == "russia" for _, c in kinds))
        self.assertFalse(any(s.address == "31.184.218.96" for s in servers))

    def test_hysteria2_fields(self):
        srv = vpn.best_server(vpn.parse_servers(json.dumps(self.cfg)))
        self.assertEqual(srv.kind, "hysteria2")
        self.assertEqual(srv.address, "84.38.186.105")
        self.assertEqual(srv.port, 8447)
        self.assertEqual(srv.params["auth"], "user:hash:streisand")
        self.assertEqual(srv.params["sni"], "www.microsoft.com")
        self.assertIn("🇩🇪", srv.name)

    def test_vless_reality_fields(self):
        servers = vpn.parse_servers(json.dumps(self.cfg))
        fi = next(s for s in servers if s.country == "finland")
        self.assertEqual(fi.address, "2.26.97.167")
        self.assertEqual(fi.params["id"], "uuid-fi")
        self.assertEqual(fi.params["network"], "grpc")
        self.assertEqual(fi.params["reality"]["publicKey"], "PUBFI")
        self.assertEqual(fi.params["grpcServiceName"], "grpc")

    def test_base64_subscription(self):
        raw = json.dumps(self.cfg).encode()
        b64 = base64.b64encode(raw).decode()
        servers = vpn.parse_servers(b64)
        self.assertTrue(any(s.kind == "hysteria2" for s in servers))

    def test_dedup_by_address_port(self):
        servers = vpn.parse_servers(json.dumps(self.cfg + self.cfg))  # дубли профилей
        keys = [s.key() for s in servers]
        self.assertEqual(len(keys), len(set(keys)))

    def test_best_server_country_filter(self):
        servers = vpn.parse_servers(json.dumps(self.cfg))
        self.assertEqual(vpn.best_server(servers, country="finland").country, "finland")
        self.assertIsNone(vpn.best_server(servers, country="spain"))

    def test_empty_and_garbage(self):
        with self.assertRaises(ValueError):
            vpn.decode_subscription("")
        with self.assertRaises(ValueError):
            vpn.decode_subscription("!!!not json!!!")


def _unknown_country_profile():
    """Сервер без опознанной страны (напр. США тегом us-node) — раньше молча
    выбрасывался, теперь берём под своим именем."""
    return {"remarks": "⚡ Fast node", "outbounds": [
        {"tag": "us-node-1", "protocol": "vless",
         "settings": {"vnext": [{"address": "5.5.5.5", "port": 443,
                                 "users": [{"id": "u", "flow": "xtls-rprx-vision"}]}]},
         "streamSettings": {"network": "tcp", "security": "reality",
                            "realitySettings": {"publicKey": "PK", "shortId": "s", "serverName": "ok.ru"}}}]}


class TestKeepMoreServers(unittest.TestCase):
    def test_unknown_country_is_kept(self):
        servers = vpn.parse_servers(json.dumps([_unknown_country_profile()]))
        self.assertEqual(len(servers), 1)
        s = servers[0]
        self.assertEqual(s.address, "5.5.5.5")
        self.assertEqual(s.country, "")
        self.assertTrue(s.name)                 # имя есть (из remarks)

    def test_domestic_ru_dropped_even_without_flag(self):
        prof = {"remarks": "Быстрый", "outbounds": [
            {"tag": "12_russia_vless-tcp", "protocol": "vless",
             "settings": {"vnext": [{"address": "77.7.7.7", "port": 443,
                                     "users": [{"id": "u"}]}]},
             "streamSettings": {"network": "tcp", "security": "reality",
                                "realitySettings": {"publicKey": "P", "shortId": "s", "serverName": "x"}}}]}
        self.assertEqual(vpn.parse_servers(json.dumps([prof])), [])

    def test_distinct_servers_same_country_not_collapsed(self):
        # Две «Нидерланды» с разными адресами -> две РАЗНЫЕ строки (разные uid).
        def nl(addr, name):
            return {"remarks": name, "outbounds": [
                {"tag": "netherlands", "protocol": "hysteria",
                 "settings": {"version": 2, "address": addr, "port": 8447},
                 "streamSettings": {"network": "hysteria", "security": "tls",
                                    "tlsSettings": {"serverName": "m.com", "alpn": ["h3"]},
                                    "hysteriaSettings": {"version": 2, "auth": "a"}}}]}
        servers = vpn.parse_servers(json.dumps([nl("1.1.1.1", "🇳🇱 Нидерланды"),
                                                nl("2.2.2.2", "🇳🇱 Нидерланды +")]))
        self.assertEqual(len(servers), 2)
        self.assertEqual(len({s.uid() for s in servers}), 2)

    def test_uid_is_stable_and_unique(self):
        s = vpn.Server(kind="hysteria2", address="9.9.9.9", port=8447)
        self.assertEqual(s.uid(), "hysteria2|9.9.9.9|8447")


def _chained_profile():
    """Реальная форма этой подписки: зарубежный выход доступен ТОЛЬКО через
    российский relay-вход (xray dialerProxy). Прямой заход душится."""
    return {"remarks": "🇳🇱 Нидерланды+", "outbounds": [
        {"tag": "wfp-in-188", "protocol": "vless",
         "settings": {"vnext": [{"address": "94.26.228.201", "port": 2087,
                                 "users": [{"id": "ru-in", "encryption": "none"}]}]},
         "streamSettings": {"network": "grpc", "security": "reality",
                            "grpcSettings": {"serviceName": "grpc"},
                            "realitySettings": {"publicKey": "1gGoLVXJDFC6viMcQR3scaNhlPd4b5SZQUfyrrnY8io", "shortId": "c226283c4de5b1ad",
                                                "serverName": "ads.x5.ru"}}},
        {"tag": "188_netherlands_vless-grpc", "protocol": "vless",
         "settings": {"vnext": [{"address": "89.124.89.129", "port": 2087,
                                 "users": [{"id": "nl-out", "encryption": "none"}]}]},
         "streamSettings": {"network": "grpc", "security": "reality",
                            "grpcSettings": {"serviceName": "grpc"},
                            "realitySettings": {"publicKey": "s-5z3Mbrz0YFAwUrZ4HxFPXiEpYL2FGxWqdIzdsU934", "shortId": "c226283c4de5b1ad",
                                                "serverName": "ads.x5.ru"},
                            "sockopt": {"dialerProxy": "wfp-in-188"}}},
        {"tag": "188_russia_vless-tcp-relay:1", "protocol": "vless",
         "settings": {"vnext": [{"address": "94.26.228.201", "port": 444,
                                 "users": [{"id": "ru", "flow": "xtls-rprx-vision"}]}]},
         "streamSettings": {"network": "tcp", "security": "reality",
                            "realitySettings": {"publicKey": "fvDOw6D_luD3gayjRMTnhxmMaziQme_DBg-bvR8Hk3k", "shortId": "c226283c4de5b1ad",
                                                "serverName": "ads.x5.ru"}}},
    ]}


class TestChains(unittest.TestCase):
    def setUp(self):
        self.servers = vpn.parse_servers(json.dumps([_chained_profile()]))

    def test_chained_exit_is_kept_with_via(self):
        # Раньше цепочки выбрасывались — терялся ЕДИНСТВЕННЫЙ рабочий маршрут.
        nl = [s for s in self.servers if s.country == "netherlands"]
        self.assertEqual(len(nl), 1)
        s = nl[0]
        self.assertEqual(s.address, "89.124.89.129")
        self.assertIsNotNone(s.via)
        self.assertEqual(s.via.address, "94.26.228.201")   # вход — российский relay
        self.assertEqual(s.via.port, 2087)

    def test_transit_entry_not_offered_as_exit(self):
        # wfp-in-188 — транзитный вход, самостоятельным выходом его показывать нельзя.
        self.assertFalse(any(s.address == "94.26.228.201" and s.port == 2087 and not s.via
                             for s in self.servers))

    def test_standalone_ru_relay_still_dropped(self):
        self.assertFalse(any(s.port == 444 for s in self.servers))

    def test_entry_is_probe_target(self):
        s = [x for x in self.servers if x.country == "netherlands"][0]
        self.assertEqual(s.entry().address, "94.26.228.201")

    def test_probe_hits_entry_not_exit(self):
        s = [x for x in self.servers if x.country == "netherlands"][0]
        seen = []
        orig = vpn._probe_tcp
        vpn._probe_tcp = lambda h, p, t: (seen.append((h, p)) or (True, 5.0))
        try:
            vpn.probe_server(s)
        finally:
            vpn._probe_tcp = orig
        self.assertEqual(seen, [("94.26.228.201", 2087)])   # бьём во вход, не в выход

    def test_same_exit_via_different_entries_not_deduped(self):
        p1 = _chained_profile()
        p2 = json.loads(json.dumps(p1))
        p2["outbounds"][0]["tag"] = "wfp-in-173"
        p2["outbounds"][0]["settings"]["vnext"][0]["address"] = "138.16.174.112"
        p2["outbounds"][1]["tag"] = "173_netherlands_vless-grpc"
        p2["outbounds"][1]["streamSettings"]["sockopt"]["dialerProxy"] = "wfp-in-173"
        servers = vpn.parse_servers(json.dumps([p1, p2]))
        nl = [s for s in servers if s.country == "netherlands"]
        self.assertEqual(len(nl), 2)                       # один выход, два входа = 2 маршрута
        self.assertEqual(len({s.uid() for s in nl}), 2)

    def test_singbox_config_uses_detour(self):
        s = [x for x in self.servers if x.country == "netherlands"][0]
        cfg = vpn.build_singbox_config(s)
        tags = {o["tag"]: o for o in cfg["outbounds"]}
        self.assertIn("vpn-in", tags)
        self.assertEqual(tags["vpn"]["detour"], "vpn-in")          # выход идёт через вход
        self.assertEqual(tags["vpn"]["server"], "89.124.89.129")
        self.assertEqual(tags["vpn-in"]["server"], "94.26.228.201")

    def test_direct_server_has_no_detour(self):
        s = vpn.best_server(vpn.parse_servers(json.dumps([_germany_hy2_profile()])))
        cfg = vpn.build_singbox_config(s)
        tags = {o["tag"] for o in cfg["outbounds"]}
        self.assertNotIn("vpn-in", tags)


class TestTunnelProbeConfig(unittest.TestCase):
    def test_probe_port_adds_socks_inbound_and_rule(self):
        s = vpn.best_server(vpn.parse_servers(json.dumps([_germany_hy2_profile()])))
        cfg = vpn.build_singbox_config(s, probe_port=1234)
        socks = [i for i in cfg["inbounds"] if i["type"] == "socks"]
        self.assertEqual(len(socks), 1)
        self.assertEqual(socks[0]["listen_port"], 1234)
        self.assertEqual(socks[0]["listen"], "127.0.0.1")          # только localhost
        self.assertEqual(cfg["route"]["rules"][0],
                         {"inbound": ["probe-in"], "outbound": "vpn"})

    def test_no_probe_port_keeps_config_clean(self):
        s = vpn.best_server(vpn.parse_servers(json.dumps([_germany_hy2_profile()])))
        cfg = vpn.build_singbox_config(s)
        self.assertTrue(all(i["type"] != "socks" for i in cfg["inbounds"]))


class TestTunnelWorks(unittest.TestCase):
    """END-TO-END проверка самой проверки: настоящий локальный SOCKS5 + TLS.

    Нужен именно такой тест: забытый `import ssl` внутри tunnel_works глотался
    широким `except` и функция молча браковала ВСЕ серверы — на моках это не
    ловилось, а в поле выглядело как «ни один сервер не работает»."""

    def setUp(self):
        import ssl as _ssl
        import tempfile
        import threading as th
        from datetime import datetime, timedelta, timezone

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .sign(key, hashes.SHA256()))
        self._tmp = tempfile.TemporaryDirectory()
        cp = Path(self._tmp.name) / "c.pem"
        kp = Path(self._tmp.name) / "k.pem"
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.TraditionalOpenSSL,
                                         serialization.NoEncryption()))
        sctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(str(cp), str(kp))

        # TLS-сервер: принимает рукопожатие и закрывается.
        tls_srv = socket.socket()
        tls_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tls_srv.bind(("127.0.0.1", 0))
        tls_srv.listen(8)
        self.tls_port = tls_srv.getsockname()[1]

        def serve_tls():
            while True:
                try:
                    c, _ = tls_srv.accept()
                except OSError:
                    return
                try:
                    with sctx.wrap_socket(c, server_side=True):
                        pass
                except OSError:
                    pass

        # Мини-SOCKS5: greeting -> CONNECT -> релей на цель.
        socks_srv = socket.socket()
        socks_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        socks_srv.bind(("127.0.0.1", 0))
        socks_srv.listen(8)
        self.socks_port = socks_srv.getsockname()[1]

        def serve_socks():
            while True:
                try:
                    c, _ = socks_srv.accept()
                except OSError:
                    return
                th.Thread(target=self._handle_socks, args=(c,), daemon=True).start()

        self._srv = (tls_srv, socks_srv)
        for fn in (serve_tls, serve_socks):
            th.Thread(target=fn, daemon=True).start()

    def _handle_socks(self, c):
        import threading as th
        try:
            c.recv(3)                                   # greeting
            c.sendall(b"\x05\x00")
            req = c.recv(10)                            # VER CMD RSV ATYP IP PORT
            port = int.from_bytes(req[8:10], "big")
            up = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

            def pump(a, b):
                try:
                    while True:
                        d = a.recv(65536)
                        if not d:
                            break
                        b.sendall(d)
                except OSError:
                    pass
                finally:
                    for s in (a, b):
                        try:
                            s.close()
                        except OSError:
                            pass

            th.Thread(target=pump, args=(c, up), daemon=True).start()
            th.Thread(target=pump, args=(up, c), daemon=True).start()
        except OSError:
            try:
                c.close()
            except OSError:
                pass

    def tearDown(self):
        for s in self._srv:
            try:
                s.close()
            except OSError:
                pass
        self._tmp.cleanup()

    def test_reports_true_when_traffic_flows(self):
        self.assertTrue(vpn.tunnel_works(self.socks_port, timeout=8.0,
                                         targets=[("127.0.0.1", self.tls_port)]))

    def test_reports_false_when_nothing_listens(self):
        dead = vpn.free_local_port()      # порт заведомо никем не занят
        self.assertFalse(vpn.tunnel_works(dead, timeout=1.0,
                                          targets=[("127.0.0.1", self.tls_port)]))

    def test_code_errors_are_not_disguised_as_dead_server(self):
        """Программная ошибка обязана всплыть, а не превратиться в «сервер мёртв»."""
        orig = vpn._socks_connect_ip
        vpn._socks_connect_ip = lambda *a, **kw: (_ for _ in ()).throw(NameError("boom"))
        try:
            with self.assertRaises(NameError):
                vpn.tunnel_works(self.socks_port, targets=[("127.0.0.1", self.tls_port)])
        finally:
            vpn._socks_connect_ip = orig


# ---- Share-ссылки: самый ходовой формат подписки (панель отдаёт base64 от строк) ----
# Раньше такие подписки не разбирались ВООБЩЕ (0 серверов): «в Happ серверы есть,
# а в FreeConnect нет» — ровно этот случай.

REALITY_LINK = (
    "vless://71c8bb98-ab9b-4bec-83bb-e32cf870b9d5@188.253.22.204:443"
    "?encryption=none&flow=xtls-rprx-vision&type=tcp&security=reality"
    "&sni=tg.capynode.com&fp=qq&pbk=sMjQ93A1pjYo-vG91WS4N7TkPgfzI9kpqxkf5YVRW0I&sid=a353848a193ec557&spx=%2F"
    "#%F0%9F%87%B1%F0%9F%87%BB%20%D0%9B%D0%B0%D1%82%D0%B2%D0%B8%D1%8F"          # 🇱🇻 Латвия
)
WS_TLS_LINK = (
    "vless://11111111-2222-3333-4444-555555555555@nl1.example.com:8443"
    "?encryption=none&security=tls&sni=nl1.example.com&type=ws&host=nl1.example.com"
    "&path=%2Fws#%D0%9D%D0%B8%D0%B4%D0%B5%D1%80%D0%BB%D0%B0%D0%BD%D0%B4%D1%8B%20%2B"
)
TROJAN_GRPC_LINK = (
    "trojan://secretpass@tr.example.net:2053"
    "?security=reality&sni=yahoo.com&fp=chrome&pbk=sMjQ93A1pjYo-vG91WS4N7TkPgfzI9kpqxkf5YVRW0I&sid=ab12"
    "&type=grpc&serviceName=grpcsvc#%D0%93%D0%B5%D1%80%D0%BC%D0%B0%D0%BD%D0%B8%D1%8F%20%E2%9A%A1"
)
HY2_LINK = ("hysteria2://mypass@fi.example.org:8447"
            "?sni=fi.example.org&insecure=1&alpn=h3&obfs=salamander&obfs-password=op"
            "#%D0%A4%D0%B8%D0%BD%D0%BB%D1%8F%D0%BD%D0%B4%D0%B8%D1%8F")
SS_LINK = ("ss://" + base64.b64encode(b"aes-256-gcm:sspass").decode()
           + "@sg.example.com:8388#%D0%A1%D0%B8%D0%BD%D0%B3%D0%B0%D0%BF%D1%83%D1%80")


def _vmess_link() -> str:
    payload = {"v": "2", "ps": "Япония", "add": "jp.example.com", "port": "443",
               "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "aid": "0", "scy": "auto",
               "net": "ws", "type": "none", "host": "jp.example.com", "path": "/vm",
               "tls": "tls", "sni": "jp.example.com"}
    return "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()


RU_LINK = ("vless://x@ru-relay.example.ru:443?security=reality&sni=a.com&pbk=K&type=tcp"
           "#%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F")


class TestShareLinks(unittest.TestCase):
    def _one(self, link: str):
        srvs = vpn.parse_servers(link)
        self.assertEqual(len(srvs), 1, f"ожидали ровно один сервер из {link[:40]}…")
        return srvs[0]

    def test_vless_reality_link(self):
        s = self._one(REALITY_LINK)
        self.assertEqual((s.kind, s.address, s.port), ("vless-reality", "188.253.22.204", 443))
        self.assertEqual(s.country, "latvia")          # страна из #Латвия
        self.assertEqual(s.params["id"], "71c8bb98-ab9b-4bec-83bb-e32cf870b9d5")
        self.assertEqual(s.params["flow"], "xtls-rprx-vision")
        self.assertEqual(s.params["reality"]["publicKey"], "sMjQ93A1pjYo-vG91WS4N7TkPgfzI9kpqxkf5YVRW0I")
        self.assertEqual(s.params["sni"], "tg.capynode.com")
        ob = vpn._server_to_outbound(s)
        self.assertEqual(ob["type"], "vless")
        self.assertTrue(ob["tls"]["reality"]["enabled"])
        self.assertEqual(ob["tls"]["utls"]["fingerprint"], "qq")

    def test_vless_ws_tls_link(self):
        s = self._one(WS_TLS_LINK)
        self.assertEqual(s.kind, "vless-tls")
        self.assertEqual(s.country, "netherlands")
        self.assertEqual(s.name, "Нидерланды +")       # имя из фрагмента, без мусора
        ob = vpn._server_to_outbound(s)
        self.assertNotIn("reality", ob["tls"])
        self.assertEqual(ob["transport"], {"type": "ws", "path": "/ws",
                                           "headers": {"Host": "nl1.example.com"}})

    def test_trojan_grpc_reality_link(self):
        s = self._one(TROJAN_GRPC_LINK)
        self.assertEqual(s.kind, "trojan-reality")
        self.assertEqual(s.params["password"], "secretpass")
        ob = vpn._server_to_outbound(s)
        self.assertEqual(ob["type"], "trojan")
        self.assertEqual(ob["transport"], {"type": "grpc", "service_name": "grpcsvc"})

    def test_hysteria2_link_with_obfs(self):
        s = self._one(HY2_LINK)
        self.assertEqual((s.kind, s.address, s.port), ("hysteria2", "fi.example.org", 8447))
        self.assertEqual(s.params["auth"], "mypass")
        ob = vpn._server_to_outbound(s)
        self.assertTrue(ob["tls"]["insecure"])         # insecure=1 в ссылке
        self.assertEqual(ob["obfs"], {"type": "salamander", "password": "op"})

    def test_shadowsocks_legacy_base64_creds(self):
        s = self._one(SS_LINK)
        self.assertEqual(s.kind, "shadowsocks")
        self.assertEqual(s.params["method"], "aes-256-gcm")
        self.assertEqual(s.params["password"], "sspass")
        ob = vpn._server_to_outbound(s)
        self.assertEqual((ob["type"], ob["method"]), ("shadowsocks", "aes-256-gcm"))

    def test_vmess_link(self):
        s = self._one(_vmess_link())
        self.assertEqual(s.kind, "vmess")
        self.assertEqual(s.params["id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        ob = vpn._server_to_outbound(s)
        self.assertEqual(ob["type"], "vmess")
        self.assertEqual(ob["transport"]["type"], "ws")

    def test_base64_list_of_links(self):
        """Панель отдаёт base64 от списка ссылок — это и есть «тот же список, что в Happ»."""
        text = "\n".join([REALITY_LINK, WS_TLS_LINK, TROJAN_GRPC_LINK, HY2_LINK])
        b64 = base64.b64encode(text.encode()).decode()
        srvs = vpn.parse_servers(b64)
        self.assertEqual(len(srvs), 4)
        self.assertEqual([s.kind for s in srvs],
                         ["hysteria2", "vless-reality", "trojan-reality", "vless-tls"])

    def test_domestic_link_is_filtered(self):
        self.assertEqual(vpn.parse_servers(RU_LINK), [])

    def test_garbage_lines_do_not_break_import(self):
        text = "мусор\n" + REALITY_LINK + "\nне ссылка вовсе\n" + WS_TLS_LINK
        self.assertEqual(len(vpn.parse_servers(text)), 2)

    def test_same_server_from_json_and_link_is_deduped(self):
        """Один и тот же выход, пришедший и профилем, и ссылкой, не должен двоиться."""
        prof = _finland_vless_profile()
        link = ("vless://11111111-1111-1111-1111-111111111111@1.2.3.4:443"
                "?security=reality&sni=a.com&pbk=K&type=tcp#Финляндия")
        text = json.dumps([prof]) + "\n" + link
        srvs = vpn.parse_servers(text)
        addrs = [(s.address, s.port) for s in srvs]
        self.assertEqual(len(addrs), len(set(addrs)))


class TestSingboxAcceptsConfigs(unittest.TestCase):
    """Наши конфиги обязан принимать НАСТОЯЩИЙ sing-box (`sing-box check`).

    Без этого теста устаревший формат DNS уехал пользователю и ронял процесс с
    FATAL: юнит-тесты проверяли структуру словаря, но не совместимость с бинарником."""

    @classmethod
    def setUpClass(cls):
        from freeconnect import singbox
        cls.exe = singbox.SINGBOX_EXE
        if not cls.exe.is_file():
            raise unittest.SkipTest("sing-box не забандлен — проверка совместимости пропущена")

    def _check(self, cfg: dict):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            r = subprocess.run([str(self.exe), "check", "-c", str(p)],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         f"sing-box отверг конфиг:\n{r.stdout}\n{r.stderr}")

    def _servers(self):
        chained = vpn.parse_servers(json.dumps([_chained_profile()]))[0]
        direct = vpn.best_server(vpn.parse_servers(json.dumps([_germany_hy2_profile()])))
        return chained, direct

    def test_tun_config_direct(self):
        _, direct = self._servers()
        self._check(vpn.build_singbox_config(direct))

    def test_tun_config_chained(self):
        chained, _ = self._servers()
        self._check(vpn.build_singbox_config(chained))

    def test_probe_config_direct(self):
        _, direct = self._servers()
        self._check(vpn.build_probe_config(direct, 1080))

    def test_probe_config_chained(self):
        chained, _ = self._servers()
        self._check(vpn.build_probe_config(chained, 1080))

    def test_probe_config_for_every_link_protocol(self):
        """Новые протоколы из share-ссылок тоже обязаны проходить `sing-box check`."""
        links = [WS_TLS_LINK, TROJAN_GRPC_LINK, HY2_LINK, SS_LINK, _vmess_link()]
        srvs = vpn.parse_servers("\n".join(links))
        self.assertEqual(len(srvs), len(links))
        for s in srvs:
            with self.subTest(kind=s.kind):
                self._check(vpn.build_probe_config(s, 1080))
                self._check(vpn.build_singbox_config(s))


class TestLiveness(unittest.TestCase):
    def _servers(self):
        return [
            vpn.Server(kind="hysteria2", address="1.1.1.1", port=8447, name="A"),
            vpn.Server(kind="vless-reality", address="2.2.2.2", port=443, name="B"),
            vpn.Server(kind="vless-reality", address="3.3.3.3", port=443, name="C"),
        ]

    def test_dead_servers_ranked_after_live(self):
        servers = self._servers()
        alive_uid = servers[2].uid()   # только C жив
        orig = vpn.probe_server
        vpn.probe_server = lambda s, timeout=2.0: (s.uid() == alive_uid, 10.0)
        try:
            cands = vpn.live_candidates(servers)
        finally:
            vpn.probe_server = orig
        self.assertEqual(cands[0].uid(), alive_uid)   # живой — первым

    def test_prefer_uid_wins_when_alive(self):
        servers = self._servers()
        prefer = servers[1].uid()
        orig = vpn.probe_server
        vpn.probe_server = lambda s, timeout=2.0: (True, 10.0)   # все живы
        try:
            cands = vpn.live_candidates(servers, prefer_uid=prefer)
        finally:
            vpn.probe_server = orig
        self.assertEqual(cands[0].uid(), prefer)

    def test_all_dead_still_returns_candidates(self):
        servers = self._servers()
        orig = vpn.probe_server
        vpn.probe_server = lambda s, timeout=2.0: (False, None)
        try:
            cands = vpn.live_candidates(servers)
        finally:
            vpn.probe_server = orig
        self.assertEqual(len(cands), 3)     # мёртвые — но пробуем, а не отказываем


class TestSingboxConfig(unittest.TestCase):
    def _fi(self):
        return next(s for s in vpn.parse_servers(json.dumps([_finland_vless_profile()]))
                    if s.country == "finland")

    def _de(self):
        return vpn.best_server(vpn.parse_servers(json.dumps([_germany_hy2_profile()])))

    def test_routes_only_discord_to_vpn(self):
        cfg = vpn.build_singbox_config(self._de())
        rule = cfg["route"]["rules"][0]
        self.assertEqual(rule["outbound"], "vpn")
        self.assertIn("Discord.exe", rule["process_name"])
        self.assertEqual(cfg["route"]["final"], "direct")   # остальное — мимо VPN
        tags = {o["tag"] for o in cfg["outbounds"]}
        self.assertEqual(tags, {"vpn", "direct"})
        self.assertEqual(cfg["inbounds"][0]["type"], "tun")

    def test_hysteria2_outbound(self):
        ob = vpn._server_to_outbound(self._de())
        self.assertEqual(ob["type"], "hysteria2")
        self.assertEqual(ob["server"], "84.38.186.105")
        self.assertEqual(ob["password"], "user:hash:streisand")
        self.assertEqual(ob["tls"]["server_name"], "www.microsoft.com")
        self.assertTrue(ob["tls"]["insecure"])   # pinned sha256 -> нестрогая проверка

    def test_vless_reality_outbound(self):
        ob = vpn._server_to_outbound(self._fi())
        self.assertEqual(ob["type"], "vless")
        self.assertEqual(ob["uuid"], "uuid-fi")
        self.assertTrue(ob["tls"]["reality"]["enabled"])
        self.assertEqual(ob["tls"]["reality"]["public_key"], "PUBFI")
        self.assertEqual(ob["transport"]["type"], "grpc")
        self.assertEqual(ob["transport"]["service_name"], "grpc")

    def test_config_is_json_serializable(self):
        json.dumps(vpn.build_singbox_config(self._fi()))   # не должно бросать


if __name__ == "__main__":
    unittest.main()
