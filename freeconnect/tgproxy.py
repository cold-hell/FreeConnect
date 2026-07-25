"""
Обход блокировки Telegram — локальный SOCKS5-прокси, который заворачивает
MTProto в WebSocket к собственному веб-входу Telegram (kws{dc}.web.telegram.org).
См. [[freeconnect]] и [[freeconnect-telegram-websocket]].

Идея (не DPI-десинк, как для Discord, а туннель через веб-вход Telegram):
1. Поднимаем SOCKS5 на 127.0.0.1:1080. Клиент Telegram настраивается ходить через него.
2. Telegram шлёт свой обфусцированный (obfuscated2) MTProto-поток на дата-центр.
3. По первым 64 байтам init-пакета определяем номер дата-центра (1..5) — расшифровав
   его AES-256-CTR (authoritative) либо по IP цели (запасной путь).
4. Открываем `wss://kws{dc}.web.telegram.org/apiws` — тот самый веб-сокет, через
   который работает web.telegram.org в браузере, — и релеим байты в обе стороны
   бинарными фреймами.

КЛЮЧЕВОЙ МОМЕНТ (без него обход не работает). Провайдер блокирует веб-вход
Telegram ПО IP, на уровне TCP: адреса, которые отдаёт DNS (149.154.174.100,
149.154.167.99, 149.154.170.100), просто не отвечают на SYN. Поэтому DNS мы
НЕ используем: подключаемся напрямую на подобранный живой адрес из DC_ENDPOINTS,
подставляя правильный SNI (имя kws{dc}...) и проверяя сертификат.

Полевые замеры (РФ, 2026-07): 149.154.167.220 даёт TCP ~45мс, валидный сертификат
и WebSocket 101 для ДЦ2/ДЦ4. TLS-отпечаток роли НЕ играет — обычный Python-TLS
проходит так же, как «хромовый», поэтому никакого uTLS/Go не требуется.

Полностью локально и бесплатно: сторонних серверов и подписок не нужно, идём в
собственную инфраструктуру Telegram. Прав администратора движок не требует (порт
1080 непривилегированный), поэтому работает независимо от winws/sing-box.
"""
from __future__ import annotations

import asyncio
import queue
import socket
import ssl
import threading
import time

from .engine import _IS_WIN  # noqa: F401  (единый признак платформы)

try:  # чистый Python + готовые колёса — обе зависимости бандлятся PyInstaller'ом
    import websockets
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _DEPS_OK = True
except Exception:  # noqa: BLE001
    _DEPS_OK = False

DEFAULT_PORT = 1080
LOCAL_HOST = "127.0.0.1"
WS_HOST_TMPL = "kws{dc}.web.telegram.org"
WS_URL_TMPL = "wss://kws{dc}.web.telegram.org/apiws"
DEFAULT_DC = 2  # запасной ДЦ (Амстердам): на нём живут аккаунты РФ/ЕС

CONNECT_TIMEOUT = 8.0   # сек на TCP-коннект к одному адресу
WS_OPEN_TIMEOUT = 15.0  # сек на TLS + WebSocket-рукопожатие

# Живые адреса веб-входа в обход DNS-блокировки (см. шапку модуля). Один сервер
# обслуживает ДЦ2 и ДЦ4 — его сертификат валиден ровно для kws2/kws4, поэтому для
# ДЦ1/3/5 он не годится и там пока остаётся только DNS.
# Порядок = порядок перебора. Список можно переопределить из config.json
# (ключ tg_endpoints), чтобы чинить блокировку без пересборки приложения.
DC_ENDPOINTS: dict[int, list[str]] = {
    2: ["149.154.167.220"],
    4: ["149.154.167.220"],
}


class TgProxyError(Exception):
    pass


def deeplink(port: int = DEFAULT_PORT, host: str = LOCAL_HOST) -> str:
    """tg://-ссылка автонастройки: открывает Telegram и предлагает добавить наш SOCKS5."""
    return f"tg://socks?server={host}&port={int(port)}"


def dc_from_init(init: bytes) -> int | None:
    """Номер дата-центра из 64-байтного obfuscated2 init-пакета.

    Ключ AES-256 = init[8:40], IV = init[40:56]. Применяя тот же CTR-кейстрим к
    принятому буферу, восстанавливаем зашифрованный хвост [56:64]: [56:60] — метка
    транспорта, [60:62] — знаковый int16 с id ДЦ (media-ДЦ идут со знаком минус,
    оттого abs), [62:64] — резерв."""
    if not _DEPS_OK or len(init) < 64:
        return None
    try:
        key, iv = bytes(init[8:40]), bytes(init[40:56])
        dec = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor().update(bytes(init))
        dc = abs(int.from_bytes(dec[60:62], "little", signed=True))
        return dc if 1 <= dc <= 5 else None
    except Exception:  # noqa: BLE001
        return None


def dc_from_ip(ip: str | None) -> int | None:
    """Грубое сопоставление IP цели SOCKS5 -> ДЦ (запасной путь, если init не распознан).
    /24-подсети ДЦ пересекаются (1↔3, 2↔4), поэтому это лишь ориентир — точный номер
    даёт dc_from_init."""
    if not ip:
        return None
    try:
        a, b, c, _d = (int(x) for x in ip.split("."))
    except Exception:  # noqa: BLE001
        return None
    if (a, b) == (149, 154):
        if c == 175:
            return 1          # 149.154.175.x — ДЦ1 (делит подсеть с ДЦ3)
        if c == 167:
            return 2          # 149.154.167.x — ДЦ2 (делит подсеть с ДЦ4)
        if 160 <= c <= 171:
            return 2
    if (a, b) == (91, 108):
        if 56 <= c <= 59:
            return 5          # 91.108.56.0/22 — ДЦ5 (Сингапур)
        if 4 <= c <= 15:
            return 4
    if (a, b) == (95, 161):
        return 2
    return None


def _parse_socks_target(atyp: int, addr_bytes: bytes) -> str | None:
    """Возвращает IPv4-строку цели SOCKS5 (для запасного dc_from_ip) либо None
    для домена/IPv6 — тогда полагаемся только на init-пакет."""
    if atyp == 0x01 and len(addr_bytes) == 4:
        return ".".join(str(x) for x in addr_bytes)
    return None


_TLS_CTX: ssl.SSLContext | None = None


def _tls_context() -> ssl.SSLContext:
    """Обычный проверяющий TLS-контекст (создаётся один раз на все соединения).

    Сертификат ПРОВЕРЯЕМ: мы ходим на голый IP, и проверка — единственное, что
    подтверждает, что на том конце действительно Telegram, а не подстава провайдера.
    ALPN строго http/1.1: WebSocket живёт поверх HTTP/1.1, а предложи мы заодно h2 —
    сервер согласует HTTP/2 и ответит на апгрейд бинарными фреймами."""
    global _TLS_CTX
    if _TLS_CTX is None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        try:
            ctx.load_default_certs()
        except Exception:  # noqa: BLE001
            pass
        ctx.set_alpn_protocols(["http/1.1"])
        _TLS_CTX = ctx
    return _TLS_CTX


def known_endpoints(dc: int, overrides: dict | None = None) -> list[str]:
    """Подобранные живые адреса для дата-центра (в обход DNS). Ключ может прийти
    из config.json строкой, поэтому смотрим оба варианта."""
    table = overrides or DC_ENDPOINTS
    return list(table.get(dc) or table.get(str(dc)) or [])


# ---- диагностика (для кнопки «проверить обход» в интерфейсе) ---------------

async def _probe_ip(dc: int, ip: str, timeout: float = 6.0) -> dict:
    """Три ступени для одного адреса: TCP -> TLS(с настоящим SNI) -> WebSocket 101.
    Возвращает вердикт по каждой ступени, чтобы человеку было видно, где рвётся."""
    host = WS_HOST_TMPL.format(dc=dc)
    loop = asyncio.get_running_loop()
    out = {"ip": ip, "tcp": "—", "tls": "—", "ws": "—", "ok": False}
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.setblocking(False)
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(loop.sock_connect(sk, (ip, 443)), timeout)
        out["tcp"] = f"ok {int((time.perf_counter() - t0) * 1000)}мс"
    except Exception:  # noqa: BLE001  блокировка по IP выглядит именно так
        out["tcp"] = "нет ответа (блокировка)"
        try:
            sk.close()
        except Exception:  # noqa: BLE001
            pass
        return out
    try:
        sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ws = await websockets.connect(
            WS_URL_TMPL.format(dc=dc), sock=sk, ssl=_tls_context(),
            server_hostname=host, subprotocols=["binary"], max_size=None,
            open_timeout=timeout, proxy=None)
        out["tls"], out["ws"], out["ok"] = "ok", "ok (101)", True
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    except ssl.SSLCertVerificationError:
        out["tls"] = f"сертификат не для {host}"
    except ssl.SSLError as e:
        out["tls"] = f"сбой TLS ({type(e).__name__})"
    except Exception as e:  # noqa: BLE001  TLS прошёл, споткнулись на веб-сокете
        out["tls"] = "ok"
        out["ws"] = f"сбой ({type(e).__name__})"
    finally:
        if not out["ok"]:
            try:
                sk.close()
            except Exception:  # noqa: BLE001
                pass
    return out


async def _diagnose(dc: int, endpoints: dict | None, timeout: float) -> dict:
    host = WS_HOST_TMPL.format(dc=dc)
    known = known_endpoints(dc, endpoints)
    dns: list[str] = []
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
        dns = sorted({ai[4][0] for ai in infos})
    except Exception:  # noqa: BLE001
        pass
    rows = []
    for ip in known:
        rows.append({**await _probe_ip(dc, ip, timeout), "source": "встроенный"})
    for ip in dns:
        if ip not in known:
            rows.append({**await _probe_ip(dc, ip, timeout), "source": "DNS"})
    return {"dc": dc, "host": host, "dns": dns, "rows": rows,
            "ok": any(r["ok"] for r in rows)}


def diagnose(dc: int = DEFAULT_DC, endpoints: dict | None = None,
             timeout: float = 6.0) -> dict:
    """Синхронная обёртка (зовётся из UI-потока, поднимает свой цикл asyncio)."""
    if not _DEPS_OK:
        return {"ok": False, "rows": [], "dns": [], "dc": dc,
                "host": WS_HOST_TMPL.format(dc=dc)}
    return asyncio.run(_diagnose(dc, endpoints, timeout))


# ---- автопоиск живых адресов ----------------------------------------------
# Если встроенный адрес однажды заблокируют, программа должна сама найти новый.
# Подсети, где живут узлы веб-входа Telegram; порядок = порядок перебора, от самых
# вероятных к остальным (рядом с известным живым узлом шанс попасть выше всего).
# Огромный 91.108.0.0/16 намеренно НЕ берём целиком — это 65к адресов.
SCAN_SUBNETS = [
    "149.154.167.0/24",   # соседи известного живого 149.154.167.220
    "149.154.160.0/20",   # весь блок веб-входа (включает предыдущую подсеть)
    "91.105.192.0/23",
    "185.76.151.0/24",
]
SCAN_CONCURRENCY = 256
VERIFY_CONCURRENCY = 24   # TLS-рукопожатие дороже TCP-коннекта, поэтому скромнее
SCAN_TIMEOUT = 3.0
SCAN_DEADLINE = 180.0     # общий предел, чтобы поиск не шёл вечно


async def _tcp_alive(ip: str, timeout: float, sem: asyncio.Semaphore) -> str | None:
    """Быстрая проверка «отвечает ли 443». Заблокированный или пустой адрес молчит."""
    async with sem:
        loop = asyncio.get_running_loop()
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sk.setblocking(False)
        try:
            await asyncio.wait_for(loop.sock_connect(sk, (ip, 443)), timeout)
            return ip
        except Exception:  # noqa: BLE001
            return None
        finally:
            try:
                sk.close()
            except Exception:  # noqa: BLE001
                pass


async def _discover(dc: int, subnets: list[str], limit: int, timeout: float,
                    deadline: float, progress) -> list[str]:
    import ipaddress
    started = time.perf_counter()
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    seen: set[str] = set()
    found: list[str] = []

    def _tick(stage: str, done: int, total: int) -> None:
        if progress:
            try:
                progress({"stage": stage, "done": done, "total": total,
                          "found": list(found)})
            except Exception:  # noqa: BLE001
                pass

    for cidr in subnets:
        if time.perf_counter() - started > deadline:
            break
        ips = [str(h) for h in ipaddress.ip_network(cidr).hosts()
               if str(h) not in seen]
        seen.update(ips)
        if not ips:
            continue

        # Фаза 1 — быстрый скан доступности (отсеивает заблокированные/пустые).
        tasks = [asyncio.create_task(_tcp_alive(ip, timeout, sem)) for ip in ips]
        alive: list[str] = []
        done = 0
        try:
            for fut in asyncio.as_completed(tasks):
                ip = await fut
                done += 1
                if ip:
                    alive.append(ip)
                if done % 128 == 0 or done == len(tasks):
                    _tick("scan", done, len(tasks))
                if time.perf_counter() - started > deadline:
                    break
        finally:
            for t in tasks:
                t.cancel()

        # Фаза 2 — подтверждаем, что это НАСТОЯЩИЙ узел kws{dc}: проверка сертификата
        # не даст принять чужой адрес или подставу провайдера за рабочий.
        # Тоже параллельно: TLS-рукопожатие ~секунда, а откликнувшихся адресов могут
        # быть сотни — последовательная проверка не укладывалась в лимит времени.
        if not alive:
            continue
        vsem = asyncio.Semaphore(VERIFY_CONCURRENCY)

        async def _verify(ip: str) -> str | None:
            async with vsem:
                return ip if (await _probe_ip(dc, ip, timeout))["ok"] else None

        vtasks = [asyncio.create_task(_verify(ip)) for ip in alive]
        checked = 0
        try:
            for fut in asyncio.as_completed(vtasks):
                ip = await fut
                checked += 1
                if ip:
                    found.append(ip)
                if checked % 16 == 0 or checked == len(vtasks) or ip:
                    _tick("verify", checked, len(vtasks))
                if len(found) >= limit or time.perf_counter() - started > deadline:
                    break
        finally:
            for t in vtasks:
                t.cancel()
        if len(found) >= limit:
            return found
    return found


def discover(dc: int = DEFAULT_DC, subnets: list[str] | None = None,
             limit: int = 3, timeout: float = SCAN_TIMEOUT,
             deadline: float = SCAN_DEADLINE, progress=None) -> list[str]:
    """Ищет живые адреса веб-входа Telegram. Синхронная обёртка (зовём из потока).

    Только по требованию (кнопка/сбой), не по расписанию: перебор ограничен по
    параллельности, времени и числу находок."""
    if not _DEPS_OK:
        return []
    return asyncio.run(_discover(dc, subnets or SCAN_SUBNETS, limit, timeout,
                                 deadline, progress))


class TgProxy:
    """Держит локальный SOCKS5→WebSocket прокси для Telegram в фоновом asyncio-потоке.

    Интерфейс намеренно повторяет [[singbox]] (available/is_running/start/stop),
    чтобы app.py управлял им единообразно."""

    _serializable = False   # pywebview не должен обходить объект при сборке js_api

    def __init__(self, log=None, endpoints: dict | None = None,
                 cf_domains: list | None = None) -> None:
        self._log = log or (lambda _m: None)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_evt: asyncio.Event | None = None
        self._port = DEFAULT_PORT
        self._running = threading.Event()
        self._stats = {"up": 0, "down": 0, "active": 0}
        # Переопределение таблицы живых адресов из config.json (tg_endpoints):
        # позволяет починить внезапно умерший IP, не пересобирая приложение.
        self._endpoints = endpoints or None
        # Резервный канал через Cloudflare: список base-доменов, за которыми стоит
        # релей на веб-вход Telegram. Идём на kws{dc}.{base} (обычный DNS, не
        # заблокированный IP Telegram) — страховка, когда прямые адреса придушат.
        # Пусто = резерва нет (работаем как раньше, только по прямым IP).
        self._cf_domains = list(cf_domains or [])

    def available(self) -> bool:
        """Есть ли зависимости для работы (websockets + cryptography)."""
        return _DEPS_OK

    def is_running(self) -> bool:
        return self._running.is_set()

    def port(self) -> int:
        return self._port

    def set_endpoints(self, endpoints: dict | None) -> None:
        """Подменяет таблицу живых адресов (после автопоиска). Применится к новым
        соединениям; уже открытые не трогаем."""
        self._endpoints = endpoints or None

    def set_cf_domains(self, cf_domains: list | None) -> None:
        """Подменяет список резервных Cloudflare-доменов (после обновления с канала).
        Применится к новым соединениям."""
        self._cf_domains = list(cf_domains or [])

    def stats(self) -> dict:
        return dict(self._stats)

    # ---- жизненный цикл -------------------------------------------------

    def start(self, port: int = DEFAULT_PORT, lan: bool = False) -> None:
        """Открывает SOCKS5-порт. Бросает TgProxyError, если зависимостей нет или
        порт занят. lan=True слушает на 0.0.0.0 (раздача в домашней сети)."""
        if not _DEPS_OK:
            raise TgProxyError("Не хватает зависимостей (websockets/cryptography)")
        if self.is_running():
            return
        self._port = int(port)
        self._stats = {"up": 0, "down": 0, "active": 0}
        ready: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, args=(self._port, lan, ready), daemon=True,
            name="tgproxy",
        )
        self._thread.start()
        try:
            err = ready.get(timeout=10)
        except queue.Empty:
            err = TgProxyError("прокси не запустился (таймаут)")
        if err is not None:
            self._thread = None
            raise TgProxyError(f"Не удалось открыть порт {self._port}: {err}")
        self._running.set()
        self._log(f"tgproxy: слушает {LOCAL_HOST}:{self._port}"
                  + (" (LAN)" if lan else ""))

    def stop(self) -> None:
        loop, evt = self._loop, self._stop_evt
        if loop is not None and evt is not None:
            try:
                loop.call_soon_threadsafe(evt.set)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=6)
        self._thread = None
        self._loop = None
        self._stop_evt = None
        self._running.clear()

    # ---- фоновый asyncio-поток -----------------------------------------

    def _run(self, port: int, lan: bool, ready: queue.Queue) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve(port, lan, ready))
        except Exception as e:  # noqa: BLE001
            # серверу не удалось стартовать до того, как мы положили результат
            try:
                ready.put_nowait(e)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()
            self._running.clear()

    async def _serve(self, port: int, lan: bool, ready: queue.Queue) -> None:
        host = "0.0.0.0" if lan else LOCAL_HOST
        self._stop_evt = asyncio.Event()
        try:
            server = await asyncio.start_server(self._handle, host, port)
        except Exception as e:  # noqa: BLE001  порт занят и т.п.
            ready.put_nowait(e)
            return
        ready.put_nowait(None)   # сигнал «порт открыт»
        async with server:
            await self._stop_evt.wait()

    # ---- обработка одного соединения -----------------------------------

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        ws = None
        try:
            target_ip = await self._socks_handshake(reader, writer)
            if target_ip is False:   # рукопожатие отклонено/битое
                return
            init = await reader.readexactly(64)
            dc = dc_from_init(init) or dc_from_ip(target_ip) or DEFAULT_DC
            ws = await self._ws_connect(dc)
            await ws.send(bytes(init))
            self._stats["active"] += 1
            await self._relay(reader, writer, ws)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            self._log(f"tgproxy: соединение оборвалось — {e}")
        finally:
            if ws is not None:
                self._stats["active"] = max(0, self._stats["active"] - 1)
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _candidates(self, dc: int) -> list[str]:
        """Адреса-кандидаты для дата-центра: сперва подобранные живые (главный путь —
        DNS отдаёт заблокированные), затем ответ DNS (сработает там, где блокировки нет)."""
        out = known_endpoints(dc, self._endpoints)
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                WS_HOST_TMPL.format(dc=dc), 443,
                family=socket.AF_INET, type=socket.SOCK_STREAM)
            for ai in infos:
                if ai[4][0] not in out:
                    out.append(ai[4][0])
        except Exception:  # noqa: BLE001  DNS может быть недоступен/отравлен
            pass
        return out

    async def _dial(self, dc: int, ip: str | None, host: str | None = None):
        """Одна попытка: TCP -> TLS с настоящим SNI -> WebSocket.

        host — имя для SNI, проверки сертификата и WS-URL (по умолчанию
        kws{dc}.web.telegram.org). ip — на какой адрес коннектиться; None означает
        «резолвить host через DNS» (для резервных Cloudflare-доменов, которые DNS
        отдаёт нормально, в отличие от заблокированного веб-входа Telegram)."""
        host = host or WS_HOST_TMPL.format(dc=dc)
        url = f"wss://{host}/apiws"
        loop = asyncio.get_running_loop()
        if ip is None:
            infos = await loop.getaddrinfo(host, 443, family=socket.AF_INET,
                                           type=socket.SOCK_STREAM)
            if not infos:
                raise TgProxyError(f"{host}: DNS не дал адреса")
            ip = infos[0][4][0]
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sk.setblocking(False)
        try:
            await asyncio.wait_for(loop.sock_connect(sk, (ip, 443)), CONNECT_TIMEOUT)
            sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # sock= перекрывает адрес из URL (идём на выбранный IP, а не в DNS),
            # server_hostname= задаёт SNI и имя для проверки сертификата,
            # proxy=None — не подхватывать системный прокси из окружения.
            return await websockets.connect(
                url, sock=sk, ssl=_tls_context(),
                server_hostname=host, subprotocols=["binary"],
                max_size=None, open_timeout=WS_OPEN_TIMEOUT, proxy=None,
            )
        except Exception:
            try:
                sk.close()   # при успехе сокетом уже владеет websockets
            except Exception:  # noqa: BLE001
                pass
            raise

    async def _ws_connect(self, dc: int):
        """Открывает веб-сокет к kws{dc}, перебирая адреса; на каждом — одна повторная
        попытка: узлы Telegram изредка сбрасывают соединение прямо на рукопожатии
        (наблюдалось на ДЦ2), и ретрай сглаживает такие транзиентные сбросы.

        Если ВСЕ прямые адреса недоступны (провайдер прибил IP веб-входа) —
        уходим в резервный канал через Cloudflare-домены (страховка)."""
        candidates = await self._candidates(dc)
        last: Exception | None = None
        for ip in candidates:
            for attempt in range(2):
                try:
                    return await self._dial(dc, ip)
                except Exception as e:  # noqa: BLE001
                    last = e
                    if attempt == 0:
                        await asyncio.sleep(0.3)

        # Прямые адреса не сработали — резерв через Cloudflare (если задан).
        for base in self._cf_domains:
            host = f"kws{dc}.{base}"
            self._log(f"tgproxy: ДЦ{dc} прямые адреса недоступны — резерв через Cloudflare ({base})")
            for attempt in range(2):
                try:
                    return await self._dial(dc, None, host=host)
                except Exception as e:  # noqa: BLE001
                    last = e
                    if attempt == 0:
                        await asyncio.sleep(0.3)

        if not candidates and not self._cf_domains:
            raise TgProxyError(f"ДЦ{dc}: нет адресов для подключения")
        raise TgProxyError(
            f"ДЦ{dc}: не подключиться ни к прямым адресам ({len(candidates)}), "
            f"ни через резерв Cloudflare ({len(self._cf_domains)}) ({last})")

    async def _socks_handshake(self, reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter):
        """Мини-SOCKS5 (без авторизации, только CONNECT). Возвращает IPv4-строку
        цели, None (домен/IPv6) или False, если запрос отклонён."""
        head = await reader.readexactly(2)
        if head[0] != 0x05:
            return False
        await reader.readexactly(head[1])          # список методов, игнорируем
        writer.write(b"\x05\x00")                  # выбираем «без авторизации»
        await writer.drain()

        ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if ver != 0x05 or cmd != 0x01:             # поддерживаем только CONNECT
            writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return False
        if atyp == 0x01:
            addr = await reader.readexactly(4)
        elif atyp == 0x03:
            addr = await reader.readexactly((await reader.readexactly(1))[0])
        elif atyp == 0x04:
            addr = await reader.readexactly(16)
        else:
            writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
            await writer.drain()
            return False
        await reader.readexactly(2)                # порт цели — не нужен, мы идём в kws
        # Успех: клиент начинает слать данные. Bound-адрес фиктивный (0.0.0.0:0).
        writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
        await writer.drain()
        return _parse_socks_target(atyp, addr)

    async def _relay(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter, ws) -> None:
        """Двунаправленный релей: TCP↔WebSocket бинарными фреймами, без доп. обёрток."""
        async def up() -> None:      # Telegram -> WebSocket
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send(data)
                self._stats["up"] += len(data)

        async def down() -> None:    # WebSocket -> Telegram
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    writer.write(msg)
                    await writer.drain()
                    self._stats["down"] += len(msg)

        tasks = {asyncio.create_task(up()), asyncio.create_task(down())}
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except Exception:  # noqa: BLE001
                pass
