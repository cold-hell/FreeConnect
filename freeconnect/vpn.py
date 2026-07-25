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
# Порядок предпочтения протоколов: Hysteria2 первым (UDP-ядро — лучший голос).
KIND_RANK = {"hysteria2": 0, "vless-reality": 1, "trojan-reality": 2}


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
    """Подписка -> отсортированный список уникальных зарубежных серверов-выходов.

    Берём ВСЕ пригодные для конвертации зарубежные серверы — даже если страну не
    опознали (тогда имя берём из remarks). Отсеиваем только домашние RU-входы и
    цепочки (dialerProxy). Раньше выбрасывались все неопознанные страны, из-за чего
    список был куда беднее, чем в самом клиенте подписки."""
    profiles = decode_subscription(text)
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
            # Имя: сначала своё из remarks (так «Нидерланды +» видно как есть),
            # иначе синтезируем из страны+протокола, иначе — из адреса.
            srv.name = (_clean_name(remarks)
                        or (_label(srv.kind, srv.country) if srv.country else "")
                        or f"{srv.address} · {_label(srv.kind, '')}")
            out.append(srv)
    out.sort(key=lambda s: (KIND_RANK.get(s.kind, 9), s.country or "zzz", s.name, s.address))
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


def build_singbox_config(server: Server, tun_name: str = "freeconn0",
                         probe_port: int | None = None) -> dict:
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
    rules: list[dict] = [
        # Весь трафик процессов Discord — в VPN.
        {"process_name": DISCORD_PROCESSES, "outbound": "vpn"},
    ]
    if probe_port:
        inbounds.append({"type": "socks", "tag": "probe-in",
                         "listen": "127.0.0.1", "listen_port": int(probe_port)})
        rules.insert(0, {"inbound": ["probe-in"], "outbound": "vpn"})

    return {
        "log": {"level": "warn"},
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
