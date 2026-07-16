"""Парсер подписки VPN-для-Discord: извлечение зарубежных серверов, приоритет
Hysteria2, пропуск цепочек (dialerProxy) и RU-relay. Фикстура структурно повторяет
реальный xray-конфиг Happ."""
import base64
import json
import unittest

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
    return {"remarks": "Hysteria2 | 🇩🇪 Германия", "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "hy2in-88", "protocol": "hysteria",
         "settings": {"version": 2, "address": "84.38.186.105", "port": 8447},
         "streamSettings": {"network": "hysteria", "security": "tls",
                            "tlsSettings": {"serverName": "www.microsoft.com",
                                            "pinnedPeerCertSha256": "SHA", "alpn": ["h3"]},
                            "hysteriaSettings": {"version": 2, "auth": "user:hash:streisand"}}},
        # цепочка (dialerProxy) — ДОЛЖНА быть пропущена в MVP
        {"tag": "hy2via-88_germany_vless-grpc", "protocol": "vless",
         "settings": {"vnext": [{"address": "64.188.67.74", "port": 2087,
                                 "users": [{"id": "x", "encryption": "none"}]}]},
         "streamSettings": {"network": "grpc", "security": "reality",
                            "grpcSettings": {"serviceName": "grpc"},
                            "realitySettings": {"publicKey": "P", "shortId": "s", "serverName": "ads.x5.ru"},
                            "sockopt": {"dialerProxy": "hy2in-88"}}},
    ]}


def _russia_relay_profile():
    return {"remarks": "🇷🇺 Россия", "outbounds": [
        {"tag": "181_russia_vless-tcp", "protocol": "vless",
         "settings": {"vnext": [{"address": "31.184.218.96", "port": 445,
                                 "users": [{"id": "ru", "flow": "xtls-rprx-vision"}]}]},
         "streamSettings": {"network": "tcp", "security": "reality",
                            "realitySettings": {"publicKey": "PRU", "shortId": "s", "serverName": "ads.x5.ru"}}},
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
        # RU-relay и цепочка dialerProxy пропущены
        self.assertFalse(any(c == "russia" for _, c in kinds))
        self.assertTrue(all(s.address != "64.188.67.74" for s in servers))  # chained skipped

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
