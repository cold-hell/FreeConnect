"""
VPN-для-Discord: парсинг пользовательской подписки (Happ/xray) в плоский список
серверов-выходов, которые мы потом сконвертируем в конфиг sing-box и через которые
погоним ВЕСЬ трафик процесса Discord (см. [[freeconnect]] план VPN-фолбэка).

Формат подписки — массив xray-профилей (VLESS+Reality/vision, Trojan+Reality,
Hysteria2). Нам не нужен весь балансер: из каждого профиля достаём отдельные
пригодные ИНОСТРАННЫЕ выходы. Приоритет — Hysteria2 (UDP/QUIC, лучший для голоса
против stateful-душения [[freeconnect-voice-5000ms-region]]).

Принципы отбора:
- берём самостоятельные выходы: hysteria2, и vless/trojan+reality без `dialerProxy`
  (цепочки прокси пропускаем — усложняют конвертацию);
- страну берём из тега аутбаунда (…_finland_… / …_germany_…) или из remarks; если не
  опознали — сервер ВСЁ РАВНО берём (просто под своим именем из remarks). Раньше мы
  молча выбрасывали все неопознанные страны — из-за этого в списке терялась куча
  рабочих серверов, хотя в клиенте (Happ) они есть;
- домашние RU-входы (обходят белые списки внутри РФ) пропускаем — нужен зарубежный выход;
- дедуп по (kind, address, port); имя сервера сохраняем из remarks, чтобы «Нидерланды»
  и «Нидерланды +» были РАЗНЫМИ строками, а не схлопывались в одну страну.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import time
import urllib.parse
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
    "united-states": ("🇺🇸", "США"),
    "sweden": ("🇸🇪", "Швеция"),
    "turkey": ("🇹🇷", "Турция"),
    "spain": ("🇪🇸", "Испания"),
    "switzerland": ("🇨🇭", "Швейцария"),
    "latvia": ("🇱🇻", "Латвия"),
    "estonia": ("🇪🇪", "Эстония"),
    "lithuania": ("🇱🇹", "Литва"),
    "singapore": ("🇸🇬", "Сингапур"),
    "canada": ("🇨🇦", "Канада"),
    "austria": ("🇦🇹", "Австрия"),
    "norway": ("🇳🇴", "Норвегия"),
    "denmark": ("🇩🇰", "Дания"),
    "czech": ("🇨🇿", "Чехия"),
    "hungary": ("🇭🇺", "Венгрия"),
    "romania": ("🇷🇴", "Румыния"),
    "hong-kong": ("🇭🇰", "Гонконг"),
    "korea": ("🇰🇷", "Корея"),
    "india": ("🇮🇳", "Индия"),
    "ireland": ("🇮🇪", "Ирландия"),
    "belgium": ("🇧🇪", "Бельгия"),
}
# Домашние (RU) входы обходят белые списки внутри РФ — как выход для Discord не годятся.
RU_MARKERS = ("🇷🇺", "russia", "россия", "russian", "moscow", "москва", "spb", "питер")
# Порядок предпочтения протоколов: Hysteria2 первым (UDP-ядро — лучший голос),
# далее Reality (маскировка под чужой TLS), затем обычный TLS и прочее.
KIND_RANK = {"hysteria2": 0, "vless-reality": 1, "trojan-reality": 2,
             "vless-tls": 3, "trojan-tls": 4, "vmess": 5, "shadowsocks": 6}

# Схемы share-ссылок, которые умеем разбирать (самый ходовой формат подписок:
# панель отдаёт их как base64-список строк, а не как JSON).
LINK_SCHEMES = ("vless://", "trojan://", "ss://", "hysteria2://", "hy2://", "vmess://")

# Признаки «заглушки» панели: клиенту, которого панель не признала (не прислал
# HWID/устройство не разрешено), она отдаёт ОДИН нерабочий сервер с такой пометкой
# в имени. Без этой проверки причина выглядела как «VPN не подключается».
STUB_MARKERS = ("данное приложение не поддерживается", "app is not supported",
                "application is not supported", "unsupported client", "unsupported app")


def looks_like_stub(text: str) -> bool:
    """Похоже ли, что панель отдала заглушку вместо настоящего списка серверов."""
    low = (text or "").lower()
    return any(m in low for m in STUB_MARKERS)


@dataclass
class Server:
    kind: str                    # hysteria2 | vless-reality | trojan-reality
    address: str
    port: int
    country: str = ""            # 'finland' и т.п. (ключ COUNTRIES) или ''
    name: str = ""               # человекочитаемое имя (из remarks или «🇩🇪 Германия · Hysteria2»)
    params: dict = field(default_factory=dict)  # реквизиты под конвертацию в sing-box
    via: "Server | None" = None  # ЦЕПОЧКА: через какой вход идём (xray dialerProxy).
                                 # У этой подписки зарубежные выходы часто доступны
                                 # ТОЛЬКО так: заход на российский relay-вход, а он уже
                                 # релеит за рубеж. Прямой заход на выход душится.

    def entry(self) -> "Server":
        """Точка, куда реально коннектимся: вход цепочки, либо сам сервер."""
        return self.via or self

    def key(self) -> tuple:
        # Вход входит в ключ: один и тот же зарубежный выход может быть доступен
        # через РАЗНЫЕ входы — это разные маршруты, схлопывать их нельзя.
        v = self.via
        return (self.kind, self.address, self.port,
                (v.kind, v.address, v.port) if v else None)

    def uid(self) -> str:
        """Стабильный идентификатор конкретного маршрута (для выбора в UI и конфиге)."""
        base = f"{self.kind}|{self.address}|{self.port}"
        if self.via:
            base += f"@{self.via.kind}|{self.via.address}|{self.via.port}"
        return base


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


def _is_domestic(*texts: str) -> bool:
    """Домашний RU-вход (relay внутри РФ)? Такие обходят белые списки, а не выводят
    трафик за рубеж — как VPN-выход для Discord не годятся, отсеиваем."""
    blob = " ".join(t for t in texts if t).lower()
    if any(m in blob for m in RU_MARKERS):
        return True
    # изолированный токен 'ru' в теге (…_russia_…, ru-1), но не 'trust'/'belarus'
    return bool(re.search(r"(?:^|[_\- ])ru(?:[_\- 0-9]|$)", blob))


def _clean_name(remark: str) -> str:
    """Чистит имя из remarks под строку списка (убираем служебный мусор/скорость)."""
    s = re.sub(r"\s+", " ", (remark or "").strip())
    # частый префикс-разделитель протокола: «Hysteria2 | 🇩🇪 Германия» -> «🇩🇪 Германия»
    if " | " in s:
        parts = [p.strip() for p in s.split("|")]
        # берём часть с флагом/страной, если есть
        for p in parts:
            if any(f in p for f, _ in COUNTRIES.values()) or _country_from_text(p):
                s = p
                break
    return s[:48]


def _label(kind: str, country: str) -> str:
    flag, ru = COUNTRIES.get(country, ("🌍", "Сервер"))
    proto = {"hysteria2": "Hysteria2", "vless-reality": "VLESS-Reality",
             "trojan-reality": "Trojan-Reality", "vless-tls": "VLESS-TLS",
             "trojan-tls": "Trojan-TLS", "vmess": "VMess",
             "shadowsocks": "Shadowsocks"}.get(kind, kind)
    return f"{flag} {ru} · {proto}"


def _try_json(text: str):
    """JSON из текста либо None (без исключений — форматов подписки много)."""
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _try_b64_text(text: str) -> str:
    """base64 (возможно без padding, с переводами строк) -> текст. '' если не вышло."""
    s = re.sub(r"\s+", "", text or "")
    if len(s) < 8:
        return ""
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=False)
    except Exception:  # noqa: BLE001
        return ""
    out = raw.decode("utf-8", "replace")
    # Мусор от «дешифровки» произвольного текста отсеиваем: в подписке должны быть
    # либо ссылки, либо JSON, либо base64-внутренность.
    if any(sch in out for sch in LINK_SCHEMES) or out.lstrip()[:1] in "[{":
        return out
    for line in out.splitlines():
        inner = _try_b64_text(line) if len(line) > 8 else ""
        if inner:
            return inner
    return ""


def _as_profiles(obj) -> list:
    """Обёртки подписки вокруг массива xray-профилей."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("configs", "outbounds", "servers", "data"):
            if isinstance(obj.get(k), list):
                return obj[k]
        return [obj]
    return []


def decode_subscription(text: str) -> list[dict]:
    """Текст подписки -> список xray-профилей. Понимает: сырой JSON-массив,
    base64(JSON), и объект-обёртку. Бросает ValueError, если не разобрали."""
    text = (text or "").strip()
    if not text:
        raise ValueError("пустая подписка")
    obj = _try_json(text)
    if obj is None:
        decoded = _try_b64_text(text)
        if decoded:
            obj = _try_json(decoded)
    if obj is None:
        raise ValueError("не удалось разобрать подписку: это не JSON-профили")
    return _as_profiles(obj)


# ---- Share-ссылки: vless://, trojan://, ss://, hysteria2://, vmess:// ----
# Самый ходовой формат подписки: панель отдаёт base64 от списка строк. Раньше мы
# такие подписки не понимали ВООБЩЕ (0 серверов) — из-за этого «в Happ серверы
# есть, а в FreeConnect нет».

def extract_links(text: str) -> list[str]:
    """Все share-ссылки из текста (по строке на ссылку; терпим и склейку в строку)."""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        while s:
            low = s.lower()
            pos = -1
            for sch in LINK_SCHEMES:
                p = low.find(sch)
                if p >= 0 and (pos < 0 or p < pos):
                    pos = p
            if pos < 0:
                break
            rest = s[pos:]
            end = len(rest)
            for i, ch in enumerate(rest):
                if ch in " \t":
                    end = i
                    break
            out.append(rest[:end])
            s = rest[end:].strip()
    return out


def _link_parts(link: str) -> tuple[str, str, str, dict, str, str]:
    """vless://user@host:port?params#name ->
    (схема, userinfo, hostport, params, name, raw).

    raw — часть после схемы без #-фрагмента и без query. Нужна для vmess://, где
    весь payload это base64-объект без «@» (разбор по @ его бы разрушил)."""
    scheme, _, rest = link.partition("://")
    frag = ""
    if "#" in rest:
        rest, _, frag = rest.partition("#")
    query: dict[str, str] = {}
    if "?" in rest:
        rest, _, q = rest.partition("?")
        query = {k.lower(): v[-1] for k, v in urllib.parse.parse_qs(q, keep_blank_values=True).items()}
    raw = rest
    userinfo, hostport = "", rest
    if "@" in rest:
        userinfo, _, hostport = rest.rpartition("@")
    return (scheme.lower(), urllib.parse.unquote(userinfo), hostport,
            query, urllib.parse.unquote(frag).strip(), raw)


def _b64_text(s: str) -> str:
    """Прямой base64 -> текст (для креденшелов ss:// и JSON vmess://), '' если не вышло."""
    try:
        raw = base64.b64decode((s or "").strip() + "=" * (-len(s or "") % 4), validate=False)
        return raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _host_port(hostport: str, default_port: int = 443) -> tuple[str, int]:
    hp = (hostport or "").strip()
    if hp.startswith("["):                      # IPv6: [::1]:443
        host, _, tail = hp.partition("]")
        host = host.lstrip("[")
        port = tail.lstrip(":").split("/")[0]
    else:
        host, _, port = hp.rpartition(":")
        if not host:                            # порта нет вовсе
            host, port = hp, ""
    host = host.strip().strip("/")
    try:
        return host, int(port)
    except (TypeError, ValueError):
        return host, default_port


def _transport_params(q: dict) -> dict:
    """Общие для ссылок сетевые параметры (транспорт + TLS) в наш внутренний вид."""
    net = (q.get("type") or q.get("net") or "tcp").lower()
    if net in ("h2", "http"):
        net = "http"
    security = (q.get("security") or q.get("tls") or "none").lower()
    p: dict = {"network": net, "tlsMode": "reality" if security == "reality" else
               ("tls" if security in ("tls", "xtls", "reality") else "none")}
    sni = q.get("sni") or q.get("host") or q.get("peer") or ""
    if q.get("fp"):
        p["fingerprint"] = q["fp"]
    if sni:
        p["sni"] = sni
    if (q.get("allowinsecure") or q.get("insecure") or "") in ("1", "true"):
        p["insecure"] = True
    if q.get("alpn"):
        p["alpn"] = [a for a in urllib.parse.unquote(q["alpn"]).split(",") if a]
    if security == "reality":
        p["reality"] = {"publicKey": q.get("pbk", ""), "shortId": q.get("sid", ""),
                        "serverName": sni, "fingerprint": q.get("fp", "chrome")}
    if net == "ws":
        p["path"] = q.get("path", "") or "/"
        if q.get("host"):
            p["host"] = q["host"]
    elif net == "grpc":
        p["serviceName"] = q.get("servicename", "") or q.get("path", "").lstrip("/")
    elif net == "http":
        p["path"] = q.get("path", "") or "/"
        if q.get("host"):
            p["host"] = q["host"]
    elif net == "httpupgrade":
        p["path"] = q.get("path", "") or "/"
        if q.get("host"):
            p["host"] = q["host"]
    return p


def _link_to_server(link: str) -> Server | None:
    """Одна share-ссылка -> Server (или None, если протокол нам не подходит)."""
    try:
        scheme, userinfo, hostport, q, name, raw = _link_parts(link)
        addr, port = _host_port(hostport)
        if not addr or not port:
            return None
        country = _country_from_text(name)

        if scheme == "vless":
            p = _transport_params(q)
            p["id"] = userinfo
            if q.get("flow"):
                p["flow"] = q["flow"]
            kind = "vless-reality" if p["tlsMode"] == "reality" else "vless-tls"
            return Server(kind=kind, address=addr, port=port, country=country,
                          name=name, params=p)

        if scheme == "trojan":
            p = _transport_params(q)
            p["password"] = userinfo
            kind = "trojan-reality" if p["tlsMode"] == "reality" else "trojan-tls"
            return Server(kind=kind, address=addr, port=port, country=country,
                          name=name, params=p)

        if scheme in ("hysteria2", "hy2"):
            p = {"auth": userinfo, "sni": q.get("sni", ""),
                 "alpn": [a for a in (q.get("alpn") or "h3").split(",") if a]}
            if (q.get("insecure") or "") in ("1", "true"):
                p["insecure"] = True
            if q.get("pinSHA256"):
                p["pinnedSha256"] = q["pinSHA256"]
            if q.get("obfs"):
                p["obfs"] = q["obfs"]
                p["obfsPassword"] = q.get("obfs-password", "")
            return Server(kind="hysteria2", address=addr, port=port, country=country,
                          name=name, params=p)

        if scheme == "ss":
            # ss://base64(method:password)@host:port  |  ss://method:password@host:port
            cred = userinfo
            if ":" not in cred:
                cred = _b64_text(cred).strip()
            if ":" not in cred:
                return None
            method, _, password = cred.partition(":")
            if not method or not password or q.get("plugin"):
                return None                          # плагины не конвертируем — не гадаем
            return Server(kind="shadowsocks", address=addr, port=port, country=country,
                          name=name,
                          params={"method": method.strip().lower(), "password": password})

        if scheme == "vmess":
            payload = raw.rpartition("@")[2]         # у vmess весь payload — base64 без «@»
            obj = _try_json(_b64_text(payload))
            if not isinstance(obj, dict):
                return None
            addr = str(obj.get("add") or addr).strip()
            try:
                port = int(obj.get("port") or port)
            except (TypeError, ValueError):
                return None
            q2 = {"type": obj.get("net", "tcp"), "security": obj.get("tls", ""),
                  "sni": obj.get("sni", "") or obj.get("host", ""), "host": obj.get("host", ""),
                  "path": obj.get("path", ""), "fp": obj.get("fp", "")}
            p = _transport_params(q2)
            p["id"] = str(obj.get("id", ""))
            p["vmessSecurity"] = str(obj.get("scy") or obj.get("security") or "auto")
            p["alterId"] = int(obj.get("aid", 0) or 0)
            nm = str(obj.get("ps") or name or "")
            return Server(kind="vmess", address=addr, port=port,
                          country=_country_from_text(nm), name=nm, params=p)
    except Exception:  # noqa: BLE001 — битая строка в подписке не должна ломать импорт
        return None
    return None


def _outbound_to_server(o: dict, fallback_country: str = "") -> Server | None:
    """Один xray-аутбаунд -> Server, либо None если не пригоден.
    fallback_country — страна из remarks профиля (когда тег её не содержит,
    напр. hysteria2-выходы с тегом hy2in-88)."""
    proto = o.get("protocol")
    ss = o.get("streamSettings", {}) or {}
    tag = o.get("tag", "") or ""
    # Цепочку (dialerProxy) здесь НЕ отбрасываем — её собирает parse_servers,
    # проставляя Server.via. Без этого терялись рабочие маршруты (см. Server.via).
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

    if proto in ("vless", "trojan", "vmess") and ss.get("security") in ("reality", "tls",
                                                                       "xtls", "none", ""):
        vnext = (o.get("settings", {}) or {}).get("vnext") or []
        servers = (o.get("settings", {}) or {}).get("servers") or []
        node = vnext[0] if vnext else (servers[0] if servers else None)
        if not node or not node.get("address") or not node.get("port"):
            return None
        rs = ss.get("realitySettings", {}) or {}
        ts = ss.get("tlsSettings", {}) or {}
        net = ss.get("network", "tcp")
        security = ss.get("security") or ""
        common: dict = {
            "network": net,
            "tlsMode": "reality" if security == "reality" else
                       ("tls" if security in ("tls", "xtls") else "none"),
        }
        if security == "reality":
            common["reality"] = {"publicKey": rs.get("publicKey", ""),
                                 "shortId": rs.get("shortId", ""),
                                 "serverName": rs.get("serverName", ""),
                                 "fingerprint": rs.get("fingerprint", "chrome")}
        elif security in ("tls", "xtls"):
            common["sni"] = ts.get("serverName", "") or node.get("address", "")
            common["fingerprint"] = ts.get("fingerprint", "chrome")
            if ts.get("allowInsecure"):
                common["insecure"] = True
        if ts.get("alpn"):
            common["alpn"] = ts["alpn"]
        if net == "grpc":
            common["grpcServiceName"] = (ss.get("grpcSettings", {}) or {}).get("serviceName", "")
        elif net == "xhttp":
            common["xhttpPath"] = (ss.get("xhttpSettings", {}) or {}).get("path", "")
        elif net in ("ws", "websocket"):
            ws = ss.get("wsSettings", {}) or {}
            common["path"] = ws.get("path", "") or "/"
            common["host"] = (ws.get("headers", {}) or {}).get("Host", "")
        if proto == "vless":
            user = (node.get("users") or [{}])[0]
            common["id"] = user.get("id", "")
            common["flow"] = user.get("flow", "")
            kind = "vless-reality" if security == "reality" else "vless-tls"
        elif proto == "vmess":
            user = (node.get("users") or [{}])[0]
            common["id"] = user.get("id", "")
            common["vmessSecurity"] = user.get("security", "auto") or "auto"
            common["alterId"] = int(user.get("alterId", 0) or 0)
            kind = "vmess"
        else:
            common["password"] = node.get("password", "")
            kind = "trojan-reality" if security == "reality" else "trojan-tls"
        return Server(kind=kind, address=node["address"], port=int(node["port"]),
                      country=country, params=common)

    if proto == "shadowsocks":
        node = ((o.get("settings", {}) or {}).get("servers") or [None])[0]
        if not node or not node.get("address") or not node.get("port"):
            return None
        return Server(kind="shadowsocks", address=node["address"], port=int(node["port"]),
                      country=country, params={"method": node.get("method", ""),
                                               "password": node.get("password", "")})

    return None


def parse_servers(text: str) -> list[Server]:
    """Подписка -> отсортированный список уникальных зарубежных серверов-выходов.

    Понимает все ходовые форматы подписки:
    - JSON-массив xray-профилей (Happ/Streisand) и объект-обёртку вокруг него;
    - base64 от JSON;
    - base64 от списка share-ссылок (vless://, trojan://, ss://, hysteria2://, vmess://);
    - просто список share-ссылок текстом.

    Берём ВСЕ пригодные для конвертации зарубежные серверы — даже если страну не
    опознали (тогда имя берём из remarks). Отсеиваем только домашние RU-входы и
    неполные цепочки (dialerProxy). Раньше выбрасывались все неопознанные страны,
    а формат «base64 со ссылками» (самый частый!) не поддерживался вообще."""
    text = (text or "").strip()
    if not text:
        return []

    profiles: list = []
    links: list[str] = []
    obj = _try_json(text)
    if obj is not None:
        profiles = _as_profiles(obj)
    else:
        decoded = _try_b64_text(text)
        if decoded:
            inner = _try_json(decoded)
            if inner is not None:
                profiles = _as_profiles(inner)
            else:
                links = extract_links(decoded)
        if not profiles and not links:
            links = extract_links(text)

    out = _servers_from_profiles(profiles) + _servers_from_links(links)
    seen: set[tuple] = set()
    uniq: list[Server] = []
    for s in out:
        k = s.key()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    # Сортировка: сначала «лучший» протокол (UDP-ядро), потом страна, потом имя.
    uniq.sort(key=lambda s: (KIND_RANK.get(s.kind, 9), s.country or "zzz", s.name, s.address))
    return uniq


def _name_for(srv: Server, remark: str) -> str:
    """Имя строки: своё из remarks (так «Нидерланды +» видно как есть), иначе
    синтезируем из страны+протокола, иначе — из адреса."""
    return (_clean_name(remark)
            or (_label(srv.kind, srv.country) if srv.country else "")
            or f"{srv.address} · {_label(srv.kind, '')}")


def _servers_from_profiles(profiles: list) -> list[Server]:
    """xray-профили -> Server'ы (с учётом цепочек dialerProxy)."""
    seen: set[tuple] = set()
    out: list[Server] = []
    for prof in profiles:
        if not isinstance(prof, dict):
            continue
        remarks = prof.get("remarks", "") or ""
        prof_country = _country_from_text(remarks)
        outs = prof.get("outbounds", []) or []
        by_tag = {o.get("tag"): o for o in outs if isinstance(o, dict) and o.get("tag")}
        # Теги, которые служат ВХОДОМ для чужих цепочек: сами по себе это не выходы
        # (обычно российский relay), поэтому в список серверов их не показываем.
        via_tags = {
            ((o.get("streamSettings", {}) or {}).get("sockopt", {}) or {}).get("dialerProxy")
            for o in outs if isinstance(o, dict)
        }
        via_tags.discard(None)

        for o in outs:
            if not isinstance(o, dict):
                continue
            tag = o.get("tag", "") or ""
            if tag in via_tags:          # транзитный вход, не самостоятельный выход
                continue
            srv = _outbound_to_server(o, fallback_country=prof_country)
            if not srv:
                continue
            # Цепочка: выход ходит через вход (dialerProxy).
            dp = ((o.get("streamSettings", {}) or {}).get("sockopt", {}) or {}).get("dialerProxy")
            if dp:
                via_ob = by_tag.get(dp)
                via_srv = _outbound_to_server(via_ob, fallback_country="") if via_ob else None
                if via_srv is None:
                    continue          # вход не сконвертировали — маршрут бесполезен
                srv.via = via_srv
            # RU-фильтр только для ПРЯМЫХ выходов: у цепочки вход и должен быть
            # российским — в этом и смысл маршрута.
            if srv.via is None and _is_domestic(tag, remarks, srv.country):
                continue
            if srv.key() in seen:
                continue
            seen.add(srv.key())
            srv.name = _name_for(srv, remarks)
            out.append(srv)
    return out


def _servers_from_links(links: list[str]) -> list[Server]:
    """Share-ссылки -> Server'ы. Имя и страна — из фрагмента ссылки (#Нидерланды)."""
    out: list[Server] = []
    for link in links:
        srv = _link_to_server(link)
        if not srv:
            continue
        if _is_domestic(srv.name, srv.country):
            continue
        srv.name = _name_for(srv, srv.name)
        out.append(srv)
    return out


def best_server(servers: list[Server], country: str | None = None) -> Server | None:
    """Авто-выбор БЕЗ проверки живости: (опц.) фильтр по стране, приоритет Hysteria2.
    Для реального включения используем pick_live_server / live_candidates."""
    pool = [s for s in servers if not country or s.country == country]
    return pool[0] if pool else None


def find_by_uid(servers: list[Server], uid: str) -> Server | None:
    for s in servers:
        if s.uid() == uid:
            return s
    return None


# ---- Проверка живости серверов (чтобы «авто» и фолбэк брали РАБОЧИЙ, а не мёртвый) ----

def _probe_tcp(host: str, port: int, timeout: float) -> tuple[bool, float | None]:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.perf_counter() - t0) * 1000.0
    except OSError:
        return False, None


def _probe_udp(host: str, port: int, timeout: float) -> tuple[bool, float | None]:
    """Живость UDP-порта (Hysteria2/QUIC). На connected UDP-сокете Windows поднимает
    ICMP «port unreachable» как ConnectionResetError — значит порт мёртв. Таймаут без
    сброса трактуем как «открыт/фильтруется» = скорее жив (сервер игнорит наш мусор)."""
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)
    except OSError:
        return False, None
    if not infos:
        return False, None
    af, socktype, proto, _, sa = infos[0]
    s = socket.socket(af, socktype, proto)
    try:
        s.settimeout(timeout)
        s.connect(sa)
        t0 = time.perf_counter()
        try:
            s.send(b"\x00" * 20)
            s.recv(1)
            return True, (time.perf_counter() - t0) * 1000.0
        except (socket.timeout, TimeoutError):
            return True, None       # открыт/фильтруется — считаем живым
        except ConnectionResetError:
            return False, None      # ICMP port unreachable — мёртв
        except OSError:
            return False, None
    finally:
        try:
            s.close()
        except OSError:
            pass


def probe_server(server: Server, timeout: float = 2.5) -> tuple[bool, float | None]:
    """(жив?, задержка_мс). Пробим ТОЧКУ ВХОДА (для цепочки — её вход, а не зарубежный
    выход: выход напрямую недоступен by design, и проба по нему всегда врала бы «мёртв»).
    TCP-connect для vless/trojan, UDP-эвристика для Hysteria2."""
    e = server.entry()
    if e.kind == "hysteria2":
        return _probe_udp(e.address, e.port, timeout)
    return _probe_tcp(e.address, e.port, timeout)


def live_candidates(servers: list[Server], prefer_uid: str | None = None,
                    country: str | None = None, timeout: float = 2.0,
                    max_probe: int = 32) -> list[Server]:
    """Возвращает серверы, упорядоченные для подключения: сначала ЖИВЫЕ, среди них —
    явно выбранный (prefer_uid), затем по протоколу (Hysteria2) и задержке; следом
    непроверенные/мёртвые как запасные. Работоспособность важнее пинга —
    пинг учитывается только среди живых. Пробим параллельно, чтобы не тормозить."""
    import concurrent.futures as cf

    pool = [s for s in servers if (not country or s.country == country)]
    if not pool:
        pool = list(servers)
    if not pool:
        return []
    # Выбранный вручную сервер двигаем в начало, чтобы он гарантированно попал в
    # набор проверки живости (иначе при большом списке он окажется за срезом).
    if prefer_uid:
        pool.sort(key=lambda s: 0 if s.uid() == prefer_uid else 1)
    probe_set = pool[:max_probe]
    results: dict[str, tuple[bool, float]] = {}
    with cf.ThreadPoolExecutor(max_workers=min(16, len(probe_set))) as ex:
        futs = {ex.submit(probe_server, s, timeout): s for s in probe_set}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                ok, ms = fut.result()
            except Exception:  # noqa: BLE001
                ok, ms = False, None
            results[s.uid()] = (ok, ms if ms is not None else 8000.0)

    def sort_key(s: Server):
        ok, ms = results.get(s.uid(), (False, 9999.0))
        is_prefer = (prefer_uid is not None and s.uid() == prefer_uid)
        # живые (0) раньше мёртвых (1); выбранный вручную — вперёд; далее протокол и пинг
        return (0 if ok else 1, 0 if is_prefer else 1, KIND_RANK.get(s.kind, 9), ms)

    pool.sort(key=sort_key)
    return pool


def pick_live_server(servers: list[Server], prefer_uid: str | None = None,
                     country: str | None = None) -> Server | None:
    cands = live_candidates(servers, prefer_uid=prefer_uid, country=country)
    return cands[0] if cands else None


def free_local_port() -> int:
    """Свободный localhost-порт под служебный SOCKS проверки туннеля."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


# ---- «Кто мы для внешнего мира»: доказательство, что VPN только для Discord ----
# Пользователь вправе не верить на слово. Проверка берёт IP+страну двумя путями:
# 1) напрямую (обычный процесс, как браузер) — должен быть ЕГО домашний IP;
# 2) через SOCKS нашего туннеля — IP страны VPN.
# Если пункт 1 показывает страну VPN, значит маршрутизация сломана, и это видно сразу.

IP_INFO_HOST = "ipwho.is"


class _ChunkedReader:
    """Разбор chunked-ответа (нам нужен только JSON-хвост, но лучше без мусора)."""

    @staticmethod
    def decode(body: bytes) -> bytes:
        out, pos = b"", 0
        try:
            while pos < len(body):
                nl = body.index(b"\r\n", pos)
                size = int(body[pos:nl].split(b";")[0], 16)
                pos = nl + 2
                if size == 0:
                    break
                out += body[pos:pos + size]
                pos += size + 2
            return out or body
        except Exception:  # noqa: BLE001
            return body


def socks_connect_host(socks_port: int, host: str, port: int, timeout: float):
    """SOCKS5 CONNECT по ИМЕНИ (ATYP=domain) — чтобы TLS-сертификат проверялся по имени."""
    sk = socket.create_connection(("127.0.0.1", int(socks_port)), timeout=timeout)
    try:
        sk.settimeout(timeout)
        sk.sendall(b"\x05\x01\x00")                     # greeting: без авторизации
        if sk.recv(2) != b"\x05\x00":
            raise OSError("socks: greeting rejected")
        hb = host.encode("idna")
        sk.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb
                   + int(port).to_bytes(2, "big"))
        rep = sk.recv(4)
        if len(rep) < 2 or rep[0] != 0x05 or rep[1] != 0x00:
            raise OSError(f"socks: connect rejected (code 0x{rep[1]:02x})"
                          if len(rep) > 1 else "socks: short reply")
        atyp = rep[3] if len(rep) > 3 else 0x01
        if atyp == 0x01:
            sk.recv(4 + 2)
        elif atyp == 0x03:
            sk.recv(sk.recv(1)[0] + 2)
        elif atyp == 0x04:
            sk.recv(16 + 2)
        return sk
    except Exception:
        try:
            sk.close()
        except OSError:
            pass
        raise


def _http_get_body(socks_port: int | None, host: str, path: str = "/",
                   timeout: float = 10.0) -> bytes:
    """GET по HTTPS напрямую либо через SOCKS-вход sing-box. HTTP/1.0 — без chunked."""
    req = (f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: FreeConnect\r\n"
           f"Accept: application/json\r\nAccept-Encoding: identity\r\n\r\n").encode()
    if socks_port:
        sk = socks_connect_host(socks_port, host, 443, timeout)
        try:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sk, server_hostname=host) as tls:
                tls.settimeout(timeout)
                tls.sendall(req)
                buf = b""
                while True:
                    chunk = tls.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except Exception:
            try:
                sk.close()
            except OSError:
                pass
            raise
    else:
        import urllib.request
        rq = urllib.request.Request(f"https://{host}{path}",
                                    headers={"User-Agent": "FreeConnect",
                                             "Accept": "application/json"})
        with urllib.request.urlopen(rq, timeout=timeout) as r:  # noqa: S310
            return r.read()      # urllib уже отдал только тело (заголовки отделены)

    head, _, body = buf.partition(b"\r\n\r\n")
    if b"chunked" in head.lower():
        body = _ChunkedReader.decode(body)
    return body


def ip_info(socks_port: int | None = None, timeout: float = 10.0) -> dict:
    """{ip, country, country_code, city} — напрямую или через туннель.

    Через `socks_port` запрос уходит ВНУТРЬ VPN (выход VPN-сервера); без него — как
    обычный процесс (браузер), то есть напрямую через домашний интернет."""
    body = _http_get_body(socks_port, IP_INFO_HOST, "/", timeout)
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        # Ответ может быть обёрнут (chunked-остатки, отладочный префикс) — берём JSON-хвост.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("не разобрал ответ сервиса определения IP") from None
        data = json.loads(text[start:end + 1])
    if not data.get("ip"):
        raise ValueError(data.get("message") or "сервис не вернул IP")
    return {"ip": str(data.get("ip")), "country": str(data.get("country") or ""),
            "country_code": str(data.get("country_code") or ""),
            "city": str(data.get("city") or "")}


COUNTRY_RU_BY_CODE = {
    "RU": "Россия", "NL": "Нидерланды", "DE": "Германия", "LV": "Латвия", "FI": "Финляндия",
    "SE": "Швеция", "PL": "Польша", "FR": "Франция", "IT": "Италия", "ES": "Испания",
    "GB": "Великобритания", "US": "США", "CA": "Канада", "CH": "Швейцария", "AT": "Австрия",
    "EE": "Эстония", "LT": "Литва", "TR": "Турция", "JP": "Япония", "SG": "Сингапур",
    "HK": "Гонконг", "KR": "Корея", "IN": "Индия", "AE": "ОАЭ", "KZ": "Казахстан",
    "UA": "Украина", "BY": "Беларусь", "GE": "Грузия", "AM": "Армения", "MD": "Молдова",
    "RO": "Румыния", "CZ": "Чехия", "HU": "Венгрия", "BG": "Болгария", "RS": "Сербия",
    "NO": "Норвегия", "DK": "Дания", "IE": "Ирландия", "BE": "Бельгия", "PT": "Португалия",
}


def country_ru(code: str, fallback: str = "") -> str:
    """Код страны -> читаемое название по-русски (иначе то, что вернул сервис)."""
    return COUNTRY_RU_BY_CODE.get((code or "").upper()) or fallback or (code or "")



# Куда стучимся, проверяя туннель. ТОЛЬКО IP и НИКАКИХ доменов: резолв имени внутри
# sing-box уходил бы в DNS, а он при поднятом TUN легко ломается — из-за этого проверка
# падала ДАЖЕ НА РАБОЧИХ серверах («поднялся, но трафик не идёт» на всех подряд).
# Anycast-адреса Cloudflare/Google отвечают TLS на 443 практически откуда угодно.
# Один адрес, а не список: на мёртвом сервере каждый лишний таргет — это ещё один
# полный таймаут, из-за чего проверка не успевала дождаться живых ЦЕПОЧЕК (два хопа
# отвечают заметно дольше прямого выхода).
TUNNEL_PROBE_TARGETS = [("1.1.1.1", 443)]


def _socks_connect_ip(socks_port: int, ip: str, port: int, timeout: float):
    """SOCKS5 CONNECT к IP:port через локальный вход sing-box. Возвращает сокет."""
    sk = socket.create_connection(("127.0.0.1", int(socks_port)), timeout=timeout)
    try:
        sk.settimeout(timeout)
        sk.sendall(b"\x05\x01\x00")                     # greeting: без авторизации
        if sk.recv(2) != b"\x05\x00":
            raise OSError("socks: greeting rejected")
        sk.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(ip)
                   + int(port).to_bytes(2, "big"))      # ATYP=IPv4 — DNS не нужен
        rep = sk.recv(4)
        if len(rep) < 2 or rep[0] != 0x05 or rep[1] != 0x00:
            raise OSError("socks: connect rejected")
        atyp = rep[3] if len(rep) > 3 else 0x01          # дочитываем bound-адрес
        if atyp == 0x01:
            sk.recv(4 + 2)
        elif atyp == 0x03:
            sk.recv(sk.recv(1)[0] + 2)
        elif atyp == 0x04:
            sk.recv(16 + 2)
        return sk
    except Exception:
        try:
            sk.close()
        except OSError:
            pass
        raise


# Сетевые сбои = «через этот сервер трафик не идёт». Всё остальное (NameError,
# AttributeError, TypeError…) — ОШИБКА В КОДЕ, и её нельзя выдавать за мёртвый
# сервер: ровно так забытый `import ssl` заставил проверку браковать ВСЕ серверы
# подряд, включая рабочие. Такие исключения пробрасываем наверх.
_NET_ERRORS = (OSError, ssl.SSLError, socket.timeout)


def tunnel_works(socks_port: int, timeout: float = 6.0, targets=None, log=None) -> bool:
    """Реально ли туннель проводит трафик: SOCKS5 CONNECT по IP + TLS-рукопожатие.

    Именно TLS (а не голый CONNECT) — доказательство, что байты ходят В ОБЕ стороны:
    мёртвый сервер часто принимает CONNECT, но данные дальше не идут."""
    for ip, port in (targets or TUNNEL_PROBE_TARGETS):
        sk = None
        try:
            sk = _socks_connect_ip(socks_port, ip, port, timeout)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False          # ходим по IP: имя проверять нечем
            ctx.verify_mode = ssl.CERT_NONE     # нам важен сам факт обмена байтами
            with ctx.wrap_socket(sk) as tls:
                if tls.version():
                    return True
        except _NET_ERRORS as e:
            if log:
                log(f"tunnel probe {ip}:{port}: {type(e).__name__}: {e}")
            continue
        finally:
            if sk is not None:
                try:
                    sk.close()
                except OSError:
                    pass
    return False


def build_probe_config(server: Server, socks_port: int) -> dict:
    """Лёгкий конфиг ТОЛЬКО для проверки сервера: SOCKS на localhost + выход.

    БЕЗ TUN — поэтому проверка не требует прав администратора, не создаёт сетевой
    адаптер, не перехватывает системный трафик и не ломает интернет на время
    перебора. Раньше каждый кандидат поднимал TUN: адаптер не успевал освободиться
    («Cannot create a file when that file already exists») и рвалась вся сеть."""
    proxies: list[dict] = []
    if server.via is not None:
        proxies.append(_server_to_outbound(server.via, tag="vpn-in"))
        main = _server_to_outbound(server, tag="vpn")
        main["detour"] = "vpn-in"
        proxies.append(main)
    else:
        proxies.append(_server_to_outbound(server, tag="vpn"))
    return {
        "log": {"level": "warn"},
        "inbounds": [{"type": "socks", "tag": "probe-in",
                      "listen": "127.0.0.1", "listen_port": int(socks_port)}],
        "outbounds": proxies + [{"type": "direct", "tag": "direct"}],
        "route": {"rules": [{"inbound": ["probe-in"], "outbound": "vpn"}],
                  "final": "direct"},
    }


# Процессы клиента Discord (десктоп/PTB/Canary) — по ним маршрутизируем в VPN.
DISCORD_PROCESSES = ["Discord.exe", "DiscordPTB.exe", "DiscordCanary.exe",
                     "DiscordDevelopment.exe"]


def _tls_block(p: dict) -> dict | None:
    """TLS-блок sing-box из наших параметров: Reality / обычный TLS / без TLS.

    Формат params у нас двух поколений (JSON-профили пишут `reality`, парсер
    share-ссылок — `tlsMode`+`reality`), поэтому читаем оба."""
    reality = p.get("reality") or {}
    mode = p.get("tlsMode") or ("reality" if reality else "none")
    sni = p.get("sni") or reality.get("serverName", "") or ""
    fp = p.get("fingerprint") or reality.get("fingerprint", "chrome")
    if mode == "reality" and reality:
        tls = {"enabled": True, "server_name": sni,
               "utls": {"enabled": True, "fingerprint": fp or "chrome"},
               "reality": {"enabled": True, "public_key": reality.get("publicKey", ""),
                           "short_id": reality.get("shortId", "")}}
    elif mode == "tls":
        tls = {"enabled": True, "server_name": sni,
               "utls": {"enabled": True, "fingerprint": fp or "chrome"}}
    else:
        return None
    if p.get("alpn"):
        tls["alpn"] = list(p["alpn"])
    if p.get("insecure") or p.get("pinnedSha256"):
        # Самоподписанный/подменный сертификат (pinned sha256) — обычная TLS-проверка
        # не пройдёт, поэтому отключаем строгость, как и делает исходный клиент.
        tls["insecure"] = True
    return tls


def _transport_block(p: dict) -> dict | None:
    """Транспорт sing-box по сети из ссылки/профиля (tcp = по умолчанию, без блока)."""
    net = (p.get("network") or "tcp").lower()
    if net == "grpc":
        return {"type": "grpc", "service_name": p.get("serviceName")
                or p.get("grpcServiceName", "")}
    if net in ("ws", "websocket"):
        tr = {"type": "ws", "path": p.get("path") or "/"}
        if p.get("host"):
            tr["headers"] = {"Host": p["host"]}
        return tr
    if net in ("http", "xhttp", "h2"):
        # sing-box зовёт это http; путь переносим как есть.
        tr = {"type": "http", "path": p.get("path") or p.get("xhttpPath") or "/"}
        if p.get("host"):
            tr["host"] = [p["host"]]
        return tr
    if net == "httpupgrade":
        tr = {"type": "httpupgrade", "path": p.get("path") or "/"}
        if p.get("host"):
            tr["host"] = p["host"]
        return tr
    return None


def _server_to_outbound(s: Server, tag: str = "vpn") -> dict:
    """Server -> outbound sing-box. ВНИМАНИЕ: маппинг проверяется вживую на машине
    (sing-box + реальный сервер); здесь — по схеме sing-box 1.x."""
    p = s.params
    if s.kind == "hysteria2":
        ob = {"type": "hysteria2", "tag": tag, "server": s.address, "server_port": s.port,
              "password": p.get("auth", ""),
              "tls": {"enabled": True, "server_name": p.get("sni", ""),
                      "alpn": p.get("alpn", ["h3"])}}
        if p.get("insecure") or p.get("pinnedSha256"):
            ob["tls"]["insecure"] = True
        if p.get("obfs"):
            ob["obfs"] = {"type": p["obfs"], "password": p.get("obfsPassword", "")}
        return ob

    if s.kind == "shadowsocks":
        ob = {"type": "shadowsocks", "tag": tag, "server": s.address, "server_port": s.port,
              "method": p.get("method", ""), "password": p.get("password", "")}
        tr = _transport_block(p)
        if tr:
            ob["transport"] = tr
        return ob

    tls = _tls_block(p)
    transport = _transport_block(p)

    if s.kind == "vmess":
        ob = {"type": "vmess", "tag": tag, "server": s.address, "server_port": s.port,
              "uuid": p.get("id", ""),
              "security": p.get("vmessSecurity") or "auto",
              "alter_id": int(p.get("alterId", 0) or 0)}
        if tls:
            ob["tls"] = tls
    elif s.kind == "vless-reality" or s.kind == "vless-tls":
        ob = {"type": "vless", "tag": tag, "server": s.address, "server_port": s.port,
              "uuid": p.get("id", "")}
        if tls:
            ob["tls"] = tls
        if p.get("flow"):
            ob["flow"] = p["flow"]
    else:  # trojan-reality / trojan-tls
        ob = {"type": "trojan", "tag": tag, "server": s.address, "server_port": s.port,
              "password": p.get("password", "")}
        if tls:
            ob["tls"] = tls
    if transport:
        ob["transport"] = transport
    return ob


def build_singbox_config(server: Server, tun_name: str = "freeconn0",
                         probe_port: int | None = None,
                         log_level: str = "warn") -> dict:
    """Полный конфиг sing-box: TUN + маршрут «процессы Discord -> VPN, остальное ->
    direct». Всё, что direct, дальше идёт через наш winws-десинк как обычно.

    Цепочка (server.via): в sing-box это `detour` — выход «vpn» устанавливает
    соединение ЧЕРЕЗ вход «vpn-in» (эквивалент xray dialerProxy)."""
    proxies: list[dict] = []
    if server.via is not None:
        proxies.append(_server_to_outbound(server.via, tag="vpn-in"))
        main = _server_to_outbound(server, tag="vpn")
        main["detour"] = "vpn-in"
        proxies.append(main)
    else:
        proxies.append(_server_to_outbound(server, tag="vpn"))

    inbounds: list[dict] = [{
        "type": "tun", "tag": "tun-in", "interface_name": tun_name,
        "address": ["172.19.0.1/30"], "auto_route": True, "strict_route": False,
        "stack": "system",
    }]
    rules: list[dict] = []
    if probe_port:
        inbounds.append({"type": "socks", "tag": "probe-in",
                         "listen": "127.0.0.1", "listen_port": int(probe_port)})
        rules.append({"inbound": ["probe-in"], "outbound": "vpn"})
    # Определяем домен по первым байтам соединения (нужно правилу ниже).
    rules.append({"action": "sniff"})
    # Локальная сеть и приватные адреса — ВСЕГДА напрямую, даже для Discord.
    # Раньше этот трафик попадал в правило Discord и уходил в VPN: голосовой движок
    # Discord долбит адреса собственной TUN-подсети (172.19.0.2:10xxx) и получал на
    # каждый такой «стук» попытку соединения через зарубежный сервер.
    rules.append({"ip_is_private": True, "outbound": "direct"})
    # Весь трафик процессов Discord — в VPN.
    rules.append({"process_name": DISCORD_PROCESSES, "outbound": "vpn"})
    # Плюс сами домены Discord: их в РФ блокируют по имени, а соединение может
    # открывать не Discord.exe (оверлей, веб-версия), и по IP такое не отличить.
    rules.append({"domain_suffix": ["discord.com", "discord.gg", "discordapp.com",
                                    "discordapp.net", "discord.media"],
                  "outbound": "vpn"})

    return {
        "log": {"level": log_level},
        # DNS обязателен: с поднятым TUN системный резолв уходит в маршруты sing-box,
        # и без явных правил имена Discord не резолвились вовсе. Домены Discord
        # резолвим ВНУТРИ туннеля (не утекают и не зависят от провайдера), остальное —
        # системным резолвером напрямую.
        # ФОРМАТ новый (sing-box ≥1.12): `type`+`server`. Старый (`address`) с 1.12
        # объявлен legacy и роняет процесс с FATAL. Проверяется `sing-box check`
        # в тестах, чтобы несовместимость ловилась до релиза, а не у пользователя.
        "dns": {
            "servers": [
                {"tag": "remote", "type": "udp", "server": "1.1.1.1", "detour": "vpn"},
                {"tag": "local", "type": "local"},
            ],
            "rules": [
                {"domain_suffix": ["discord.com", "discord.gg", "discordapp.com",
                                   "discordapp.net", "discord.media"],
                 "server": "remote"},
            ],
            "final": "local",
        },
        "inbounds": inbounds,
        "outbounds": proxies + [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": rules,
            "final": "direct",
            "auto_detect_interface": True,
            # С 1.12 обязателен: иначе «missing route.default_domain_resolver» -> FATAL.
            "default_domain_resolver": {"server": "local"},
        },
    }
