"""
VPN-для-Discord: парсинг пользовательской подписки (Happ/xray) в плоский список
серверов-выходов, которые мы потом сконвертируем в конфиг sing-box и через которые
погоним ВЕСЬ трафик процесса Discord (см. [[freeconnect]] план VPN-фолбэка).

Формат подписки — массив xray-профилей (VLESS+Reality/vision, Trojan+Reality,
Hysteria2). Нам не нужен весь балансер: из каждого профиля достаём отдельные
пригодные ИНОСТРАННЫЕ выходы. Приоритет — Hysteria2 (UDP/QUIC, лучший для голоса
против stateful-душения [[freeconnect-voice-5000ms-region]]).

MVP-упрощения (осознанно):
- берём ТОЛЬКО самостоятельные выходы: hysteria2, и vless/trojan+reality без
  `dialerProxy` (цепочки прокси в MVP пропускаем — усложняют конвертацию);
- страну берём из тега аутбаунда (…_finland_… / …_germany_…) или из remarks;
- RU-теги (relay-входы внутри РФ) пропускаем — нам нужен зарубежный выход;
- дедуп по (kind, address, port).
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

# Известные зарубежные страны в тегах/remarks -> (флаг, читаемое имя).
COUNTRIES: dict[str, tuple[str, str]] = {
    "finland": ("🇫🇮", "Финляндия"),
    "germany": ("🇩🇪", "Германия"),
    "italy": ("🇮🇹", "Италия"),
    "japan": ("🇯🇵", "Япония"),
    "netherlands": ("🇳🇱", "Нидерланды"),
    "poland": ("🇵🇱", "Польша"),
    "france": ("🇫🇷", "Франция"),
    "united-kingdom": ("🇬🇧", "Великобритания"),
}
# Порядок предпочтения протоколов: Hysteria2 первым (UDP-ядро — лучший голос).
KIND_RANK = {"hysteria2": 0, "vless-reality": 1, "trojan-reality": 2}


@dataclass
class Server:
    kind: str                    # hysteria2 | vless-reality | trojan-reality
    address: str
    port: int
    country: str = ""            # 'finland' и т.п. (ключ COUNTRIES) или ''
    name: str = ""               # человекочитаемое: «🇩🇪 Германия · Hysteria2»
    params: dict = field(default_factory=dict)  # реквизиты под конвертацию в sing-box

    def key(self) -> tuple:
        return (self.kind, self.address, self.port)


def _country_from_text(text: str) -> str:
    """Опознаёт страну по английскому слову из тега (…_germany_…), по русскому
    названию или по флагу-эмодзи из remarks («🇩🇪 Германия»)."""
    if not text:
        return ""
    t = text.lower()
    for word, (flag, ru) in COUNTRIES.items():
        if word in t or ru.lower() in t or flag in text:
            return word
    return ""


def _label(kind: str, country: str) -> str:
    flag, ru = COUNTRIES.get(country, ("🌍", "Сервер"))
    proto = {"hysteria2": "Hysteria2", "vless-reality": "VLESS-Reality",
             "trojan-reality": "Trojan-Reality"}.get(kind, kind)
    return f"{flag} {ru} · {proto}"


def decode_subscription(text: str) -> list[dict]:
    """Текст подписки -> список xray-профилей. Понимает: сырой JSON-массив,
    base64(JSON), и объект-обёртку. Бросает ValueError, если не разобрали."""
    text = (text or "").strip()
    if not text:
        raise ValueError("пустая подписка")

    def _as_profiles(obj):
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            # частые обёртки
            for k in ("configs", "outbounds", "servers", "data"):
                if isinstance(obj.get(k), list):
                    return obj[k]
            return [obj]
        raise ValueError("неожиданная структура подписки")

    # 1) прямой JSON
    try:
        return _as_profiles(json.loads(text))
    except json.JSONDecodeError:
        pass
    # 2) base64 -> JSON
    try:
        pad = "=" * (-len(text) % 4)
        raw = base64.b64decode(text + pad, validate=False).decode("utf-8", "replace")
        return _as_profiles(json.loads(raw))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"не удалось разобрать подписку: {e}")


def _outbound_to_server(o: dict, fallback_country: str = "") -> Server | None:
    """Один xray-аутбаунд -> Server, либо None если не пригоден для MVP.
    fallback_country — страна из remarks профиля (когда тег её не содержит,
    напр. hysteria2-выходы с тегом hy2in-88)."""
    proto = o.get("protocol")
    ss = o.get("streamSettings", {}) or {}
    tag = o.get("tag", "") or ""
    # Цепочки прокси (dialerProxy) в MVP пропускаем.
    if (ss.get("sockopt", {}) or {}).get("dialerProxy"):
        return None
    country = _country_from_text(tag) or fallback_country

    if proto == "hysteria":
        st = o.get("settings", {}) or {}
        if int(st.get("version", 0) or ss.get("hysteriaSettings", {}).get("version", 0)) != 2:
            return None
        tls = ss.get("tlsSettings", {}) or {}
        hy = ss.get("hysteriaSettings", {}) or {}
        addr, port = st.get("address"), st.get("port")
        if not addr or not port:
            return None
        return Server(kind="hysteria2", address=addr, port=int(port), country=country,
                      params={"auth": hy.get("auth", ""), "sni": tls.get("serverName", ""),
                              "pinnedSha256": tls.get("pinnedPeerCertSha256", ""),
                              "alpn": tls.get("alpn", ["h3"])})

    if proto in ("vless", "trojan") and ss.get("security") == "reality":
        vnext = (o.get("settings", {}) or {}).get("vnext") or []
        servers = (o.get("settings", {}) or {}).get("servers") or []
        node = vnext[0] if vnext else (servers[0] if servers else None)
        if not node or not node.get("address") or not node.get("port"):
            return None
        rs = ss.get("realitySettings", {}) or {}
        net = ss.get("network", "tcp")
        common = {
            "network": net,
            "reality": {"publicKey": rs.get("publicKey", ""), "shortId": rs.get("shortId", ""),
                        "serverName": rs.get("serverName", ""), "fingerprint": rs.get("fingerprint", "chrome")},
        }
        if net == "grpc":
            common["grpcServiceName"] = (ss.get("grpcSettings", {}) or {}).get("serviceName", "")
        elif net == "xhttp":
            common["xhttpPath"] = (ss.get("xhttpSettings", {}) or {}).get("path", "")
        if proto == "vless":
            user = (node.get("users") or [{}])[0]
            common["id"] = user.get("id", "")
            common["flow"] = user.get("flow", "")
            kind = "vless-reality"
        else:
            common["password"] = node.get("password", "")
            kind = "trojan-reality"
        return Server(kind=kind, address=node["address"], port=int(node["port"]),
                      country=country, params=common)

    return None


def parse_servers(text: str) -> list[Server]:
    """Подписка -> отсортированный список уникальных зарубежных серверов-выходов."""
    profiles = decode_subscription(text)
    seen: set[tuple] = set()
    out: list[Server] = []
    for prof in profiles:
        if not isinstance(prof, dict):
            continue
        prof_country = _country_from_text(prof.get("remarks", ""))
        for o in prof.get("outbounds", []) or []:
            srv = _outbound_to_server(o, fallback_country=prof_country)
            if not srv or not srv.country:   # берём только опознанные зарубежные
                continue
            if srv.key() in seen:
                continue
            seen.add(srv.key())
            srv.name = _label(srv.kind, srv.country)
            out.append(srv)
    out.sort(key=lambda s: (KIND_RANK.get(s.kind, 9), s.country, s.address))
    return out


def best_server(servers: list[Server], country: str | None = None) -> Server | None:
    """Авто-выбор: (опц.) фильтр по стране, затем приоритет Hysteria2 (уже отсортировано)."""
    pool = [s for s in servers if not country or s.country == country]
    return pool[0] if pool else None


# Процессы клиента Discord (десктоп/PTB/Canary) — по ним маршрутизируем в VPN.
DISCORD_PROCESSES = ["Discord.exe", "DiscordPTB.exe", "DiscordCanary.exe",
                     "DiscordDevelopment.exe"]


def _server_to_outbound(s: Server, tag: str = "vpn") -> dict:
    """Server -> outbound sing-box. ВНИМАНИЕ: маппинг проверяется вживую на машине
    (sing-box + реальный сервер); здесь — по схеме sing-box 1.x."""
    p = s.params
    if s.kind == "hysteria2":
        ob = {"type": "hysteria2", "tag": tag, "server": s.address, "server_port": s.port,
              "password": p.get("auth", ""),
              "tls": {"enabled": True, "server_name": p.get("sni", ""),
                      "alpn": p.get("alpn", ["h3"])}}
        # Самоподписанный/подменный SNI (pinned sha256 в xray) — обычная TLS-валидация
        # не пройдёт, поэтому отключаем строгую проверку (как и делает исходный клиент).
        if p.get("pinnedSha256"):
            ob["tls"]["insecure"] = True
        return ob

    reality = p.get("reality", {})
    tls = {"enabled": True, "server_name": reality.get("serverName", ""),
           "utls": {"enabled": True, "fingerprint": reality.get("fingerprint", "chrome")},
           "reality": {"enabled": True, "public_key": reality.get("publicKey", ""),
                       "short_id": reality.get("shortId", "")}}
    net = p.get("network", "tcp")
    transport = None
    if net == "grpc":
        transport = {"type": "grpc", "service_name": p.get("grpcServiceName", "")}
    elif net == "xhttp":
        # sing-box зовёт это http; путь переносим как есть.
        transport = {"type": "http", "path": p.get("xhttpPath", "")}

    if s.kind == "vless-reality":
        ob = {"type": "vless", "tag": tag, "server": s.address, "server_port": s.port,
              "uuid": p.get("id", ""), "tls": tls}
        if p.get("flow"):
            ob["flow"] = p["flow"]
    else:  # trojan-reality
        ob = {"type": "trojan", "tag": tag, "server": s.address, "server_port": s.port,
              "password": p.get("password", ""), "tls": tls}
    if transport:
        ob["transport"] = transport
    return ob


def build_singbox_config(server: Server, tun_name: str = "freeconn0") -> dict:
    """Полный конфиг sing-box: TUN + маршрут «процессы Discord -> VPN, остальное ->
    direct». Всё, что direct, дальше идёт через наш winws-десинк как обычно."""
    return {
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "tun", "tag": "tun-in", "interface_name": tun_name,
            "address": ["172.19.0.1/30"], "auto_route": True, "strict_route": False,
            "stack": "system",
        }],
        "outbounds": [
            _server_to_outbound(server, tag="vpn"),
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                # Весь трафик процессов Discord — в VPN.
                {"process_name": DISCORD_PROCESSES, "outbound": "vpn"},
            ],
            "final": "direct",
            "auto_detect_interface": True,
        },
    }
